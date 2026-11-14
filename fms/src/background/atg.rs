//! ATG poller: a Veeder-Root Tank Level System (TLS) serial-over-TCP client
//! for the ATG console appliance. Each cycle it polls i201 (inventory), i205
//! (tank status) and i101 (system status), updates the shared `last_atg`
//! reading, persists to `atg_readings`, and forwards events to the log-server
//! pusher.
//!
//! The wire format is specified in `specs/host_specs/sim/tls-protocol.md`. The
//! logic is split so that the I/O shell (`run`/`poll_once`) stays thin and the
//! parsing/interpretation is pure and unit-testable: `interpret_cycle` turns
//! three raw response frames into an [`AtgReading`] plus the log events to
//! emit, with no socket or database access.

use std::time::Duration;

use chrono::{DateTime, Utc};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    time,
};

use crate::{
    config::Config,
    state::{AppState, AtgReading, EventKind, LogEvent},
};

// ---------------------------------------------------------------------------
// Protocol constants
// ---------------------------------------------------------------------------

const SOH: u8 = 0x01;
const ETX: u8 = 0x03;

/// Six-character computer-format function codes, all-devices (`TT=00`).
const CODE_INVENTORY: &str = "i20100";
const CODE_TANK_STATUS: &str = "i20500";
const CODE_SYSTEM_STATUS: &str = "i10100";

const ON_DEMAND_READ_TIMEOUT: Duration = Duration::from_secs(5);

/// Limit connection establishment and all three requests to one polling timeout.
const POLL_CYCLE_TIMEOUT: Duration = Duration::from_secs(15);

/// i101 `AANNTT` entry for the bund liquid-sensor fuel alarm (sensor 1):
/// Liquid Sensor (`03`) / Sensor Fuel Alarm (`03`) / sensor `01`.
const BUND_ALARM_ENTRY: &str = "030301";
/// i101 all-normal entry (`AA=00`, `NN=00`, `TT=00`).
const ALL_NORMAL_ENTRY: &str = "000000";

// ---------------------------------------------------------------------------
// Errors and decoded types
// ---------------------------------------------------------------------------

/// A failure to decode a response frame. A cycle that yields any of these is
/// rejected wholesale — no partial reading is recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AtgError {
    /// Frame is too short to contain the mandatory framing.
    ShortFrame,
    /// First byte is not `SOH`.
    BadStart,
    /// No `ETX` terminator found.
    NoEtx,
    /// Computed checksum does not agree with the four hex digits on the wire.
    ChecksumMismatch { computed: u16, received: u16 },
    /// The console replied with the fixed unrecognised-command error frame
    /// (`\x019999FF1B\x03`).
    ConsoleError,
    /// The echoed function code did not match the code we requested.
    UnexpectedCode { want: String, got: String },
    /// The code-specific body did not match its expected layout.
    MalformedBody,
}

impl std::fmt::Display for AtgError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for AtgError {}

/// The seven i201 inventory fields (tls-protocol.md §6.2), in metric units.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Inventory {
    pub volume_l: f64,
    pub tc_volume_l: f64,
    pub ullage_l: f64,
    pub level_mm: f64,
    pub water_mm: f64,
    pub temperature_c: f64,
    pub water_volume_l: f64,
}

/// The outcome of interpreting one poll cycle: the reading to publish, the log
/// events to emit, and the raw i101 body to carry forward for edge detection.
pub(crate) struct CycleOutcome {
    pub reading: AtgReading,
    pub events: Vec<LogEvent>,
    pub alarm_flags: String,
}

// ---------------------------------------------------------------------------
// Wire-format helpers (pure)
// ---------------------------------------------------------------------------

/// Build a command frame: `SOH` followed by the six-character function code.
/// Commands carry no security code, no checksum and no terminator
/// (tls-protocol.md §2).
pub(crate) fn build_request(code: &str) -> Vec<u8> {
    let mut frame = Vec::with_capacity(1 + code.len());
    frame.push(SOH);
    frame.extend_from_slice(code.as_bytes());
    frame
}

/// Validate and split a computer-format response into its echoed function code
/// and its data body (the bytes between the `YYMMDDHHmm` timestamp and the
/// `&&` checksum tag). Verifies the checksum per tls-protocol.md §4.
pub(crate) fn parse_response(data: &[u8]) -> Result<(String, String), AtgError> {
    if data.is_empty() {
        return Err(AtgError::ShortFrame);
    }
    if data[0] != SOH {
        return Err(AtgError::BadStart);
    }
    // Fixed unrecognised-command error frame: SOH "9999" <checksum> ETX.
    if data.len() >= 5 && &data[1..5] == b"9999" {
        return Err(AtgError::ConsoleError);
    }

    let etx = data.iter().position(|&b| b == ETX).ok_or(AtgError::NoEtx)?;

    // Minimum frame: SOH(1) + echo(6) + timestamp(10) + "&&"(2) + checksum(4) + ETX(1).
    if etx < 1 + 6 + 10 + 2 + 4 {
        return Err(AtgError::ShortFrame);
    }
    // The "&&" data-termination tag immediately precedes the checksum digits.
    if &data[etx - 6..etx - 4] != b"&&" {
        return Err(AtgError::MalformedBody);
    }

    // Checksum covers every byte up to (not including) the four hex digits —
    // SOH through the second '&' (tls-protocol.md §4): C = 2's complement of S,
    // so a valid frame satisfies (S + C) mod 2^16 == 0.
    let summed = &data[..etx - 4];
    let computed = 0u16.wrapping_sub(
        summed
            .iter()
            .fold(0u16, |acc, &b| acc.wrapping_add(u16::from(b))),
    );
    let checksum_text =
        std::str::from_utf8(&data[etx - 4..etx]).map_err(|_| AtgError::MalformedBody)?;
    let received = u16::from_str_radix(checksum_text, 16).map_err(|_| AtgError::MalformedBody)?;
    if received != computed {
        return Err(AtgError::ChecksumMismatch { computed, received });
    }

    let echo = std::str::from_utf8(&data[1..7])
        .map_err(|_| AtgError::MalformedBody)?
        .to_owned();
    let body = std::str::from_utf8(&data[17..etx - 6])
        .map_err(|_| AtgError::MalformedBody)?
        .to_owned();
    Ok((echo, body))
}

