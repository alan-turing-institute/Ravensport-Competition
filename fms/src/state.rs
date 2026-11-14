use chrono::{DateTime, Utc};
use serde::Serialize;
use sqlx::PgPool;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use uuid::Uuid;

use crate::config::Config;

// ---------------------------------------------------------------------------
// Log server event (sent to the background pusher task)
// ---------------------------------------------------------------------------

/// The classes of event the FMS pushes to the log server (fms.md §5.4). The
/// pusher selects a target by matching on this exhaustively, so a new class
/// does not compile until it has been given one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventKind {
    AtgReading,
    AtgAlarm,
    JobOpened,
    TransactionOpen,
    TransactionClosed,
    BdnLinked,
    /// An active job abandoned, recorded in `job_cancellations` (fms.md §7.4,
    /// §8.5).
    JobCancelled,
    Login,
    LoginFailed,
    CardChange,
}

impl EventKind {
    /// Wire name, per `hist.md` §4.2. Also what identifies an event in the
    /// drop warnings, which is the only record of a loss (§5.4).
    pub fn name(self) -> &'static str {
        match self {
            EventKind::AtgReading => "atg_reading",
            EventKind::AtgAlarm => "atg_alarm",
            EventKind::JobOpened => "job_opened",
            EventKind::TransactionOpen => "transaction_open",
            EventKind::TransactionClosed => "transaction_closed",
            EventKind::BdnLinked => "bdn_linked",
            EventKind::JobCancelled => "job_cancelled",
            EventKind::Login => "login",
            EventKind::LoginFailed => "login_failed",
            EventKind::CardChange => "card_change",
        }
    }

    /// `_msg` — the event in words. VictoriaLogs requires a message field and
    /// substitutes placeholder text of its own when one is absent, which would
    /// then be what an administrator or the reconciliation user reads in
    /// Grafana (`hist.md` §4.3).
    pub fn msg(self) -> &'static str {
        match self {
            EventKind::AtgReading => "atg reading",
            EventKind::AtgAlarm => "atg alarm",
            EventKind::JobOpened => "job opened",
            EventKind::TransactionOpen => "transaction opened",
            EventKind::TransactionClosed => "transaction closed",
            EventKind::BdnLinked => "bunker delivery note linked",
            EventKind::JobCancelled => "job cancelled",
            EventKind::Login => "login",
            EventKind::LoginFailed => "login failed",
            EventKind::CardChange => "card change",
        }
    }
}

/// One record bound for the log server.
///
/// `fields` is flat and already carries the names `hist.md` §4 specifies: the
/// pusher writes them at the top level of the emitted record rather than nested
/// under a payload key, because §4.2's selectors address them there.
#[derive(Debug, Clone)]
pub struct LogEvent {
    pub kind: EventKind,
    /// Becomes `_time` on the emitted record.
    pub time: DateTime<Utc>,
    pub fields: serde_json::Map<String, serde_json::Value>,
}

impl LogEvent {
    /// Build an event from a `json!({ … })` object literal.
    ///
    /// Panics if handed anything but a JSON object. That is a mistake in the
    /// literal at the call site, fixed once at compile-adjacent test time, not
    /// a condition that can arise from data.
    pub fn new(kind: EventKind, time: DateTime<Utc>, fields: serde_json::Value) -> Self {
        match fields {
            serde_json::Value::Object(map) => Self {
                kind,
                time,
                fields: map,
            },
            other => panic!("LogEvent fields must be a JSON object, got {other}"),
        }
    }

    /// Build a bunkering event: the job's envelope plus the event's own fields.
    ///
    /// Every bunkering event is built here rather than with [`LogEvent::new`],
    /// so the envelope cannot drift between emission sites — the failure that
    /// left the fill close without `counterparty` and `grade`.
    ///
    /// Panics if `kind` is not a bunkering event, or if `fields` repeats an
    /// envelope field: both are mistakes at the call site, not conditions that
    /// can arise from data.
    pub fn bunkering(
        kind: EventKind,
        time: DateTime<Utc>,
        envelope: &Envelope,
        fields: serde_json::Value,
    ) -> Self {
        assert!(
            matches!(
                kind,
                EventKind::JobOpened
                    | EventKind::TransactionOpen
                    | EventKind::TransactionClosed
                    | EventKind::BdnLinked
                    | EventKind::JobCancelled
            ),
            "{kind:?} is not a bunkering event"
        );
        let mut event = Self::new(kind, time, fields);
        let serde_json::Value::Object(envelope_fields) = envelope.to_json() else {
            unreachable!("Envelope::to_json builds an object");
        };
        for (key, value) in envelope_fields {
            assert!(
                !event.fields.contains_key(&key),
                "{kind:?}: `{key}` is an envelope field; pass it through the Envelope"
            );
            event.fields.insert(key, value);
        }
        event
    }
}

/// The fields every bunkering event carries (hist.md §4.2), so that any one
/// event can be attributed to its job, operator and authorising session without
/// a second lookup. The values describe the job, not the request that triggered
/// the event: a BDN entered under a later session still carries the session the
/// job was created under.
#[derive(Debug, Clone, PartialEq)]
pub struct Envelope {
    pub job_id: Uuid,
    pub job_type: JobType,
    pub operator: String,
    pub session_id: Uuid,
    pub counterparty: String,
    pub grade: String,
}

impl Envelope {
    pub fn from_job(job: &Job) -> Self {
        Self {
            job_id: job.job_id(),
            job_type: job.job_type(),
            operator: job.operator().to_owned(),
            session_id: job.session_id(),
            counterparty: job.counterparty().to_owned(),
            grade: job.grade().to_owned(),
        }
    }

    fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "job_id": self.job_id,
            "type": self.job_type,
            "operator": self.operator,
            "session_id": self.session_id,
            "counterparty": self.counterparty,
            "grade": self.grade,
        })
    }
}

// ---------------------------------------------------------------------------
// In-memory session
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct Session {
    pub session_id: Uuid,
    pub username: String,
    pub role: String,
    pub expires_at: DateTime<Utc>,
}

// ---------------------------------------------------------------------------
// Job and its mutable state
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum JobType {
    Dispense,
    Fill,
}

