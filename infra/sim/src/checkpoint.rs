//! The checkpoint (sim.md §9, sim-impl.md "Checkpoint document"): its types,
//! the refusal rules on reading it, and the atomic write.
//!
//! The checkpoint is written to the sim host's in-memory run directory on a
//! stop, and read on start from the read-only state directory the deployment
//! mounts (sim-network.md §7). Every date-time in it is UTC, in RFC 3339 with a
//! `Z` designator.

use std::fs;
use std::io::{self, Write};
use std::path::Path;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;

use crate::actors::dock_operator::DockOperatorState;
use crate::actors::fuel_supplier::FuelSupplierState;
use crate::actors::reconciliation_user::{AuditPeriod, ReconciliationUserState};
use crate::actors::road_tanker_driver::RoadTankerDriverState;
use crate::actors::systems_administrator::SystemsAdministratorState;
use crate::appliances::atg_console::AtgConsoleState;
use crate::appliances::dispenser::DispenserState;
use crate::physical::atg_probe::AtgProbeState;
use crate::physical::fuel_tank::{FuelTankRows, FuelTankState};
use crate::physical::tug_arrival_generator::TugArrivalState;

/// Incremented whenever the document's shape changes.
pub const FORMAT_VERSION: u32 = 3;

/// The checkpoint's file name, in the run directory when written and in the
/// state directory when read.
pub const CHECKPOINT_FILE: &str = "checkpoint.json";
/// The reseed file's name in the state directory.
pub const RESEED_FILE: &str = "reseed.json";
/// Created in the run directory once a checkpoint is accepted; the deployment
/// waits on it before removing the checkpoint's mount
/// (`deployment/deployment.md` §8.1).
pub const LOADED_MARKER: &str = "checkpoint-loaded";

/// The sim's state at a stop.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Checkpoint {
    pub format_version: u32,
    /// The sim time at which the checkpoint was written.
    #[serde(with = "utc")]
    pub written_at: PrimitiveDateTime,
    /// The compiled parameters it was written under.
    pub parameters: Parameters,
    pub fuel_tank: FuelTankState,
    pub atg_probe: AtgProbeState,
    pub atg_console: AtgConsoleState,
    pub dispenser: DispenserState,
    pub tug_arrivals: TugArrivalState,
    pub dock_operator: DockOperatorState,
    pub road_tanker_driver: RoadTankerDriverState,
    pub fuel_supplier: FuelSupplierState,
    pub reconciliation_user: ReconciliationUserState,
    pub systems_administrator: SystemsAdministratorState,
}

/// The values of the orchestrator's `Config` that decide the simulator's
/// behaviour: every value other than its addresses, transports and seeds, and
/// other than the start state's values that a warm start replaces with the
/// checkpoint's states (sim-impl.md, "Start state"), the initial volume and
/// temperature excepted. A checkpoint written under different parameters is
/// refused, naming the first field that differs.
///
/// `initial_window` is the first retrieval width that
/// `reconciliation_user::retrieval_widths` gives at the roster's arrival rate
/// (`coupled::config`). The width floor that the same computation gives is
/// not recorded: it follows from the roster, which is.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Parameters {
    pub tank_capacity_l: f64,
    pub k_l_per_mm: f64,
    pub inflow_rate_l_per_min: f64,
    pub outflow_rate_l_per_min: f64,
    pub initial_volume_l: f64,
    pub initial_temperature_c: f64,
    pub flow_meter_k_factor: f64,
    pub probe_noise_enabled: bool,
    pub overfill_interlock_pct: f64,
    pub vessels: Vec<VesselParameters>,
    pub refuel_volume_min_l: f64,
    pub refuel_volume_max_l: f64,
    pub driver_id: String,
    pub tanker_registration: String,
    pub poll_interval: Duration,
    pub reorder_threshold_l: f64,
    pub delivery_volume_l: f64,
    pub lead_time: Duration,
    pub audit_period: AuditPeriod,
    pub settling_delay: Duration,
    pub initial_window: Duration,
    pub boundary_margin: Duration,
    pub clipboard_deadline: Duration,
    pub desktop_settle: Duration,
    pub terminal_settle: Duration,
    pub key_interval: Duration,
    pub connect_deadline: Duration,
    pub admin_check_interval: Duration,
    pub admin_step_deadline: Duration,
}