/// Decode eight upper-case big-endian ASCII-hex nibbles into an IEEE-754
/// single-precision float (tls-protocol.md §5).
fn decode_float(hex: &str) -> Option<f32> {
    if hex.len() != 8 {
        return None;
    }
    let mut bytes = [0u8; 4];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(f32::from_be_bytes(bytes))
}

/// Parse an i201 body: the `TT p ssss NN` prefix followed by seven 8-char
/// floats (tls-protocol.md §6.2).
fn parse_i201(body: &str) -> Result<Inventory, AtgError> {
    const PREFIX: usize = 9; // TT(2) + p(1) + ssss(4) + NN(2)
    const FIELDS: usize = 7;
    if body.len() < PREFIX + FIELDS * 8 {
        return Err(AtgError::MalformedBody);
    }
    // NN — the float-field count — is fixed at 07 in the testbed (§6.2/§7).
    let count = u8::from_str_radix(&body[7..9], 16).map_err(|_| AtgError::MalformedBody)?;
    if count as usize != FIELDS {
        return Err(AtgError::MalformedBody);
    }

    let mut f = [0f64; FIELDS];
    for (i, field) in f.iter_mut().enumerate() {
        let start = PREFIX + i * 8;
        *field = f64::from(decode_float(&body[start..start + 8]).ok_or(AtgError::MalformedBody)?);
    }

    Ok(Inventory {
        volume_l: f[0],
        tc_volume_l: f[1],
        ullage_l: f[2],
        level_mm: f[3],
        water_mm: f[4],
        temperature_c: f[5],
        water_volume_l: f[6],
    })
}

/// Parse an i101 body (concatenated `AANNTT` entries); returns whether the bund
/// liquid-sensor fuel alarm is present (tls-protocol.md §6.1).
fn parse_i101(body: &str) -> Result<bool, AtgError> {
    if body.is_empty() || !body.len().is_multiple_of(6) {
        return Err(AtgError::MalformedBody);
    }
    let bund = body
        .as_bytes()
        .chunks(6)
        .any(|entry| entry == BUND_ALARM_ENTRY.as_bytes());
    Ok(bund)
}

/// Parse an i205 body (`TT nn NN…`); returns the active tank-alarm type numbers
/// (empty in the testbed) (tls-protocol.md §6.3).
fn parse_i205(body: &str) -> Result<Vec<u8>, AtgError> {
    // TT(2) + nn(2) then nn × NN(2).
    if body.len() < 4 {
        return Err(AtgError::MalformedBody);
    }
    let count = u8::from_str_radix(&body[2..4], 16).map_err(|_| AtgError::MalformedBody)? as usize;
    if body.len() < 4 + count * 2 {
        return Err(AtgError::MalformedBody);
    }
    let mut alarms = Vec::with_capacity(count);
    for i in 0..count {
        let start = 4 + i * 2;
        alarms.push(
            u8::from_str_radix(&body[start..start + 2], 16).map_err(|_| AtgError::MalformedBody)?,
        );
    }
    Ok(alarms)
}

/// Human-readable name for an i101 `AANNTT` alarm entry.
fn alarm_code_name(code: &str) -> &'static str {
    match code {
        BUND_ALARM_ENTRY => "bund_leak",
        _ => "unknown",
    }
}

fn expect_code(echo: &str, want: &str) -> Result<(), AtgError> {
    if echo == want {
        Ok(())
    } else {
        Err(AtgError::UnexpectedCode {
            want: want.to_owned(),
            got: echo.to_owned(),
        })
    }
}

// ---------------------------------------------------------------------------
// Cycle interpretation (pure)
// ---------------------------------------------------------------------------

