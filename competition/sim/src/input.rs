//! The start-up input (episode-runner-impl.md §2, start-up input): a JSON
//! object with exactly twelve fields, which the entry point reads once, before
//! any entity of the simulator starts (episodes.md §4.3, start-up input).
//!
//! A missing file, a missing or unknown field, or a value out of range refuses
//! the input, and the error names the reason. A `next_audit_at` that has passed
//! when the entry point reads the input is out of range.

use std::fmt;
use std::time::Duration;

use serde_json::{Map, Value};
use time::format_description::well_known::Rfc3339;
use time::{OffsetDateTime, PrimitiveDateTime, UtcOffset};

use sim::actors::fuel_supplier::SupplierStart;
use sim::coupled::Start;

/// Where the deployment attaches the start-up input (sim-network.md §7).
pub const PATH: &str = "/var/lib/sim/startup/startup.json";

/// The fields of the start-up input, in the order of episode-runner-impl.md §2.
pub const FIELDS: [&str; 12] = [
    "sim_tug_arrivals",
    "initial_volume_l",
    "order_pending_s",
    "first_poll_after_s",
    "first_check_after_s",
    "next_audit_at",
    "initial_cumulative_pulses",
    "initial_meter_totaliser_l",
    "first_bdn_number",
    "first_ticket_number",
    "held_since",
    "held_artefacts",
];

/// The start-up input as read. The next audit stays an instant until
/// [`Input::start`] measures it from the simulator's start.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Input {
    pub tug_seed: u64,
    pub initial_volume_l: f64,
    pub supplier_start: SupplierStart,
    pub first_check_after: Duration,
    pub next_audit_at: PrimitiveDateTime,
    pub initial_cumulative_pulses: u32,
    pub initial_meter_totaliser_l: f64,
    pub first_bdn_number: u32,
    pub first_ticket_number: u32,
}

/// Why the start-up input was refused: a startup fault, which stops the
/// simulator before step 0.
#[derive(Debug, Clone, PartialEq)]
pub enum InputError {
    /// The text is not JSON.
    NotJson(String),
    /// The JSON is not an object.
    NotObject,
    /// A field of the twelve is absent.
    Missing(&'static str),
    /// A field outside the twelve is present.
    Unknown(String),
    /// A field's value is of the wrong kind or out of range.
    Invalid { field: &'static str, reason: String },
}

impl fmt::Display for InputError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            InputError::NotJson(error) => write!(f, "the start-up input is not JSON: {error}"),
            InputError::NotObject => write!(f, "the start-up input is not a JSON object"),
            InputError::Missing(field) => write!(f, "the start-up input lacks the field {field}"),
            InputError::Unknown(field) => {
                write!(f, "the start-up input holds the unknown field {field}")
            }
            InputError::Invalid { field, reason } => {
                write!(f, "the start-up input's field {field} is invalid: {reason}")
            }
        }
    }
}

impl std::error::Error for InputError {}

/// Validates the text of a start-up input against a tank of
/// `tank_capacity_l` litres.
pub fn parse(text: &str, tank_capacity_l: f64) -> Result<Input, InputError> {
    let value: Value =
        serde_json::from_str(text).map_err(|error| InputError::NotJson(error.to_string()))?;
    let Value::Object(fields) = value else {
        return Err(InputError::NotObject);
    };
    if let Some(unknown) = fields.keys().find(|key| !FIELDS.contains(&key.as_str())) {
        return Err(InputError::Unknown(unknown.clone()));
    }
    if let Some(missing) = FIELDS.iter().find(|field| !fields.contains_key(**field)) {
        return Err(InputError::Missing(missing));
    }

    let initial_volume_l = number(&fields, "initial_volume_l")?;
    if !(initial_volume_l > 0.0 && initial_volume_l <= tank_capacity_l) {
        return Err(invalid(
            "initial_volume_l",
            format!("{initial_volume_l} lies outside the tank, above 0 and up to {tank_capacity_l} litres"),
        ));
    }
    let order_pending = optional_seconds(&fields, "order_pending_s")?;
    let first_poll_after = optional_seconds(&fields, "first_poll_after_s")?;
    let supplier_start = match (order_pending, first_poll_after) {
        (Some(remaining), None) => SupplierStart::OrderPending(remaining),
        (None, Some(after)) => SupplierStart::FirstPollAfter(after),
        _ => {
            return Err(invalid(
                "order_pending_s",
                "exactly one of order_pending_s and first_poll_after_s holds a time".to_string(),
            ))
        }
    };
    let held_since = fields.get("held_since").expect("checked above");
    if !held_since.is_null() {
        return Err(invalid(
            "held_since",
            "this build holds no paper from before the start, so the field is null".to_string(),
        ));
    }
    match fields.get("held_artefacts").expect("checked above") {
        Value::Array(artefacts) if artefacts.is_empty() => {}
        _ => {
            return Err(invalid(
                "held_artefacts",
                "this build holds no paper from before the start, so the list is empty".to_string(),
            ))
        }
    }

    Ok(Input {
        tug_seed: unsigned(&fields, "sim_tug_arrivals", u64::MAX)?,
        initial_volume_l,
        supplier_start,
        first_check_after: seconds(&fields, "first_check_after_s")?,
        next_audit_at: instant(&fields, "next_audit_at")?,
        initial_cumulative_pulses: unsigned(&fields, "initial_cumulative_pulses", u32::MAX.into())?
            as u32,
        initial_meter_totaliser_l: non_negative(&fields, "initial_meter_totaliser_l")?,
        first_bdn_number: positive_number(&fields, "first_bdn_number")?,
        first_ticket_number: positive_number(&fields, "first_ticket_number")?,
    })
}

