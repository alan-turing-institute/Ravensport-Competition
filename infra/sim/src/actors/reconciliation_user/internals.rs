//! Private implementation of the reconciliation user actor. Not visible to
//! other modules.
//!
//! Here: the audit's types, the audit of a retrieved window, and `spawn`, which
//! validates the configuration and starts the audit loop. The loop, the audit
//! procedure, the retrieval, the session and the clipboard read are its
//! submodules (sim-impl.md, "Reconciliation user"). An item used only by the
//! tests is compiled only for them, under `#[cfg(test)]`.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use serde_json::Value;
use time::format_description::well_known::Rfc3339;
use time::{OffsetDateTime, PrimitiveDateTime, UtcOffset};
use tokio::sync::mpsc;

use super::{AuditPeriod, ReconciliationRecord, ReconciliationUserState};

mod audit;
mod audit_loop;
mod clipboard;
mod retrieval;
mod session;
#[cfg(test)]
mod workstation_double;
use crate::constants::FLOW_METER_K_FACTOR;

impl ReconciliationRecord {
    /// The instant that assigns the artefact to an audit window: the start of
    /// the transfer, which the paper and the log record share (sim.md §8.3).
    fn delivery_commenced_at(&self) -> PrimitiveDateTime {
        match self {
            ReconciliationRecord::Bdn(bdn) => bdn.delivery_commenced_at,
            ReconciliationRecord::DeliveryTicket(ticket) => ticket.delivery_commenced_at,
        }
    }

    /// The number printed on the artefact, by which a finding names it.
    fn document_number(&self) -> &str {
        match self {
            ReconciliationRecord::Bdn(bdn) => &bdn.bdn_number,
            ReconciliationRecord::DeliveryTicket(ticket) => &ticket.ticket_number,
        }
    }
}

/// Split the held and newly drained artefacts at `window`: those commencing
/// within it, for this audit, and those commencing after it, held for the next
/// (sim-impl.md, "Accumulation and partitioning"). Order is preserved in each.
///
/// # Panics
///
/// On an artefact commencing before the window. The settling delay is at least
/// the longest credible transfer, so the previous window's audit drained every
/// artefact of that window; one arriving later is an invariant violation.
fn partition(
    artefacts: Vec<ReconciliationRecord>,
    window: &AuditWindow,
) -> (Vec<ReconciliationRecord>, Vec<ReconciliationRecord>) {
    let mut audited = Vec::new();
    let mut held = Vec::new();
    for artefact in artefacts {
        let commenced = artefact.delivery_commenced_at();
        if window.contains(commenced) {
            audited.push(artefact);
        } else if commenced >= window.end {
            held.push(artefact);
        } else {
            panic!(
                "reconciliation_user: {} commenced at {commenced}, before the audited window \
                 from {} to {}; the settling delay should make this impossible",
                artefact.document_number(),
                window.start,
                window.end,
            );
        }
    }
    (audited, held)
}

/// The period one audit covers: `start` included, `end` excluded, both UTC.
/// A transfer commencing exactly at a boundary belongs to the window it opens.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct AuditWindow {
    start: PrimitiveDateTime,
    end: PrimitiveDateTime,
}

impl AuditWindow {
    fn contains(&self, instant: PrimitiveDateTime) -> bool {
        self.start <= instant && instant < self.end
    }

    /// The bounds retrieved for this window: wider by `margin` on both sides,
    /// so that a transfer commencing just inside the window is retrieved whole,
    /// and further back by the FMS session lifetime, so that the `LOGIN`
    /// opening a session is retrieved with its transactions. Both widen what is
    /// retrieved, not what is audited.
    fn retrieval_bounds(&self, margin: Duration) -> (PrimitiveDateTime, PrimitiveDateTime) {
        let session_lifetime = Duration::from_secs(crate::constants::SESSION_TIMEOUT_S);
        (self.start - margin - session_lifetime, self.end + margin)
    }
}

/// The audit calendar (sim-impl.md, "Cadence and window").
impl AuditPeriod {
    /// The window of this period containing `instant`.
    fn containing(self, instant: PrimitiveDateTime) -> AuditWindow {
        let date = instant.date();
        let start = match self {
            AuditPeriod::Month => date.replace_day(1).expect("every month has a first day"),
            AuditPeriod::Week => {
                date - time::Duration::days(date.weekday().number_days_from_monday().into())
            }
            AuditPeriod::Day => date,
            AuditPeriod::Interval(_) => {
                unreachable!(
                    "a period in hours has no calendar window; its schedule fixes its windows"
                )
            }
        }
        .midnight();
        AuditWindow {
            start,
            end: self.following_start(start),
        }
    }

    /// The window after `window`, starting where it ends.
    fn next(self, window: &AuditWindow) -> AuditWindow {
        self.containing(window.end)
    }

    /// The first window after the actor is spawned: the window containing the
    /// spawn instant, starting at that instant. The paper artefacts from
    /// before it are not held, so a window reaching further back would find
    /// records with no paper.
    fn first_window(self, spawned_at: PrimitiveDateTime) -> AuditWindow {
        AuditWindow {
            start: spawned_at,
            end: self.containing(spawned_at).end,
        }
    }

    /// Whether `delay` lies within its bounds. At least `boundary_margin`, the
    /// longest credible transfer, so that a transfer commencing just before a
    /// window ends has been posted before the audit drains; shorter than the
    /// shortest period of this kind, so that each audit begins before the
    /// following window ends.
    fn accepts_settling_delay(self, delay: Duration, boundary_margin: Duration) -> bool {
        const DAY: Duration = Duration::from_secs(24 * 60 * 60);
        let shortest = match self {
            AuditPeriod::Month => 28 * DAY,
            AuditPeriod::Week => 7 * DAY,
            AuditPeriod::Day => DAY,
            AuditPeriod::Interval(period) => period,
        };
        boundary_margin <= delay && delay < shortest
    }

    /// The start of the period after the one starting at `start`.
    fn following_start(self, start: PrimitiveDateTime) -> PrimitiveDateTime {
        match self {
            AuditPeriod::Month => {
                let month = start.month().next();
                let year = if month == time::Month::January {
                    start.year() + 1
                } else {
                    start.year()
                };
                time::Date::from_calendar_date(year, month, 1)
                    .expect("every month has a first day")
                    .midnight()
            }
            AuditPeriod::Week => start + time::Duration::weeks(1),
            AuditPeriod::Day => start + time::Duration::days(1),
            AuditPeriod::Interval(period) => start + period,
        }
    }
}

/// The measured size of the records retrieved for one tug arrival: a day's
/// records at one arrival an hour, `LOGIN` records included, are 62.6 KB on the
/// deployed system (sim-impl.md, "Splitting the window").
const RECORD_BYTES_PER_ARRIVAL: f64 = 62_600.0 / 24.0;

/// `initial_window` and `width_floor` at the dock's arrival rate of
/// `arrivals_per_h`: the times whose expected records fill half and a
/// sixteenth of the clipboard's ceiling (sim-impl.md, "Splitting the window").
/// The dock's arrival rate is the sum over the roster of one over each tug's
/// mean time away (sim.md §6), and a compressed scenario raises it by dividing
/// each mean time away, so both widths follow it with no value of their own.
///
/// # Panics
///
/// On a rate that is not positive and finite, which `world_schema.py` refuses.
pub(super) fn retrieval_widths(arrivals_per_h: f64) -> (Duration, Duration) {
    assert!(
        arrivals_per_h.is_finite() && arrivals_per_h > 0.0,
        "reconciliation_user: an arrival rate of {arrivals_per_h} gives no retrieval width"
    );
    let bytes_per_hour = RECORD_BYTES_PER_ARRIVAL * arrivals_per_h;
    let ceiling = clipboard::CLIPBOARD_CEILING_BYTES as f64;
    let filling = |share: f64| Duration::from_secs_f64(ceiling * share / bytes_per_hour * 3600.0);
    (filling(1.0 / 2.0), filling(1.0 / 16.0))
}

/// A failure that prevents an audit from completing, grouped by the site whose
/// budget it consumes (sim-impl.md, "Failure handling"). None is a finding: a
/// failure says nothing about whether the records agree.
#[derive(Debug, Clone, PartialEq, Eq)]
enum AuditFailure {
    Terminal(TerminalFailure),
    Retrieval(RetrievalFailure),
    Clipboard(ClipboardFailure),
}

impl AuditFailure {
    /// Whether a failure within a session has already ended it: the connection
    /// was lost, or the reassembly guard refused a chunk (sim-impl.md, "Failure
    /// handling"). It exhausts its site's budget at once.
    fn ends_the_session(&self) -> bool {
        matches!(
            self,
            AuditFailure::Terminal(TerminalFailure::ConnectionLost)
                | AuditFailure::Clipboard(
                    ClipboardFailure::DeclaredLengthOverCeiling { .. }
                        | ClipboardFailure::ExceedsDeclaredLength { .. }
                )
        )
    }
}

/// Starting the terminal and confirming the session.
#[derive(Debug, Clone, PartialEq, Eq)]
enum TerminalFailure {
    /// The latch returned a value other than the one echoed: a session-state
    /// failure, never an audit that found no records.
    LatchMismatch,
    /// The latch returned nothing: also a session-state failure.
    LatchAbsent,
    /// Connecting failed, at any stage up to the clipboard channel's setup.
    ConnectionFailed,
    /// The connection was lost once the session was established, which ends
    /// the session.
    ConnectionLost,
    /// The workstation's certificate did not verify: not retried, and nothing
    /// was sent inside the session.
    CertificateVerification,
    /// A line held a character the scancode table lacks; none of it was typed.
    UntypableCharacter(char),
}

/// The client's diagnostic in place of records, or a response that did not
/// arrive whole (sim-impl.md, "Retrieval").
#[derive(Debug, Clone, PartialEq, Eq)]
enum RetrievalFailure {
    Unreachable,
    AuthorisationRefused,
    /// Certificate verification failed. Never retried, and never bypassed
    /// (`recon.md` §6.4).
    CertificateVerification,
    MalformedResponse,
    /// The response did not finish: no completion line, or a transfer the
    /// client reports ended early.
    Incomplete,
}

/// A clipboard read (`recon.md` §11.2).
#[derive(Debug, Clone, PartialEq, Eq)]
enum ClipboardFailure {
    /// The read did not complete within its deadline.
    DeadlineReached,
    /// The transfer declared a length over the channel bound; refused before
    /// any of it is accumulated.
    DeclaredLengthOverCeiling { declared: usize },
    /// The chunks of a message carried more than it declared.
    ExceedsDeclaredLength { declared: usize, received: usize },
    /// A file-descriptor format was offered, or Unicode text was not.
    NonTextFormat,
    /// The selection held content from before the read began.
    StaleSelection,
    /// The workstation sent its failure response in place of the data.
    ResponseFailed,
}

/// The kinds of finding an audit can make, each with its disposition
/// (sim-impl.md, "Outcome").
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FindingKind {
    RecordDisagreesWithPaper,
    /// A completed transaction, or a link, with no paper artefact.
    RecordWithoutPaper,
    /// A totaliser reading below the one before it.
    TotaliserDecrease,
    PaperWithoutRecord,
    /// A dispense paired with its BDN by content, its `bdn_linked` not having
    /// arrived.
    LostLink,
    RecordInconsistency(Inconsistency),
    /// A field a check reads that is absent or cannot be read as its type.
    UnreadableField,
    /// An event without a readable `job_id`, or a job's events with no instant
    /// to assign them to a window by.
    UnassignableRecord,
    MissingLogin,
    CardTableChange,
}

/// The checks between records whose failure is reported (sim-impl.md,
/// "Checks"): each has a benign cause in the modelled port.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Inconsistency {
    /// The ATG delta beyond ±1% of the corrected meter volume.
    AtgAgainstMeter,
    /// The delivered volume beyond ±0.5% of the preset.
    PresetAgainstDelivered,
    /// A dispense that began and has no close.
    OpenWithoutClose,
    /// A totaliser reading above the previous transaction's end.
    TotaliserGap,
    /// Two transactions whose `opened_at`–`closed_at` intervals overlap.
    ConcurrentTransactions,
}

impl FindingKind {
    /// Whether the finding's subject is a document number rather than a job,
    /// a card or a record.
    fn names_a_document(self) -> bool {
        matches!(
            self,
            FindingKind::RecordDisagreesWithPaper
                | FindingKind::PaperWithoutRecord
                | FindingKind::LostLink
        )
    }