#[derive(Debug, Clone, Copy, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum JobStatus {
    Active,
    Running,
    /// Dispense metering is complete and the transaction is recorded, but the
    /// job is not yet closed: it awaits the operator entering the BDN number
    /// (narrative §279 — the paper document is recorded before the job closes).
    #[serde(rename = "awaiting_bdn")]
    AwaitingBdn,
    Closed,
    Cancelled,
}

#[derive(Debug, Clone)]
pub struct Job {
    job_id: Uuid,
    job_type: JobType,
    counterparty: String,
    grade: String,
    operator: String,
    /// The authenticated session the job was created under (fms.md §8.3). Held
    /// here so every event of the job carries it in its envelope (hist.md §4.2)
    /// without a database read — including the dispense close, which the Modbus
    /// task writes with no request and so no session of its own.
    session_id: Uuid,
    status: JobStatus,
    rfid_validated: bool,
    /// ATG level recorded when the job was created.
    atg_volume_at_job_open: Option<f64>,
    /// ATG level recorded when the dispense was started (set by start_dispense).
    atg_volume_at_tx_open: Option<f64>,
    /// When the dispense started (set by start_dispense, with the instant
    /// `transaction_open` publishes). The dispense close records it as
    /// `opened_at` — the transfer's start, not the job's creation (fms.md §8.4).
    tx_opened_at: Option<DateTime<Utc>>,
    /// Authorisation UUID generated at start_dispense time.
    authorisation_id: Option<Uuid>,
    preset_volume_l: Option<f64>,
    /// Cumulative totaliser reading at the moment the pump was enabled.
    /// 32-bit unsigned pulse count widened to `i64` for the Postgres BIGINT column.
    start_totaliser_pulses: Option<i64>,
    /// Running dispensed volume in litres, updated by the Modbus poller each cycle.
    current_dispensed_l: Option<f64>,
    created_at: DateTime<Utc>,
}

// ---------------------------------------------------------------------------
// Job construction and lifecycle
// ---------------------------------------------------------------------------

/// Largest preset volume the dispenser's 16-bit "unsigned div10" register can
/// represent (dispenser-modbus-tcp-spec.md §7): 65535 ÷ 10 = 6553.5 L.
pub const DISPENSE_PRESET_MAX_L: f64 = 6553.5;

/// Whether a job in `status` blocks a new job from the single job slot: one
/// active, running or awaiting its BDN (fms.md §7.4).
fn blocks_new_job(status: JobStatus) -> bool {
    matches!(
        status,
        JobStatus::Active | JobStatus::Running | JobStatus::AwaitingBdn
    )
}

/// The volume the dispenser's preset register holds for a requested preset
/// (fms.md §7.4): the request rounded to the register's 0.1 L resolution. It is
/// the volume the FMS checks against the cavitation threshold, records and
/// writes. The request must be finite, at most 6553.5 L, and large enough to
/// round to a non-zero register value (at least 0.05 L).
pub fn register_preset_l(requested: f64) -> Result<f64, JobValidationError> {
    if !requested.is_finite() {
        return Err(JobValidationError::NonFiniteValue("preset_volume_l"));
    }
    if requested <= 0.0 {
        return Err(JobValidationError::NonPositivePreset(requested));
    }
    if requested > DISPENSE_PRESET_MAX_L {
        return Err(JobValidationError::PresetExceedsRegisterLimit(requested));
    }
    let tenths = (requested * 10.0).round();
    if tenths < 1.0 {
        return Err(JobValidationError::PresetBelowRegisterResolution(requested));
    }
    Ok(tenths / 10.0)
}

#[derive(Debug, PartialEq)]
pub enum JobValidationError {
    EmptyField(&'static str),
    NonFiniteValue(&'static str),
    NegativeVolume { field: &'static str, value: f64 },
    NonPositivePreset(f64),
    PresetExceedsRegisterLimit(f64),
    PresetBelowRegisterResolution(f64),
    NegativeTotaliserPulses(i64),
    TotaliserExceedsU32(i64),
}

impl std::fmt::Display for JobValidationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyField(name) => write!(f, "{name} must not be empty"),
            Self::NonFiniteValue(name) => write!(f, "{name} must be a finite number"),
            Self::NegativeVolume { field, value } => {
                write!(f, "{field} must not be negative (got {value})")
            }
            Self::NonPositivePreset(v) => {
                write!(f, "preset volume must be greater than 0 (got {v})")
            }
            Self::PresetExceedsRegisterLimit(v) => write!(
                f,
                "preset volume {v} L exceeds the dispenser register limit ({DISPENSE_PRESET_MAX_L} L)"
            ),
            Self::PresetBelowRegisterResolution(v) => write!(
                f,
                "preset volume {v} L rounds to zero at the dispenser register's 0.1 L resolution"
            ),
            Self::NegativeTotaliserPulses(v) => {
                write!(f, "totaliser pulse count must not be negative (got {v})")
            }
            Self::TotaliserExceedsU32(v) => write!(
                f,
                "totaliser pulse count {v} exceeds the 32-bit register maximum ({})",
                u32::MAX
            ),
        }
    }
}

impl Job {
    /// Checks the fields a job is created with, other than the ATG volume: none
    /// may be empty. `new` applies the same checks, so a request can be checked
    /// before its ATG reading is taken.
    pub fn validate_fields(
        counterparty: &str,
        grade: &str,
        operator: &str,
    ) -> Result<(), JobValidationError> {
        if counterparty.is_empty() {
            return Err(JobValidationError::EmptyField("counterparty"));
        }
        if grade.is_empty() {
            return Err(JobValidationError::EmptyField("grade"));
        }
        if operator.is_empty() {
            return Err(JobValidationError::EmptyField("operator"));
        }
        Ok(())
    }

