//! The audit loop (sim-impl.md, "The audit loop"): one window after another,
//! each audited `settling_delay` after it ends, its paper artefacts drained and
//! partitioned, the audit conducted through a [`Connector`], and the result
//! emitted.

use std::time::Duration;

use time::format_description::well_known::Rfc3339;
use time::PrimitiveDateTime;
use tokio::sync::mpsc::error::TryRecvError;
use tokio::sync::{mpsc, oneshot};

use super::audit::{conduct, AuditPlan, Conducted, Connector};
use super::{
    outcome, partition, reconcile, AuditOutcome, AuditPeriod, AuditWindow, ReconciliationRecord,
    ReconciliationUserState,
};
use crate::clock::SimClock;
use crate::stop::{after_peer, rest, sleep_until, StopRequest};

const ENTITY: &str = "reconciliation_user";

/// The loop's configuration (sim-impl.md, "The audit loop").
#[derive(Debug, Clone, Copy)]
pub(super) struct LoopSettings {
    pub(super) audit_period: AuditPeriod,
    pub(super) settling_delay: Duration,
    pub(super) boundary_margin: Duration,
    pub(super) initial_window: Duration,
    /// From the actor's spawn to its first audit, for an audit period in
    /// hours; `None` for a calendar period.
    pub(super) first_audit_after: Option<Duration>,
    pub(super) width_floor: Duration,
}

/// When each audit runs and the window it covers (sim-impl.md, "Cadence and
/// window"). A calendar period audits each window one settling delay after it
/// ends. A period in hours has its boundaries fixed by the first audit, which
/// the start state places, and each later audit runs one period after the one
/// before.
#[derive(Debug, Clone, Copy)]
enum Schedule {
    Calendar {
        period: AuditPeriod,
        window: AuditWindow,
        settling_delay: Duration,
    },
    Interval {
        period: Duration,
        spawned_at: PrimitiveDateTime,
        audit_at: PrimitiveDateTime,
        settling_delay: Duration,
    },
}

impl Schedule {
    fn start(settings: &LoopSettings, spawned_at: PrimitiveDateTime) -> Schedule {
        match settings.audit_period {
            AuditPeriod::Interval(period) => Schedule::Interval {
                period,
                spawned_at,
                audit_at: spawned_at
                    + settings
                        .first_audit_after
                        .expect("spawn checks that a period in hours has a first audit"),
                settling_delay: settings.settling_delay,
            },
            calendar => Schedule::Calendar {
                period: calendar,
                window: calendar.first_window(spawned_at),
                settling_delay: settings.settling_delay,
            },
        }
    }

    /// The window the next audit covers, and when that audit runs.
    fn current(&self) -> (AuditWindow, PrimitiveDateTime) {
        match *self {
            Schedule::Calendar {
                window,
                settling_delay,
                ..
            } => (window, window.end + settling_delay),
            Schedule::Interval {
                period,
                spawned_at,
                audit_at,
                settling_delay,
            } => {
                // The window ends one settling delay before its audit. The part
                // of a window before the spawn instant is cut away, since the
                // paper from before the sim started is not held, and `spawn`
                // refuses a first window that ends at or before that instant.
                let end = audit_at - settling_delay;
                let start = end - period;
                let window = AuditWindow {
                    start: start.max(spawned_at),
                    end,
                };
                (window, audit_at)
            }
        }
    }

    /// Move to the following audit.
    fn advance(&mut self) {
        match self {
            Schedule::Calendar { period, window, .. } => *window = period.next(window),
            Schedule::Interval {
                period, audit_at, ..
            } => *audit_at += *period,
        }
    }

    /// The schedule of a warm start, whose first window is the saved window
    /// (sim-impl.md, "Stopping and warm start"). A calendar period continues
    /// with the calendar windows after it. A period in hours audits the saved
    /// window one settling delay after it ends, whole, since its artefacts were
    /// saved with it, and each later window follows it without a gap, one
    /// period long.
    fn resume(settings: &LoopSettings, saved: AuditWindow) -> Schedule {
        match settings.audit_period {
            AuditPeriod::Interval(period) => Schedule::Interval {
                period,
                spawned_at: saved.start,
                audit_at: saved.end + settings.settling_delay,
                settling_delay: settings.settling_delay,
            },
            calendar => Schedule::Calendar {
                period: calendar,
                window: saved,
                settling_delay: settings.settling_delay,
            },
        }
    }
}