    /// Fatal only where something was written and the modelled port has no
    /// benign cause: a record that disagrees with its paper, a record with no
    /// paper, or a totaliser decrease. An absence requires only that something
    /// did not arrive, which `narrative.md` §5 allows.
    fn is_fatal(self) -> bool {
        match self {
            FindingKind::RecordDisagreesWithPaper
            | FindingKind::RecordWithoutPaper
            | FindingKind::TotaliserDecrease => true,
            FindingKind::PaperWithoutRecord
            | FindingKind::LostLink
            | FindingKind::RecordInconsistency(_)
            | FindingKind::UnreadableField
            | FindingKind::UnassignableRecord
            | FindingKind::MissingLogin
            | FindingKind::CardTableChange => false,
        }
    }
}

/// One finding of an audit, naming the document or job it concerns.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Finding {
    kind: FindingKind,
    subject: String,
}

impl Finding {
    fn new(kind: FindingKind, subject: &str) -> Finding {
        Finding {
            kind,
            subject: subject.to_string(),
        }
    }
}

/// The largest deviation each numeric check found in a window, as a fraction
/// of its tolerance: 1 is at the tolerance. `None` where the check ran on no
/// transaction (sim-impl.md, "Reporting").
#[derive(Debug, Clone, Copy, Default, PartialEq)]
struct Margins {
    /// The BDN volume against the totaliser delta, ±0.5%.
    bdn_volume: Option<f64>,
    /// The ATG delta against the corrected meter volume, ±1%.
    atg: Option<f64>,
    /// The delivered volume against the preset, ±0.5%.
    preset: Option<f64>,
}

impl Margins {
    fn widen_bdn_volume(&mut self, fraction: f64) {
        Self::widen(&mut self.bdn_volume, fraction);
    }

    fn widen_atg(&mut self, fraction: f64) {
        Self::widen(&mut self.atg, fraction);
    }

    fn widen_preset(&mut self, fraction: f64) {
        Self::widen(&mut self.preset, fraction);
    }

    fn widen(margin: &mut Option<f64>, fraction: f64) {
        *margin = Some(margin.map_or(fraction, |largest| largest.max(fraction)));
    }
}

/// What the audit of one window found, and the counts its report carries.
#[derive(Debug)]
struct Reconciliation {
    window: AuditWindow,
    findings: Vec<Finding>,
    margins: Margins,
    /// The lengths of the runs of unmatched artefacts, in commencement order.
    unmatched_runs: Vec<usize>,
    artefacts_reconciled: usize,
    records_retrieved: usize,
    refused_jobs: usize,
}

/// The report of an audit that did not end fatally (sim-impl.md, "Reporting").
#[derive(Debug)]
struct AuditReport {
    window: AuditWindow,
    artefacts_reconciled: usize,
    records_retrieved: usize,
    retrievals: usize,
    /// Jobs the FMS refused: context, not findings.
    refused_jobs: usize,
    margins: Margins,
    /// Each kind of finding with the documents, jobs or cards it names.
    findings: Vec<(FindingKind, Vec<String>)>,
    unmatched_runs: usize,
    longest_unmatched_run: usize,
}

/// The result of an audit: emitted at INFO, at WARN, or by `panic!`.
#[derive(Debug)]
enum AuditOutcome {
    Clean(AuditReport),
    Findings(AuditReport),
    /// The fatal message (sim-impl.md, "Outcome").
    Fatal(String),
}

/// One line of the retrieval, held whole as its map of fields (sim-impl.md,
/// "Windows, margins and grouping"). The store returns every value as text.
#[derive(Debug, Clone, PartialEq)]
struct Event(serde_json::Map<String, Value>);

/// Why a field of an [`Event`] cannot be read. Reported by the check that reads
/// the field; never a retrieval failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum FieldProblem {
    Absent,
    NotText,
    NotAnInstant,
    NotANumber,
}

impl Event {
    /// A line that is not a JSON object is a malformed response.
    fn parse(line: &str) -> Result<Event, RetrievalFailure> {
        match serde_json::from_str(line) {
            Ok(Value::Object(fields)) => Ok(Event(fields)),
            _ => Err(RetrievalFailure::MalformedResponse),
        }
    }

    fn text(&self, field: &str) -> Result<&str, FieldProblem> {
        match self.0.get(field) {
            None => Err(FieldProblem::Absent),
            Some(Value::String(text)) => Ok(text),
            Some(_) => Err(FieldProblem::NotText),
        }
    }

    /// An RFC 3339 instant, in UTC.
    fn instant(&self, field: &str) -> Result<PrimitiveDateTime, FieldProblem> {
        let instant = OffsetDateTime::parse(self.text(field)?, &Rfc3339)
            .map_err(|_| FieldProblem::NotAnInstant)?
            .to_offset(UtcOffset::UTC);
        Ok(PrimitiveDateTime::new(instant.date(), instant.time()))
    }

    /// A number, which the store returns as text.
    fn number(&self, field: &str) -> Result<f64, FieldProblem> {
        self.text(field)?
            .parse()
            .map_err(|_| FieldProblem::NotANumber)
    }

    fn is(&self, event_type: &str) -> bool {
        self.text("event_type") == Ok(event_type)
    }
}

/// One job's events and the instant that assigned them to the window.
#[derive(Debug, Clone, PartialEq)]
struct JobRecord {
    job_id: String,
    assigned_at: PrimitiveDateTime,
    events: Vec<Event>,
}

/// The retrieved events of one window, grouped by job.
#[derive(Debug, Default, PartialEq)]
struct Grouping {
    /// Jobs that began a transfer, for the checks.
    transactions: Vec<JobRecord>,
    /// Jobs of `job_opened` events, alone or with a `job_cancelled`: reported as
    /// context, never checked.
    refused_jobs: Vec<JobRecord>,
    /// Each reported as [`FindingKind::UnassignableRecord`].
    unassignable: Vec<Vec<Event>>,
}

/// Group `events` by `job_id`, assign each group by its instant, and keep the
/// groups `window` contains (sim-impl.md, "Windows, margins and grouping").
fn group(events: Vec<Event>, window: &AuditWindow) -> Grouping {
    // Jobs in order of first appearance, each job's events in retrieval order.
    let mut jobs: Vec<(String, Vec<Event>)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut unassignable = Vec::new();
    for event in events {
        let Ok(job_id) = event.text("job_id") else {
            unassignable.push(vec![event]);
            continue;
        };
        match index.get(job_id) {
            Some(&i) => jobs[i].1.push(event),
            None => {
                index.insert(job_id.to_string(), jobs.len());
                jobs.push((job_id.to_string(), vec![event]));
            }
        }
    }

    let mut grouping = Grouping::default();
    for (job_id, events) in jobs {
        let (assigned_at, into) = match assignment(&events) {
            Some(Assignment::Transfer(at)) => (at, &mut grouping.transactions),
            Some(Assignment::NoTransfer(at)) => (at, &mut grouping.refused_jobs),
            None => {
                unassignable.push(events);
                continue;
            }
        };
        if window.contains(assigned_at) {
            into.push(JobRecord {
                job_id,
                assigned_at,
                events,
            });
        }
    }

    // Reported by the window containing the earliest `_time`, so once. Without
    // a readable `_time` a record cannot be placed, and is reported by every
    // audit that retrieves it rather than by none.
    unassignable.retain(|events| earliest_time(events).is_none_or(|t| window.contains(t)));
    unassignable.sort_by_key(|events| earliest_time(events));
    grouping.unassignable = unassignable;
    grouping
}

/// How a job's events are assigned to a window.
enum Assignment {
    /// By the `opened_at` of its close, else of its open.
    Transfer(PrimitiveDateTime),
    /// A job of `job_opened` events, alone or with a `job_cancelled`, by its
    /// `created_at`.
    NoTransfer(PrimitiveDateTime),
}

/// The instant that assigns one job's events, or `None` when there is none
/// (sim-impl.md, "Windows, margins and grouping").
fn assignment(events: &[Event]) -> Option<Assignment> {
    let read = |event_type: &str, field: &str| {
        earliest(events, event_type).and_then(|event| event.instant(field).ok())
    };
    if let Some(at) =
        read("transaction_closed", "opened_at").or_else(|| read("transaction_open", "opened_at"))
    {
        return Some(Assignment::Transfer(at));
    }
    // A job that never began a transfer: refused at creation, or refused at its
    // start and then cancelled. A cancel without its `job_opened` has no
    // `created_at`, so is unassignable.
    if events
        .iter()
        .all(|event| event.is("job_opened") || event.is("job_cancelled"))
    {
        return read("job_opened", "created_at").map(Assignment::NoTransfer);
    }
    None
}

/// The earliest event of `event_type` by `_time`; one without a readable
/// `_time` sorts after every one with.
fn earliest<'a>(events: &'a [Event], event_type: &str) -> Option<&'a Event> {
    events
        .iter()
        .filter(|event| event.is(event_type))
        .min_by_key(|event| {
            let at = event.instant("_time").ok();
            (at.is_none(), at)
        })
}

fn earliest_time(events: &[Event]) -> Option<PrimitiveDateTime> {
    events
        .iter()
        .filter_map(|event| event.instant("_time").ok())
        .min()
}

/// The audit of one window (sim-impl.md, "Pairing", "Checks" and "Outcome"):
/// sets the authentication and card-table records aside, groups the bunkering
/// events, pairs them with the paper artefacts, and runs the checks. Returns
/// every finding; their dispositions are decided by the caller.
fn reconcile(
    artefacts: Vec<ReconciliationRecord>,
    records: Vec<Event>,
    window: &AuditWindow,
) -> Reconciliation {
    let records_retrieved = records.len();
    // The syslog records carry no `event_type`; grouped, each would be
    // unassignable.
    let (bunkering, logged): (Vec<Event>, Vec<Event>) = records
        .into_iter()
        .partition(|record| record.0.contains_key("event_type"));
    let grouping = group(bunkering, window);
    let mut findings = Vec::new();

    for events in &grouping.unassignable {
        let subject = events
            .first()
            .and_then(|event| event.text("job_id").ok())
            .unwrap_or("(no job_id)");
        findings.push(Finding::new(FindingKind::UnassignableRecord, subject));
    }

    let mut margins = Margins::default();
    let pairing = pair(&artefacts, &grouping.transactions);
    for (artefact, transaction, paired_by) in &pairing.pairs {
        if *paired_by == PairedBy::Content {
            findings.push(Finding::new(
                FindingKind::LostLink,
                artefact.document_number(),
            ));
        }
        compare(artefact, transaction, &mut findings, &mut margins);
    }
    for transaction in &pairing.unpaired_transactions {
        findings.push(Finding::new(
            FindingKind::RecordWithoutPaper,
            &transaction.job_id,
        ));
    }
    for artefact in &pairing.unpaired_artefacts {
        findings.push(Finding::new(
            FindingKind::PaperWithoutRecord,
            artefact.document_number(),
        ));
    }

    check_transactions(&grouping.transactions, &mut findings, &mut margins);
    check_sessions(&grouping.transactions, &logged, &mut findings);
    for record in &logged {
        if record.text("msg_id") == Ok("CARD-CHANGE")
            && record.instant("_time").is_ok_and(|at| window.contains(at))
        {
            let card = record.text("fms@99999.card_number").unwrap_or("(no card)");
            findings.push(Finding::new(FindingKind::CardTableChange, card));
        }
    }

    Reconciliation {
        window: *window,
        unmatched_runs: unmatched_runs(&artefacts, &pairing.unpaired_artefacts),
        artefacts_reconciled: pairing.pairs.len(),
        records_retrieved,
        refused_jobs: grouping.refused_jobs.len(),
        findings,
        margins,
    }
}

/// The lengths of the runs the unmatched artefacts form: taken in
/// commencement order, unmatched artefacts with no matched one between them
/// (sim-impl.md, "Reporting").
fn unmatched_runs(
    artefacts: &[ReconciliationRecord],
    unmatched: &[&ReconciliationRecord],
) -> Vec<usize> {
    let mut in_order: Vec<&ReconciliationRecord> = artefacts.iter().collect();
    in_order.sort_by_key(|artefact| artefact.delivery_commenced_at());
    let mut runs = Vec::new();
    let mut current = 0;
    for artefact in in_order {
        if unmatched.iter().any(|other| std::ptr::eq(*other, artefact)) {
            current += 1;
        } else if current > 0 {
            runs.push(current);
            current = 0;
        }
    }
    if current > 0 {
        runs.push(current);
    }
    runs
}