    pub fn new(
        job_type: JobType,
        counterparty: String,
        grade: String,
        operator: String,
        session_id: Uuid,
        atg_volume_at_job_open: Option<f64>,
    ) -> Result<Self, JobValidationError> {
        Self::validate_fields(&counterparty, &grade, &operator)?;
        if let Some(v) = atg_volume_at_job_open {
            if !v.is_finite() {
                return Err(JobValidationError::NonFiniteValue("atg_volume_at_job_open"));
            }
            if v < 0.0 {
                return Err(JobValidationError::NegativeVolume {
                    field: "atg_volume_at_job_open",
                    value: v,
                });
            }
        }
        Ok(Self {
            job_id: Uuid::new_v4(),
            job_type,
            counterparty,
            grade,
            operator,
            session_id,
            status: JobStatus::Active,
            rfid_validated: false,
            atg_volume_at_job_open,
            atg_volume_at_tx_open: None,
            tx_opened_at: None,
            authorisation_id: None,
            preset_volume_l: None,
            start_totaliser_pulses: None,
            current_dispensed_l: None,
            created_at: Utc::now(),
        })
    }

    /// Checks a preset volume for a job of `job_type`: finite and positive. For
    /// dispense jobs the value must also fit the dispenser's 16-bit "unsigned
    /// div10" register (≤ 6553.5 L). For fill jobs the volume is transcribed
    /// from the tanker's meter and is not written to a Modbus register, so no
    /// register-size limit applies.
    pub fn validate_preset_volume_l(
        job_type: JobType,
        volume: f64,
    ) -> Result<(), JobValidationError> {
        if !volume.is_finite() {
            return Err(JobValidationError::NonFiniteValue("preset_volume_l"));
        }
        if volume <= 0.0 {
            return Err(JobValidationError::NonPositivePreset(volume));
        }
        if job_type == JobType::Dispense && volume > DISPENSE_PRESET_MAX_L {
            return Err(JobValidationError::PresetExceedsRegisterLimit(volume));
        }
        Ok(())
    }

    /// Sets the preset volume, after the checks of `validate_preset_volume_l`.
    pub fn set_preset_volume_l(&mut self, volume: f64) -> Result<(), JobValidationError> {
        Self::validate_preset_volume_l(self.job_type, volume)?;
        self.preset_volume_l = Some(volume);
        Ok(())
    }

    pub fn set_atg_volume_at_tx_open(&mut self, volume: f64) -> Result<(), JobValidationError> {
        if !volume.is_finite() {
            return Err(JobValidationError::NonFiniteValue("atg_volume_at_tx_open"));
        }
        if volume < 0.0 {
            return Err(JobValidationError::NegativeVolume {
                field: "atg_volume_at_tx_open",
                value: volume,
            });
        }
        self.atg_volume_at_tx_open = Some(volume);
        Ok(())
    }

    /// Records the cumulative totaliser reading at pump-enable time. The
    /// dispenser exposes a 32-bit pulse count as a register pair, so the value
    /// must be non-negative and representable as a 32-bit unsigned integer.
    pub fn set_start_totaliser_pulses(&mut self, pulses: i64) -> Result<(), JobValidationError> {
        if pulses < 0 {
            return Err(JobValidationError::NegativeTotaliserPulses(pulses));
        }
        if pulses > i64::from(u32::MAX) {
            return Err(JobValidationError::TotaliserExceedsU32(pulses));
        }
        self.start_totaliser_pulses = Some(pulses);
        Ok(())
    }

    /// Check metering before changing the job or persisting a transaction.
    pub(crate) fn validate_dispensed_volume(volume: f64) -> Result<(), JobValidationError> {
        if !volume.is_finite() {
            return Err(JobValidationError::NonFiniteValue("current_dispensed_l"));
        }
        if volume < 0.0 {
            return Err(JobValidationError::NegativeVolume {
                field: "current_dispensed_l",
                value: volume,
            });
        }
        Ok(())
    }

    pub fn update_current_dispensed_l(&mut self, volume: f64) -> Result<(), JobValidationError> {
        Self::validate_dispensed_volume(volume)?;
        self.current_dispensed_l = Some(volume);
        Ok(())
    }

    // --- Getters ---

    pub fn job_id(&self) -> Uuid {
        self.job_id
    }
    pub fn job_type(&self) -> JobType {
        self.job_type
    }
    pub fn counterparty(&self) -> &str {
        &self.counterparty
    }
    pub fn grade(&self) -> &str {
        &self.grade
    }
    pub fn operator(&self) -> &str {
        &self.operator
    }
    pub fn session_id(&self) -> Uuid {
        self.session_id
    }
    pub fn status(&self) -> JobStatus {
        self.status
    }
    pub fn rfid_validated(&self) -> bool {
        self.rfid_validated
    }
    pub fn atg_volume_at_job_open(&self) -> Option<f64> {
        self.atg_volume_at_job_open
    }
    pub fn atg_volume_at_tx_open(&self) -> Option<f64> {
        self.atg_volume_at_tx_open
    }
    pub fn set_tx_opened_at(&mut self, at: DateTime<Utc>) {
        self.tx_opened_at = Some(at);
    }
    pub fn tx_opened_at(&self) -> Option<DateTime<Utc>> {
        self.tx_opened_at
    }
    pub fn authorisation_id(&self) -> Option<Uuid> {
        self.authorisation_id
    }
    pub fn preset_volume_l(&self) -> Option<f64> {
        self.preset_volume_l
    }
    pub fn start_totaliser_pulses(&self) -> Option<i64> {
        self.start_totaliser_pulses
    }
    pub fn current_dispensed_l(&self) -> Option<f64> {
        self.current_dispensed_l
    }
    pub fn created_at(&self) -> DateTime<Utc> {
        self.created_at
    }

    // --- Lifecycle setters (no physical quantity guards) ---

    pub fn set_status(&mut self, status: JobStatus) {
        self.status = status;
    }
    pub fn set_rfid_validated(&mut self, validated: bool) {
        self.rfid_validated = validated;
    }
    pub fn set_authorisation_id(&mut self, id: Uuid) {
        self.authorisation_id = Some(id);
    }
}

// ---------------------------------------------------------------------------
// Last ATG reading (shared between ATG poller and API handlers)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct AtgReading {
    volume_l: f64,
    ullage_l: f64,
    level_mm: f64,
    temperature_c: f64,
    /// Raw i101 System Status Report body for this cycle (`000000` when no
    /// alarm is active, `030301` when the bund liquid-sensor alarm is latched).
    alarm_flags: String,
    /// Whether the bund liquid-sensor fuel alarm (i101 entry `030301`) is
    /// latched as of this reading.
    bund_alarm_active: bool,
    recorded_at: DateTime<Utc>,
}