impl Input {
    /// The start state at the simulator's start, `now`. The next audit is
    /// measured from `now`, and must lie after it. The competition's audit
    /// period is an interval in hours, so the first audit is always given;
    /// `period_in_hours` is false only for a build against unscaled constants.
    pub fn start(
        &self,
        now: PrimitiveDateTime,
        period_in_hours: bool,
    ) -> Result<Start, InputError> {
        if !period_in_hours {
            return Err(invalid(
                "next_audit_at",
                "the audit period of this build is a calendar period, which places every audit itself"
                    .to_string(),
            ));
        }
        let until = self.next_audit_at - now;
        if !until.is_positive() {
            return Err(invalid(
                "next_audit_at",
                format!(
                    "{} has passed at the simulator's start, {now}",
                    self.next_audit_at
                ),
            ));
        }
        Ok(Start {
            tug_seed: self.tug_seed,
            initial_volume_l: self.initial_volume_l,
            supplier_start: self.supplier_start,
            first_check_after: self.first_check_after,
            first_audit_after: Some(until.unsigned_abs()),
            initial_cumulative_pulses: self.initial_cumulative_pulses,
            initial_meter_totaliser_l: self.initial_meter_totaliser_l,
            first_bdn_number: self.first_bdn_number,
            first_ticket_number: self.first_ticket_number,
        })
    }
}

fn invalid(field: &'static str, reason: String) -> InputError {
    InputError::Invalid { field, reason }
}

fn number(fields: &Map<String, Value>, field: &'static str) -> Result<f64, InputError> {
    match fields.get(field).and_then(Value::as_f64) {
        Some(value) if value.is_finite() => Ok(value),
        _ => Err(invalid(field, "not a finite number".to_string())),
    }
}

fn non_negative(fields: &Map<String, Value>, field: &'static str) -> Result<f64, InputError> {
    let value = number(fields, field)?;
    if value < 0.0 {
        return Err(invalid(field, format!("{value} is negative")));
    }
    Ok(value)
}

fn seconds(fields: &Map<String, Value>, field: &'static str) -> Result<Duration, InputError> {
    Ok(Duration::from_secs_f64(non_negative(fields, field)?))
}

fn optional_seconds(
    fields: &Map<String, Value>,
    field: &'static str,
) -> Result<Option<Duration>, InputError> {
    if fields.get(field).is_some_and(Value::is_null) {
        return Ok(None);
    }
    seconds(fields, field).map(Some)
}

fn unsigned(fields: &Map<String, Value>, field: &'static str, max: u64) -> Result<u64, InputError> {
    match fields.get(field).and_then(Value::as_u64) {
        Some(value) if value <= max => Ok(value),
        _ => Err(invalid(field, format!("not an integer from 0 to {max}"))),
    }
}

fn positive_number(fields: &Map<String, Value>, field: &'static str) -> Result<u32, InputError> {
    let value = unsigned(fields, field, u32::MAX.into())?;
    if value == 0 {
        return Err(invalid(field, "a paper number starts at 1".to_string()));
    }
    Ok(value as u32)
}