/// Where the loop starts and how it comes to rest (sim-impl.md, "Stopping and
/// warm start").
pub(super) struct Lifecycle {
    /// The saved window and artefacts on a warm start; `None` on a cold start.
    pub(super) start: Option<ReconciliationUserState>,
    pub(super) stop: StopRequest,
    pub(super) dock_operator_at_rest: oneshot::Receiver<()>,
    pub(super) report: oneshot::Sender<ReconciliationUserState>,
}

/// Audit one window after another until the sim is stopped. Panics on a fatal
/// outcome or a failure the budgets do not absorb. After a stop it begins no
/// further audit; an audit in progress finishes. Once the dock operator has
/// reported it drains the artefacts posted since, reports its window and every
/// artefact held, and never returns.
pub(super) async fn run(
    clock: SimClock,
    mut record_rx: mpsc::UnboundedReceiver<ReconciliationRecord>,
    mut connector: impl Connector,
    settings: LoopSettings,
    lifecycle: Lifecycle,
) {
    let Lifecycle {
        start,
        mut stop,
        dock_operator_at_rest,
        report,
    } = lifecycle;
    // A cold start's schedule begins at the spawn, a warm start's at the saved
    // window. Artefacts dated in a later window than the one being audited are
    // held; a warm start holds the ones saved with its window.
    let (mut schedule, mut held) = match start {
        Some(state) => (
            Schedule::resume(
                &settings,
                AuditWindow {
                    start: state.window_start,
                    end: state.window_end,
                },
            ),
            state.records,
        ),
        None => (Schedule::start(&settings, clock.now_datetime()), Vec::new()),
    };
    loop {
        // Every transfer commencing in the window has completed and posted its
        // paper by the end of the settling delay.
        let (window, audit_at) = schedule.current();
        tokio::select! {
            biased;
            _ = stop.requested() => break,
            _ = sleep_until(&clock, audit_at) => {}
        }
        drain(&mut record_rx, &mut held);
        let (audited, later) = partition(std::mem::take(&mut held), &window);
        held = later;

        let plan = AuditPlan {
            window,
            bounds: window.retrieval_bounds(settings.boundary_margin),
            initial_width: settings.initial_window,
            width_floor: settings.width_floor,
        };
        let conducted = conduct(&clock, &mut connector, &plan, |records, retrievals| {
            outcome(reconcile(audited, records, &window), retrievals)
        })
        .await
        .unwrap_or_else(|message| panic!("{message}"));
        emit(&window, conducted);

        schedule.advance();
    }

    // Every artefact the dock operator will post has been posted once it has
    // reported.
    after_peer(ENTITY, "dock operator", dock_operator_at_rest).await;
    drain(&mut record_rx, &mut held);
    let (window, _) = schedule.current();
    rest(
        ENTITY,
        report,
        ReconciliationUserState {
            window_start: window.start,
            window_end: window.end,
            records: held,
        },
    )
    .await
}

/// Take every artefact posted so far into `held`.
fn drain(
    record_rx: &mut mpsc::UnboundedReceiver<ReconciliationRecord>,
    held: &mut Vec<ReconciliationRecord>,
) {
    loop {
        match record_rx.try_recv() {
            Ok(record) => held.push(record),
            Err(TryRecvError::Empty) => return,
            Err(TryRecvError::Disconnected) => {
                panic!("reconciliation_user: the dock operator's record channel closed")
            }
        }
    }
}

/// Emit an audit's result: one INFO event for a clean audit, one WARN for an
/// audit with findings, a WARN for a summary entry not written, and a `panic!`
/// for a fatal outcome, which the orchestrator logs as the one ERROR.
fn emit(window: &AuditWindow, conducted: Conducted) {
    let window = format!("{} to {}", instant(window.start), instant(window.end));
    match conducted.outcome {
        AuditOutcome::Clean(report) => tracing::info!(
            window,
            artefacts_reconciled = report.artefacts_reconciled,
            records_retrieved = report.records_retrieved,
            retrievals = report.retrievals,
            refused_jobs = report.refused_jobs,
            margins = ?report.margins,
            "reconciliation audit: the records agreed"
        ),
        AuditOutcome::Findings(report) => tracing::warn!(
            window,
            findings = ?report.findings,
            unmatched_runs = report.unmatched_runs,
            longest_unmatched_run = report.longest_unmatched_run,
            artefacts_reconciled = report.artefacts_reconciled,
            records_retrieved = report.records_retrieved,
            retrievals = report.retrievals,
            refused_jobs = report.refused_jobs,
            margins = ?report.margins,
            "reconciliation audit: findings"
        ),
        AuditOutcome::Fatal(message) => panic!("{message}"),
    }
    if !conducted.summary_written {
        tracing::warn!(
            window,
            "reconciliation audit: the summary entry was not confirmed on the workstation"
        );
    }
}