/// The audit's result (sim-impl.md, "Outcome" and "Reporting"): fatal if any
/// finding is of a fatal kind, else findings if there are any, else clean.
fn outcome(reconciliation: Reconciliation, retrievals: usize) -> AuditOutcome {
    // Each kind with the subjects it names, in the order first found.
    let mut findings: Vec<(FindingKind, Vec<String>)> = Vec::new();
    for finding in reconciliation.findings {
        match findings.iter_mut().find(|(kind, _)| *kind == finding.kind) {
            Some((_, subjects)) => subjects.push(finding.subject),
            None => findings.push((finding.kind, vec![finding.subject])),
        }
    }
    let window = reconciliation.window;

    if findings.iter().any(|(kind, _)| kind.is_fatal()) {
        let fatal: Vec<String> = findings
            .iter()
            .filter(|(kind, _)| kind.is_fatal())
            .map(|(kind, subjects)| format!("{kind:?}: {}", subjects.join(", ")))
            .collect();
        let reported: Vec<String> = findings
            .iter()
            .filter(|(kind, _)| !kind.is_fatal())
            .map(|(kind, subjects)| format!("{} {kind:?}", subjects.len()))
            .collect();
        return AuditOutcome::Fatal(format!(
            "reconciliation_user: the audit of {} to {} found {}; also reported: {}",
            window.start,
            window.end,
            fatal.join("; "),
            if reported.is_empty() {
                "nothing".to_string()
            } else {
                reported.join(", ")
            },
        ));
    }

    let report = AuditReport {
        window,
        artefacts_reconciled: reconciliation.artefacts_reconciled,
        records_retrieved: reconciliation.records_retrieved,
        retrievals,
        refused_jobs: reconciliation.refused_jobs,
        margins: reconciliation.margins,
        unmatched_runs: reconciliation.unmatched_runs.len(),
        longest_unmatched_run: reconciliation
            .unmatched_runs
            .iter()
            .copied()
            .max()
            .unwrap_or(0),
        findings,
    };
    if report.findings.is_empty() {
        AuditOutcome::Clean(report)
    } else {
        AuditOutcome::Findings(report)
    }
}

/// How an artefact was paired with its transaction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PairedBy {
    /// The number the FMS records against the transaction.
    DocumentNumber,
    /// Vessel, commencement and volume, the link not having arrived.
    Content,
}

/// The artefacts and transactions of one window, paired (sim-impl.md,
/// "Pairing").
struct Pairing<'a> {
    pairs: Vec<(&'a ReconciliationRecord, &'a JobRecord, PairedBy)>,
    unpaired_artefacts: Vec<&'a ReconciliationRecord>,
    /// Completed transactions, and links, with no paper. A dispense that began
    /// and did not complete needs none, so is never here.
    unpaired_transactions: Vec<&'a JobRecord>,
}

/// Pair each artefact with its transaction by document number, then each
/// completed dispense without a link with an unpaired BDN by content.
fn pair<'a>(artefacts: &'a [ReconciliationRecord], transactions: &'a [JobRecord]) -> Pairing<'a> {
    let mut paired = vec![false; artefacts.len()];
    let mut pairs = Vec::new();
    let mut unpaired_transactions = Vec::new();
    let mut by_content = Vec::new();

    for transaction in transactions {
        let close = earliest(&transaction.events, "transaction_closed");
        let number = match earliest(&transaction.events, "bdn_linked") {
            Some(link) => link.text("bdn_number").ok().map(|number| (true, number)),
            None => close
                .and_then(|close| close.text("ticket_number").ok())
                .map(|number| (false, number)),
        };
        match number {
            Some((is_bdn, number)) => {
                let found = (0..artefacts.len()).find(|&i| {
                    !paired[i]
                        && matches!(artefacts[i], ReconciliationRecord::Bdn(_)) == is_bdn
                        && artefacts[i].document_number() == number
                });
                match found {
                    Some(i) => {
                        paired[i] = true;
                        pairs.push((&artefacts[i], transaction, PairedBy::DocumentNumber));
                    }
                    None => unpaired_transactions.push(transaction),
                }
            }
            None if close.is_some() => by_content.push(transaction),
            // Began and did not complete: no paper by design.
            None => {}
        }
    }

    for transaction in by_content {
        let found = (0..artefacts.len())
            .find(|&i| !paired[i] && matches_by_content(&artefacts[i], transaction));
        match found {
            Some(i) => {
                paired[i] = true;
                pairs.push((&artefacts[i], transaction, PairedBy::Content));
            }
            None => unpaired_transactions.push(transaction),
        }
    }

    let unpaired_artefacts = artefacts
        .iter()
        .zip(&paired)
        .filter(|(_, paired)| !**paired)
        .map(|(artefact, _)| artefact)
        .collect();
    Pairing {
        pairs,
        unpaired_artefacts,
        unpaired_transactions,
    }
}

/// A BDN matching a completed dispense on vessel and commencement exactly, and
/// on volume within ±0.5% of the totaliser delta.
fn matches_by_content(artefact: &ReconciliationRecord, transaction: &JobRecord) -> bool {
    let ReconciliationRecord::Bdn(bdn) = artefact else {
        return false;
    };
    let Some(close) = earliest(&transaction.events, "transaction_closed") else {
        return false;
    };
    close.text("counterparty") == Ok(bdn.vessel_id.0.to_string().as_str())
        && close.instant("opened_at") == Ok(bdn.delivery_commenced_at)
        && totaliser_delta(close).is_ok_and(|delta| within(bdn.volume_l, delta, 0.005))
}

/// The litres a dispense close's totaliser readings span, by the compiled
/// K-factor.
fn totaliser_delta(close: &Event) -> Result<f64, FieldProblem> {
    let start = close.number("start_totaliser_pulses")?;
    let end = close.number("end_totaliser_pulses")?;
    Ok((end - start) / FLOW_METER_K_FACTOR)
}

/// The line the actor appends on the workstation (sim-impl.md, "Reporting"):
/// the period, `agreed` or `disagreed`, and the document numbers concerned,
/// sorted, each once. A fatal audit writes none: the sim ends before the
/// terminal is closed.
fn summary_entry(outcome: &AuditOutcome) -> Option<String> {
    let report = match outcome {
        AuditOutcome::Clean(report) | AuditOutcome::Findings(report) => report,
        AuditOutcome::Fatal(_) => return None,
    };
    // A card-table change is context, not a comparison of records.
    let disagreed = report
        .findings
        .iter()
        .any(|(kind, _)| *kind != FindingKind::CardTableChange);
    // Only document numbers are named (narrative.md §3); a finding naming a
    // job alone is named by the WARN.
    let mut documents: Vec<&str> = report
        .findings
        .iter()
        .filter(|(kind, _)| kind.names_a_document())
        .flat_map(|(_, subjects)| subjects.iter().map(String::as_str))
        .collect();
    documents.sort_unstable();
    documents.dedup();

    let instant = |at: PrimitiveDateTime| {
        at.assume_utc()
            .format(&Rfc3339)
            .expect("a UTC date-time always formats as RFC 3339")
    };
    let mut entry = format!(
        "{} {} {}",
        instant(report.window.start),
        instant(report.window.end),
        if disagreed { "disagreed" } else { "agreed" },
    );
    for document in documents {
        entry.push(' ');
        entry.push_str(document);
    }
    Some(entry)
}

/// How far `value` lies from `reference`, as a fraction of `tolerance` (itself
/// a fraction of `reference`): 1 is at the tolerance.
fn deviation(value: f64, reference: f64, tolerance: f64) -> f64 {
    (value - reference).abs() / (tolerance * reference)
}

/// Whether `value` lies within `tolerance` (a fraction) of `reference`.
fn within(value: f64, reference: f64, tolerance: f64) -> bool {
    (value - reference).abs() <= tolerance * reference
}

/// Read a field a check needs, reporting it as unreadable if it cannot be.
fn read<T>(
    value: Result<T, FieldProblem>,
    subject: &str,
    findings: &mut Vec<Finding>,
) -> Option<T> {
    match value {
        Ok(value) => Some(value),
        Err(_) => {
            findings.push(Finding::new(FindingKind::UnreadableField, subject));
            None
        }
    }
}

/// Compare a paired artefact with its transaction: every copied value exactly,
/// the BDN volume within ±0.5% of the totaliser delta (sim-impl.md, "Checks").
/// The envelope and the commencement are compared on every event carrying
/// them, so one altered event is found.
fn compare(
    artefact: &ReconciliationRecord,
    transaction: &JobRecord,
    findings: &mut Vec<Finding>,
    margins: &mut Margins,
) {
    let job = transaction.job_id.as_str();
    let commencement = artefact.delivery_commenced_at();
    // The counterparty the FMS records: the vessel's IMO number, or the
    // tanker's registration. A ticket carries no operator.
    let (counterparty, grade, operator) = match artefact {
        ReconciliationRecord::Bdn(bdn) => (
            bdn.vessel_id.0.to_string(),
            bdn.grade.as_str(),
            Some(bdn.operator_id.as_str()),
        ),
        ReconciliationRecord::DeliveryTicket(ticket) => (
            ticket.tanker_registration.clone(),
            ticket.grade.as_str(),
            None,
        ),
    };
    // (field on the close, the factor converting it to the paper's unit, the
    // value on the paper).
    let measurements = match artefact {
        ReconciliationRecord::Bdn(bdn) => vec![
            ("temperature_c", 1.0, bdn.temperature_c),
            ("density_kg_per_l", 1000.0, bdn.density_kg_per_m3),
            ("temp_corrected_volume_l", 1.0, bdn.temp_corrected_volume_l),
        ],
        ReconciliationRecord::DeliveryTicket(ticket) => vec![
            ("delivered_volume_l", 1.0, ticket.gross_volume_l),
            ("temperature_c", 1.0, ticket.temperature_c),
            ("density_kg_per_l", 1.0, ticket.density_kg_per_l),
            (
                "temp_corrected_volume_l",
                1.0,
                ticket.temp_corrected_volume_l,
            ),
        ],
    };

    // One finding per document, however many of its values differ.
    let mut disagrees = false;
    for event in &transaction.events {
        disagrees |= differs(
            event.text("counterparty"),
            counterparty.as_str(),
            job,
            findings,
        );
        disagrees |= differs(event.text("grade"), grade, job, findings);
        if let Some(operator) = operator {
            disagrees |= differs(event.text("operator"), operator, job, findings);
        }
        if event.is("transaction_open") || event.is("transaction_closed") {
            disagrees |= differs(event.instant("opened_at"), commencement, job, findings);
        }
    }
    if let Some(close) = earliest(&transaction.events, "transaction_closed") {
        for (field, factor, paper) in measurements {
            let record = close.number(field).map(|value| value * factor);
            disagrees |= differs(record, paper, job, findings);
        }
        match artefact {
            ReconciliationRecord::Bdn(bdn) => {
                if let Some(delta) = read(totaliser_delta(close), job, findings) {
                    margins.widen_bdn_volume(deviation(bdn.volume_l, delta, 0.005));
                    disagrees |= !within(bdn.volume_l, delta, 0.005);
                }
            }
            ReconciliationRecord::DeliveryTicket(ticket) => {
                disagrees |= differs(
                    close.text("tanker_registration"),
                    ticket.tanker_registration.as_str(),
                    job,
                    findings,
                );
            }
        }
    }
    if disagrees {
        findings.push(Finding::new(
            FindingKind::RecordDisagreesWithPaper,
            artefact.document_number(),
        ));
    }
}

/// Whether a record's field differs from the paper's value. An unreadable
/// field is reported, and is not a difference: the comparison cannot be made.
fn differs<T: PartialEq>(
    record: Result<T, FieldProblem>,
    paper: T,
    subject: &str,
    findings: &mut Vec<Finding>,
) -> bool {
    read(record, subject, findings).is_some_and(|value| value != paper)
}