/// Turn the three raw response frames of one poll cycle into the reading to
/// publish and the events to emit. Pure: no socket, no database, no clock —
/// the caller injects `now` and the previous cycle's i101 body.
pub(crate) fn interpret_cycle(
    i201_raw: &[u8],
    i205_raw: &[u8],
    i101_raw: &[u8],
    prev_alarm_flags: &str,
    now: DateTime<Utc>,
) -> Result<CycleOutcome, AtgError> {
    let (echo, body201) = parse_response(i201_raw)?;
    expect_code(&echo, CODE_INVENTORY)?;
    let inv = parse_i201(&body201)?;

    let (echo, body205) = parse_response(i205_raw)?;
    expect_code(&echo, CODE_TANK_STATUS)?;
    let tank_alarms = parse_i205(&body205)?;

    let (echo, body101) = parse_response(i101_raw)?;
    expect_code(&echo, CODE_SYSTEM_STATUS)?;
    let bund_alarm_active = parse_i101(&body101)?;

    // The FMS reconciles on field 2, TC Volume (tls-protocol.md §6.2).
    let reading = AtgReading::new(
        inv.tc_volume_l,
        inv.ullage_l,
        inv.level_mm,
        inv.temperature_c,
        body101.clone(),
        bund_alarm_active,
        now,
    )
    .map_err(|_| AtgError::MalformedBody)?;

    // hist.md §4.1 names these as metrics, not events: the pusher turns the two
    // readings into `atg_tank_volume_litres` and `atg_tank_temperature_celsius`.
    let mut events = vec![LogEvent::new(
        EventKind::AtgReading,
        now,
        serde_json::json!({
            "volume_l": reading.volume_l(),
            "temperature_c": reading.temperature_c(),
        }),
    )];

    // Rising edge: emit the bund alarm once, when it latches.
    let prev_bund = prev_alarm_flags.contains(BUND_ALARM_ENTRY);
    if bund_alarm_active && !prev_bund {
        events.push(LogEvent::new(
            EventKind::AtgAlarm,
            now,
            serde_json::json!({
                "alarm": alarm_code_name(BUND_ALARM_ENTRY),
                "code": BUND_ALARM_ENTRY,
                "active": 1,
            }),
        ));
    }

    // Tank alarms from i205 (none in the testbed; surfaced generically).
    for code in &tank_alarms {
        events.push(LogEvent::new(
            EventKind::AtgAlarm,
            now,
            serde_json::json!({
                "alarm": "tank_alarm",
                "tank_alarm_type": code,
                "active": 1,
            }),
        ));
    }

    Ok(CycleOutcome {
        reading,
        events,
        alarm_flags: body101,
    })
}

// ---------------------------------------------------------------------------
// I/O shell
// ---------------------------------------------------------------------------

/// Send one command and read the full response (up to and including `ETX`).
async fn request(stream: &mut TcpStream, code: &str) -> anyhow::Result<Vec<u8>> {
    stream.write_all(&build_request(code)).await?;
    let mut buf = Vec::new();
    let mut byte = [0u8; 1];
    loop {
        stream.read_exact(&mut byte).await?;
        buf.push(byte[0]);
        if byte[0] == ETX {
            break;
        }
    }
    Ok(buf)
}

/// Apply a timeout and include the operation and duration in the error on expiry.
async fn with_timeout<T>(
    operation: &str,
    timeout: Duration,
    future: impl std::future::Future<Output = anyhow::Result<T>>,
) -> anyhow::Result<T> {
    match time::timeout(timeout, future).await {
        Ok(result) => result,
        Err(_) => anyhow::bail!("{operation} timed out after {} ms", timeout.as_millis()),
    }
}

/// Raw inventory, tank-status and system-status reports from one polling cycle.
#[derive(Debug)]
pub(crate) struct RawCycle {
    pub inventory: Vec<u8>,
    pub tank_status: Vec<u8>,
    pub system_status: Vec<u8>,
}

/// Read all three reports within a single timeout.
async fn poll_network_phase(addr: &str, timeout: Duration) -> anyhow::Result<RawCycle> {
    with_timeout("ATG poll cycle", timeout, async {
        let mut stream = TcpStream::connect(addr).await?;
        Ok(RawCycle {
            inventory: request(&mut stream, CODE_INVENTORY).await?,
            tank_status: request(&mut stream, CODE_TANK_STATUS).await?,
            system_status: request(&mut stream, CODE_SYSTEM_STATUS).await?,
        })
    })
    .await
}

pub async fn poll_once(
    state: &AppState,
    config: &Config,
    prev_alarm_flags: &mut String,
) -> anyhow::Result<()> {
    let addr = format!("{}:{}", config.atg_host, config.atg_port);
    let cycle = poll_network_phase(&addr, POLL_CYCLE_TIMEOUT).await?;

    let now = Utc::now();
    match interpret_cycle(
        &cycle.inventory,
        &cycle.tank_status,
        &cycle.system_status,
        prev_alarm_flags,
        now,
    ) {
        Ok(outcome) => {
            *state.last_atg.write().await = Some(outcome.reading.clone());

            sqlx::query!(
                "INSERT INTO atg_readings (volume_l, temperature_c, alarm_flags, recorded_at)\n         VALUES ($1, $2, $3, $4)",
                outcome.reading.volume_l(),
                outcome.reading.temperature_c(),
                outcome.reading.alarm_flags(),
                now,
            )
            .execute(&state.db)
            .await?;

            for event in outcome.events {
                state.emit_log_event(event);
            }

            *prev_alarm_flags = outcome.alarm_flags;
        }
        Err(e) => tracing::warn!("ATG cycle rejected: {e}"),
    }

    Ok(())
}

/// Issue a single on-demand `i201` inventory poll and return the
/// temperature-corrected volume — the field the FMS reconciles on
/// (tls-protocol.md §6.2, matching [`interpret_cycle`]). Used to capture the
/// tank volume at a transaction boundary: the periodic cache (`last_atg`,
/// refreshed every `atg_poll_interval`) would be up to one interval stale, and
/// since the tank is still at a higher level earlier in the poll cycle, that
/// staleness systematically inflates the meter-versus-ATG delta beyond the ±1%
/// reconciliation tolerance the invariant depends on (narrative.md §6, fms.md
/// §5.1). By the time a dispense/fill closes, flow has stopped and the level is
/// static, so a fresh read is accurate to within the flow meter's pulse
/// quantisation.
///
/// Transaction-scoped: unlike [`poll_once`] it does not update the shared
/// reading, write an `atg_readings` row, or emit log events, so the periodic
/// time-series keeps its uniform cadence for the rate-of-change checks.
pub async fn read_tc_volume_on_demand(config: &Config) -> anyhow::Result<f64> {
    read_tc_volume_at(
        &format!("{}:{}", config.atg_host, config.atg_port),
        ON_DEMAND_READ_TIMEOUT,
    )
    .await
}