// ---------------------------------------------------------------------------
// ATG reading construction
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq)]
pub enum AtgReadingValidationError {
    NonFiniteValue(&'static str),
    NegativeValue {
        field: &'static str,
        value: f64,
    },
    /// `alarm_flags` carries the raw i101 System Status Report body: a
    /// non-empty string of concatenated six-character `AANNTT` entries
    /// (tls-protocol.md §6.1).
    InvalidAlarmFlags(String),
}

impl std::fmt::Display for AtgReadingValidationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NonFiniteValue(name) => write!(f, "{name} must be a finite number"),
            Self::NegativeValue { field, value } => {
                write!(f, "{field} must not be negative (got {value})")
            }
            Self::InvalidAlarmFlags(flags) => write!(
                f,
                "alarm_flags must be a non-empty multiple of six decimal digits \
                 (i101 AANNTT entries) (got {flags:?})"
            ),
        }
    }
}

impl AtgReading {
    /// Construct an ATG reading, validating all physical quantities.
    ///
    /// `alarm_flags` is the raw i101 System Status Report body: a non-empty
    /// string of concatenated six-character `AANNTT` entries, each digit `0`–`9`
    /// (tls-protocol.md §6.1). `bund_alarm_active` records whether the bund
    /// liquid-sensor fuel alarm (entry `030301`) is latched. `recorded_at` is
    /// accepted as-is.
    pub fn new(
        volume_l: f64,
        ullage_l: f64,
        level_mm: f64,
        temperature_c: f64,
        alarm_flags: String,
        bund_alarm_active: bool,
        recorded_at: DateTime<Utc>,
    ) -> Result<Self, AtgReadingValidationError> {
        if !volume_l.is_finite() {
            return Err(AtgReadingValidationError::NonFiniteValue("volume_l"));
        }
        if volume_l < 0.0 {
            return Err(AtgReadingValidationError::NegativeValue {
                field: "volume_l",
                value: volume_l,
            });
        }
        if !ullage_l.is_finite() {
            return Err(AtgReadingValidationError::NonFiniteValue("ullage_l"));
        }
        if ullage_l < 0.0 {
            return Err(AtgReadingValidationError::NegativeValue {
                field: "ullage_l",
                value: ullage_l,
            });
        }
        if !level_mm.is_finite() {
            return Err(AtgReadingValidationError::NonFiniteValue("level_mm"));
        }
        if level_mm < 0.0 {
            return Err(AtgReadingValidationError::NegativeValue {
                field: "level_mm",
                value: level_mm,
            });
        }
        if !temperature_c.is_finite() {
            return Err(AtgReadingValidationError::NonFiniteValue("temperature_c"));
        }
        if alarm_flags.is_empty()
            || !alarm_flags.len().is_multiple_of(6)
            || !alarm_flags.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(AtgReadingValidationError::InvalidAlarmFlags(alarm_flags));
        }
        Ok(Self {
            volume_l,
            ullage_l,
            level_mm,
            temperature_c,
            alarm_flags,
            bund_alarm_active,
            recorded_at,
        })
    }

    pub fn volume_l(&self) -> f64 {
        self.volume_l
    }
    pub fn ullage_l(&self) -> f64 {
        self.ullage_l
    }
    pub fn level_mm(&self) -> f64 {
        self.level_mm
    }
    pub fn temperature_c(&self) -> f64 {
        self.temperature_c
    }
    pub fn alarm_flags(&self) -> &str {
        &self.alarm_flags
    }
    pub fn bund_alarm_active(&self) -> bool {
        self.bund_alarm_active
    }
    pub fn recorded_at(&self) -> DateTime<Utc> {
        self.recorded_at
    }
}

// ---------------------------------------------------------------------------
// Shared application state
// ---------------------------------------------------------------------------

#[derive(Clone)]
pub struct AppState {
    pub db: PgPool,
    pub config: Arc<Config>,
    pub sessions: Arc<RwLock<HashMap<String, Session>>>,
    pub current_job: Arc<RwLock<Option<Job>>>,
    pub last_atg: Arc<RwLock<Option<AtgReading>>>,
    pub log_queues: crate::log_queue::LogQueues,
}

impl AppState {
    pub fn new(db: PgPool, config: Arc<Config>, log_queues: crate::log_queue::LogQueues) -> Self {
        Self {
            db,
            config,
            sessions: Arc::new(RwLock::new(HashMap::new())),
            current_job: Arc::new(RwLock::new(None)),
            last_atg: Arc::new(RwLock::new(None)),
            log_queues,
        }
    }

    /// Enqueue a log event for its class's pusher task without ever blocking the
    /// caller.
    ///
    /// The ATG poller, the Modbus poller and the job handlers must never stall on
    /// telemetry, and the authoritative record is the durable ledger, not this
    /// stream (fms.md §5.4). If the class's queue is full the **oldest** event in
    /// it is discarded to make room, so a sustained outage loses the start of the
    /// gap and keeps the records nearest the present. The discard is recorded at
    /// WARN because it is the only trace the lost event leaves.
    pub fn emit_log_event(&self, event: LogEvent) {
        let queue = self.log_queues.for_class(event.kind.class());
        if let Some(discarded) = queue.push(event) {
            tracing::warn!(
                event_type = discarded.kind.name(),
                "log queue full; discarded the oldest queued event"
            );
        }
    }

    /// Atomically claim the current-job slot for `job`.
    ///
    /// Returns `true` if the slot was free (no job active, running, or awaiting BDN) and
    /// `job` was installed, or `false` if a job is already active or running.
    /// The check and the set are performed under a single write lock so that
    /// two concurrent creates cannot both observe an empty slot and race.
    pub async fn try_set_current_job(&self, job: Job) -> bool {
        let mut guard = self.current_job.write().await;
        if guard.as_ref().is_some_and(|j| blocks_new_job(j.status)) {
            return false;
        }
        *guard = Some(job);
        true
    }

    /// Whether the slot holds a job in progress: active, running or awaiting
    /// its BDN. A closed or cancelled job stays in the slot without blocking a
    /// new one. A create checks this before any ATG read (digital.md §4.1,
    /// step 2); `try_set_current_job` still decides the claim.
    pub async fn job_in_progress(&self) -> bool {
        self.current_job
            .read()
            .await
            .as_ref()
            .is_some_and(|j| blocks_new_job(j.status))
    }