/// The checks between records (sim-impl.md, "Checks"): the ATG against the
/// meter, preset against delivered, an open with no close, the totaliser, and
/// concurrency.
fn check_transactions(
    transactions: &[JobRecord],
    findings: &mut Vec<Finding>,
    margins: &mut Margins,
) {
    let inconsistency = |kind: Inconsistency, job: &str, findings: &mut Vec<Finding>| {
        findings.push(Finding::new(FindingKind::RecordInconsistency(kind), job));
    };
    // (opened, closed, job) of each completed transaction, for concurrency.
    let mut intervals = Vec::new();
    // (start, end, job) of each completed dispense, in commencement order.
    let mut totalisers = Vec::new();

    for transaction in transactions {
        let job = transaction.job_id.as_str();
        let Some(close) = earliest(&transaction.events, "transaction_closed") else {
            if earliest(&transaction.events, "transaction_open").is_some() {
                inconsistency(Inconsistency::OpenWithoutClose, job, findings);
            }
            continue;
        };
        let is_fill = close.text("type") == Ok("fill");

        // The ATG volumes are already compensated, so the meter side alone is
        // corrected: the corrected volume the record carries.
        let open = read(close.number("atg_volume_at_open_l"), job, findings);
        let closing = read(close.number("atg_volume_at_close_l"), job, findings);
        let corrected = read(close.number("temp_corrected_volume_l"), job, findings);
        if let (Some(open), Some(closing), Some(corrected)) = (open, closing, corrected) {
            let atg_delta = if is_fill {
                closing - open
            } else {
                open - closing
            };
            margins.widen_atg(deviation(atg_delta, corrected, 0.01));
            if !within(atg_delta, corrected, 0.01) {
                inconsistency(Inconsistency::AtgAgainstMeter, job, findings);
            }
        }

        if !is_fill {
            let preset = read(close.number("preset_volume_l"), job, findings);
            let delivered = read(close.number("volume_dispensed_l"), job, findings);
            if let (Some(preset), Some(delivered)) = (preset, delivered) {
                margins.widen_preset(deviation(delivered, preset, 0.005));
                if !within(delivered, preset, 0.005) {
                    inconsistency(Inconsistency::PresetAgainstDelivered, job, findings);
                }
            }
            let start = read(close.number("start_totaliser_pulses"), job, findings);
            let end = read(close.number("end_totaliser_pulses"), job, findings);
            if let (Some(start), Some(end)) = (start, end) {
                if end < start {
                    findings.push(Finding::new(FindingKind::TotaliserDecrease, job));
                }
                totalisers.push((transaction.assigned_at, start, end, job));
            }
        }

        let opened = read(close.instant("opened_at"), job, findings);
        let closed = read(close.instant("closed_at"), job, findings);
        if let (Some(opened), Some(closed)) = (opened, closed) {
            intervals.push((opened, closed, job));
        }
    }

    totalisers.sort_by_key(|&(commenced, ..)| commenced);
    for pair in totalisers.windows(2) {
        let (_, _, previous_end, _) = pair[0];
        let (_, start, _, job) = pair[1];
        if start < previous_end {
            findings.push(Finding::new(FindingKind::TotaliserDecrease, job));
        } else if start > previous_end {
            inconsistency(Inconsistency::TotaliserGap, job, findings);
        }
    }

    intervals.sort_by_key(|&(opened, ..)| opened);
    let mut latest_close: Option<PrimitiveDateTime> = None;
    for (opened, closed, job) in intervals {
        if latest_close.is_some_and(|latest| opened < latest) {
            inconsistency(Inconsistency::ConcurrentTransactions, job, findings);
        }
        latest_close = Some(latest_close.map_or(closed, |latest| latest.max(closed)));
    }
}

/// Every transaction's `session_id` resolves to a `LOGIN`, whose
/// structured-data parameters are stored under their identifier (hist.md
/// §4.3): `fms@99999.session_id` there, `session_id` on the bunkering events.
fn check_sessions(transactions: &[JobRecord], logged: &[Event], findings: &mut Vec<Finding>) {
    let logins: HashSet<&str> = logged
        .iter()
        .filter(|record| record.text("msg_id") == Ok("LOGIN"))
        .filter_map(|record| record.text("fms@99999.session_id").ok())
        .collect();
    for transaction in transactions {
        let job = transaction.job_id.as_str();
        let mut unresolved = false;
        for event in &transaction.events {
            if let Some(session) = read(event.text("session_id"), job, findings) {
                unresolved |= !logins.contains(session);
            }
        }
        if unresolved {
            findings.push(Finding::new(FindingKind::MissingLogin, job));
        }
    }
}

/// The actor's configuration, as `spawn` receives it.
pub(super) struct Settings {
    pub(super) local_addr: std::net::SocketAddr,
    pub(super) recon_addr: std::net::SocketAddr,
    pub(super) audit_period: AuditPeriod,
    pub(super) settling_delay: Duration,
    pub(super) first_audit_after: Option<Duration>,
    pub(super) initial_window: Duration,
    pub(super) width_floor: Duration,
    pub(super) boundary_margin: Duration,
    pub(super) clipboard_deadline: Duration,
    pub(super) desktop_settle: Duration,
    pub(super) terminal_settle: Duration,
    pub(super) key_interval: Duration,
    pub(super) connect_deadline: Duration,
}

/// Validate the configuration on the synchronous path, build the IronRDP
/// connector, and start the audit loop (sim-impl.md, "The audit loop").
///
/// # Panics
///
/// As a startup fault, on a settling delay outside its bounds, on a first
/// audit given for a calendar period or missing for a period in hours, on a
/// first window of a period in hours that ends at or before the spawn, or on a
/// deployment root the TLS configuration cannot take.
pub(super) fn spawn(
    clock: crate::clock::SimClock,
    record_rx: mpsc::UnboundedReceiver<ReconciliationRecord>,
    settings: Settings,
    start: Option<ReconciliationUserState>,
    stop: crate::stop::StopRequest,
    dock_operator_at_rest: tokio::sync::oneshot::Receiver<()>,
    report: tokio::sync::oneshot::Sender<ReconciliationUserState>,
) -> tokio::task::JoinHandle<()> {
    if !settings
        .audit_period
        .accepts_settling_delay(settings.settling_delay, settings.boundary_margin)
    {
        panic!(
            "reconciliation_user: a settling delay of {:?} is outside its bounds for a {:?} \
             period: at least the boundary margin of {:?} and shorter than the shortest \
             period of its kind",
            settings.settling_delay, settings.audit_period, settings.boundary_margin,
        );
    }
    if matches!(settings.audit_period, AuditPeriod::Interval(_))
        != settings.first_audit_after.is_some()
    {
        panic!(
            "reconciliation_user: the start state places the first audit exactly for a period \
             in hours, but the period is {:?} and the first audit {:?}",
            settings.audit_period, settings.first_audit_after,
        );
    }
    // The first window of a period in hours ends one settling delay before
    // the first audit, and the actor holds no paper from before its spawn, so
    // that window must end after the spawn (sim-impl.md, "A period in hours").
    if let Some(first_audit_after) = settings.first_audit_after {
        if first_audit_after <= settings.settling_delay {
            panic!(
                "reconciliation_user: the first audit {:?} after the spawn audits a window \
                 that ends at or before the spawn, one settling delay of {:?} earlier",
                first_audit_after, settings.settling_delay,
            );
        }
    }
    let tls = session::tls_config(crate::constants::CA_ROOT_PEM)
        .unwrap_or_else(|error| panic!("reconciliation_user: the deployment root: {error}"));
    let connector = session::RdpConnector {
        clock: clock.clone(),
        local_addr: settings.local_addr,
        recon_addr: settings.recon_addr,
        tls,
        settings: session::SessionSettings {
            key_interval: settings.key_interval,
            desktop_settle: settings.desktop_settle,
            terminal_settle: settings.terminal_settle,
            clipboard_deadline: settings.clipboard_deadline,
            connect_deadline: settings.connect_deadline,
        },
    };
    tokio::spawn(audit_loop::run(
        clock,
        record_rx,
        connector,
        audit_loop::LoopSettings {
            audit_period: settings.audit_period,
            settling_delay: settings.settling_delay,
            boundary_margin: settings.boundary_margin,
            initial_window: settings.initial_window,
            first_audit_after: settings.first_audit_after,
            width_floor: settings.width_floor,
        },
        audit_loop::Lifecycle {
            start,
            stop,
            dock_operator_at_rest,
            report,
        },
    ))
}

/// The audit logic, tested in-module rather than through `spawn()`
/// (sim-tests.md, "Reconciliation user").
#[cfg(test)]
mod tests {
    use time::macros::datetime;

    use super::*;

    fn january_2025() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-01-01 0:00),
            end: datetime!(2025-02-01 0:00),
        }
    }

    #[test]
    fn a_window_includes_its_start_and_excludes_its_end() {
        let window = january_2025();
        assert!(window.contains(datetime!(2025-01-01 0:00)));
        assert!(window.contains(datetime!(2025-01-31 23:59:59.999999999)));
        assert!(!window.contains(datetime!(2025-02-01 0:00)));
        assert!(!window.contains(datetime!(2024-12-31 23:59:59.999999999)));
    }

    #[test]
    fn each_artefact_reports_its_own_commencement() {
        let bdn = fixtures::bdn("BDN-000001", datetime!(2025-01-31 23:58));
        let ticket = fixtures::ticket("DT-000001", datetime!(2025-02-01 0:02));
        assert_eq!(bdn.delivery_commenced_at(), datetime!(2025-01-31 23:58));
        assert_eq!(ticket.delivery_commenced_at(), datetime!(2025-02-01 0:02));
    }
}

/// The audit calendar (sim-tests.md, "Audit calendar").
#[cfg(test)]
mod calendar_tests {
    use std::time::Duration;

    use time::macros::datetime;
    use time::PrimitiveDateTime;

    use super::*;

    const DAY: Duration = Duration::from_secs(24 * 60 * 60);

    fn window(start: PrimitiveDateTime, end: PrimitiveDateTime) -> AuditWindow {
        AuditWindow { start, end }
    }

    #[test]
    fn a_month_window_runs_from_the_first_to_the_first_of_the_next_month() {
        assert_eq!(
            AuditPeriod::Month.containing(datetime!(2025-01-15 12:00)),
            window(datetime!(2025-01-01 0:00), datetime!(2025-02-01 0:00)),
        );
    }

    #[test]
    fn a_week_window_runs_from_monday_to_monday() {
        let monday_to_monday = window(datetime!(2025-01-13 0:00), datetime!(2025-01-20 0:00));
        assert_eq!(
            AuditPeriod::Week.containing(datetime!(2025-01-16 9:30)),
            monday_to_monday,
        );
        assert_eq!(
            AuditPeriod::Week.containing(datetime!(2025-01-13 0:00)),
            monday_to_monday,
        );
    }

    #[test]
    fn a_day_window_runs_from_midnight_to_midnight() {
        assert_eq!(
            AuditPeriod::Day.containing(datetime!(2025-01-15 14:00)),
            window(datetime!(2025-01-15 0:00), datetime!(2025-01-16 0:00)),
        );
    }

    #[test]
    fn the_next_window_follows_without_a_gap_across_a_month_and_a_year_end() {
        let cases = [
            (
                AuditPeriod::Month,
                window(datetime!(2024-12-01 0:00), datetime!(2025-01-01 0:00)),
                window(datetime!(2025-01-01 0:00), datetime!(2025-02-01 0:00)),
            ),
            (
                AuditPeriod::Month,
                window(datetime!(2024-02-01 0:00), datetime!(2024-03-01 0:00)),
                window(datetime!(2024-03-01 0:00), datetime!(2024-04-01 0:00)),
            ),
            (
                AuditPeriod::Week,
                window(datetime!(2024-12-30 0:00), datetime!(2025-01-06 0:00)),
                window(datetime!(2025-01-06 0:00), datetime!(2025-01-13 0:00)),
            ),
            (
                AuditPeriod::Day,
                window(datetime!(2024-12-31 0:00), datetime!(2025-01-01 0:00)),
                window(datetime!(2025-01-01 0:00), datetime!(2025-01-02 0:00)),
            ),
        ];
        for (period, current, following) in cases {
            assert_eq!(
                period.next(&current),
                following,
                "{period:?} after {current:?}"
            );
        }
        // The week containing Tuesday 31 December 2024 is the one above.
        assert_eq!(
            AuditPeriod::Week.containing(datetime!(2024-12-31 12:00)),
            window(datetime!(2024-12-30 0:00), datetime!(2025-01-06 0:00)),
        );
        // A leap-year February runs to 1 March.
        assert_eq!(
            AuditPeriod::Month.containing(datetime!(2024-02-29 12:00)),
            window(datetime!(2024-02-01 0:00), datetime!(2024-03-01 0:00)),
        );
    }