/// The two figures a transaction boundary takes from one `i201` read: the
/// temperature-compensated volume and the fuel temperature. The dispense close
/// records both from the same reading (fms.md §5.2).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TankReading {
    pub tc_volume_l: f64,
    pub temperature_c: f64,
}

/// Read the current volume and temperature from `addr` within `timeout`.
pub(crate) async fn read_tc_reading_at(
    addr: &str,
    timeout: Duration,
) -> anyhow::Result<TankReading> {
    with_timeout(
        "on-demand ATG read",
        timeout,
        read_tc_reading_at_unbounded(addr),
    )
    .await
}

async fn read_tc_reading_at_unbounded(addr: &str) -> anyhow::Result<TankReading> {
    let mut stream = TcpStream::connect(addr).await?;
    let raw = request(&mut stream, CODE_INVENTORY).await?;
    let (echo, body) = parse_response(&raw)?;
    expect_code(&echo, CODE_INVENTORY)?;
    let inv = parse_i201(&body)?;
    let volume = inv.tc_volume_l;
    if !volume.is_finite() || volume < 0.0 {
        anyhow::bail!("console reported a non-physical TC volume ({volume})");
    }
    // The dispense close records this temperature (fms.md §5.2); the periodic
    // path's `AtgReading::new` refuses a non-finite one, and so must this read.
    if !inv.temperature_c.is_finite() {
        anyhow::bail!(
            "console reported a non-physical temperature ({})",
            inv.temperature_c
        );
    }
    Ok(TankReading {
        tc_volume_l: volume,
        temperature_c: inv.temperature_c,
    })
}

/// [`read_tc_reading_at`], volume only.
pub(crate) async fn read_tc_volume_at(addr: &str, timeout: Duration) -> anyhow::Result<f64> {
    Ok(read_tc_reading_at(addr, timeout).await?.tc_volume_l)
}

/// An on-demand read with a fallback to the cached periodic reading: returns
/// the fresh reading, or the cache (logging a warning under `label`) if the
/// console is unreachable and the cached reading is sufficiently fresh and not
/// future-dated (fms.md §5.1).
pub async fn read_tc_reading_or_cached(state: &AppState, label: &str) -> Option<TankReading> {
    let addr = format!("{}:{}", state.config.atg_host, state.config.atg_port);
    let error = match read_tc_reading_at(&addr, ON_DEMAND_READ_TIMEOUT).await {
        Ok(reading) => return Some(reading),
        Err(e) => e,
    };
    // Discard the cache if the poll interval is too large to set a cache age limit.
    let Ok(max_age) = chrono::Duration::from_std(state.config.atg_poll_interval * 2) else {
        tracing::warn!(
            "on-demand ATG read at {label} failed ({error}); the ATG poll interval is too large \
             to set a cache age limit, so no volume is recorded"
        );
        return None;
    };
    let cached = state
        .last_atg
        .read()
        .await
        .as_ref()
        .filter(|r| {
            let age = Utc::now() - r.recorded_at();
            age >= chrono::Duration::zero() && age <= max_age
        })
        .map(|r| TankReading {
            tc_volume_l: r.volume_l(),
            temperature_c: r.temperature_c(),
        });
    match cached {
        Some(reading) => {
            tracing::warn!("on-demand ATG read at {label} failed ({error}); using cached reading");
            Some(reading)
        }
        None => {
            tracing::warn!(
                "on-demand ATG read at {label} failed ({error}) and no sufficiently fresh \
                 cached reading exists; recording no volume"
            );
            None
        }
    }
}

/// [`read_tc_reading_or_cached`], volume only: the rule of a dispense's
/// close (fms.md §5.1).
pub async fn read_tc_volume_or_cached(state: &AppState, label: &str) -> Option<f64> {
    read_tc_reading_or_cached(state, label)
        .await
        .map(|r| r.tc_volume_l)
}

/// An on-demand read for a fill's opening or closing volume: the fresh
/// reading, or `None` if the read fails, logged as a warning. A fill never
/// falls back to the cached reading; it records null instead (fms.md §5.1).
pub async fn read_tc_volume_or_null(state: &AppState, label: &str) -> Option<f64> {
    match read_tc_volume_on_demand(&state.config).await {
        Ok(volume) => Some(volume),
        Err(e) => {
            tracing::warn!("on-demand ATG read at {label} failed ({e}); recording no volume");
            None
        }
    }
}

pub async fn run(state: AppState) {
    let config = state.config.clone();
    let mut prev_alarm_flags = ALL_NORMAL_ENTRY.to_owned();
    let mut interval = time::interval(config.atg_poll_interval);
    interval.set_missed_tick_behavior(time::MissedTickBehavior::Skip);

    loop {
        interval.tick().await;
        if let Err(e) = poll_once(&state, &config, &mut prev_alarm_flags).await {
            tracing::warn!("ATG poll error: {e}");
        }
    }
}