    /// Clear the current-job slot if it still holds `job_id`.
    ///
    /// Used to roll back an in-memory reservation when the durable insert that
    /// follows it fails, so a failed create does not strand the slot.
    pub async fn clear_current_job(&self, job_id: Uuid) {
        let mut guard = self.current_job.write().await;
        if guard.as_ref().map(|j| j.job_id) == Some(job_id) {
            *guard = None;
        }
    }

    /// Return the session for a bearer token, evicting it if expired.
    pub async fn get_session(&self, token: &str) -> Option<Session> {
        let mut sessions = self.sessions.write().await;
        match sessions.get(token) {
            Some(s) if s.expires_at > Utc::now() => Some(s.clone()),
            Some(_) => {
                sessions.remove(token);
                None
            }
            None => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration as ChronoDuration;
    use std::sync::Arc;
    use std::time::Duration;

    fn make_state() -> AppState {
        let pool = sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgres://localhost/fms_test")
            .expect("lazy pool must not fail");
        let config = Arc::new(crate::config::Config {
            database_url: String::new(),
            listen_port: 443,
            tls_cert_path: String::new(),
            tls_key_path: String::new(),
            admin_username: String::new(),
            admin_password_hash: String::new(),
            ldap_uri: String::new(),
            ldap_base_dn: String::new(),
            seed_cards: Vec::new(),
            atg_host: String::new(),
            atg_port: 10001,
            atg_poll_interval: Duration::from_secs(30),
            dispenser_host: String::new(),
            dispenser_port: 502,
            modbus_poll_interval: Duration::from_secs(5),
            vessel_rfid_host: String::new(),
            vessel_rfid_tcp_port: 9001,
            vmagent_url: String::new(),
            vlagent_jsonline_url: String::new(),
            vlagent_syslog_host: String::new(),
            vlagent_syslog_port: 514,
            session_timeout: Duration::from_secs(1800),
            tank_capacity_l: 50_000.0,
            cavitation_threshold_pct: 10.0,
            k_factor: 100.0,
            grade: "MGO".to_owned(),
            density_kg_per_l: 0.840,
        });
        AppState::new(
            pool,
            config,
            crate::log_queue::LogQueues::new(crate::log_queue::QUEUE_DEPTH),
        )
    }

    fn make_job(status: JobStatus) -> Job {
        Job {
            job_id: Uuid::new_v4(),
            job_type: JobType::Dispense,
            counterparty: "MV Harrier".to_owned(),
            grade: "MGO".to_owned(),
            operator: "test-operator".to_owned(),
            session_id: Uuid::nil(),
            status,
            rfid_validated: false,
            atg_volume_at_job_open: None,
            atg_volume_at_tx_open: None,
            tx_opened_at: None,
            authorisation_id: None,
            preset_volume_l: None,
            start_totaliser_pulses: None,
            current_dispensed_l: None,
            created_at: Utc::now(),
        }
    }

    fn insert_session(state: &AppState, token: &str, role: &str, expires_in: ChronoDuration) {
        let session = Session {
            session_id: Uuid::new_v4(),
            username: "testuser".to_owned(),
            role: role.to_owned(),
            expires_at: Utc::now() + expires_in,
        };
        state
            .sessions
            .try_write()
            .expect("lock must not be contested in tests")
            .insert(token.to_owned(), session);
    }

    #[tokio::test]
    async fn get_session_returns_valid_session() {
        let state = make_state();
        insert_session(&state, "tok1", "admin", ChronoDuration::seconds(60));
        let result = state.get_session("tok1").await;
        assert!(result.is_some());
        assert_eq!(result.unwrap().role, "admin");
    }

    #[tokio::test]
    async fn get_session_evicts_and_returns_none_for_expired() {
        let state = make_state();
        insert_session(&state, "tok2", "operator", ChronoDuration::seconds(-1));
        let result = state.get_session("tok2").await;
        assert!(result.is_none(), "expired session must return None");
        // Token must have been evicted from the map.
        assert!(
            !state.sessions.read().await.contains_key("tok2"),
            "expired token must be removed from session map"
        );
    }

    #[tokio::test]
    async fn get_session_returns_none_for_unknown_token() {
        let state = make_state();
        assert!(state.get_session("no-such-token").await.is_none());
    }

    fn make_dispense_job() -> Job {
        Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            Some(30_000.0),
        )
        .expect("valid inputs must succeed")
    }

    // --- LogEvent::bunkering envelope (hist.md §4.2) ---

    fn job_with_session(session_id: Uuid) -> Job {
        Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            session_id,
            None,
        )
        .expect("valid inputs must succeed")
    }

    /// The dispense close is emitted by the Modbus task, which the API tests do
    /// not drive; this is its coverage at unit level, through the one builder
    /// every bunkering event goes through.
    #[test]
    fn bunkering_event_carries_the_six_envelope_fields_and_its_own() {
        let session = Uuid::from_u128(7);
        let job = job_with_session(session);
        let event = LogEvent::bunkering(
            EventKind::TransactionClosed,
            Utc::now(),
            &Envelope::from_job(&job),
            serde_json::json!({ "volume_dispensed_l": 500.0 }),
        );
        let expected = [
            ("job_id", serde_json::json!(job.job_id())),
            ("type", serde_json::json!("dispense")),
            ("operator", serde_json::json!("ops-1")),
            ("session_id", serde_json::json!(session)),
            ("counterparty", serde_json::json!("TUG-01")),
            ("grade", serde_json::json!("MGO")),
            ("volume_dispensed_l", serde_json::json!(500.0)),
        ];
        for (field, value) in &expected {
            assert_eq!(event.fields.get(*field), Some(value), "field `{field}`");
        }
        assert_eq!(event.fields.len(), expected.len(), "no fields beyond these");
    }

    #[test]
    #[should_panic(expected = "is an envelope field")]
    fn bunkering_refuses_an_envelope_field_among_the_event_fields() {
        let job = job_with_session(Uuid::from_u128(7));
        LogEvent::bunkering(
            EventKind::JobOpened,
            Utc::now(),
            &Envelope::from_job(&job),
            serde_json::json!({ "operator": "someone-else" }),
        );
    }