    #[test]
    fn a_settling_delay_outside_its_bounds_is_refused() {
        let nanosecond = Duration::from_nanos(1);
        let margin = Duration::from_secs(6 * 60 * 60);
        for (period, shortest) in [
            (AuditPeriod::Month, 28 * DAY),
            (AuditPeriod::Week, 7 * DAY),
            (AuditPeriod::Day, DAY),
        ] {
            assert!(
                period.accepts_settling_delay(shortest - nanosecond, margin),
                "{period:?} refused a delay short of {shortest:?}",
            );
            assert!(
                !period.accepts_settling_delay(shortest, margin),
                "{period:?} accepted a delay of {shortest:?}",
            );
            assert!(
                period.accepts_settling_delay(margin, margin),
                "{period:?} refused a delay equal to the margin",
            );
            assert!(
                !period.accepts_settling_delay(margin - nanosecond, margin),
                "{period:?} accepted a delay short of the margin",
            );
        }
    }

    #[test]
    fn the_first_window_starts_no_earlier_than_the_sim() {
        let spawned_at = datetime!(2025-01-15 14:00);

        let first_month = AuditPeriod::Month.first_window(spawned_at);
        assert_eq!(first_month, window(spawned_at, datetime!(2025-02-01 0:00)));
        assert_eq!(
            AuditPeriod::Month.next(&first_month),
            window(datetime!(2025-02-01 0:00), datetime!(2025-03-01 0:00)),
        );

        let first_day = AuditPeriod::Day.first_window(spawned_at);
        assert_eq!(first_day, window(spawned_at, datetime!(2025-01-16 0:00)));
        assert_eq!(
            AuditPeriod::Day.next(&first_day),
            window(datetime!(2025-01-16 0:00), datetime!(2025-01-17 0:00)),
        );
    }
}

/// Paper artefacts for the in-module tests, each varying only in its document
/// number and commencement.
#[cfg(test)]
mod fixtures {
    use time::PrimitiveDateTime;

    use serde_json::{json, Value};
    use time::format_description::well_known::Rfc3339;

    use super::{Event, ReconciliationRecord};
    use crate::actors::dock_operator::BDN;
    use crate::actors::road_tanker_driver::DeliveryTicket;
    use crate::actors::tug_engineer::VesselId;
    use crate::constants::FLOW_METER_K_FACTOR;

    pub(super) fn bdn(number: &str, commenced: PrimitiveDateTime) -> ReconciliationRecord {
        ReconciliationRecord::Bdn(BDN {
            bdn_number: number.to_string(),
            delivery_commenced_at: commenced,
            vessel_id: VesselId(1999997),
            vessel_name: "Tug Alpha",
            port: "Ravensport",
            supplier_name: "Ravensport Harbour Authority",
            supplier_address: "Fuel Dock, East Quay, Ravensport RV1 4QD",
            supplier_telephone: "01632 960482",
            grade: "MGO".to_string(),
            volume_l: 500.0,
            temperature_c: 15.0,
            temp_corrected_volume_l: 500.0,
            quantity_t: 0.42,
            density_kg_per_m3: 840.0,
            sulphur_pct_mm: 0.10,
            flashpoint_c: 66.0,
            sulphur_limit_declared: true,
            start_totaliser_l: 1000.0,
            end_totaliser_l: 1500.0,
            operator_id: "jrenard".to_string(),
            engineer_id: "engineer-1".to_string(),
            signed_by_operator: true,
            signed_by_engineer: true,
        })
    }

    pub(super) fn ticket(number: &str, commenced: PrimitiveDateTime) -> ReconciliationRecord {
        ReconciliationRecord::DeliveryTicket(DeliveryTicket {
            ticket_number: number.to_string(),
            delivery_commenced_at: commenced,
            grade: "MGO".to_string(),
            gross_volume_l: 18_000.0,
            temperature_c: 15.0,
            temp_corrected_volume_l: 18_000.0,
            density_kg_per_l: 0.84,
            start_totaliser_l: 0.0,
            end_totaliser_l: 18_000.0,
            tanker_registration: "AB12 CDE".to_string(),
            driver_id: "driver-01".to_string(),
            signed_by_driver: true,
            signed_by_operator: true,
        })
    }

    /// An instant as the store returns it: RFC 3339 text in UTC.
    pub(super) fn text(at: PrimitiveDateTime) -> Value {
        Value::String(at.assume_utc().format(&Rfc3339).unwrap())
    }

    /// A number as the store returns it: as text.
    pub(super) fn number(value: f64) -> Value {
        Value::String(value.to_string())
    }

    pub(super) fn pulses(value: u64) -> Value {
        Value::String(value.to_string())
    }

    /// The pulses one 500 L dispense adds to the totaliser.
    pub(super) fn dispense_pulses() -> u64 {
        (500.0 * FLOW_METER_K_FACTOR) as u64
    }

    /// An event of `envelope`'s job carrying `fields` as well.
    pub(super) fn record(envelope: &Value, fields: Value) -> Event {
        let mut map = envelope.as_object().unwrap().clone();
        map.extend(fields.as_object().unwrap().clone());
        Event(map)
    }

    /// The four events of a 500 L dispense to the fixture vessel, commencing at
    /// `at` with the totaliser at `start`, as the FMS records it (hist.md §4.2).
    pub(super) fn dispense(
        job: &str,
        bdn: &str,
        session: &str,
        at: PrimitiveDateTime,
        start: u64,
    ) -> Vec<Event> {
        let envelope = json!({
            "job_id": job, "type": "dispense", "operator": "jrenard",
            "session_id": session, "counterparty": "1999997", "grade": "MGO",
        });
        let closed_at = at + time::Duration::minutes(2);
        let transaction = format!("t-{job}");
        vec![
            record(
                &envelope,
                json!({
                    "_time": text(at - time::Duration::minutes(1)), "event_type": "job_opened",
                    "created_at": text(at - time::Duration::minutes(1)),
                    "atg_volume_at_open_l": number(35_000.0),
                }),
            ),
            record(
                &envelope,
                json!({
                    "_time": text(at), "event_type": "transaction_open",
                    "authorisation_id": format!("a-{job}"), "preset_volume_l": number(500.0),
                    "atg_volume_at_open_l": number(35_000.0), "opened_at": text(at),
                }),
            ),
            record(
                &envelope,
                json!({
                    "_time": text(closed_at), "event_type": "transaction_closed",
                    "transaction_id": transaction, "authorisation_id": format!("a-{job}"),
                    "preset_volume_l": number(500.0),
                    "start_totaliser_pulses": pulses(start),
                    "end_totaliser_pulses": pulses(start + dispense_pulses()),
                    "volume_dispensed_l": number(500.0), "temperature_c": number(15.0),
                    "density_kg_per_l": number(0.84), "temp_corrected_volume_l": number(500.0),
                    "atg_volume_at_open_l": number(35_000.0), "atg_volume_at_close_l": number(34_500.0),
                    "opened_at": text(at), "closed_at": text(closed_at),
                }),
            ),
            record(
                &envelope,
                json!({
                    "_time": text(closed_at + time::Duration::minutes(1)), "event_type": "bdn_linked",
                    "transaction_id": format!("l-{job}"), "references_transaction_id": transaction,
                    "bdn_number": bdn,
                }),
            ),
        ]
    }

    /// The two events of an 18,000 L fill from the fixture tanker, commencing
    /// at `at` (hist.md §4.2).
    pub(super) fn fill(
        job: &str,
        ticket: &str,
        session: &str,
        at: PrimitiveDateTime,
    ) -> Vec<Event> {
        let envelope = json!({
            "job_id": job, "type": "fill", "operator": "jrenard",
            "session_id": session, "counterparty": "AB12 CDE", "grade": "MGO",
        });
        let closed_at = at + time::Duration::minutes(30);
        vec![
            record(
                &envelope,
                json!({
                    "_time": text(at - time::Duration::minutes(5)), "event_type": "job_opened",
                    "created_at": text(at - time::Duration::minutes(5)),
                    "atg_volume_at_open_l": number(16_000.0),
                }),
            ),
            record(
                &envelope,
                json!({
                    "_time": text(closed_at), "event_type": "transaction_closed",
                    "transaction_id": format!("t-{job}"), "ticket_number": ticket,
                    "delivered_volume_l": number(18_000.0), "temperature_c": number(15.0),
                    "density_kg_per_l": number(0.84), "temp_corrected_volume_l": number(18_000.0),
                    "supplier": "Ravensport Fuels", "tanker_registration": "AB12 CDE",
                    "atg_volume_at_open_l": number(16_000.0), "atg_volume_at_close_l": number(34_000.0),
                    "opened_at": text(at), "closed_at": text(closed_at),
                }),
            ),
        ]
    }

    /// The FMS's `LOGIN` record of a session, its structured-data parameters
    /// under their identifier (hist.md §4.3).
    pub(super) fn login(session: &str, at: PrimitiveDateTime) -> Event {
        Event(
            json!({
                "_time": text(at), "_msg": "login", "msg_id": "LOGIN", "app_name": "fms",
                "hostname": "fms-01", "fms@99999.username": "jrenard",
                "fms@99999.role": "operator", "fms@99999.session_id": session,
                "fms@99999.source_address": "10.20.0.12", "fms@99999.result": "success",
            })
            .as_object()
            .unwrap()
            .clone(),
        )
    }
}

/// Accumulation (sim-tests.md, "Accumulation").
#[cfg(test)]
mod accumulation_tests {
    use time::macros::datetime;

    use super::*;

    fn january_2025() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-01-01 0:00),
            end: datetime!(2025-02-01 0:00),
        }
    }

    #[test]
    fn an_artefact_dated_within_the_window_is_audited() {
        let first = fixtures::bdn("BDN-000001", datetime!(2025-01-01 0:00));
        let last = fixtures::ticket("DT-000001", datetime!(2025-01-31 23:59:59.999999999));
        let (audited, held) = partition(vec![first.clone(), last.clone()], &january_2025());
        assert_eq!(audited, vec![first, last]);
        assert!(held.is_empty(), "held {held:?}");
    }

    #[test]
    fn an_artefact_dated_after_the_window_is_held() {
        let at_end = fixtures::bdn("BDN-000002", datetime!(2025-02-01 0:00));
        let later = fixtures::ticket("DT-000002", datetime!(2025-02-07 12:00));
        let (audited, held) = partition(vec![at_end.clone(), later.clone()], &january_2025());
        assert!(audited.is_empty(), "audited {audited:?}");
        assert_eq!(held, vec![at_end, later]);
    }

    #[test]
    fn a_held_artefact_is_audited_in_the_next_window() {
        let january = fixtures::bdn("BDN-000003", datetime!(2025-01-20 9:00));
        let february = fixtures::bdn("BDN-000004", datetime!(2025-02-03 9:00));
        let (_, held) = partition(vec![january, february.clone()], &january_2025());

        let march = fixtures::ticket("DT-000003", datetime!(2025-03-02 9:00));
        let drained = held.into_iter().chain([march.clone()]).collect();
        let february_window = AuditPeriod::Month.next(&january_2025());
        let (audited, held) = partition(drained, &february_window);
        assert_eq!(audited, vec![february]);
        assert_eq!(held, vec![march]);
    }

    #[test]
    #[should_panic(expected = "BDN-000005")]
    fn an_artefact_dated_before_the_window_is_fatal() {
        let early = fixtures::bdn("BDN-000005", datetime!(2024-12-31 23:59:59.999999999));
        partition(vec![early], &january_2025());
    }
}

/// Records and grouping (sim-tests.md, "Records and grouping").
#[cfg(test)]
mod grouping_tests {
    use serde_json::{json, Value};
    use time::format_description::well_known::Rfc3339;
    use time::macros::datetime;
    use time::PrimitiveDateTime;

    use super::*;