fn instant(
    fields: &Map<String, Value>,
    field: &'static str,
) -> Result<PrimitiveDateTime, InputError> {
    let text = fields
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(field, "not a string".to_string()))?;
    let instant = OffsetDateTime::parse(text, &Rfc3339)
        .map_err(|error| {
            invalid(
                field,
                format!("{text:?} is not a time of RFC 3339: {error}"),
            )
        })?
        .to_offset(UtcOffset::UTC);
    Ok(PrimitiveDateTime::new(instant.date(), instant.time()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::macros::datetime;

    const CAPACITY: f64 = 50_000.0;

    fn valid() -> Map<String, Value> {
        let Value::Object(fields) = serde_json::json!({
            "sim_tug_arrivals": 18_446_744_073_709_551_615u64,
            "initial_volume_l": 30_000.0,
            "order_pending_s": null,
            "first_poll_after_s": 95.5,
            "first_check_after_s": 600.0,
            "next_audit_at": "2026-11-10T14:00:00Z",
            "initial_cumulative_pulses": 70_000,
            "initial_meter_totaliser_l": 54_000.0,
            "first_bdn_number": 37,
            "first_ticket_number": 4,
            "held_since": null,
            "held_artefacts": []
        }) else {
            unreachable!()
        };
        fields
    }

    fn parse_fields(fields: Map<String, Value>) -> Result<Input, InputError> {
        parse(&Value::Object(fields).to_string(), CAPACITY)
    }

    #[test]
    fn a_valid_input_gives_its_start_state() {
        let input = parse_fields(valid()).unwrap();
        assert_eq!(input.tug_seed, u64::MAX);
        assert_eq!(input.initial_volume_l, 30_000.0);
        assert_eq!(
            input.supplier_start,
            SupplierStart::FirstPollAfter(Duration::from_secs_f64(95.5))
        );
        assert_eq!(input.first_check_after, Duration::from_secs(600));
        assert_eq!(input.next_audit_at, datetime!(2026-11-10 14:00));
        assert_eq!(input.initial_cumulative_pulses, 70_000);
        assert_eq!(input.initial_meter_totaliser_l, 54_000.0);
        assert_eq!((input.first_bdn_number, input.first_ticket_number), (37, 4));
    }

    #[test]
    fn an_order_pending_starts_the_supplier_with_it() {
        let mut fields = valid();
        fields.insert("order_pending_s".into(), serde_json::json!(1_200.0));
        fields.insert("first_poll_after_s".into(), Value::Null);
        assert_eq!(
            parse_fields(fields).unwrap().supplier_start,
            SupplierStart::OrderPending(Duration::from_secs(1_200))
        );
    }

    #[test]
    fn the_next_audit_is_measured_from_the_start() {
        let input = parse_fields(valid()).unwrap();
        let start = input.start(datetime!(2026-11-10 9:00), true).unwrap();
        assert_eq!(start.first_audit_after, Some(Duration::from_secs(5 * 3600)));
        assert_eq!(start.first_bdn_number, 37);
    }

    #[test]
    fn a_next_audit_that_has_passed_is_refused() {
        let input = parse_fields(valid()).unwrap();
        for now in [datetime!(2026-11-10 14:00), datetime!(2026-11-10 14:00:01)] {
            let error = input.start(now, true).unwrap_err();
            assert!(
                matches!(
                    error,
                    InputError::Invalid {
                        field: "next_audit_at",
                        ..
                    }
                ),
                "{error}"
            );
        }
    }

    #[test]
    fn a_calendar_audit_period_is_refused() {
        let input = parse_fields(valid()).unwrap();
        assert!(input.start(datetime!(2026-11-10 9:00), false).is_err());
    }

    #[test]
    fn each_missing_field_is_named() {
        for field in FIELDS {
            let mut fields = valid();
            fields.remove(field);
            assert_eq!(
                parse_fields(fields).unwrap_err(),
                InputError::Missing(field)
            );
        }
    }

    #[test]
    fn an_unknown_field_is_named() {
        let mut fields = valid();
        fields.insert("sim_start_state".into(), serde_json::json!(1));
        assert_eq!(
            parse_fields(fields).unwrap_err(),
            InputError::Unknown("sim_start_state".into())
        );
    }

    #[test]
    fn text_that_is_not_an_object_is_refused() {
        assert!(matches!(
            parse("[1, 2]", CAPACITY),
            Err(InputError::NotObject)
        ));
        assert!(matches!(parse("{", CAPACITY), Err(InputError::NotJson(_))));
    }

    #[test]
    fn values_out_of_range_are_refused() {
        let cases: [(&str, Value); 12] = [
            ("initial_volume_l", serde_json::json!(0.0)),
            ("initial_volume_l", serde_json::json!(50_000.5)),
            ("first_poll_after_s", serde_json::json!(-1.0)),
            ("first_poll_after_s", Value::Null),
            ("first_check_after_s", serde_json::json!("600")),
            ("next_audit_at", serde_json::json!("2026-11-10 14:00")),
            (
                "initial_cumulative_pulses",
                serde_json::json!(4_294_967_296u64),
            ),
            ("initial_meter_totaliser_l", serde_json::json!(-0.5)),
            ("first_bdn_number", serde_json::json!(0)),
            ("first_ticket_number", serde_json::json!(1.5)),
            ("held_since", serde_json::json!("2026-11-09T00:00:00Z")),
            (
                "held_artefacts",
                serde_json::json!([{"bdn_number": "BDN-1"}]),
            ),
        ];
        for (field, value) in cases {
            let mut fields = valid();
            fields.insert(field.into(), value.clone());
            let error = parse_fields(fields).unwrap_err();
            assert!(
                matches!(error, InputError::Invalid { .. }),
                "{field} = {value}: {error}"
            );
        }
    }

    #[test]
    fn both_supplier_times_together_are_refused() {
        let mut fields = valid();
        fields.insert("order_pending_s".into(), serde_json::json!(1_200.0));
        assert!(matches!(
            parse_fields(fields).unwrap_err(),
            InputError::Invalid {
                field: "order_pending_s",
                ..
            }
        ));
    }

    #[test]
    fn an_instant_with_an_offset_is_read_in_utc() {
        let mut fields = valid();
        fields.insert(
            "next_audit_at".into(),
            serde_json::json!("2026-11-10T15:00:00+01:00"),
        );
        assert_eq!(
            parse_fields(fields).unwrap().next_audit_at,
            datetime!(2026-11-10 14:00)
        );
    }
}