    #[test]
    #[should_panic(expected = "is not a bunkering event")]
    fn bunkering_refuses_a_non_bunkering_kind() {
        let job = job_with_session(Uuid::from_u128(7));
        LogEvent::bunkering(
            EventKind::Login,
            Utc::now(),
            &Envelope::from_job(&job),
            serde_json::json!({}),
        );
    }

    // --- Job::new() constructor guards ---

    #[test]
    fn new_job_rejects_empty_counterparty() {
        let err = Job::new(
            JobType::Dispense,
            String::new(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            None,
        )
        .unwrap_err();
        assert_eq!(err, JobValidationError::EmptyField("counterparty"));
    }

    #[test]
    fn new_job_rejects_empty_grade() {
        let err = Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            String::new(),
            "ops-1".to_owned(),
            Uuid::nil(),
            None,
        )
        .unwrap_err();
        assert_eq!(err, JobValidationError::EmptyField("grade"));
    }

    #[test]
    fn new_job_rejects_empty_operator() {
        let err = Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            String::new(),
            Uuid::nil(),
            None,
        )
        .unwrap_err();
        assert_eq!(err, JobValidationError::EmptyField("operator"));
    }

    #[test]
    fn new_job_rejects_non_finite_atg_volume() {
        let err = Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            Some(f64::NAN),
        )
        .unwrap_err();
        assert_eq!(
            err,
            JobValidationError::NonFiniteValue("atg_volume_at_job_open")
        );
    }

    #[test]
    fn new_job_rejects_negative_atg_volume() {
        let err = Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            Some(-1.0),
        )
        .unwrap_err();
        assert_eq!(
            err,
            JobValidationError::NegativeVolume {
                field: "atg_volume_at_job_open",
                value: -1.0
            }
        );
    }

    #[test]
    fn new_job_accepts_none_atg_volume() {
        assert!(Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            None
        )
        .is_ok());
    }

    #[test]
    fn new_job_accepts_zero_atg_volume() {
        assert!(Job::new(
            JobType::Dispense,
            "TUG-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            Some(0.0)
        )
        .is_ok());
    }

    #[test]
    fn new_job_starts_with_expected_defaults() {
        let job = make_dispense_job();
        assert_eq!(job.status, JobStatus::Active);
        assert!(!job.rfid_validated);
        assert!(job.atg_volume_at_tx_open.is_none());
        assert!(job.authorisation_id.is_none());
        assert!(job.preset_volume_l.is_none());
        assert!(job.start_totaliser_pulses.is_none());
        assert!(job.current_dispensed_l.is_none());
    }

    // --- set_preset_volume_l ---

    #[test]
    fn set_preset_rejects_nan() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_preset_volume_l(f64::NAN).unwrap_err(),
            JobValidationError::NonFiniteValue("preset_volume_l")
        );
    }

    #[test]
    fn set_preset_rejects_infinity() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_preset_volume_l(f64::INFINITY).unwrap_err(),
            JobValidationError::NonFiniteValue("preset_volume_l")
        );
    }

    #[test]
    fn set_preset_rejects_zero() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_preset_volume_l(0.0).unwrap_err(),
            JobValidationError::NonPositivePreset(0.0)
        );
    }

    #[test]
    fn set_preset_rejects_negative() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_preset_volume_l(-100.0).unwrap_err(),
            JobValidationError::NonPositivePreset(-100.0)
        );
    }

    #[test]
    fn set_preset_dispense_rejects_above_register_limit() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_preset_volume_l(6553.6).unwrap_err(),
            JobValidationError::PresetExceedsRegisterLimit(6553.6)
        );
    }

    #[test]
    fn set_preset_dispense_accepts_register_limit() {
        let mut job = make_dispense_job();
        assert!(job.set_preset_volume_l(DISPENSE_PRESET_MAX_L).is_ok());
        assert_eq!(job.preset_volume_l, Some(DISPENSE_PRESET_MAX_L));
    }

    #[test]
    fn set_preset_fill_accepts_above_register_limit() {
        let mut job = Job::new(
            JobType::Fill,
            "TANKER-01".to_owned(),
            "MGO".to_owned(),
            "ops-1".to_owned(),
            Uuid::nil(),
            None,
        )
        .unwrap();
        assert!(job.set_preset_volume_l(20_000.0).is_ok());
        assert_eq!(job.preset_volume_l, Some(20_000.0));
    }

    // --- set_atg_volume_at_tx_open ---

    #[test]
    fn set_atg_tx_open_rejects_non_finite() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_atg_volume_at_tx_open(f64::NAN).unwrap_err(),
            JobValidationError::NonFiniteValue("atg_volume_at_tx_open")
        );
    }

    #[test]
    fn set_atg_tx_open_rejects_negative() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_atg_volume_at_tx_open(-0.1).unwrap_err(),
            JobValidationError::NegativeVolume {
                field: "atg_volume_at_tx_open",
                value: -0.1,
            }
        );
    }

    #[test]
    fn set_atg_tx_open_accepts_zero() {
        let mut job = make_dispense_job();
        assert!(job.set_atg_volume_at_tx_open(0.0).is_ok());
        assert_eq!(job.atg_volume_at_tx_open, Some(0.0));
    }

    // --- set_start_totaliser_pulses ---

    #[test]
    fn set_start_totaliser_rejects_negative() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.set_start_totaliser_pulses(-1).unwrap_err(),
            JobValidationError::NegativeTotaliserPulses(-1)
        );
    }

    #[test]
    fn set_start_totaliser_rejects_above_u32_max() {
        let mut job = make_dispense_job();
        let too_large = i64::from(u32::MAX) + 1;
        assert_eq!(
            job.set_start_totaliser_pulses(too_large).unwrap_err(),
            JobValidationError::TotaliserExceedsU32(too_large)
        );
    }

    #[test]
    fn set_start_totaliser_accepts_u32_max() {
        let mut job = make_dispense_job();
        let max = i64::from(u32::MAX);
        assert!(job.set_start_totaliser_pulses(max).is_ok());
        assert_eq!(job.start_totaliser_pulses, Some(max));
    }

    #[test]
    fn set_start_totaliser_accepts_zero() {
        let mut job = make_dispense_job();
        assert!(job.set_start_totaliser_pulses(0).is_ok());
        assert_eq!(job.start_totaliser_pulses, Some(0));
    }

    // --- update_current_dispensed_l ---

    #[test]
    fn update_dispensed_rejects_non_finite() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.update_current_dispensed_l(f64::NAN).unwrap_err(),
            JobValidationError::NonFiniteValue("current_dispensed_l")
        );
    }

    #[test]
    fn update_dispensed_rejects_negative() {
        let mut job = make_dispense_job();
        assert_eq!(
            job.update_current_dispensed_l(-0.5).unwrap_err(),
            JobValidationError::NegativeVolume {
                field: "current_dispensed_l",
                value: -0.5,
            }
        );
    }

    #[test]
    fn update_dispensed_accepts_zero() {
        let mut job = make_dispense_job();
        assert!(job.update_current_dispensed_l(0.0).is_ok());
        assert_eq!(job.current_dispensed_l, Some(0.0));
    }

    #[test]
    fn validate_dispensed_volume_checks() {
        assert!(Job::validate_dispensed_volume(100.0).is_ok());
        assert!(Job::validate_dispensed_volume(0.0).is_ok());
        assert!(Job::validate_dispensed_volume(-1.0).is_err());
        assert!(Job::validate_dispensed_volume(f64::NAN).is_err());
        assert!(Job::validate_dispensed_volume(f64::INFINITY).is_err());
    }

    // --- Job lifecycle setters ---

    #[test]
    fn set_status_updates_field() {
        let mut job = make_dispense_job();
        job.set_status(JobStatus::Running);
        assert_eq!(job.status, JobStatus::Running);
    }

    #[test]
    fn set_rfid_validated_marks_validated() {
        let mut job = make_dispense_job();
        assert!(!job.rfid_validated);
        job.set_rfid_validated(true);
        assert!(job.rfid_validated);
    }

    #[test]
    fn set_authorisation_id_records_id() {
        let mut job = make_dispense_job();
        let id = Uuid::new_v4();
        job.set_authorisation_id(id);
        assert_eq!(job.authorisation_id, Some(id));
    }

    // --- AtgReading::new() constructor guards ---

    #[test]
    fn new_atg_reading_rejects_nan_volume_l() {
        let err = AtgReading::new(
            f64::NAN,
            25_000.0,
            1_234.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(err, AtgReadingValidationError::NonFiniteValue("volume_l"));
    }

    #[test]
    fn new_atg_reading_rejects_infinite_volume_l() {
        let err = AtgReading::new(
            f64::INFINITY,
            25_000.0,
            1_234.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(err, AtgReadingValidationError::NonFiniteValue("volume_l"));
    }

    #[test]
    fn new_atg_reading_rejects_negative_volume_l() {
        let err = AtgReading::new(
            -1.0,
            25_000.0,
            1_234.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::NegativeValue {
                field: "volume_l",
                value: -1.0
            }
        );
    }

    #[test]
    fn new_atg_reading_accepts_zero_volume_l() {
        // An empty tank is physically valid.
        assert!(AtgReading::new(
            0.0,
            50_000.0,
            0.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now()
        )
        .is_ok());
    }

    #[test]
    fn new_atg_reading_rejects_nan_ullage_l() {
        let err = AtgReading::new(
            25_000.0,
            f64::NAN,
            1_234.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(err, AtgReadingValidationError::NonFiniteValue("ullage_l"));
    }

    #[test]
    fn new_atg_reading_rejects_negative_ullage_l() {
        let err = AtgReading::new(
            25_000.0,
            -0.5,
            1_234.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::NegativeValue {
                field: "ullage_l",
                value: -0.5
            }
        );
    }

    #[test]
    fn new_atg_reading_accepts_zero_ullage_l() {
        // A completely full tank has zero ullage.
        assert!(AtgReading::new(
            50_000.0,
            0.0,
            9_999.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now()
        )
        .is_ok());
    }

    #[test]
    fn new_atg_reading_rejects_nan_level_mm() {
        let err = AtgReading::new(
            25_000.0,
            25_000.0,
            f64::NAN,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(err, AtgReadingValidationError::NonFiniteValue("level_mm"));
    }

    #[test]
    fn new_atg_reading_rejects_negative_level_mm() {
        let err = AtgReading::new(
            25_000.0,
            25_000.0,
            -1.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::NegativeValue {
                field: "level_mm",
                value: -1.0
            }
        );
    }

    #[test]
    fn new_atg_reading_accepts_zero_level_mm() {
        assert!(AtgReading::new(
            0.0,
            50_000.0,
            0.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now()
        )
        .is_ok());
    }

    #[test]
    fn new_atg_reading_rejects_nan_temperature_c() {
        let err = AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            f64::NAN,
            "000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::NonFiniteValue("temperature_c")
        );
    }

    #[test]
    fn new_atg_reading_accepts_negative_temperature_c() {
        // Sub-zero ambient temperatures are physically plausible.
        assert!(AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            -5.0,
            "000000".to_owned(),
            false,
            Utc::now()
        )
        .is_ok());
    }

    #[test]
    fn new_atg_reading_rejects_alarm_flags_too_short() {
        let err = AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            15.0,
            "0000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::InvalidAlarmFlags("0000000".to_owned())
        );
    }

    #[test]
    fn new_atg_reading_rejects_alarm_flags_too_long() {
        let err = AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            15.0,
            "000000000".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::InvalidAlarmFlags("000000000".to_owned())
        );
    }

    #[test]
    fn new_atg_reading_rejects_alarm_flags_non_digit_char() {
        let err = AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            15.0,
            "0303X1".to_owned(),
            false,
            Utc::now(),
        )
        .unwrap_err();
        assert_eq!(
            err,
            AtgReadingValidationError::InvalidAlarmFlags("0303X1".to_owned())
        );
    }

    #[test]
    fn new_atg_reading_accepts_all_zero_alarm_flags() {
        assert!(AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            15.0,
            "000000".to_owned(),
            false,
            Utc::now()
        )
        .is_ok());
    }

    #[test]
    fn new_atg_reading_accepts_bund_alarm_entry() {
        let r = AtgReading::new(
            25_000.0,
            25_000.0,
            1_234.0,
            15.0,
            "030301".to_owned(),
            true,
            Utc::now(),
        )
        .unwrap();
        assert_eq!(r.alarm_flags, "030301");
        assert!(r.bund_alarm_active);
    }

    #[test]
    fn new_atg_reading_assigns_fields_correctly() {
        let now = Utc::now();
        let r = AtgReading::new(
            25_000.0,
            24_500.0,
            1_234.0,
            15.5,
            "030301".to_owned(),
            true,
            now,
        )
        .unwrap();
        assert_eq!(r.volume_l, 25_000.0);
        assert_eq!(r.ullage_l, 24_500.0);
        assert_eq!(r.level_mm, 1_234.0);
        assert_eq!(r.temperature_c, 15.5);
        assert_eq!(r.alarm_flags, "030301");
        assert!(r.bund_alarm_active);
        assert_eq!(r.recorded_at, now);
    }
    // -----------------------------------------------------------------------
    // try_set_current_job / clear_current_job
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn try_set_current_job_claims_empty_slot() {
        let state = make_state();
        let job = make_job(JobStatus::Active);
        let job_id = job.job_id;
        assert!(
            state.try_set_current_job(job).await,
            "empty slot must be claimable"
        );
        assert_eq!(
            state.current_job.read().await.as_ref().map(|j| j.job_id),
            Some(job_id),
            "the claimed job must occupy the slot"
        );
    }

    #[tokio::test]
    async fn try_set_current_job_rejects_when_active() {
        let state = make_state();
        assert!(state.try_set_current_job(make_job(JobStatus::Active)).await);
        assert!(
            !state.try_set_current_job(make_job(JobStatus::Active)).await,
            "a second job must be rejected while one is active"
        );
    }

    #[tokio::test]
    async fn try_set_current_job_rejects_when_running() {
        let state = make_state();
        assert!(
            state
                .try_set_current_job(make_job(JobStatus::Running))
                .await
        );
        assert!(
            !state.try_set_current_job(make_job(JobStatus::Active)).await,
            "a second job must be rejected while one is running"
        );
    }

    #[tokio::test]
    async fn try_set_current_job_rejects_when_awaiting_bdn() {
        let state = make_state();
        assert!(
            state
                .try_set_current_job(make_job(JobStatus::AwaitingBdn))
                .await
        );
        assert!(
            !state.try_set_current_job(make_job(JobStatus::Active)).await,
            "a new job must be rejected while a dispense awaits its BDN"
        );
    }

    #[tokio::test]
    async fn try_set_current_job_claims_slot_after_closed() {
        let state = make_state();
        assert!(state.try_set_current_job(make_job(JobStatus::Closed)).await);
        assert!(
            state.try_set_current_job(make_job(JobStatus::Active)).await,
            "a closed job must not block a new one"
        );
    }

    #[tokio::test]
    async fn try_set_current_job_claims_slot_after_cancelled() {
        let state = make_state();
        assert!(
            state
                .try_set_current_job(make_job(JobStatus::Cancelled))
                .await
        );
        assert!(
            state.try_set_current_job(make_job(JobStatus::Active)).await,
            "a cancelled job must not block a new one"
        );
    }

    /// The regression test for the race: many creates arriving together must
    /// resolve to exactly one winner, never more.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn concurrent_try_set_current_job_admits_exactly_one() {
        let state = make_state();
        let attempts = 32;

        let mut handles = Vec::with_capacity(attempts);
        for _ in 0..attempts {
            let state = state.clone();
            handles.push(tokio::spawn(async move {
                state.try_set_current_job(make_job(JobStatus::Active)).await
            }));
        }

        let mut winners = 0;
        for h in handles {
            if h.await.expect("task panicked") {
                winners += 1;
            }
        }
        assert_eq!(winners, 1, "exactly one concurrent create may win the slot");
        assert!(
            state.current_job.read().await.is_some(),
            "the winning job must remain installed"
        );
    }

    #[tokio::test]
    async fn clear_current_job_removes_matching_job() {
        let state = make_state();
        let job = make_job(JobStatus::Active);
        let job_id = job.job_id;
        state.try_set_current_job(job).await;
        state.clear_current_job(job_id).await;
        assert!(
            state.current_job.read().await.is_none(),
            "clearing the matching job must empty the slot"
        );
    }

    #[tokio::test]
    async fn clear_current_job_ignores_other_job() {
        let state = make_state();
        let job = make_job(JobStatus::Active);
        let job_id = job.job_id;
        state.try_set_current_job(job).await;
        state.clear_current_job(Uuid::new_v4()).await; // a different id
        assert_eq!(
            state.current_job.read().await.as_ref().map(|j| j.job_id),
            Some(job_id),
            "clearing a non-matching id must leave the slot untouched"
        );
    }

    /// The preset is the register's value: the request rounded to 0.1 L, a half
    /// away from zero as the register encoding rounds it (fms.md §7.4).
    #[test]
    fn register_preset_rounds_to_the_register_resolution() {
        assert_eq!(register_preset_l(1234.56), Ok(1234.6));
        assert_eq!(register_preset_l(500.0), Ok(500.0));
        assert_eq!(register_preset_l(0.05), Ok(0.1));
        assert_eq!(
            register_preset_l(DISPENSE_PRESET_MAX_L),
            Ok(DISPENSE_PRESET_MAX_L)
        );
    }

    /// A preset the register cannot hold is refused: one that rounds to zero,
    /// one above 6553.5 L, and one that is not a positive finite number.
    #[test]
    fn register_preset_refuses_what_the_register_cannot_hold() {
        assert_eq!(
            register_preset_l(0.04),
            Err(JobValidationError::PresetBelowRegisterResolution(0.04))
        );
        assert_eq!(
            register_preset_l(6553.6),
            Err(JobValidationError::PresetExceedsRegisterLimit(6553.6))
        );
        assert_eq!(
            register_preset_l(0.0),
            Err(JobValidationError::NonPositivePreset(0.0))
        );
        assert_eq!(
            register_preset_l(f64::NAN),
            Err(JobValidationError::NonFiniteValue("preset_volume_l"))
        );
    }
}