// ---------------------------------------------------------------------------
// Tests (Phase 1 — written before the leaf parsers are implemented)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    // -- frame-building test helpers (mirror the sim console's framing) --

    /// Two's-complement checksum of all bytes up to (not including) the four
    /// checksum digits (tls-protocol.md §4).
    fn checksum(bytes: &[u8]) -> u16 {
        let sum = bytes
            .iter()
            .fold(0u16, |acc, &b| acc.wrapping_add(u16::from(b)));
        0u16.wrapping_sub(sum)
    }

    /// Build a complete computer-format response frame for `echo` + `body`,
    /// using a fixed timestamp.
    pub(super) fn frame(echo: &str, body: &str) -> Vec<u8> {
        let mut m = vec![SOH];
        m.extend_from_slice(echo.as_bytes());
        m.extend_from_slice(b"2501010000"); // YYMMDDHHmm
        m.extend_from_slice(body.as_bytes());
        m.extend_from_slice(b"&&");
        let c = checksum(&m);
        m.extend_from_slice(format!("{c:04X}").as_bytes());
        m.push(ETX);
        m
    }

    fn encode_float(value: f32) -> String {
        value
            .to_be_bytes()
            .iter()
            .map(|b| format!("{b:02X}"))
            .collect()
    }

    /// i201 body for the canonical test reading: volume 35 000 L, TC volume
    /// 35 000 L, ullage 15 000 L, level 5 000 mm, water 0, temp 15 °C, water
    /// volume 0 (matches infra/sim atg_console.rs).
    fn i201_body() -> String {
        let fields = [35_000.0_f32, 35_000.0, 15_000.0, 5_000.0, 0.0, 15.0, 0.0];
        let mut b = String::from("01D000007"); // TT=01, p=D, ssss=0000, NN=07
        for f in fields {
            b.push_str(&encode_float(f));
        }
        b
    }

    fn now() -> DateTime<Utc> {
        DateTime::from_timestamp(1_700_000_000, 0).unwrap()
    }

    fn count_events(events: &[LogEvent], kind: EventKind) -> usize {
        events.iter().filter(|e| e.kind == kind).count()
    }

    // -- build_request --

    #[test]
    fn build_request_has_no_terminator_or_checksum() {
        let frame = build_request(CODE_INVENTORY);
        assert_eq!(frame, b"\x01i20100");
        assert_eq!(frame.len(), 7);
        assert_ne!(*frame.last().unwrap(), ETX);
    }

    // -- decode_float --

    #[test]
    fn decode_float_manual_worked_example() {
        // tls-protocol.md §5: 461C4000 == 10000.0
        assert_eq!(decode_float("461C4000"), Some(10_000.0));
    }

    #[test]
    fn decode_float_zero_convention() {
        assert_eq!(decode_float("00000000"), Some(0.0));
    }

    // -- parse_response --

    #[test]
    fn parse_response_splits_echo_and_body() {
        let f = frame(CODE_INVENTORY, &i201_body());
        let (echo, body) = parse_response(&f).expect("valid frame");
        assert_eq!(echo, CODE_INVENTORY);
        assert_eq!(body, i201_body());
    }

    #[test]
    fn parse_response_rejects_bad_checksum() {
        let mut f = frame(CODE_SYSTEM_STATUS, ALL_NORMAL_ENTRY);
        // Corrupt a body byte without touching the checksum digits.
        let body_pos = 1 + 6 + 10; // SOH + echo + timestamp
        f[body_pos] ^= 0x01;
        assert!(matches!(
            parse_response(&f),
            Err(AtgError::ChecksumMismatch { .. })
        ));
    }

    #[test]
    fn parse_response_rejects_wrong_start_byte() {
        let mut f = frame(CODE_SYSTEM_STATUS, ALL_NORMAL_ENTRY);
        f[0] = 0x02;
        assert_eq!(parse_response(&f), Err(AtgError::BadStart));
    }

    #[test]
    fn parse_response_rejects_missing_etx() {
        let mut f = frame(CODE_SYSTEM_STATUS, ALL_NORMAL_ENTRY);
        f.pop(); // drop the ETX
        assert_eq!(parse_response(&f), Err(AtgError::NoEtx));
    }

    #[test]
    fn parse_response_recognises_console_error_frame() {
        assert_eq!(
            parse_response(b"\x019999FF1B\x03"),
            Err(AtgError::ConsoleError)
        );
    }

    // -- parse_i201 --

    #[test]
    fn parse_i201_decodes_seven_fields() {
        let inv = parse_i201(&i201_body()).expect("valid i201 body");
        assert!((inv.volume_l - 35_000.0).abs() < 1.0);
        assert!((inv.tc_volume_l - 35_000.0).abs() < 1.0);
        assert!((inv.ullage_l - 15_000.0).abs() < 1.0);
        assert!((inv.level_mm - 5_000.0).abs() < 1.0);
        assert!((inv.water_mm - 0.0).abs() < 1.0);
        assert!((inv.temperature_c - 15.0).abs() < 1.0);
        assert!((inv.water_volume_l - 0.0).abs() < 1.0);
    }

    #[test]
    fn parse_i201_rejects_wrong_field_count() {
        let mut body = i201_body();
        body.replace_range(7..9, "06"); // NN = 06 instead of 07
        assert_eq!(parse_i201(&body), Err(AtgError::MalformedBody));
    }

    // -- parse_i101 --

    #[test]
    fn parse_i101_all_normal_is_no_alarm() {
        assert_eq!(parse_i101(ALL_NORMAL_ENTRY), Ok(false));
    }

    #[test]
    fn parse_i101_bund_entry_is_alarm() {
        assert_eq!(parse_i101(BUND_ALARM_ENTRY), Ok(true));
    }

    // -- parse_i205 --

    #[test]
    fn parse_i205_no_tank_alarms() {
        assert_eq!(parse_i205("0100"), Ok(vec![]));
    }

    #[test]
    fn parse_i205_one_tank_alarm() {
        // TT=01, nn=01, one alarm type 0x04 (Overfill).
        assert_eq!(parse_i205("010104"), Ok(vec![4]));
    }

    // -- alarm_code_name --

    #[test]
    fn alarm_code_name_bund() {
        assert_eq!(alarm_code_name(BUND_ALARM_ENTRY), "bund_leak");
    }

    // -- interpret_cycle --

    #[test]
    fn interpret_cycle_clean_state() {
        let i201 = frame(CODE_INVENTORY, &i201_body());
        let i205 = frame(CODE_TANK_STATUS, "0100");
        let i101 = frame(CODE_SYSTEM_STATUS, ALL_NORMAL_ENTRY);

        let out =
            interpret_cycle(&i201, &i205, &i101, ALL_NORMAL_ENTRY, now()).expect("clean cycle");

        assert!((out.reading.volume_l() - 35_000.0).abs() < 1.0);
        assert!((out.reading.ullage_l() - 15_000.0).abs() < 1.0);
        assert!((out.reading.level_mm() - 5_000.0).abs() < 1.0);
        assert!((out.reading.temperature_c() - 15.0).abs() < 1.0);
        assert!(!out.reading.bund_alarm_active());
        assert_eq!(out.reading.alarm_flags(), ALL_NORMAL_ENTRY);
        assert_eq!(out.alarm_flags, ALL_NORMAL_ENTRY);
        assert_eq!(count_events(&out.events, EventKind::AtgReading), 1);
        assert_eq!(count_events(&out.events, EventKind::AtgAlarm), 0);
    }

    #[test]
    fn interpret_cycle_bund_alarm_rising_edge() {
        let i201 = frame(CODE_INVENTORY, &i201_body());
        let i205 = frame(CODE_TANK_STATUS, "0100");
        let i101 = frame(CODE_SYSTEM_STATUS, BUND_ALARM_ENTRY);

        let out = interpret_cycle(&i201, &i205, &i101, ALL_NORMAL_ENTRY, now()).expect("cycle");

        assert!(out.reading.bund_alarm_active());
        assert_eq!(out.alarm_flags, BUND_ALARM_ENTRY);
        assert_eq!(count_events(&out.events, EventKind::AtgAlarm), 1);
        let alarm = out
            .events
            .iter()
            .find(|e| e.kind == EventKind::AtgAlarm)
            .unwrap();
        assert_eq!(alarm.fields["code"], BUND_ALARM_ENTRY);
    }

    #[test]
    fn interpret_cycle_bund_alarm_latched_emits_no_duplicate() {
        let i201 = frame(CODE_INVENTORY, &i201_body());
        let i205 = frame(CODE_TANK_STATUS, "0100");
        let i101 = frame(CODE_SYSTEM_STATUS, BUND_ALARM_ENTRY);

        // Previous cycle already had the bund alarm latched.
        let out = interpret_cycle(&i201, &i205, &i101, BUND_ALARM_ENTRY, now()).expect("cycle");

        assert!(out.reading.bund_alarm_active());
        assert_eq!(count_events(&out.events, EventKind::AtgAlarm), 0);
    }

    #[test]
    fn interpret_cycle_rejects_unexpected_function_code() {
        // i205 frame supplied where the i201 response is expected.
        let wrong = frame(CODE_TANK_STATUS, "0100");
        let i205 = frame(CODE_TANK_STATUS, "0100");
        let i101 = frame(CODE_SYSTEM_STATUS, ALL_NORMAL_ENTRY);

        assert!(matches!(
            interpret_cycle(&wrong, &i205, &i101, ALL_NORMAL_ENTRY, now()),
            Err(AtgError::UnexpectedCode { .. })
        ));
    }

    #[test]
    fn interpret_cycle_propagates_checksum_error() {
        let i201 = frame(CODE_INVENTORY, &i201_body());
        let i205 = frame(CODE_TANK_STATUS, "0100");
        let mut i101 = frame(CODE_SYSTEM_STATUS, ALL_NORMAL_ENTRY);
        let body_pos = 1 + 6 + 10;
        i101[body_pos] ^= 0x01; // corrupt the i101 body

        assert!(matches!(
            interpret_cycle(&i201, &i205, &i101, ALL_NORMAL_ENTRY, now()),
            Err(AtgError::ChecksumMismatch { .. })
        ));
    }
}