/// A roster vessel's compiled values (sim.md §6).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VesselParameters {
    pub vessel_id: u32,
    pub name: String,
    pub rfid_card: u32,
    pub mean_time_away: Duration,
}

/// New seeds for one or more random sequences (sim.md §9.3). A field present
/// replaces that sequence's seed and sets its position to zero.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Reseed {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tug_arrival_seed: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub atg_probe_noise_seed: Option<u64>,
}

/// Read the checkpoint, if one is present, from `state_dir`. `Ok(None)` is a
/// cold start. A checkpoint that does not parse, has a different format
/// version or parameter, or was written later than `now` or more than
/// `max_start_gap` before it is refused; so is a reseed file with an unknown
/// field, or one without a checkpoint. The error is the startup fault's
/// message. An accepted checkpoint, with any reseed applied, is returned once
/// the marker has been created in `run_dir`.
pub fn read(
    state_dir: &Path,
    run_dir: &Path,
    now: PrimitiveDateTime,
    parameters: &Parameters,
    max_start_gap: Duration,
) -> Result<Option<Checkpoint>, String> {
    let checkpoint_path = state_dir.join(CHECKPOINT_FILE);
    let reseed_path = state_dir.join(RESEED_FILE);

    let Some(text) = read_if_present(&checkpoint_path)? else {
        if reseed_path.exists() {
            return Err(format!(
                "{} is present without a checkpoint",
                reseed_path.display()
            ));
        }
        return Ok(None);
    };

    let document: serde_json::Value = serde_json::from_str(&text)
        .map_err(|error| format!("{} does not parse: {error}", checkpoint_path.display()))?;
    let version = document
        .get("format_version")
        .and_then(serde_json::Value::as_u64);
    if version != Some(u64::from(FORMAT_VERSION)) {
        return Err(format!(
            "{} has format version {}; this sim reads format version {FORMAT_VERSION}",
            checkpoint_path.display(),
            version.map_or_else(|| "none".to_string(), |v| v.to_string()),
        ));
    }
    let mut checkpoint: Checkpoint = serde_json::from_value(document)
        .map_err(|error| format!("{} does not parse: {error}", checkpoint_path.display()))?;

    if let Some(field) = first_differing_parameter(&checkpoint.parameters, parameters) {
        return Err(format!(
            "{} was written under a different value of the parameter {field}",
            checkpoint_path.display()
        ));
    }

    let written_at = checkpoint.written_at;
    if written_at > now {
        return Err(format!(
            "{} was written at {}, after the sim's start at {}",
            checkpoint_path.display(),
            rfc3339(written_at),
            rfc3339(now),
        ));
    }
    if now - written_at > max_start_gap {
        return Err(format!(
            "{} was written at {}, more than {}s before the sim's start at {}",
            checkpoint_path.display(),
            rfc3339(written_at),
            max_start_gap.as_secs(),
            rfc3339(now),
        ));
    }

    if let Some(text) = read_if_present(&reseed_path)? {
        let reseed: Reseed = serde_json::from_str(&text)
            .map_err(|error| format!("{} is refused: {error}", reseed_path.display()))?;
        if let Some(seed) = reseed.tug_arrival_seed {
            checkpoint.tug_arrivals.seed = seed;
            for tug in &mut checkpoint.tug_arrivals.tugs {
                tug.word_pos = 0;
            }
        }
        if let Some(seed) = reseed.atg_probe_noise_seed {
            checkpoint.atg_probe.noise_seed = seed;
            checkpoint.atg_probe.noise_word_pos = 0;
        }
    }

    let marker = run_dir.join(LOADED_MARKER);
    fs::File::create(&marker)
        .map_err(|error| format!("cannot create {}: {error}", marker.display()))?;
    Ok(Some(checkpoint))
}