    fn january_2025() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-01-01 0:00),
            end: datetime!(2025-02-01 0:00),
        }
    }

    fn february_2025() -> AuditWindow {
        AuditPeriod::Month.next(&january_2025())
    }

    /// An instant as the store returns it: RFC 3339 text in UTC.
    fn text(at: PrimitiveDateTime) -> Value {
        Value::String(at.assume_utc().format(&Rfc3339).unwrap())
    }

    fn event(fields: Value) -> Event {
        match fields {
            Value::Object(map) => Event(map),
            other => panic!("not an object: {other}"),
        }
    }

    fn job_opened(job: &str, created: PrimitiveDateTime) -> Event {
        event(json!({
            "_time": text(created), "event_type": "job_opened", "job_id": job,
            "created_at": text(created),
        }))
    }

    fn opened(job: &str, at: PrimitiveDateTime) -> Event {
        event(json!({
            "_time": text(at), "event_type": "transaction_open", "job_id": job,
            "opened_at": text(at),
        }))
    }

    fn closed(job: &str, opened: PrimitiveDateTime, closed: PrimitiveDateTime) -> Event {
        event(json!({
            "_time": text(closed), "event_type": "transaction_closed", "job_id": job,
            "opened_at": text(opened), "closed_at": text(closed),
        }))
    }

    fn bdn_linked(job: &str, at: PrimitiveDateTime) -> Event {
        event(json!({
            "_time": text(at), "event_type": "bdn_linked", "job_id": job,
            "bdn_number": "BDN-000001",
        }))
    }

    fn job_cancelled(job: &str, at: PrimitiveDateTime) -> Event {
        event(json!({
            "_time": text(at), "event_type": "job_cancelled", "job_id": job,
            "cancelled_at": text(at),
        }))
    }

    fn ids(records: &[JobRecord]) -> Vec<&str> {
        records.iter().map(|r| r.job_id.as_str()).collect()
    }

    #[test]
    fn a_line_that_is_not_a_json_object_is_a_malformed_response() {
        for line in [
            "curl: (7) Failed to connect to hist-dmz-01 port 443",
            r#"{"_time":"2025-01-31T"#,
            r#"["job_opened"]"#,
        ] {
            assert_eq!(
                Event::parse(line),
                Err(RetrievalFailure::MalformedResponse),
                "{line}"
            );
        }
        assert_eq!(
            Event::parse(r#"{"job_id":"j1"}"#),
            Ok(event(json!({"job_id": "j1"}))),
        );
    }

    #[test]
    fn a_field_reads_as_its_value_or_the_reason_it_cannot() {
        let e = event(json!({
            "job_id": "j1",
            "opened_at": "2026-10-16T09:27:13.631777577Z",
            "closed_at": "2026-10-16T10:29:41+01:00",
            "preset_volume_l": 700.95,
            "created_at": "shortly before",
        }));
        assert_eq!(e.text("job_id"), Ok("j1"));
        assert_eq!(
            e.instant("opened_at"),
            Ok(datetime!(2026-10-16 9:27:13.631777577))
        );
        assert_eq!(e.instant("closed_at"), Ok(datetime!(2026-10-16 9:29:41)));
        assert_eq!(e.text("bdn_number"), Err(FieldProblem::Absent));
        assert_eq!(e.text("preset_volume_l"), Err(FieldProblem::NotText));
        assert_eq!(e.instant("created_at"), Err(FieldProblem::NotAnInstant));
    }

    #[test]
    fn the_events_of_a_transfer_spanning_the_window_end_form_one_transaction() {
        let events = vec![
            job_opened("d1", datetime!(2025-01-31 23:50)),
            opened("d1", datetime!(2025-01-31 23:55)),
            closed(
                "d1",
                datetime!(2025-01-31 23:55),
                datetime!(2025-02-01 0:05),
            ),
            bdn_linked("d1", datetime!(2025-02-01 0:06)),
        ];
        let grouping = group(events.clone(), &january_2025());
        assert_eq!(
            grouping.transactions,
            vec![JobRecord {
                job_id: "d1".to_string(),
                assigned_at: datetime!(2025-01-31 23:55),
                events,
            }],
        );
        assert!(grouping.refused_jobs.is_empty());
        assert!(grouping.unassignable.is_empty());
    }

    #[test]
    fn a_transaction_is_assigned_by_the_opened_at_on_its_close() {
        // A fill: no transaction_open.
        let events = vec![
            job_opened("f1", datetime!(2025-01-31 22:50)),
            closed(
                "f1",
                datetime!(2025-01-31 23:00),
                datetime!(2025-02-01 0:30),
            ),
        ];
        let grouping = group(events, &january_2025());
        assert_eq!(ids(&grouping.transactions), ["f1"]);
        assert_eq!(
            grouping.transactions[0].assigned_at,
            datetime!(2025-01-31 23:00)
        );
    }

    #[test]
    fn the_close_decides_where_open_and_close_disagree() {
        let events = vec![
            job_opened("d1", datetime!(2025-01-31 23:50)),
            opened("d1", datetime!(2025-01-31 23:58)),
            closed("d1", datetime!(2025-02-01 0:02), datetime!(2025-02-01 0:10)),
        ];
        assert_eq!(group(events.clone(), &january_2025()), Grouping::default());
        assert_eq!(ids(&group(events, &february_2025()).transactions), ["d1"]);
    }

    #[test]
    fn a_dispense_without_a_close_is_assigned_by_its_open() {
        let events = vec![
            job_opened("d1", datetime!(2025-01-31 10:00)),
            opened("d1", datetime!(2025-01-31 10:01)),
        ];
        let grouping = group(events, &january_2025());
        assert_eq!(ids(&grouping.transactions), ["d1"]);
        assert_eq!(
            grouping.transactions[0].assigned_at,
            datetime!(2025-01-31 10:01)
        );
    }

    #[test]
    fn a_job_that_never_began_a_transfer_is_assigned_by_its_creation() {
        let grouping = group(
            vec![job_opened("r1", datetime!(2025-01-31 12:00))],
            &january_2025(),
        );
        assert!(grouping.transactions.is_empty());
        assert_eq!(ids(&grouping.refused_jobs), ["r1"]);
        assert_eq!(
            grouping.refused_jobs[0].assigned_at,
            datetime!(2025-01-31 12:00)
        );
    }

    #[test]
    fn a_cancelled_job_is_a_refused_job_assigned_by_its_creation() {
        let events = vec![
            job_opened("r1", datetime!(2025-01-31 23:58)),
            job_cancelled("r1", datetime!(2025-02-01 0:01)),
        ];
        let january = group(events.clone(), &january_2025());
        assert!(january.transactions.is_empty());
        assert!(january.unassignable.is_empty());
        assert_eq!(ids(&january.refused_jobs), ["r1"]);
        assert_eq!(
            january.refused_jobs[0].assigned_at,
            datetime!(2025-01-31 23:58)
        );
        assert_eq!(group(events, &february_2025()), Grouping::default());
    }

    #[test]
    fn a_cancel_without_its_job_opened_is_unassignable() {
        let grouping = group(
            vec![job_cancelled("c1", datetime!(2025-01-31 12:00))],
            &january_2025(),
        );
        assert!(grouping.refused_jobs.is_empty());
        assert_eq!(grouping.unassignable.len(), 1);
    }

    #[test]
    fn creation_never_assigns_a_transfer() {
        let events = vec![
            job_opened("d1", datetime!(2025-01-31 23:59:30)),
            opened("d1", datetime!(2025-02-01 0:00)),
            closed("d1", datetime!(2025-02-01 0:00), datetime!(2025-02-01 0:05)),
        ];
        assert_eq!(group(events.clone(), &january_2025()), Grouping::default());
        assert_eq!(ids(&group(events, &february_2025()).transactions), ["d1"]);
    }

    #[test]
    fn a_record_that_cannot_be_assigned_is_reported_once() {
        let without_job = event(json!({
            "_time": text(datetime!(2025-01-31 12:00)), "event_type": "transaction_closed",
        }));
        let lone_link = bdn_linked("b1", datetime!(2025-01-31 13:00));
        let later_link = bdn_linked("b2", datetime!(2025-02-01 0:30));
        let events = vec![without_job.clone(), lone_link.clone(), later_link.clone()];

        let january = group(events.clone(), &january_2025());
        assert_eq!(
            january.unassignable,
            vec![vec![without_job], vec![lone_link]]
        );
        assert!(january.transactions.is_empty());

        let february = group(events, &february_2025());
        assert_eq!(february.unassignable, vec![vec![later_link]]);
        assert!(!FindingKind::UnassignableRecord.is_fatal());
    }

    #[test]
    fn a_transaction_outside_the_window_is_discarded_after_grouping_in_module() {
        let events = vec![
            opened("before", datetime!(2024-12-31 22:00)),
            closed(
                "before",
                datetime!(2024-12-31 22:00),
                datetime!(2024-12-31 22:10),
            ),
            opened("within", datetime!(2025-01-15 9:00)),
            closed(
                "within",
                datetime!(2025-01-15 9:00),
                datetime!(2025-01-15 9:10),
            ),
            opened("after", datetime!(2025-02-01 3:00)),
            closed(
                "after",
                datetime!(2025-02-01 3:00),
                datetime!(2025-02-01 3:10),
            ),
        ];
        let grouping = group(events, &january_2025());
        assert_eq!(ids(&grouping.transactions), ["within"]);
    }
}

/// Pairing and checks (sim-tests.md, "Pairing and checks"), with the two
/// "Audit types" tests whose subjects these extend: the retrieval bounds, now
/// reaching back one session lifetime, and the dispositions, now three fatal.
#[cfg(test)]
mod check_tests {
    use std::time::Duration;

    use serde_json::{json, Value};
    use time::macros::datetime;
    use time::PrimitiveDateTime;

    use super::fixtures::{dispense, dispense_pulses, fill, login, number, pulses, record, text};
    use super::*;
    use crate::actors::dock_operator::BDN;
    use crate::actors::road_tanker_driver::DeliveryTicket;
    use crate::actors::tug_engineer::VesselId;
    use crate::constants::SESSION_TIMEOUT_S;

    const DISPENSE_AT: PrimitiveDateTime = datetime!(2025-01-15 9:00);
    const FILL_AT: PrimitiveDateTime = datetime!(2025-01-20 10:00);
    const SECOND_DISPENSE_AT: PrimitiveDateTime = datetime!(2025-01-25 9:00);
    const SECOND_FILL_AT: PrimitiveDateTime = datetime!(2025-01-28 10:00);

    /// The first dispense's opening totaliser reading, in pulses.
    const TOTALISER_START: u64 = 100_000;