fn instant(at: PrimitiveDateTime) -> String {
    at.assume_utc()
        .format(&Rfc3339)
        .expect("a UTC date-time always formats as RFC 3339")
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::io::Write;
    use std::sync::{Arc, Once};
    use std::{fs, path::Path};

    use time::macros::datetime;
    use time::PrimitiveDateTime;
    use tracing_subscriber::fmt::MakeWriter;

    use super::*;
    use crate::actors::reconciliation_user::internals::fixtures::{
        bdn, dispense, fill, login, ticket,
    };
    use crate::actors::reconciliation_user::internals::retrieval::{retrieval_command, Span};
    use crate::actors::reconciliation_user::internals::session::latch_value;
    use crate::actors::reconciliation_user::internals::workstation_double::{
        Connection, Connections, Double, Log, Recorded,
    };
    use crate::actors::reconciliation_user::internals::{AuditFailure, AuditWindow, Event};
    use crate::clock::TimeModel;
    use crate::constants::SESSION_TIMEOUT_S;
    use crate::stop::{stop_channel, StopSender};

    const HOUR: Duration = Duration::from_secs(3_600);
    const DAY: Duration = Duration::from_secs(86_400);
    const SPAWNED: PrimitiveDateTime = datetime!(2025-01-15 12:00);

    fn settings(audit_period: AuditPeriod) -> LoopSettings {
        LoopSettings {
            audit_period,
            settling_delay: 6 * HOUR,
            boundary_margin: 2 * HOUR,
            initial_window: 31 * DAY,
            first_audit_after: None,
            width_floor: DAY,
        }
    }

    /// A cold-started loop that is never stopped: its stop request has no
    /// sender.
    fn running() -> Lifecycle {
        let (_stop, stop) = stop_channel();
        let (_dock_operator_report, dock_operator_at_rest) = oneshot::channel();
        let (report, _report_rx) = oneshot::channel();
        Lifecycle {
            start: None,
            stop,
            dock_operator_at_rest,
            report,
        }
    }

    /// The test's ends of a loop that is stopped during the test.
    struct Stopping {
        stop: StopSender,
        dock_operator_at_rest: oneshot::Sender<()>,
        report_rx: oneshot::Receiver<ReconciliationUserState>,
    }

    /// A loop starting from `start`, with the ends that stop it.
    fn stopping(start: Option<ReconciliationUserState>) -> (Lifecycle, Stopping) {
        let (stop, stop_rx) = stop_channel();
        let (dock_operator_at_rest, dock_operator_at_rest_rx) = oneshot::channel();
        let (report, report_rx) = oneshot::channel();
        (
            Lifecycle {
                start,
                stop: stop_rx,
                dock_operator_at_rest: dock_operator_at_rest_rx,
                report,
            },
            Stopping {
                stop,
                dock_operator_at_rest,
                report_rx,
            },
        )
    }

    /// The first window of a monthly run spawned at `SPAWNED`, and the next.
    fn first() -> AuditWindow {
        AuditWindow {
            start: SPAWNED,
            end: datetime!(2025-02-01 0:00),
        }
    }

    fn february() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-02-01 0:00),
            end: datetime!(2025-03-01 0:00),
        }
    }

    fn latch(window: &AuditWindow) -> Result<String, AuditFailure> {
        Ok(format!("{}\n", latch_value(window, 1)))
    }

    /// The retrieval's response: the records as newline-delimited JSON, and the
    /// completion line.
    fn response(records: &[Event]) -> Result<String, AuditFailure> {
        let mut text: String = records
            .iter()
            .map(|event| format!("{}\n", serde_json::to_string(&event.0).unwrap()))
            .collect();
        text.push_str("retrieval-end 0\n");
        Ok(text)
    }

    fn entry(window: &AuditWindow, result: &str) -> String {
        let instant = |at: PrimitiveDateTime| {
            at.assume_utc()
                .format(&time::format_description::well_known::Rfc3339)
                .unwrap()
        };
        format!("{} {} {result}", instant(window.start), instant(window.end))
    }

    fn confirmed(entry: &str) -> Result<String, AuditFailure> {
        Ok(format!("{entry}\n"))
    }

    fn append(entry: &str) -> String {
        format!("echo {entry} >> ~/reconciliation-audits.txt")
    }

    /// A clean session of `window` retrieving `records`.
    fn clean_session(window: &AuditWindow, records: &[Event]) -> Connection {
        Connection::Accepted(Double::answering([
            latch(window),
            response(records),
            confirmed(&entry(window, "agreed")),
        ]))
    }

    /// The retrieval bounds of `window` under `settings(Month)`.
    fn bounds(window: &AuditWindow) -> Span {
        Span {
            start: window.start - 2 * HOUR - Duration::from_secs(SESSION_TIMEOUT_S),
            end: window.end + 2 * HOUR,
        }
    }

    fn typed(log: &Log) -> Vec<String> {
        log.lock()
            .unwrap()
            .iter()
            .filter_map(|recorded| match recorded {
                Recorded::Typed(line) => Some(line.clone()),
                _ => None,
            })
            .collect()
    }

    fn connections_made(log: &Log) -> usize {
        log.lock()
            .unwrap()
            .iter()
            .filter(|recorded| **recorded == Recorded::Connected)
            .count()
    }

    fn synthetic() -> (SimClock, tokio::runtime::Runtime) {
        SimClock::isolated(TimeModel::Synthetic, SPAWNED)
    }

    /// Sleep until `instant` on the sim clock.
    async fn until(clock: &SimClock, instant: PrimitiveDateTime) {
        let now = clock.now_datetime();
        clock.sleep((instant - now).unsigned_abs()).await;
    }

    // --- Log capture -----------------------------------------------------------------

    thread_local! {
        /// The formatted log events of the thread they were emitted on.
        static EVENTS: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    }

    /// Writes each event to its own thread's buffer.
    struct ThreadBuffer;

    impl Write for ThreadBuffer {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            EVENTS.with(|events| events.borrow_mut().extend_from_slice(bytes));
            Ok(bytes.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'a> MakeWriter<'a> for ThreadBuffer {
        type Writer = ThreadBuffer;
        fn make_writer(&'a self) -> ThreadBuffer {
            ThreadBuffer
        }
    }

    /// The events this thread has emitted since `capture`.
    struct Captured;

    impl Captured {
        /// The lines at `level`: `INFO`, `WARN` or `ERROR`.
        fn at(&self, level: &str) -> Vec<String> {
            EVENTS.with(|events| {
                String::from_utf8(events.borrow().clone())
                    .unwrap()
                    .lines()
                    .filter(|line| line.split_whitespace().next() == Some(level))
                    .map(str::to_string)
                    .collect()
            })
        }
    }

    /// Captures this thread's events at INFO and above. One subscriber serves
    /// the whole test binary, installed once and never dropped: `tracing`
    /// caches whether each call site is enabled for the whole process and
    /// recomputes it whenever a subscriber is installed or dropped, so a
    /// subscriber per test, dropped while another test's actor emits, can leave
    /// that event's call site disabled. The isolated runtime is
    /// single-threaded, so the actor emits on the test's own thread.
    fn capture() -> Captured {
        static INSTALLED: Once = Once::new();
        INSTALLED.call_once(|| {
            let subscriber = tracing_subscriber::fmt()
                .with_writer(ThreadBuffer)
                .with_ansi(false)
                .without_time()
                .with_max_level(tracing::Level::INFO)
                .finish();
            tracing::subscriber::set_global_default(subscriber)
                .expect("no other global subscriber in the library's tests");
        });
        EVENTS.with(|events| events.borrow_mut().clear());
        Captured
    }

    /// The message a task panicked with.
    async fn panic_message(task: tokio::task::JoinHandle<()>) -> String {
        let payload = task.await.unwrap_err().into_panic();
        match payload.downcast::<String>() {
            Ok(message) => *message,
            Err(payload) => payload
                .downcast::<&str>()
                .map(|message| message.to_string())
                .unwrap_or_default(),
        }
    }

    // --- Cadence -----------------------------------------------------------------------

    #[test]
    fn each_audit_covers_its_calendar_window() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(
                &clock,
                [
                    clean_session(&first(), &[]),
                    clean_session(&february(), &[]),
                ],
            );
            let log = Arc::clone(&connections.log);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-03-01 7:00)).await;

            let typed = typed(&log);
            assert!(
                typed.contains(&retrieval_command(&bounds(&first()))),
                "{typed:?}"
            );
            assert!(
                typed.contains(&retrieval_command(&bounds(&february()))),
                "{typed:?}"
            );
        });
    }

    #[test]
    fn an_audit_runs_the_settling_delay_after_its_window_ends() {
        for (period, window_end) in [
            (AuditPeriod::Day, datetime!(2025-01-16 0:00)),
            (AuditPeriod::Week, datetime!(2025-01-20 0:00)),
            (AuditPeriod::Month, datetime!(2025-02-01 0:00)),
        ] {
            let (clock, runtime) = synthetic();
            runtime.block_on(async {
                let window = AuditWindow {
                    start: SPAWNED,
                    end: window_end,
                };
                let (_record_tx, record_rx) = mpsc::unbounded_channel();
                let connections = Connections::new(&clock, [clean_session(&window, &[])]);
                let log = Arc::clone(&connections.log);
                let _task = tokio::spawn(run(
                    clock.clone(),
                    record_rx,
                    connections,
                    settings(period),
                    running(),
                ));

                let audit_at = window_end + 6 * HOUR;
                until(&clock, audit_at - Duration::from_secs(1)).await;
                assert_eq!(connections_made(&log), 0, "{period:?}: connected early");
                until(&clock, audit_at + Duration::from_secs(1)).await;
                assert_eq!(connections_made(&log), 1, "{period:?}: no audit");
            });
        }
    }

    #[test]
    fn each_audit_opens_its_own_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(
                &clock,
                [
                    clean_session(&first(), &[]),
                    clean_session(&february(), &[]),
                ],
            );
            let log = Arc::clone(&connections.log);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-03-01 7:00)).await;

            assert_eq!(connections_made(&log), 2);
            let typed = typed(&log);
            for window in [first(), february()] {
                let latch = format!(
                    "echo {} | xclip -selection clipboard",
                    latch_value(&window, 1)
                );
                assert!(typed.contains(&latch), "{typed:?}");
            }
        });
    }

    // --- Accumulation ----------------------------------------------------------------

    #[test]
    fn an_artefact_is_audited_in_the_window_its_commencement_falls_in() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let january_at = datetime!(2025-01-20 9:00);
            let february_at = datetime!(2025-02-10 9:00);
            let (record_tx, record_rx) = mpsc::unbounded_channel();
            record_tx.send(bdn("BDN-000001", january_at)).unwrap();
            record_tx.send(bdn("BDN-000002", february_at)).unwrap();

            let mut january = vec![login("s1", january_at - time::Duration::minutes(10))];
            january.extend(dispense("d1", "BDN-000001", "s1", january_at, 100_000));
            let mut february_records = vec![login("s2", february_at - time::Duration::minutes(10))];
            february_records.extend(dispense("d2", "BDN-000002", "s2", february_at, 150_000));
            let connections = Connections::new(
                &clock,
                [
                    clean_session(&first(), &january),
                    clean_session(&february(), &february_records),
                ],
            );
            let log = Arc::clone(&connections.log);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-03-01 7:00)).await;

            let typed = typed(&log);
            assert!(
                typed.contains(&append(&entry(&first(), "agreed"))),
                "{typed:?}"
            );
            assert!(
                typed.contains(&append(&entry(&february(), "agreed"))),
                "{typed:?}"
            );
        });
    }

    #[test]
    fn a_transfer_spanning_the_period_end_is_audited_once_in_the_opening_window() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let at = datetime!(2025-01-31 23:58);
            let (record_tx, record_rx) = mpsc::unbounded_channel();
            record_tx.send(bdn("BDN-000001", at)).unwrap();

            let mut records = vec![login("s1", at - time::Duration::minutes(10))];
            records.extend(dispense("d1", "BDN-000001", "s1", at, 100_000));
            let connections = Connections::new(
                &clock,
                [
                    clean_session(&first(), &records),
                    clean_session(&february(), &records),
                ],
            );
            let log = Arc::clone(&connections.log);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-03-01 7:00)).await;

            let typed = typed(&log);
            assert!(
                typed.contains(&append(&entry(&first(), "agreed"))),
                "{typed:?}"
            );
            assert!(
                typed.contains(&append(&entry(&february(), "agreed"))),
                "{typed:?}"
            );
        });
    }

    // --- Emission ----------------------------------------------------------------------

    #[test]
    fn a_clean_audit_is_reported_at_info() {
        let (clock, runtime) = synthetic();
        let captured = capture();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(&clock, [clean_session(&first(), &[])]);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-02-01 7:00)).await;
        });
        let info = captured.at("INFO");
        assert_eq!(info.len(), 1, "{info:?}");
        for detail in ["2025-01-15T12:00:00Z", "retrievals", "margins"] {
            assert!(info[0].contains(detail), "{detail} missing: {}", info[0]);
        }
        assert!(captured.at("WARN").is_empty());
        assert!(captured.at("ERROR").is_empty());
    }

    #[test]
    fn findings_are_reported_in_one_warn_and_not_at_error() {
        let (clock, runtime) = synthetic();
        let captured = capture();
        runtime.block_on(async {
            let (record_tx, record_rx) = mpsc::unbounded_channel();
            record_tx
                .send(bdn("BDN-000001", datetime!(2025-01-20 9:00)))
                .unwrap();
            record_tx
                .send(bdn("BDN-000002", datetime!(2025-01-21 9:00)))
                .unwrap();
            let disagreed = entry(&first(), "disagreed BDN-000001 BDN-000002");
            let connections = Connections::new(
                &clock,
                [Connection::Accepted(Double::answering([
                    latch(&first()),
                    response(&[]),
                    confirmed(&disagreed),
                ]))],
            );
            let log = Arc::clone(&connections.log);
            let task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-02-01 7:00)).await;

            assert!(!task.is_finished(), "the task ended on a finding");
            assert!(typed(&log).contains(&append(&disagreed)));
        });
        let warn = captured.at("WARN");
        assert_eq!(warn.len(), 1, "{warn:?}");
        for detail in ["BDN-000001", "BDN-000002"] {
            assert!(warn[0].contains(detail), "{detail} missing: {}", warn[0]);
        }
        assert!(captured.at("ERROR").is_empty());
    }

    #[test]
    fn a_summary_not_written_is_reported_at_warn() {
        let (clock, runtime) = synthetic();
        let captured = capture();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(
                &clock,
                [Connection::Accepted(Double::answering([
                    latch(&first()),
                    response(&[]),
                    Ok("an earlier entry\n".to_string()),
                ]))],
            );
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-02-01 7:00)).await;
        });
        assert_eq!(captured.at("INFO").len(), 1);
        let warn = captured.at("WARN");
        assert_eq!(warn.len(), 1, "{warn:?}");
        for detail in ["summary", "2025-01-15T12:00:00Z"] {
            assert!(warn[0].contains(detail), "{detail} missing: {}", warn[0]);
        }
    }

    #[test]
    fn a_fatal_outcome_panics_with_its_message() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let at = datetime!(2025-01-20 9:00);
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let mut records = vec![login("s1", at - time::Duration::minutes(10))];
            records.extend(dispense("d1", "BDN-000001", "s1", at, 100_000));
            let connections = Connections::new(
                &clock,
                [Connection::Accepted(Double::answering([
                    latch(&first()),
                    response(&records),
                ]))],
            );
            let log = Arc::clone(&connections.log);
            let task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            let message = panic_message(task).await;
            assert!(
                message.starts_with("reconciliation_user: the audit of"),
                "{message}"
            );
            assert!(message.contains("RecordWithoutPaper"), "{message}");
            assert!(!typed(&log).iter().any(|line| line.contains(">>")));
        });
    }

    #[test]
    fn an_unabsorbed_failure_panics_with_its_message() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let refused = Ok(concat!(
                "curl: (22) The requested URL returned error: 401\n",
                r#"{"message":"Invalid username or password"}"#,
                "\nretrieval-end 22\n"
            )
            .to_string());
            let connections = Connections::new(
                &clock,
                [Connection::Accepted(Double::answering([
                    latch(&first()),
                    refused,
                ]))],
            );
            let task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            assert_eq!(
                panic_message(task).await,
                "reconciliation_user: the audit of 2025-01-15T12:00:00Z to \
                 2025-02-01T00:00:00Z failed at retrieval (the log server refused the \
                 credential), before any session restart"
            );
        });
    }

    #[test]
    fn a_second_audit_appends_its_own_entry() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(
                &clock,
                [
                    clean_session(&first(), &[]),
                    clean_session(&february(), &[]),
                ],
            );
            let log = Arc::clone(&connections.log);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                running(),
            ));
            until(&clock, datetime!(2025-03-01 7:00)).await;

            let appends: Vec<String> = typed(&log)
                .into_iter()
                .filter(|line| {
                    line.contains("reconciliation-audits.txt") && line.starts_with("echo")
                })
                .collect();
            assert_eq!(
                appends,
                [
                    append(&entry(&first(), "agreed")),
                    append(&entry(&february(), "agreed"))
                ]
            );
        });
    }

    // --- The filesystem ----------------------------------------------------------------

    #[test]
    fn the_actor_writes_nothing_to_disk() {
        // Assembled, so that this file does not name them itself.
        let forbidden = [
            format!("{}::{}", "std", "fs"),
            format!("{}::{}", "File", "create"),
            format!("{}::{}", "fs", "write"),
            format!("{}{}", "Open", "Options"),
        ];
        let actor = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/actors/reconciliation_user");
        let mut sources = vec![actor.join("mod.rs"), actor.join("internals.rs")];
        for entry in fs::read_dir(actor.join("internals")).unwrap() {
            sources.push(entry.unwrap().path());
        }
        for source in sources {
            let text = fs::read_to_string(&source).unwrap();
            for name in &forbidden {
                assert!(
                    !text.contains(name.as_str()),
                    "{} uses {name}",
                    source.display()
                );
            }
        }
    }

    // --- A period in hours (sim-impl.md, "A period in hours") ------------------------

    fn hours_settings(period: Duration, first_audit_after: Duration) -> LoopSettings {
        LoopSettings {
            first_audit_after: Some(first_audit_after),
            ..settings(AuditPeriod::Interval(period))
        }
    }

    #[test]
    fn a_period_in_hours_audits_from_the_drawn_first_audit_one_period_apart() {
        let period = 144 * HOUR;
        let mut schedule = Schedule::start(&hours_settings(period, 10 * HOUR), SPAWNED);
        // The first window ends one settling delay before the first audit, and
        // its part before the spawn is cut away.
        assert_eq!(
            schedule.current(),
            (
                AuditWindow {
                    start: SPAWNED,
                    end: SPAWNED + 4 * HOUR
                },
                SPAWNED + 10 * HOUR
            )
        );
        schedule.advance();
        assert_eq!(
            schedule.current(),
            (
                AuditWindow {
                    start: SPAWNED + 4 * HOUR,
                    end: SPAWNED + 148 * HOUR
                },
                SPAWNED + 154 * HOUR
            )
        );
    }

    #[test]
    fn a_calendar_period_keeps_its_calendar_windows() {
        let mut schedule = Schedule::start(&settings(AuditPeriod::Month), SPAWNED);
        assert_eq!(schedule.current(), (first(), first().end + 6 * HOUR));
        schedule.advance();
        assert_eq!(schedule.current(), (february(), february().end + 6 * HOUR));
    }

    /// The first audit runs when the start state places it, over the window
    /// from the spawn to one settling delay before it, and the next follows
    /// one period later.
    #[test]
    fn a_period_in_hours_audits_its_first_window_and_the_next_one_period_later() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let period = 144 * HOUR;
            let first_audit_after = 10 * HOUR;
            let opening = AuditWindow {
                start: SPAWNED,
                end: SPAWNED + 4 * HOUR,
            };
            let second = AuditWindow {
                start: SPAWNED + 4 * HOUR,
                end: SPAWNED + 148 * HOUR,
            };
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(
                &clock,
                [clean_session(&opening, &[]), clean_session(&second, &[])],
            );
            let log = Arc::clone(&connections.log);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                hours_settings(period, first_audit_after),
                running(),
            ));

            until(&clock, SPAWNED + first_audit_after - Duration::from_secs(1)).await;
            assert_eq!(connections_made(&log), 0, "connected early");
            until(&clock, SPAWNED + first_audit_after + Duration::from_secs(1)).await;
            assert_eq!(
                connections_made(&log),
                1,
                "the first window's audit did not run"
            );
            let typed_first = typed(&log);
            assert!(
                typed_first.contains(&retrieval_command(&bounds(&opening))),
                "{typed_first:?}"
            );
            assert!(
                typed_first.contains(&append(&entry(&opening, "agreed"))),
                "{typed_first:?}"
            );

            until(
                &clock,
                SPAWNED + first_audit_after + period + Duration::from_secs(1),
            )
            .await;
            assert_eq!(
                connections_made(&log),
                2,
                "no second audit one period later"
            );
            assert!(typed(&log).contains(&retrieval_command(&bounds(&second))));
        });
    }
    // --- Stopping and warm start -------------------------------------------------------

    #[test]
    fn reconciliation_user_does_not_begin_audit_after_stop() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(&clock, [clean_session(&first(), &[])]);
            let log = Arc::clone(&connections.log);
            let (lifecycle, stopping) = stopping(None);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                lifecycle,
            ));

            until(&clock, datetime!(2025-01-20 0:00)).await;
            stopping.stop.stop();
            stopping.dock_operator_at_rest.send(()).unwrap();
            let report = stopping.report_rx.await.unwrap();
            until(&clock, datetime!(2025-02-01 7:00)).await;

            assert_eq!(connections_made(&log), 0, "no session is opened");
            assert_eq!(report.window_start, first().start);
            assert_eq!(report.window_end, first().end);
            assert!(report.records.is_empty());
        });
    }

    #[test]
    fn reconciliation_user_finishes_audit_in_progress() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            // The retrieval's read is held until the stop has been requested.
            let (release, held) = oneshot::channel();
            let Connection::Accepted(double) = clean_session(&first(), &[]) else {
                unreachable!("a clean session is accepted");
            };
            let connections =
                Connections::new(&clock, [Connection::Accepted(double.holding_read(1, held))]);
            let log = Arc::clone(&connections.log);
            let (lifecycle, stopping) = stopping(None);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                lifecycle,
            ));

            until(&clock, datetime!(2025-02-01 7:00)).await;
            assert_eq!(connections_made(&log), 1, "the audit has begun");
            stopping.stop.stop();
            stopping.dock_operator_at_rest.send(()).unwrap();
            release.send(()).unwrap();

            let report = stopping.report_rx.await.unwrap();
            assert!(
                typed(&log).contains(&append(&entry(&first(), "agreed"))),
                "the audit in progress writes its summary entry"
            );
            assert_eq!(report.window_start, february().start);
            assert_eq!(report.window_end, february().end);
        });
    }

    #[test]
    fn reconciliation_user_report_includes_artefacts_posted_after_its_stop() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            // Dated in February, so held at the January audit.
            let held = bdn("BDN-000001", datetime!(2025-02-01 3:00));
            let (record_tx, record_rx) = mpsc::unbounded_channel();
            record_tx.send(held.clone()).unwrap();
            let connections = Connections::new(&clock, [clean_session(&first(), &[])]);
            let (lifecycle, stopping) = stopping(None);
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                lifecycle,
            ));

            until(&clock, datetime!(2025-02-10 0:00)).await;
            stopping.stop.stop();
            tokio::task::yield_now().await;
            // Posted by the dock operator while it comes to rest.
            let posted = ticket("TICKET-000001", datetime!(2025-02-09 9:00));
            record_tx.send(posted.clone()).unwrap();
            stopping.dock_operator_at_rest.send(()).unwrap();

            let report = stopping.report_rx.await.unwrap();
            assert_eq!(report.records, vec![held, posted]);
        });
    }

    #[test]
    fn reconciliation_user_audits_saved_window() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let saved = AuditWindow {
                start: SPAWNED - 7 * DAY,
                end: datetime!(2025-02-01 0:00),
            };
            let bdn_at = datetime!(2025-01-10 9:00);
            let ticket_at = datetime!(2025-01-11 9:00);
            let mut records = vec![
                login("s1", bdn_at - time::Duration::minutes(10)),
                login("s2", ticket_at - time::Duration::minutes(10)),
            ];
            records.extend(dispense("d1", "BDN-000001", "s1", bdn_at, 100_000));
            records.extend(fill("f1", "TICKET-000001", "s2", ticket_at));

            let (_record_tx, record_rx) = mpsc::unbounded_channel();
            let connections = Connections::new(&clock, [clean_session(&saved, &records)]);
            let log = Arc::clone(&connections.log);
            let (lifecycle, _stopping) = stopping(Some(ReconciliationUserState {
                window_start: saved.start,
                window_end: saved.end,
                records: vec![
                    bdn("BDN-000001", bdn_at),
                    ticket("TICKET-000001", ticket_at),
                ],
            }));
            let _task = tokio::spawn(run(
                clock.clone(),
                record_rx,
                connections,
                settings(AuditPeriod::Month),
                lifecycle,
            ));
            until(&clock, datetime!(2025-02-01 7:00)).await;

            let typed = typed(&log);
            assert!(
                typed.contains(&retrieval_command(&bounds(&saved))),
                "{typed:?}"
            );
            assert!(
                typed.contains(&append(&entry(&saved, "agreed"))),
                "{typed:?}"
            );
        });
    }
}