// ---------------------------------------------------------------------------
// Contract tests — the FMS client against the real sim ATG console
//
// Proves the FMS parses the exact bytes the sim's `atg_console` emits, so the
// two implementations cannot drift apart again (the divergence this refactor
// corrected). No database or socket-to-FMS: the console runs over TCP loopback
// and its frames are fed straight through the FMS parser, so this runs on any
// host (no Docker). The console reads simulated wall-clock time for the
// response timestamp, hence the isolated `SimClock` + `block_on` pattern rather
// than `#[tokio::test]`.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod contract {
    use std::net::SocketAddr;
    use std::time::Duration;

    use crate::state::EventKind;

    use chrono::{DateTime, Utc};
    use sim::appliances::atg_console::{self, AtgConsoleState};
    use sim::clock::{SimClock, TimeModel};
    use sim::physical::atg_probe::ProbeReading;
    use sim::physical::bund_leak_sensor::BundReading;
    use time::macros::datetime;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};
    use tokio::sync::{mpsc, oneshot, watch};

    use super::{
        build_request, interpret_cycle, parse_response, ALL_NORMAL_ENTRY, BUND_ALARM_ENTRY,
        CODE_INVENTORY, CODE_SYSTEM_STATUS, CODE_TANK_STATUS, ETX,
    };

    // Console geometry: with level 5 000 mm this yields volume 35 000 L,
    // ullage 15 000 L (matches infra/sim atg_console.rs).
    const K_L_PER_MM: f64 = 7.0;
    const TANK_CAPACITY_L: f64 = 50_000.0;
    const TEST_READING: ProbeReading = ProbeReading {
        level_mm: 5_000.0,
        temperature_c: 15.0,
    };

    /// Counterpart channel ends the neighbouring sim entities would hold.
    struct Console {
        probe_rx: mpsc::Receiver<oneshot::Sender<ProbeReading>>,
        bund_tx: watch::Sender<BundReading>,
        _alarm_clear_tx: mpsc::Sender<()>,
        // Held open: the console treats a closed state channel as a fault.
        _state_tx: mpsc::Sender<oneshot::Sender<AtgConsoleState>>,
    }

    fn free_addr() -> SocketAddr {
        std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
    }

    fn spawn_console(clock: SimClock, addr: SocketAddr) -> Console {
        let (probe_tx, probe_rx) = mpsc::channel(16);
        let (bund_tx, bund_rx) = watch::channel(BundReading::default());
        let (alarm_clear_tx, alarm_clear_rx) = mpsc::channel(16);
        let (state_tx, state_rx) = mpsc::channel(1);
        atg_console::spawn(
            clock,
            K_L_PER_MM,
            TANK_CAPACITY_L,
            AtgConsoleState::default(),
            state_rx,
            probe_tx,
            bund_rx,
            alarm_clear_rx,
            addr,
        );
        Console {
            probe_rx,
            bund_tx,
            _alarm_clear_tx: alarm_clear_tx,
            _state_tx: state_tx,
        }
    }

    async fn read_frame(stream: &mut TcpStream) -> Vec<u8> {
        let mut buf = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            stream.read_exact(&mut byte).await.unwrap();
            buf.push(byte[0]);
            if byte[0] == ETX {
                break;
            }
        }
        buf
    }

    /// Service the one probe request an i201 poll triggers.
    async fn service_probe(console: &mut Console) {
        let reply = console.probe_rx.recv().await.unwrap();
        reply.send(TEST_READING).unwrap();
    }

    /// Poll the three FMS codes on one connection (as `poll_once` does),
    /// servicing the probe for i201. Returns the raw response frames.
    async fn poll(stream: &mut TcpStream, console: &mut Console) -> (Vec<u8>, Vec<u8>, Vec<u8>) {
        stream
            .write_all(&build_request(CODE_INVENTORY))
            .await
            .unwrap();
        service_probe(console).await;
        let i201 = read_frame(stream).await;

        stream
            .write_all(&build_request(CODE_TANK_STATUS))
            .await
            .unwrap();
        let i205 = read_frame(stream).await;

        stream
            .write_all(&build_request(CODE_SYSTEM_STATUS))
            .await
            .unwrap();
        let i101 = read_frame(stream).await;

        (i201, i205, i101)
    }

    fn now() -> DateTime<Utc> {
        DateTime::from_timestamp(1_700_000_000, 0).unwrap()
    }

    #[test]
    fn fms_parses_real_console_clean_cycle() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
        runtime.block_on(async {
            let addr = free_addr();
            let mut console = spawn_console(clock.clone(), addr);
            let mut stream = TcpStream::connect(addr).await.unwrap();

            let (i201, i205, i101) = poll(&mut stream, &mut console).await;

            // The FMS agrees with the console on each echoed function code.
            assert_eq!(parse_response(&i201).unwrap().0, CODE_INVENTORY);
            assert_eq!(parse_response(&i205).unwrap().0, CODE_TANK_STATUS);
            assert_eq!(parse_response(&i101).unwrap().0, CODE_SYSTEM_STATUS);

            let out = interpret_cycle(&i201, &i205, &i101, ALL_NORMAL_ENTRY, now())
                .expect("FMS should interpret the real console's cycle");
            assert!((out.reading.volume_l() - 35_000.0).abs() < 1.0);
            assert!((out.reading.ullage_l() - 15_000.0).abs() < 1.0);
            assert!((out.reading.level_mm() - 5_000.0).abs() < 1.0);
            assert!((out.reading.temperature_c() - 15.0).abs() < 1.0);
            assert!(!out.reading.bund_alarm_active());
            assert_eq!(out.reading.alarm_flags(), ALL_NORMAL_ENTRY);
        });
    }

    #[test]
    fn on_demand_read_returns_current_tc_volume() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
        runtime.block_on(async {
            let addr = free_addr();
            let addr_str = addr.to_string();
            let mut console = spawn_console(clock.clone(), addr);

            // The on-demand read issues one i201, which triggers one probe
            // request; service it concurrently with the read.
            let (volume, ()) = tokio::join!(
                super::read_tc_volume_at(&addr_str, super::ON_DEMAND_READ_TIMEOUT),
                service_probe(&mut console),
            );

            // Level 5 000 mm × k 7.0 = 35 000 L; TC volume equals volume at 15 °C.
            let volume = volume.expect("on-demand read should succeed");
            assert!((volume - 35_000.0).abs() < 1.0);
        });
    }

    /// The dispense close records the fuel temperature from the same `i201` read
    /// it takes for the close volume (fms.md §5.2), so the on-demand read must
    /// return both.
    #[test]
    fn on_demand_read_returns_tc_volume_and_temperature() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
        runtime.block_on(async {
            let addr = free_addr();
            let addr_str = addr.to_string();
            let mut console = spawn_console(clock.clone(), addr);

            let (reading, ()) = tokio::join!(
                super::read_tc_reading_at(&addr_str, super::ON_DEMAND_READ_TIMEOUT),
                service_probe(&mut console),
            );

            let reading = reading.expect("on-demand read should succeed");
            assert!((reading.tc_volume_l - 35_000.0).abs() < 1.0);
            // The physical model holds the fuel at 15 °C (sim.md §1.2).
            assert!((reading.temperature_c - 15.0).abs() < 1.0);
        });
    }

    /// Accept a connection without replying, tolerating client disconnection.
    async fn spawn_silent_console() -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            let mut request = [0_u8; 7];
            let _ = stream.read_exact(&mut request).await;
            std::future::pending::<()>().await;
        });
        (addr, server)
    }

    #[tokio::test]
    async fn on_demand_read_times_out_when_console_is_silent() {
        let (addr, server) = spawn_silent_console().await;

        let error = super::read_tc_volume_at(&addr.to_string(), Duration::from_millis(200))
            .await
            .unwrap_err();
        server.abort();

        assert!(error.to_string().contains("timed out"));
    }

    #[tokio::test]
    async fn poll_cycle_times_out_when_console_is_silent() {
        let (addr, server) = spawn_silent_console().await;

        let error = super::poll_network_phase(&addr.to_string(), Duration::from_millis(200))
            .await
            .unwrap_err();
        server.abort();

        assert!(error.to_string().contains("timed out"));
    }

    /// NaN must fail the read: its comparisons would bypass the cavitation checks.
    #[tokio::test]
    async fn on_demand_read_rejects_non_finite_volume() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let Ok((mut stream, _)) = listener.accept().await else {
                return;
            };
            let mut request = [0_u8; 7];
            let _ = stream.read_exact(&mut request).await;
            let nan = f32::NAN.to_bits();
            let mut body = String::from("01D000007");
            for value in [
                nan, nan, nan, 0x45000000, 0x00000000, 0x41700000, 0x00000000,
            ] {
                body.push_str(&format!("{value:08X}"));
            }
            let _ = stream
                .write_all(&super::tests::frame(CODE_INVENTORY, &body))
                .await;
        });

        let error = super::read_tc_volume_at(&addr.to_string(), super::ON_DEMAND_READ_TIMEOUT)
            .await
            .unwrap_err();
        server.abort();

        assert!(error.to_string().contains("non-physical"));
    }

    /// The dispense close records the temperature from the fresh read (fms.md
    /// §5.2), so a non-finite temperature must fail the read, as the periodic
    /// path's `AtgReading::new` refuses it, rather than reach the transaction.
    #[tokio::test]
    async fn on_demand_read_rejects_non_finite_temperature() {
        for temperature in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr = listener.local_addr().unwrap();
            let server = tokio::spawn(async move {
                let Ok((mut stream, _)) = listener.accept().await else {
                    return;
                };
                let mut request = [0_u8; 7];
                let _ = stream.read_exact(&mut request).await;
                // Volume, TC volume and ullage valid (35 000, 35 000, 15 000 L);
                // level 2 048 mm; temperature the value under test.
                let mut body = String::from("01D000007");
                for value in [
                    0x4708B800,
                    0x4708B800,
                    0x466A6000,
                    0x45000000,
                    0x00000000,
                    temperature.to_bits(),
                    0x00000000,
                ] {
                    body.push_str(&format!("{value:08X}"));
                }
                let _ = stream
                    .write_all(&super::tests::frame(CODE_INVENTORY, &body))
                    .await;
            });

            let error = super::read_tc_reading_at(&addr.to_string(), super::ON_DEMAND_READ_TIMEOUT)
                .await
                .unwrap_err();
            server.abort();

            assert!(
                error.to_string().contains("non-physical"),
                "temperature {temperature}: {error}"
            );
        }
    }

    #[test]
    fn fms_parses_real_console_bund_alarm() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
        runtime.block_on(async {
            let addr = free_addr();
            let mut console = spawn_console(clock.clone(), addr);

            // Latch the bund alarm before polling.
            console.bund_tx.send(BundReading { wet: true }).unwrap();
            tokio::task::yield_now().await;

            let mut stream = TcpStream::connect(addr).await.unwrap();
            let (i201, i205, i101) = poll(&mut stream, &mut console).await;

            let out = interpret_cycle(&i201, &i205, &i101, ALL_NORMAL_ENTRY, now())
                .expect("FMS should interpret the real console's cycle");
            assert!(out.reading.bund_alarm_active());
            assert_eq!(out.reading.alarm_flags(), BUND_ALARM_ENTRY);
            assert_eq!(
                out.events
                    .iter()
                    .filter(|e| e.kind == EventKind::AtgAlarm)
                    .count(),
                1
            );
        });
    }
}