    fn january_2025() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-01-01 0:00),
            end: datetime!(2025-02-01 0:00),
        }
    }

    fn card_change(at: PrimitiveDateTime) -> Event {
        Event(
            json!({
                "_time": text(at), "_msg": "card change", "msg_id": "CARD-CHANGE",
                "app_name": "fms", "hostname": "fms-01", "fms@99999.card_id": "c-1",
                "fms@99999.card_number": "67826", "fms@99999.label": "1999997",
                "fms@99999.action": "enrol",
            })
            .as_object()
            .unwrap()
            .clone(),
        )
    }

    /// One window's paper artefacts and retrieved records.
    struct Audit {
        artefacts: Vec<ReconciliationRecord>,
        records: Vec<Event>,
    }

    impl Audit {
        /// A dispense and a fill, each with its paper and its session's `LOGIN`.
        fn clean() -> Audit {
            let mut records = vec![
                login("s1", DISPENSE_AT - time::Duration::minutes(10)),
                login("s2", FILL_AT - time::Duration::minutes(10)),
            ];
            records.extend(dispense(
                "d1",
                "BDN-000001",
                "s1",
                DISPENSE_AT,
                TOTALISER_START,
            ));
            records.extend(fill("f1", "DT-000001", "s2", FILL_AT));
            Audit {
                artefacts: vec![
                    fixtures::bdn("BDN-000001", DISPENSE_AT),
                    fixtures::ticket("DT-000001", FILL_AT),
                ],
                records,
            }
        }

        /// Add a second dispense whose totaliser opens at `start`.
        fn with_second_dispense(mut self, start: u64) -> Audit {
            self.records.push(login(
                "s3",
                SECOND_DISPENSE_AT - time::Duration::minutes(10),
            ));
            self.records.extend(dispense(
                "d2",
                "BDN-000002",
                "s3",
                SECOND_DISPENSE_AT,
                start,
            ));
            self.artefacts
                .push(fixtures::bdn("BDN-000002", SECOND_DISPENSE_AT));
            self
        }

        /// The kinds of finding, in a fixed order so that a comparison does not
        /// depend on the order the checks run in.
        fn findings(self) -> Vec<FindingKind> {
            let mut kinds: Vec<FindingKind> =
                reconcile(self.artefacts, self.records, &january_2025())
                    .findings
                    .into_iter()
                    .map(|finding| finding.kind)
                    .collect();
            kinds.sort_by_key(|kind| format!("{kind:?}"));
            kinds
        }

        /// Set `field` on every event of `job` of `event_type`, or of every
        /// type for `None`.
        fn set(&mut self, job: &str, event_type: Option<&str>, field: &str, value: Value) {
            for event in self.events_of(job, event_type) {
                event.0.insert(field.to_string(), value.clone());
            }
        }

        fn remove(&mut self, job: &str, event_type: Option<&str>) {
            self.records.retain(|event| {
                !(event.text("job_id") == Ok(job) && event_type.is_none_or(|t| event.is(t)))
            });
        }

        fn events_of<'a>(
            &'a mut self,
            job: &'a str,
            event_type: Option<&'a str>,
        ) -> impl Iterator<Item = &'a mut Event> {
            self.records.iter_mut().filter(move |event| {
                event.text("job_id") == Ok(job) && event_type.is_none_or(|t| event.is(t))
            })
        }

        fn bdn(&mut self, number: &str) -> &mut BDN {
            self.artefacts
                .iter_mut()
                .find_map(|artefact| match artefact {
                    ReconciliationRecord::Bdn(bdn) if bdn.bdn_number == number => Some(bdn),
                    _ => None,
                })
                .expect("no such BDN")
        }

        fn ticket(&mut self, number: &str) -> &mut DeliveryTicket {
            self.artefacts
                .iter_mut()
                .find_map(|artefact| match artefact {
                    ReconciliationRecord::DeliveryTicket(ticket)
                        if ticket.ticket_number == number =>
                    {
                        Some(ticket)
                    }
                    _ => None,
                })
                .expect("no such ticket")
        }
    }

    /// What a test alters on a paper artefact, and how.
    type Alteration<T> = (&'static str, fn(&mut T));

    /// At least one finding, every one of `kind`.
    fn assert_only(findings: &[FindingKind], kind: FindingKind, altered: &str) {
        assert!(
            !findings.is_empty() && findings.iter().all(|found| *found == kind),
            "altering the {altered} returned {findings:?}, not only {kind:?}"
        );
    }

    #[test]
    fn the_clean_window_yields_no_finding() {
        let audit = Audit::clean().with_second_dispense(TOTALISER_START + dispense_pulses());
        assert_eq!(audit.findings(), []);
    }

    // --- Pairing ---------------------------------------------------------------

    #[test]
    fn a_bdn_pairs_with_the_dispense_that_links_its_number() {
        // Posted in the reverse of the dispenses' order: paired by number, not
        // by position. Paired with the other dispense, each BDN's commencement
        // would disagree with its record.
        let mut audit = Audit::clean().with_second_dispense(TOTALISER_START + dispense_pulses());
        audit.artefacts.reverse();
        assert_eq!(audit.findings(), []);
    }

    #[test]
    fn a_ticket_pairs_with_the_fill_that_closes_with_its_number() {
        let mut audit = Audit::clean();
        audit
            .records
            .push(login("s4", SECOND_FILL_AT - time::Duration::minutes(10)));
        audit
            .records
            .extend(fill("f2", "DT-000002", "s4", SECOND_FILL_AT));
        audit
            .artefacts
            .insert(0, fixtures::ticket("DT-000002", SECOND_FILL_AT));
        assert_eq!(audit.findings(), []);
    }

    #[test]
    fn a_dispense_whose_link_was_lost_pairs_by_content() {
        let mut audit = Audit::clean();
        audit.remove("d1", Some("bdn_linked"));
        assert_eq!(audit.findings(), [FindingKind::LostLink]);

        // The pair is checked like any other.
        let mut audit = Audit::clean();
        audit.remove("d1", Some("bdn_linked"));
        audit.bdn("BDN-000001").grade = "DMA".to_string();
        assert_eq!(
            audit.findings(),
            [FindingKind::LostLink, FindingKind::RecordDisagreesWithPaper]
        );
        assert!(!FindingKind::LostLink.is_fatal());
    }

    #[test]
    fn content_pairing_requires_the_exact_commencement() {
        let mut audit = Audit::clean();
        audit.remove("d1", Some("bdn_linked"));
        audit.bdn("BDN-000001").delivery_commenced_at += time::Duration::nanoseconds(1);
        assert_eq!(
            audit.findings(),
            [
                FindingKind::PaperWithoutRecord,
                FindingKind::RecordWithoutPaper
            ]
        );
    }

    #[test]
    fn a_link_naming_an_undrained_document_is_a_record_without_paper() {
        let mut audit = Audit::clean();
        audit.set("d1", Some("bdn_linked"), "bdn_number", json!("BDN-999999"));
        assert_eq!(
            audit.findings(),
            [
                FindingKind::PaperWithoutRecord,
                FindingKind::RecordWithoutPaper
            ]
        );
    }

    #[test]
    fn a_dispense_that_never_completed_needs_no_paper() {
        let mut audit = Audit::clean();
        audit.remove("d1", Some("transaction_closed"));
        audit.remove("d1", Some("bdn_linked"));
        audit
            .artefacts
            .retain(|artefact| !matches!(artefact, ReconciliationRecord::Bdn(_)));
        assert_eq!(
            audit.findings(),
            [FindingKind::RecordInconsistency(
                Inconsistency::OpenWithoutClose
            )]
        );
    }

    #[test]
    fn an_artefact_with_no_transaction_is_reported() {
        let mut audit = Audit::clean();
        audit.remove("d1", None);
        audit.remove("f1", None);
        assert_eq!(
            audit.findings(),
            [
                FindingKind::PaperWithoutRecord,
                FindingKind::PaperWithoutRecord
            ]
        );
        assert!(!FindingKind::PaperWithoutRecord.is_fatal());
    }

    // --- Checks ----------------------------------------------------------------

    #[test]
    fn every_copied_bdn_field_must_equal_the_record() {
        let alterations: [Alteration<BDN>; 7] = [
            ("vessel", |bdn| bdn.vessel_id = VesselId(1999998)),
            ("grade", |bdn| bdn.grade = "DMA".to_string()),
            ("operator", |bdn| bdn.operator_id = "mtate".to_string()),
            ("commencement", |bdn| {
                bdn.delivery_commenced_at += time::Duration::nanoseconds(1)
            }),
            ("temperature", |bdn| bdn.temperature_c = 15.5),
            // In kg/m³ on the BDN, against kg/L × 1000 on the record.
            ("density", |bdn| bdn.density_kg_per_m3 = 841.0),
            ("corrected volume", |bdn| {
                bdn.temp_corrected_volume_l = 500.5
            }),
        ];
        for (altered, alter) in alterations {
            let mut audit = Audit::clean();
            alter(audit.bdn("BDN-000001"));
            assert_only(
                &audit.findings(),
                FindingKind::RecordDisagreesWithPaper,
                altered,
            );
        }
        assert!(FindingKind::RecordDisagreesWithPaper.is_fatal());
    }

    #[test]
    fn every_transcribed_ticket_field_must_equal_the_record() {
        let alterations: [Alteration<DeliveryTicket>; 7] = [
            ("commencement", |ticket| {
                ticket.delivery_commenced_at += time::Duration::nanoseconds(1)
            }),
            ("grade", |ticket| ticket.grade = "DMA".to_string()),
            ("delivered volume", |ticket| {
                ticket.gross_volume_l = 18_001.0
            }),
            ("temperature", |ticket| ticket.temperature_c = 15.5),
            ("density", |ticket| ticket.density_kg_per_l = 0.841),
            ("corrected volume", |ticket| {
                ticket.temp_corrected_volume_l = 18_001.0
            }),
            ("tanker registration", |ticket| {
                ticket.tanker_registration = "XY98 ZYX".to_string()
            }),
        ];
        for (altered, alter) in alterations {
            let mut audit = Audit::clean();
            alter(audit.ticket("DT-000001"));
            assert_only(
                &audit.findings(),
                FindingKind::RecordDisagreesWithPaper,
                altered,
            );
        }

        // The job authorised a tanker other than the one delivering.
        let mut audit = Audit::clean();
        audit.set("f1", None, "counterparty", json!("XY98 ZYX"));
        assert_only(
            &audit.findings(),
            FindingKind::RecordDisagreesWithPaper,
            "fill's counterparty",
        );
    }

    #[test]
    fn the_bdn_volume_is_within_half_a_percent_of_the_totaliser_delta() {
        // The delta is 500 L: (end - start) pulses / the compiled K-factor.
        for within in [502.5, 497.5] {
            let mut audit = Audit::clean();
            audit.bdn("BDN-000001").volume_l = within;
            assert_eq!(audit.findings(), [], "BDN volume {within} L");
        }
        for beyond in [502.6, 497.4] {
            let mut audit = Audit::clean();
            audit.bdn("BDN-000001").volume_l = beyond;
            assert_eq!(
                audit.findings(),
                [FindingKind::RecordDisagreesWithPaper],
                "BDN volume {beyond} L"
            );
        }
    }

    #[test]
    fn the_atg_delta_is_compared_with_the_meter_within_one_percent() {
        let inconsistency = FindingKind::RecordInconsistency(Inconsistency::AtgAgainstMeter);
        // The dispense drew 500 L corrected, from 35,000 L; the fill delivered
        // 18,000 L corrected, onto 16,000 L.
        for (job, close, consistent) in [
            ("d1", 34_495.0, true),
            ("d1", 34_494.9, false),
            ("f1", 34_180.0, true),
            ("f1", 34_180.1, false),
        ] {
            let mut audit = Audit::clean();
            audit.set(
                job,
                Some("transaction_closed"),
                "atg_volume_at_close_l",
                number(close),
            );
            let expected: &[FindingKind] = if consistent { &[] } else { &[inconsistency] };
            assert_eq!(
                audit.findings(),
                expected,
                "{job} closing ATG volume {close} L"
            );
        }
        assert!(!inconsistency.is_fatal());
    }

    #[test]
    fn the_preset_is_compared_with_the_delivered_volume_within_half_a_percent() {
        let inconsistency = FindingKind::RecordInconsistency(Inconsistency::PresetAgainstDelivered);
        for (delivered, consistent) in [(502.5, true), (502.6, false)] {
            let mut audit = Audit::clean();
            audit.set(
                "d1",
                Some("transaction_closed"),
                "volume_dispensed_l",
                number(delivered),
            );
            let expected: &[FindingKind] = if consistent { &[] } else { &[inconsistency] };
            assert_eq!(audit.findings(), expected, "delivered {delivered} L");
        }
    }

    #[test]
    fn a_totaliser_decrease_is_fatal() {
        // Within a transaction.
        let mut audit = Audit::clean();
        audit.set(
            "d1",
            Some("transaction_closed"),
            "end_totaliser_pulses",
            pulses(TOTALISER_START - 1),
        );
        assert!(audit.findings().contains(&FindingKind::TotaliserDecrease));

        // From one transaction's end to the next one's start.
        let audit = Audit::clean().with_second_dispense(TOTALISER_START + dispense_pulses() - 1);
        assert_eq!(audit.findings(), [FindingKind::TotaliserDecrease]);
        assert!(FindingKind::TotaliserDecrease.is_fatal());
    }

    #[test]
    fn a_totaliser_gap_is_reported() {
        let audit = Audit::clean().with_second_dispense(TOTALISER_START + dispense_pulses() + 1);
        assert_eq!(
            audit.findings(),
            [FindingKind::RecordInconsistency(
                Inconsistency::TotaliserGap
            )]
        );
    }

    #[test]
    fn overlapping_transactions_are_reported() {
        // The fill commences during the dispense, which runs for two minutes.
        let overlapping = DISPENSE_AT + time::Duration::minutes(1);
        let mut audit = Audit::clean();
        audit.remove("f1", None);
        audit
            .records
            .extend(fill("f1", "DT-000001", "s2", overlapping));
        audit.ticket("DT-000001").delivery_commenced_at = overlapping;
        assert_eq!(
            audit.findings(),
            [FindingKind::RecordInconsistency(
                Inconsistency::ConcurrentTransactions
            )]
        );
    }

    #[test]
    fn a_session_resolves_to_a_login_under_its_prefixed_name() {
        let is_s1_login = |event: &Event| event.text("fms@99999.session_id") == Ok("s1");

        let mut audit = Audit::clean();
        audit.records.retain(|event| !is_s1_login(event));
        assert_eq!(audit.findings(), [FindingKind::MissingLogin]);

        // Addressed by the unprefixed name, the LOGIN matches nothing.
        let mut audit = Audit::clean();
        for event in audit.records.iter_mut().filter(|event| is_s1_login(event)) {
            let session = event.0.remove("fms@99999.session_id").unwrap();
            event.0.insert("session_id".to_string(), session);
        }
        assert_eq!(audit.findings(), [FindingKind::MissingLogin]);
        assert!(!FindingKind::MissingLogin.is_fatal());
    }

    #[test]
    fn an_unreadable_field_is_reported() {
        let mut audit = Audit::clean();
        for event in audit.events_of("d1", Some("transaction_closed")) {
            event.0.remove("atg_volume_at_close_l");
        }
        assert_eq!(audit.findings(), [FindingKind::UnreadableField]);

        let mut audit = Audit::clean();
        audit.set(
            "d1",
            Some("transaction_closed"),
            "temperature_c",
            json!("warm"),
        );
        assert_eq!(audit.findings(), [FindingKind::UnreadableField]);
        assert!(!FindingKind::UnreadableField.is_fatal());
    }

    #[test]
    fn card_table_changes_in_the_window_are_reported_as_context() {
        let mut audit = Audit::clean();
        audit.records.push(card_change(datetime!(2025-01-10 12:00)));
        assert_eq!(audit.findings(), [FindingKind::CardTableChange]);

        let mut audit = Audit::clean();
        audit.records.push(card_change(datetime!(2024-12-31 23:00)));
        assert_eq!(audit.findings(), []);
        assert!(!FindingKind::CardTableChange.is_fatal());
    }

    // --- Audit types, extended ---------------------------------------------------

    #[test]
    fn the_retrieval_bounds_widen_the_window_by_the_margin_and_the_session_lifetime() {
        let window = january_2025();
        let lifetime = Duration::from_secs(SESSION_TIMEOUT_S);
        assert_eq!(
            window.retrieval_bounds(Duration::from_secs(6 * 60 * 60)),
            (
                datetime!(2024-12-31 18:00) - lifetime,
                datetime!(2025-02-01 6:00)
            ),
        );
        assert_eq!(window, january_2025());
    }

    #[test]
    fn only_a_written_disagreement_or_an_unbacked_record_is_fatal() {
        assert!(FindingKind::RecordDisagreesWithPaper.is_fatal());
        assert!(FindingKind::RecordWithoutPaper.is_fatal());
        assert!(FindingKind::TotaliserDecrease.is_fatal());
        assert!(!FindingKind::PaperWithoutRecord.is_fatal());
        assert!(!FindingKind::LostLink.is_fatal());
        for inconsistency in [
            Inconsistency::AtgAgainstMeter,
            Inconsistency::PresetAgainstDelivered,
            Inconsistency::OpenWithoutClose,
            Inconsistency::TotaliserGap,
            Inconsistency::ConcurrentTransactions,
        ] {
            assert!(!FindingKind::RecordInconsistency(inconsistency).is_fatal());
        }
        assert!(!FindingKind::UnreadableField.is_fatal());
        assert!(!FindingKind::UnassignableRecord.is_fatal());
        assert!(!FindingKind::MissingLogin.is_fatal());
        assert!(!FindingKind::CardTableChange.is_fatal());
    }

    /// Outcome and report (sim-tests.md, "Outcome and report"), built on the
    /// fixtures above.
    mod outcome_tests {
        use super::*;

        /// The retrievals the window is taken to have needed.
        const RETRIEVALS: usize = 3;

        impl Audit {
            fn reconcile(self) -> Reconciliation {
                reconcile(self.artefacts, self.records, &january_2025())
            }
        }

        /// Five dispenses in commencement order, 5 to 25 January, the
        /// totaliser continuous across them, each with its BDN.
        fn five_dispenses() -> Audit {
            let mut audit = Audit {
                artefacts: Vec::new(),
                records: vec![login("s1", datetime!(2025-01-01 0:30))],
            };
            for i in 0..5_u64 {
                let at = datetime!(2025-01-05 9:00) + time::Duration::days(5 * i as i64);
                let job = format!("d{}", i + 1);
                let bdn = format!("BDN-00000{}", i + 1);
                let start = TOTALISER_START + i * dispense_pulses();
                audit.records.extend(dispense(&job, &bdn, "s1", at, start));
                audit.artefacts.push(fixtures::bdn(&bdn, at));
            }
            audit
        }

        /// A job the FMS refused: a `job_opened` alone.
        fn refused_job(job: &str, at: PrimitiveDateTime) -> Event {
            let envelope = json!({
                "job_id": job, "type": "dispense", "operator": "jrenard",
                "session_id": "s1", "counterparty": "1999997", "grade": "MGO",
            });
            record(
                &envelope,
                json!({
                    "_time": text(at), "event_type": "job_opened", "created_at": text(at),
                    "atg_volume_at_open_l": number(4_000.0),
                }),
            )
        }

        fn report(outcome: AuditOutcome) -> AuditReport {
            match outcome {
                AuditOutcome::Clean(report) | AuditOutcome::Findings(report) => report,
                AuditOutcome::Fatal(message) => panic!("fatal: {message}"),
            }
        }

        /// The subjects the report names for `kind`.
        fn subjects(report: &AuditReport, kind: FindingKind) -> Vec<&str> {
            report
                .findings
                .iter()
                .find(|(found, _)| *found == kind)
                .map(|(_, subjects)| subjects.iter().map(String::as_str).collect())
                .unwrap_or_default()
        }

        fn assert_close(actual: Option<f64>, expected: f64, margin: &str) {
            let actual = actual.unwrap_or_else(|| panic!("no {margin} margin"));
            assert!(
                (actual - expected).abs() < 1e-9,
                "{margin} margin {actual}, expected {expected}"
            );
        }

        #[test]
        fn a_clean_audit_reports_its_counts_and_margins() {
            let mut audit = Audit::clean();
            audit
                .records
                .push(refused_job("r1", datetime!(2025-01-12 9:00)));
            let records = audit.records.len();

            let AuditOutcome::Clean(report) = outcome(audit.reconcile(), RETRIEVALS) else {
                panic!("a clean window is a clean outcome");
            };
            assert_eq!(report.window, january_2025());
            assert_eq!(report.artefacts_reconciled, 2);
            assert_eq!(report.records_retrieved, records);
            assert_eq!(report.retrievals, RETRIEVALS);
            assert_eq!(report.refused_jobs, 1);
            assert!(report.findings.is_empty(), "{:?}", report.findings);
            assert_close(report.margins.bdn_volume, 0.0, "BDN volume");
            assert_close(report.margins.atg, 0.0, "ATG");
            assert_close(report.margins.preset, 0.0, "preset");
        }

        #[test]
        fn a_margin_is_the_largest_deviation_as_a_fraction_of_its_tolerance() {
            // 1 L and 2 L from deltas of 500 L, against a tolerance of 2.5 L.
            let mut audit =
                Audit::clean().with_second_dispense(TOTALISER_START + dispense_pulses());
            audit.bdn("BDN-000001").volume_l = 501.0;
            audit.bdn("BDN-000002").volume_l = 502.0;
            // 5 L from 500 L, against a tolerance of 5 L.
            audit.set(
                "d1",
                Some("transaction_closed"),
                "atg_volume_at_close_l",
                number(34_495.0),
            );
            let margins = audit.reconcile().margins;
            assert_close(margins.bdn_volume, 0.8, "BDN volume");
            assert_close(margins.atg, 1.0, "ATG");

            // A fill alone: no dispense to measure the BDN volume or preset by.
            let mut audit = Audit::clean();
            audit.remove("d1", None);
            audit
                .artefacts
                .retain(|artefact| !matches!(artefact, ReconciliationRecord::Bdn(_)));
            let margins = audit.reconcile().margins;
            assert_eq!(margins.bdn_volume, None);
            assert_eq!(margins.preset, None);
            assert_close(margins.atg, 0.0, "ATG");
        }

        #[test]
        fn reported_findings_make_one_summary_naming_each_subject() {
            let mut audit = Audit::clean();
            audit
                .records
                .retain(|event| event.text("fms@99999.session_id") != Ok("s1"));
            audit.records.push(card_change(datetime!(2025-01-10 12:00)));
            let mut second = card_change(datetime!(2025-01-11 12:00));
            second
                .0
                .insert("fms@99999.card_number".to_string(), json!("67827"));
            audit.records.push(second);

            let AuditOutcome::Findings(report) = outcome(audit.reconcile(), RETRIEVALS) else {
                panic!("reported findings are a findings outcome");
            };
            assert_eq!(report.findings.len(), 2, "{:?}", report.findings);
            assert_eq!(subjects(&report, FindingKind::MissingLogin), ["d1"]);
            assert_eq!(
                subjects(&report, FindingKind::CardTableChange),
                ["67826", "67827"]
            );
            assert_eq!(report.window, january_2025());
            assert!(report.margins.bdn_volume.is_some());
        }

        #[test]
        fn unmatched_artefacts_are_summarised_as_runs() {
            let mut audit = five_dispenses();
            for job in ["d2", "d4", "d5"] {
                audit.remove(job, None);
            }
            let some = report(outcome(audit.reconcile(), RETRIEVALS));
            assert_eq!(subjects(&some, FindingKind::PaperWithoutRecord).len(), 3);
            assert_eq!(some.unmatched_runs, 2);
            assert_eq!(some.longest_unmatched_run, 2);

            let mut audit = five_dispenses();
            for job in ["d1", "d2", "d3", "d4", "d5"] {
                audit.remove(job, None);
            }
            let all = report(outcome(audit.reconcile(), RETRIEVALS));
            assert_eq!(all.unmatched_runs, 1);
            assert_eq!(all.longest_unmatched_run, 5);
        }

        #[test]
        fn a_fatal_finding_makes_the_outcome_fatal() {
            let mut audit = Audit::clean();
            audit.bdn("BDN-000001").grade = "DMA".to_string();
            audit
                .artefacts
                .retain(|artefact| !matches!(artefact, ReconciliationRecord::DeliveryTicket(_)));
            audit
                .records
                .retain(|event| event.text("fms@99999.session_id") != Ok("s1"));

            let AuditOutcome::Fatal(message) = outcome(audit.reconcile(), RETRIEVALS) else {
                panic!("a disagreement and a record with no paper are fatal");
            };
            for expected in [
                january_2025().start.to_string().as_str(),
                "BDN-000001",
                "f1",
                "1 MissingLogin",
            ] {
                assert!(
                    message.contains(expected),
                    "{expected:?} not in {message:?}"
                );
            }

            // Reported findings alone are never fatal.
            let mut audit = Audit::clean();
            audit
                .records
                .retain(|event| event.text("fms@99999.session_id") != Ok("s1"));
            assert!(matches!(
                outcome(audit.reconcile(), RETRIEVALS),
                AuditOutcome::Findings(_)
            ));
        }

        /// Summary entry (sim-tests.md, "Summary entry").
        mod summary_tests {
            use super::*;

            /// The January window, as the entry writes it.
            const PERIOD: &str = "2025-01-01T00:00:00Z 2025-02-01T00:00:00Z";

            fn entry(audit: Audit) -> Option<String> {
                summary_entry(&outcome(audit.reconcile(), RETRIEVALS))
            }

            /// The BDN paired by content, its link lost; the ticket with no
            /// record.
            fn lost_link_and_unrecorded_ticket() -> Audit {
                let mut audit = Audit::clean();
                audit.remove("d1", Some("bdn_linked"));
                audit.remove("f1", None);
                audit
            }

            fn missing_login() -> Audit {
                let mut audit = Audit::clean();
                audit
                    .records
                    .retain(|event| event.text("fms@99999.session_id") != Ok("s1"));
                audit
            }

            fn card_change_alone() -> Audit {
                let mut audit = Audit::clean();
                audit.records.push(card_change(datetime!(2025-01-10 12:00)));
                audit
            }

            #[test]
            fn a_clean_audit_summary_states_the_period_and_agreement() {
                assert_eq!(entry(Audit::clean()), Some(format!("{PERIOD} agreed")));
            }

            #[test]
            fn a_summary_names_the_documents_that_did_not_agree() {
                assert_eq!(
                    entry(lost_link_and_unrecorded_ticket()),
                    Some(format!("{PERIOD} disagreed BDN-000001 DT-000001"))
                );
            }

            #[test]
            fn a_finding_without_a_document_names_nothing() {
                assert_eq!(entry(missing_login()), Some(format!("{PERIOD} disagreed")));
            }

            #[test]
            fn a_card_table_change_alone_leaves_the_records_agreed() {
                assert_eq!(entry(card_change_alone()), Some(format!("{PERIOD} agreed")));
            }

            #[test]
            fn a_fatal_audit_writes_no_summary() {
                let mut audit = Audit::clean();
                audit.bdn("BDN-000001").grade = "DMA".to_string();
                assert_eq!(entry(audit), None);
            }

            #[test]
            fn a_summary_is_one_line_of_characters_a_shell_passes_unchanged() {
                for audit in [
                    Audit::clean(),
                    lost_link_and_unrecorded_ticket(),
                    missing_login(),
                    card_change_alone(),
                ] {
                    let line = entry(audit).expect("not fatal, so an entry");
                    assert!(
                        line.chars()
                            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | ':' | ' ')),
                        "{line:?}"
                    );
                }
            }
        }
    }
}