/// Write `checkpoint` to `run_dir`, once the fuel tank's rows confirm the sim
/// is at rest (sim.md §9.1). The document is written to a temporary file,
/// synchronised and renamed, so that a reader never sees a partial file.
///
/// # Panics
///
/// If the sim is not at rest, an invariant violation; nothing is written.
pub fn write_at_rest(
    run_dir: &Path,
    rows: &FuelTankRows,
    checkpoint: &Checkpoint,
) -> io::Result<()> {
    assert!(
        rows.at_rest(),
        "orchestrator: the sim is not at rest after every actor has reported: {rows:?}"
    );
    let path = run_dir.join(CHECKPOINT_FILE);
    let temporary = run_dir.join(format!("{CHECKPOINT_FILE}.tmp"));
    let document = serde_json::to_vec_pretty(checkpoint).map_err(io::Error::other)?;
    let mut file = fs::File::create(&temporary)?;
    file.write_all(&document)?;
    file.sync_all()?;
    fs::rename(&temporary, &path)
}

/// The file's contents, or `None` if it does not exist.
fn read_if_present(path: &Path) -> Result<Option<String>, String> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(Some(text)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!("cannot read {}: {error}", path.display())),
    }
}

/// The name of the first parameter, in name order, whose value differs.
fn first_differing_parameter(saved: &Parameters, compiled: &Parameters) -> Option<String> {
    let as_map = |parameters: &Parameters| match serde_json::to_value(parameters) {
        Ok(serde_json::Value::Object(map)) => map,
        _ => unreachable!("Parameters serialises as an object"),
    };
    let (saved, compiled) = (as_map(saved), as_map(compiled));
    compiled
        .iter()
        .find(|(name, value)| saved.get(*name) != Some(value))
        .map(|(name, _)| name.clone())
}

fn rfc3339(at: PrimitiveDateTime) -> String {
    at.assume_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .expect("a UTC date-time always formats as RFC 3339")
}

/// Serde for a sim date-time: RFC 3339 with a `Z` designator. Every sim
/// date-time is UTC (sim.md §2.1), so the offset is asserted on writing and
/// required on reading.
pub mod utc {
    use serde::{de, Deserialize, Deserializer, Serializer};
    use time::format_description::well_known::Rfc3339;
    use time::{OffsetDateTime, PrimitiveDateTime, UtcOffset};

    pub fn serialize<S: Serializer>(
        at: &PrimitiveDateTime,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&super::rfc3339(*at))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<PrimitiveDateTime, D::Error> {
        let text = String::deserialize(deserializer)?;
        let at = OffsetDateTime::parse(&text, &Rfc3339).map_err(de::Error::custom)?;
        if at.offset() != UtcOffset::UTC {
            return Err(de::Error::custom(format!("{text} is not UTC")));
        }
        Ok(PrimitiveDateTime::new(at.date(), at.time()))
    }

    /// The same, for an optional date-time.
    pub mod option {
        use serde::{Deserialize, Deserializer, Serializer};
        use time::PrimitiveDateTime;

        pub fn serialize<S: Serializer>(
            at: &Option<PrimitiveDateTime>,
            serializer: S,
        ) -> Result<S::Ok, S::Error> {
            match at {
                Some(at) => super::serialize(at, serializer),
                None => serializer.serialize_none(),
            }
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(
            deserializer: D,
        ) -> Result<Option<PrimitiveDateTime>, D::Error> {
            #[derive(Deserialize)]
            struct Utc(#[serde(with = "super")] PrimitiveDateTime);
            Ok(Option::<Utc>::deserialize(deserializer)?.map(|Utc(at)| at))
        }
    }
}

/// Serde for a random generator's position, a `u128`, as a decimal string:
/// JSON numbers do not carry 128-bit integers exactly.
pub mod decimal {
    use serde::{de, Deserialize, Deserializer, Serializer};

    pub fn serialize<S: Serializer>(value: &u128, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u128, D::Error> {
        String::deserialize(deserializer)?
            .parse()
            .map_err(de::Error::custom)
    }
}

/// A text field held as `&'static str`: the vessel and port constants written
/// on a dispense job and its BDN. Serde's derive borrows a field whose type is
/// written `&str` from the input, which a checkpoint read from a file cannot
/// lend; a field of this alias type, read with [`static_str`], is not
/// borrowed.
pub type StaticStr = &'static str;

/// Serde for a [`StaticStr`] field. Reading one is part of a warm start, which
/// happens once per process, so the text is leaked to give it the static
/// lifetime.
pub mod static_str {
    use serde::{Deserialize, Deserializer};

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<&'static str, D::Error> {
        Ok(String::deserialize(deserializer)?.leak())
    }
}
