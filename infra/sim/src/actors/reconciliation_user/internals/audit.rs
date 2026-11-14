//! The audit procedure (sim-impl.md, "Failure handling"): one audit through a
//! [`Connector`], its session steps, the three failure budgets, the retries,
//! and the one whole-session restart.

use std::time::Duration;

use time::format_description::well_known::Rfc3339;
use time::PrimitiveDateTime;

use super::retrieval::{Retrieval, RetrievalStop};
use super::session::{close, latch_value, open_and_confirm, record_summary, Workstation};
use super::{
    summary_entry, AuditFailure, AuditOutcome, AuditWindow, ClipboardFailure, Event,
    RetrievalFailure, TerminalFailure,
};
use crate::clock::SimClock;

/// Terminals opened in one session before its budget is exhausted.
const TERMINAL_ATTEMPTS: u32 = 3;

/// Clipboard failures in one session that exhaust its budget.
const CLIPBOARD_FAILURES: u32 = 3;

/// The sim's backoff for transient digital-world I/O, slept on the sim clock
/// before each retry (sim-impl.md, "Retry policy").
const BACKOFF: [Duration; 3] = [
    Duration::from_secs(1),
    Duration::from_secs(2),
    Duration::from_secs(4),
];

/// Yields a connected workstation. Connecting to the workstation with IronRDP
/// implements it, and so does the connector double the tests drive.
#[allow(async_fn_in_trait)]
pub(super) trait Connector {
    type Workstation: Workstation;
    async fn connect(&mut self) -> Result<Self::Workstation, AuditFailure>;
}

/// What one audit retrieves: the window it audits, the bounds retrieved for it,
/// the width of the first span, and the width below which halving stops.
#[derive(Debug, Clone, Copy)]
pub(super) struct AuditPlan {
    pub(super) window: AuditWindow,
    pub(super) bounds: (PrimitiveDateTime, PrimitiveDateTime),
    pub(super) initial_width: Duration,
    pub(super) width_floor: Duration,
}

/// An audit that completed: its outcome, and whether its summary entry was
/// confirmed on the workstation.
#[derive(Debug)]
pub(super) struct Conducted {
    pub(super) outcome: AuditOutcome,
    pub(super) summary_written: bool,
}

/// Conduct one audit (sim-impl.md, "Failure handling"). `reconcile` takes the
/// retrieved records and the retrievals used to the audit's outcome. A failure
/// the budgets do not absorb returns its fatal message; the actor panics with
/// it, before the terminal is closed.
pub(super) async fn conduct(
    clock: &SimClock,
    connector: &mut impl Connector,
    plan: &AuditPlan,
    reconcile: impl FnOnce(Vec<Event>, usize) -> AuditOutcome,
) -> Result<Conducted, String> {
    let mut retrieval = Retrieval::new(plan.bounds, plan.initial_width, plan.width_floor);
    // The latch value's attempt number runs on across the restart.
    let mut attempt = 0;
    let mut restarted = false;
    let mut workstation = loop {
        let mut workstation = connect(clock, connector)
            .await
            .map_err(|failure| fatal_message(&plan.window, &failure, restarted))?;
        match session(clock, &mut workstation, plan, &mut retrieval, &mut attempt).await {
            Ok(()) => break workstation,
            Err(Stop::Exhausted(_)) if !restarted => {
                workstation.disconnect().await;
                restarted = true;
            }
            Err(Stop::Exhausted(failure) | Stop::Fatal(failure)) => {
                return Err(fatal_message(&plan.window, &failure, restarted));
            }
        }
    };

    let retrieved = retrieval.finish();
    let outcome = reconcile(retrieved.records, retrieved.retrievals);
    // A fatal outcome ends the sim at its `panic!`, before the terminal closes.
    let Some(entry) = summary_entry(&outcome) else {
        return Ok(Conducted {
            outcome,
            summary_written: false,
        });
    };
    let summary_written = record_summary(&mut workstation, &entry).await.is_ok();
    close(&mut workstation).await;
    Ok(Conducted {
        outcome,
        summary_written,
    })
}

/// Connect, retrying a failed connection under the transient policy. A
/// certificate that does not verify is not retried.
async fn connect<C: Connector>(
    clock: &SimClock,
    connector: &mut C,
) -> Result<C::Workstation, AuditFailure> {
    let mut backoff = BACKOFF.iter();
    loop {
        match connector.connect().await {
            Ok(workstation) => return Ok(workstation),
            Err(AuditFailure::Terminal(TerminalFailure::ConnectionFailed)) => {
                match backoff.next() {
                    Some(&delay) => clock.sleep(delay).await,
                    None => {
                        return Err(AuditFailure::Terminal(TerminalFailure::ConnectionFailed));
                    }
                }
            }
            Err(failure) => return Err(failure),
        }
    }
}

/// Why a session stopped short of the retrieved window.
enum Stop {
    /// The terminal or clipboard budget: the session is restarted once.
    Exhausted(AuditFailure),
    /// Neither retried nor restarted.
    Fatal(AuditFailure),
}

/// One session: the wait for the desktop, terminal start within its budget,
/// then the retrieval from where it stands, within the clipboard and retrieval
/// budgets.
async fn session(
    clock: &SimClock,
    workstation: &mut impl Workstation,
    plan: &AuditPlan,
    retrieval: &mut Retrieval,
    attempt: &mut u32,
) -> Result<(), Stop> {
    // Once in each session, before its first terminal. A failure while waiting
    // ends the session with no terminal attempted in it.
    workstation.await_desktop().await.map_err(Stop::Exhausted)?;
    for tried in 1..=TERMINAL_ATTEMPTS {
        *attempt += 1;
        match open_and_confirm(workstation, &latch_value(&plan.window, *attempt)).await {
            Ok(()) => break,
            Err(failure) if fatal_at_once(&failure) => return Err(Stop::Fatal(failure)),
            Err(failure) if failure.ends_the_session() => return Err(Stop::Exhausted(failure)),
            Err(failure) if tried == TERMINAL_ATTEMPTS => return Err(Stop::Exhausted(failure)),
            Err(_) => {}
        }
    }

    let mut clipboard_failures = 0;
    let mut retries = 0;
    let mut span = retrieval.position();
    loop {
        let stop = match retrieval.continue_in(workstation).await {
            Ok(()) => return Ok(()),
            Err(stop) => stop,
        };
        // A span that succeeded resets the count for the next.
        if retrieval.position() != span {
            span = retrieval.position();
            retries = 0;
        }
        let failure = match stop {
            // A span at the floor reaching its deadline is not repeated in this
            // session: only a fresh one distinguishes a channel that no longer
            // delivers from an oversized day.
            RetrievalStop::AtFloor(_) => {
                return Err(Stop::Exhausted(AuditFailure::Clipboard(
                    ClipboardFailure::DeadlineReached,
                )));
            }
            RetrievalStop::Failed(failure) => failure,
        };
        if failure.ends_the_session() {
            return Err(Stop::Exhausted(failure));
        }
        match &failure {
            AuditFailure::Clipboard(_) => {
                clipboard_failures += 1;
                if clipboard_failures == CLIPBOARD_FAILURES {
                    return Err(Stop::Exhausted(failure));
                }
            }
            AuditFailure::Retrieval(
                RetrievalFailure::Unreachable
                | RetrievalFailure::Incomplete
                | RetrievalFailure::MalformedResponse,
            ) => match BACKOFF.get(retries) {
                Some(&delay) => {
                    retries += 1;
                    clock.sleep(delay).await;
                }
                None => return Err(Stop::Fatal(failure)),
            },
            _ => return Err(Stop::Fatal(failure)),
        }
    }
}

/// Failures neither retried nor restarted: a certificate that does not verify,
/// and a character the scancode table lacks, which is the sim's own fault.
fn fatal_at_once(failure: &AuditFailure) -> bool {
    matches!(
        failure,
        AuditFailure::Terminal(
            TerminalFailure::CertificateVerification | TerminalFailure::UntypableCharacter(_)
        )
    )
}

/// The message the actor panics with: the window, the site, the failure, and
/// whether the restart had been used.
fn fatal_message(window: &AuditWindow, failure: &AuditFailure, restarted: bool) -> String {
    let instant = |at: PrimitiveDateTime| {
        at.assume_utc()
            .format(&Rfc3339)
            .expect("a UTC date-time always formats as RFC 3339")
    };
    let (site, what) = describe(failure);
    format!(
        "reconciliation_user: the audit of {} to {} failed at {site} ({what}), {}",
        instant(window.start),
        instant(window.end),
        if restarted {
            "after the session restart"
        } else {
            "before any session restart"
        },
    )
}

/// The site a failure belongs to, and the failure in words.
fn describe(failure: &AuditFailure) -> (&'static str, String) {
    match failure {
        AuditFailure::Terminal(failure) => match failure {
            TerminalFailure::ConnectionFailed => ("connecting", "the connection failed".into()),
            TerminalFailure::ConnectionLost => ("the session", "the connection was lost".into()),
            TerminalFailure::CertificateVerification => (
                "connecting",
                "the workstation's certificate did not verify".into(),
            ),
            TerminalFailure::LatchMismatch => (
                "terminal start",
                "the latch returned a different value".into(),
            ),
            TerminalFailure::LatchAbsent => ("terminal start", "the latch returned nothing".into()),
            TerminalFailure::UntypableCharacter(character) => (
                "typing",
                format!("a command held {character:?}, which the scancode table lacks"),
            ),
        },
        AuditFailure::Retrieval(failure) => (
            "retrieval",
            match failure {
                RetrievalFailure::Unreachable => "the log server could not be reached",
                RetrievalFailure::AuthorisationRefused => "the log server refused the credential",
                RetrievalFailure::CertificateVerification => {
                    "the log server's certificate did not verify"
                }
                RetrievalFailure::MalformedResponse => "the response was malformed",
                RetrievalFailure::Incomplete => "the response was incomplete",
            }
            .into(),
        ),
        AuditFailure::Clipboard(failure) => (
            "the clipboard",
            match failure {
                ClipboardFailure::DeadlineReached => {
                    "a span of one day or less reached its deadline".into()
                }
                ClipboardFailure::DeclaredLengthOverCeiling { declared } => {
                    format!("a transfer declared {declared} bytes, over the channel bound")
                }
                ClipboardFailure::ExceedsDeclaredLength { declared, received } => {
                    format!("a transfer carried {received} bytes of {declared} declared")
                }
                ClipboardFailure::NonTextFormat => {
                    "a file-descriptor format was offered, or no text".into()
                }
                ClipboardFailure::StaleSelection => {
                    "the selection held content from before the read".into()
                }
                ClipboardFailure::ResponseFailed => {
                    "the workstation sent its failure response".into()
                }
            },
        ),
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use time::macros::datetime;

    use super::*;
    use crate::actors::reconciliation_user::internals::retrieval::{retrieval_command, Span};
    use crate::actors::reconciliation_user::internals::session::latch_value;
    use crate::actors::reconciliation_user::internals::workstation_double::{
        Connection, Connections, Double, Recorded,
    };
    use crate::actors::reconciliation_user::internals::{
        outcome, reconcile, ClipboardFailure, TerminalFailure,
    };
    use crate::clock::TimeModel;

    const DAY: Duration = Duration::from_secs(86_400);
    const ENTRY: &str = "2025-01-01T00:00:00Z 2025-02-01T00:00:00Z agreed";

    fn january() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-01-01 0:00),
            end: datetime!(2025-02-01 0:00),
        }
    }

    /// January, retrieved in one span.
    fn one_span() -> AuditPlan {
        let window = january();
        AuditPlan {
            window,
            bounds: (window.start, window.end),
            initial_width: 31 * DAY,
            width_floor: DAY,
        }
    }

    /// January's first `days` days, retrieved one day at a time.
    fn by_day(days: u32) -> AuditPlan {
        let window = january();
        AuditPlan {
            window,
            bounds: (window.start, window.start + DAY * days),
            initial_width: DAY,
            width_floor: DAY,
        }
    }

    fn span(start: PrimitiveDateTime, end: PrimitiveDateTime) -> Span {
        Span { start, end }
    }

    fn retrieval_of(plan: &AuditPlan) -> String {
        retrieval_command(&span(plan.bounds.0, plan.bounds.1))
    }

    fn latch(attempt: u32) -> Result<String, AuditFailure> {
        Ok(format!("{}\n", latch_value(&january(), attempt)))
    }

    fn latch_command(attempt: u32) -> String {
        format!(
            "echo {} | xclip -selection clipboard",
            latch_value(&january(), attempt)
        )
    }

    fn mismatch() -> Result<String, AuditFailure> {
        Ok("latch-20241201000000-1\n".to_string())
    }

    fn empty() -> Result<String, AuditFailure> {
        Ok("retrieval-end 0\n".to_string())
    }

    fn unreachable() -> Result<String, AuditFailure> {
        Ok("curl: (7) Failed to connect to hist-dmz-01 port 443\nretrieval-end 7\n".to_string())
    }

    fn refused() -> Result<String, AuditFailure> {
        Ok(concat!(
            "curl: (22) The requested URL returned error: 401\n",
            r#"{"message":"Invalid username or password","statusCode":401}"#,
            "\nretrieval-end 22\n"
        )
        .to_string())
    }

    fn confirmed() -> Result<String, AuditFailure> {
        Ok(format!("{ENTRY}\n"))
    }

    fn stale() -> Result<String, AuditFailure> {
        Err(AuditFailure::Clipboard(ClipboardFailure::StaleSelection))
    }

    /// A login-shaped record at `time`: set aside before grouping, so a
    /// retrieval of them reconciles clean.
    fn login_at(time: &str) -> Result<String, AuditFailure> {
        Ok(format!(
            "{{\"_time\":\"{time}\",\"msg_id\":\"LOGIN\",\"fms@99999.session_id\":\"s-{time}\"}}\nretrieval-end 0\n"
        ))
    }

    fn session(reads: impl IntoIterator<Item = Result<String, AuditFailure>>) -> Connection {
        Connection::Accepted(Double::answering(reads))
    }

    fn clean(window: AuditWindow) -> impl FnOnce(Vec<Event>, usize) -> AuditOutcome {
        move |records, retrievals| outcome(reconcile(Vec::new(), records, &window), retrievals)
    }

    fn synthetic() -> (SimClock, tokio::runtime::Runtime) {
        SimClock::isolated(TimeModel::Synthetic, datetime!(2025-02-03 09:00))
    }

    fn count(typed: &[String], line: &str) -> usize {
        typed.iter().filter(|typed| *typed == line).count()
    }

    /// Where the last connection's actions begin in the log.
    fn last_connection(recorded: &[Recorded]) -> usize {
        recorded
            .iter()
            .rposition(|recorded| *recorded == Recorded::Connected)
            .unwrap()
    }

    /// The first line typed in `recorded`.
    fn first_typed(recorded: &[Recorded]) -> Option<&str> {
        recorded.iter().find_map(|recorded| match recorded {
            Recorded::Typed(line) => Some(line.as_str()),
            _ => None,
        })
    }

    fn waits_for_the_desktop(recorded: &[Recorded]) -> usize {
        recorded
            .iter()
            .filter(|recorded| **recorded == Recorded::AwaitedDesktop)
            .count()
    }

    // --- The procedure -------------------------------------------------------------

    #[test]
    fn a_clean_audit_records_its_summary_and_closes() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections =
                Connections::new(&clock, [session([latch(1), empty(), confirmed()])]);
            let conducted = conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert!(matches!(conducted.outcome, AuditOutcome::Clean(_)));
            assert!(conducted.summary_written);
            assert_eq!(
                connections.recorded(),
                [
                    Recorded::Connected,
                    Recorded::AwaitedDesktop,
                    Recorded::OpenedTerminal,
                    Recorded::ClearedLatch,
                    Recorded::Typed(latch_command(1)),
                    Recorded::Read,
                    Recorded::ClearedLatch,
                    Recorded::Typed(retrieval_of(&plan)),
                    Recorded::Read,
                    Recorded::Typed(format!("echo {ENTRY} >> ~/reconciliation-audits.txt")),
                    Recorded::ClearedLatch,
                    Recorded::Typed(
                        "tail -n 1 ~/reconciliation-audits.txt | xclip -selection clipboard"
                            .to_string()
                    ),
                    Recorded::Read,
                    Recorded::Typed("exit".to_string()),
                    Recorded::Disconnected,
                ]
            );
        });
    }

    #[test]
    fn a_fatal_outcome_writes_no_entry_and_does_not_close() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(&clock, [session([latch(1), empty()])]);
            let conducted = conduct(&clock, &mut connections, &plan, |_, _| {
                AuditOutcome::Fatal("the records disagreed".to_string())
            })
            .await
            .unwrap();
            assert!(matches!(conducted.outcome, AuditOutcome::Fatal(_)));
            assert!(!conducted.summary_written);
            assert_eq!(connections.recorded().last(), Some(&Recorded::Read));
            assert_eq!(connections.typed().last(), Some(&retrieval_of(&plan)));
        });
    }

    #[test]
    fn a_summary_that_is_not_written_still_closes_the_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let unconfirmed = Ok("an earlier entry\n".to_string());
            let mut connections =
                Connections::new(&clock, [session([latch(1), empty(), unconfirmed])]);
            let conducted = conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert!(!conducted.summary_written);
            let recorded = connections.recorded();
            assert_eq!(
                recorded[recorded.len() - 2..],
                [Recorded::Typed("exit".to_string()), Recorded::Disconnected]
            );
        });
    }

    // --- Connecting ----------------------------------------------------------------

    #[test]
    fn a_failed_connection_is_retried_after_one_two_and_four_seconds() {
        let failed =
            || Connection::Refused(AuditFailure::Terminal(TerminalFailure::ConnectionFailed));

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    failed(),
                    failed(),
                    failed(),
                    session([latch(1), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            let at = &connections.attempted_at;
            assert_eq!(at.len(), 4);
            assert_eq!(at[3].duration_since(at[0]), Duration::from_secs(7));
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections =
                Connections::new(&clock, [failed(), failed(), failed(), failed()]);
            assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .is_err());
            assert_eq!(connections.attempted_at.len(), 4);
            assert!(!connections.recorded().contains(&Recorded::OpenedTerminal));
        });
    }

    #[test]
    fn a_certificate_failure_at_the_workstation_is_fatal_at_once() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    Connection::Refused(AuditFailure::Terminal(
                        TerminalFailure::CertificateVerification,
                    )),
                    session([latch(1), empty(), confirmed()]),
                ],
            );
            assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .is_err());
            assert_eq!(connections.attempted_at.len(), 1);
        });
    }

    #[test]
    fn an_untypable_character_is_fatal_at_once() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let refusing = Double::answering([]).refusing_typing(AuditFailure::Terminal(
                TerminalFailure::UntypableCharacter('\t'),
            ));
            let mut connections = Connections::new(
                &clock,
                [
                    Connection::Accepted(refusing),
                    session([latch(1), empty(), confirmed()]),
                ],
            );
            assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .is_err());
            assert_eq!(connections.made(), 1);
            assert_eq!(connections.typed(), [latch_command(1)]);
        });
    }

    // --- Waiting for the desktop ---------------------------------------------------

    #[test]
    fn the_desktop_is_awaited_before_the_first_terminal() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections =
                Connections::new(&clock, [session([latch(1), empty(), confirmed()])]);
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(
                connections.recorded()[..3],
                [
                    Recorded::Connected,
                    Recorded::AwaitedDesktop,
                    Recorded::OpenedTerminal,
                ]
            );
        });
    }

    #[test]
    fn the_desktop_is_awaited_once_in_each_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [session([mismatch(), latch(2), empty(), confirmed()])],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(waits_for_the_desktop(&connections.recorded()), 1);
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([mismatch(), mismatch(), mismatch()]),
                    session([latch(4), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            let recorded = connections.recorded();
            let second = last_connection(&recorded);
            assert_eq!(waits_for_the_desktop(&recorded[..second]), 1);
            assert_eq!(waits_for_the_desktop(&recorded[second..]), 1);
            assert_eq!(recorded[second + 1], Recorded::AwaitedDesktop);
        });
    }

    #[test]
    fn a_connection_lost_awaiting_the_desktop_restarts_the_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let lost = Double::default()
                .refusing_desktop(AuditFailure::Terminal(TerminalFailure::ConnectionLost));
            let mut connections = Connections::new(
                &clock,
                [
                    Connection::Accepted(lost),
                    session([latch(1), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 2);
            let recorded = connections.recorded();
            let second = last_connection(&recorded);
            assert!(!recorded[..second].contains(&Recorded::OpenedTerminal));
            assert_eq!(recorded[second - 1], Recorded::Disconnected);
            // No terminal was attempted in the first session.
            assert_eq!(
                first_typed(&recorded[second..]),
                Some(latch_command(1).as_str())
            );
        });
    }

    // --- Terminal start ------------------------------------------------------------

    #[test]
    fn the_terminal_is_attempted_three_times_in_a_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [session([
                    mismatch(),
                    mismatch(),
                    latch(3),
                    empty(),
                    confirmed(),
                ])],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 1);
            let recorded = connections.recorded();
            let opened = recorded
                .iter()
                .filter(|recorded| **recorded == Recorded::OpenedTerminal)
                .count();
            assert_eq!(opened, 3);
            let typed = connections.typed();
            assert_eq!(
                typed[..4],
                [
                    latch_command(1),
                    latch_command(2),
                    latch_command(3),
                    retrieval_of(&plan)
                ]
            );
        });
    }

    #[test]
    fn a_third_unconfirmed_terminal_restarts_the_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([mismatch(), mismatch(), mismatch()]),
                    session([latch(4), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 2);
            let recorded = connections.recorded();
            let second = recorded
                .iter()
                .rposition(|recorded| *recorded == Recorded::Connected)
                .unwrap();
            assert_eq!(recorded[second - 1], Recorded::Disconnected);
            assert_eq!(
                first_typed(&recorded[second..]),
                Some(latch_command(4).as_str())
            );
        });
    }

    #[test]
    fn a_session_ended_at_terminal_start_restarts_at_once() {
        let lost = AuditFailure::Terminal(TerminalFailure::ConnectionLost);
        let refused = AuditFailure::Clipboard(ClipboardFailure::DeclaredLengthOverCeiling {
            declared: 9_999_999,
        });
        for (case, first) in [
            (
                "lost while typing the latch command",
                Double::default().refusing_typing(lost.clone()),
            ),
            ("lost during the latch read", Double::answering([Err(lost)])),
            (
                "refused by the guard during the latch read",
                Double::answering([Err(refused)]),
            ),
        ] {
            let (clock, runtime) = synthetic();
            runtime.block_on(async {
                let plan = one_span();
                let mut connections = Connections::new(
                    &clock,
                    [
                        Connection::Accepted(first),
                        session([latch(2), empty(), confirmed()]),
                    ],
                );
                let conducted = conduct(&clock, &mut connections, &plan, clean(plan.window)).await;
                assert_eq!(connections.made(), 2, "{case}");
                let recorded = connections.recorded();
                let second = recorded
                    .iter()
                    .rposition(|recorded| *recorded == Recorded::Connected)
                    .unwrap();
                let opened = recorded[..second]
                    .iter()
                    .filter(|recorded| **recorded == Recorded::OpenedTerminal)
                    .count();
                assert_eq!(opened, 1, "{case}: terminals opened in the first session");
                assert_eq!(
                    first_typed(&recorded[second..]),
                    Some(latch_command(2).as_str()),
                    "{case}"
                );
                conducted.unwrap();
            });
        }
    }

    // --- Clipboard -----------------------------------------------------------------

    #[test]
    fn clipboard_failures_retype_the_span_and_three_exhaust_the_budget() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [session([latch(1), stale(), stale(), empty(), confirmed()])],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 1);
            assert_eq!(count(&connections.typed(), &retrieval_of(&plan)), 3);
            let recorded = connections.recorded();
            for (at, entry) in recorded.iter().enumerate() {
                if *entry == Recorded::Typed(retrieval_of(&plan)) {
                    assert_eq!(recorded[at - 1], Recorded::ClearedLatch);
                }
            }
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([latch(1), stale(), stale(), stale()]),
                    session([latch(2), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 2);
        });
    }

    #[test]
    fn a_guard_refusal_restarts_the_session_at_once() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let over_bound = Err(AuditFailure::Clipboard(
                ClipboardFailure::DeclaredLengthOverCeiling {
                    declared: 9_999_999,
                },
            ));
            let mut connections = Connections::new(
                &clock,
                [
                    session([latch(1), over_bound]),
                    session([latch(2), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 2);
            assert_eq!(count(&connections.typed(), &retrieval_of(&plan)), 2);
        });
    }

    #[test]
    fn a_deadline_at_the_floor_restarts_the_session() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = by_day(1);
            let deadline = Err(AuditFailure::Clipboard(ClipboardFailure::DeadlineReached));
            let mut connections = Connections::new(
                &clock,
                [
                    session([latch(1), deadline]),
                    session([latch(2), empty(), confirmed()]),
                ],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 2);
            assert_eq!(count(&connections.typed(), &retrieval_of(&plan)), 2);
        });
    }

    #[test]
    fn the_restart_resumes_at_the_span_that_failed() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = by_day(7);
            let over_bound = Err(AuditFailure::Clipboard(
                ClipboardFailure::DeclaredLengthOverCeiling {
                    declared: 9_999_999,
                },
            ));
            let mut connections = Connections::new(
                &clock,
                [
                    session([
                        latch(1),
                        login_at("2025-01-01T09:00:00Z"),
                        login_at("2025-01-02T09:00:00Z"),
                        over_bound,
                    ]),
                    session([
                        latch(2),
                        login_at("2025-01-03T09:00:00Z"),
                        login_at("2025-01-04T09:00:00Z"),
                        login_at("2025-01-05T09:00:00Z"),
                        login_at("2025-01-06T09:00:00Z"),
                        login_at("2025-01-07T09:00:00Z"),
                        confirmed(),
                    ]),
                ],
            );
            let retrieved = Mutex::new(Vec::new());
            conduct(&clock, &mut connections, &plan, |records, retrievals| {
                *retrieved.lock().unwrap() = records.clone();
                outcome(reconcile(Vec::new(), records, &plan.window), retrievals)
            })
            .await
            .unwrap();

            let recorded = connections.recorded();
            let second = recorded
                .iter()
                .rposition(|recorded| *recorded == Recorded::Connected)
                .unwrap();
            let third_day = retrieval_command(&span(
                datetime!(2025-01-03 0:00),
                datetime!(2025-01-04 0:00),
            ));
            let after_latch = recorded[second..]
                .iter()
                .filter_map(|recorded| match recorded {
                    Recorded::Typed(line) => Some(line.clone()),
                    _ => None,
                })
                .nth(1);
            assert_eq!(after_latch, Some(third_day));
            assert_eq!(retrieved.lock().unwrap().len(), 7);
        });
    }

    // --- Retrieval -----------------------------------------------------------------

    #[test]
    fn a_retrieval_failure_is_retried_after_one_two_and_four_seconds() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [session([
                    latch(1),
                    unreachable(),
                    unreachable(),
                    unreachable(),
                    empty(),
                    confirmed(),
                ])],
            );
            let started = clock.now();
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 1);
            assert_eq!(count(&connections.typed(), &retrieval_of(&plan)), 4);
            assert_eq!(clock.now().duration_since(started), Duration::from_secs(7));
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([
                        latch(1),
                        unreachable(),
                        unreachable(),
                        unreachable(),
                        unreachable(),
                    ]),
                    session([latch(2), empty(), confirmed()]),
                ],
            );
            assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .is_err());
            assert_eq!(connections.made(), 1);
        });
    }

    #[test]
    fn a_successful_span_resets_the_retrieval_count() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = by_day(2);
            let mut connections = Connections::new(
                &clock,
                [session([
                    latch(1),
                    unreachable(),
                    unreachable(),
                    unreachable(),
                    empty(),
                    unreachable(),
                    unreachable(),
                    unreachable(),
                    empty(),
                    confirmed(),
                ])],
            );
            conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .unwrap();
            assert_eq!(connections.made(), 1);
        });
    }

    #[test]
    fn a_refused_credential_or_log_server_certificate_is_fatal_at_once() {
        let certificate = || {
            Ok("curl: (60) SSL certificate problem: unable to get local issuer certificate\nretrieval-end 60\n"
                .to_string())
        };
        for failure in [refused(), certificate()] {
            let (clock, runtime) = synthetic();
            runtime.block_on(async {
                let plan = one_span();
                let mut connections = Connections::new(
                    &clock,
                    [
                        session([latch(1), failure]),
                        session([latch(2), empty(), confirmed()]),
                    ],
                );
                assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                    .await
                    .is_err());
                assert_eq!(connections.made(), 1);
                assert_eq!(count(&connections.typed(), &retrieval_of(&plan)), 1);
            });
        }
    }

    // --- Exhaustion ----------------------------------------------------------------

    #[test]
    fn a_second_exhaustion_is_fatal() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([mismatch(), mismatch(), mismatch()]),
                    session([mismatch(), mismatch(), mismatch()]),
                    session([latch(7), empty(), confirmed()]),
                ],
            );
            assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .is_err());
            assert_eq!(connections.made(), 2);
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([mismatch(), mismatch(), mismatch()]),
                    session([latch(4), stale(), stale(), stale()]),
                    session([latch(5), empty(), confirmed()]),
                ],
            );
            assert!(conduct(&clock, &mut connections, &plan, clean(plan.window))
                .await
                .is_err());
            assert_eq!(connections.made(), 2);
        });
    }

    #[test]
    fn the_fatal_message_names_the_window_the_site_the_failure_and_the_restart() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(
                &clock,
                [
                    session([mismatch(), mismatch(), mismatch()]),
                    session([mismatch(), mismatch(), mismatch()]),
                ],
            );
            assert_eq!(
                conduct(&clock, &mut connections, &plan, clean(plan.window))
                    .await
                    .unwrap_err(),
                "reconciliation_user: the audit of 2025-01-01T00:00:00Z to \
                 2025-02-01T00:00:00Z failed at terminal start (the latch returned a \
                 different value), after the session restart"
            );
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let mut connections = Connections::new(&clock, [session([latch(1), refused()])]);
            assert_eq!(
                conduct(&clock, &mut connections, &plan, clean(plan.window))
                    .await
                    .unwrap_err(),
                "reconciliation_user: the audit of 2025-01-01T00:00:00Z to \
                 2025-02-01T00:00:00Z failed at retrieval (the log server refused the \
                 credential), before any session restart"
            );
        });

        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let plan = one_span();
            let lost = || {
                Connection::Accepted(
                    Double::default()
                        .refusing_typing(AuditFailure::Terminal(TerminalFailure::ConnectionLost)),
                )
            };
            let mut connections = Connections::new(&clock, [lost(), lost()]);
            assert_eq!(
                conduct(&clock, &mut connections, &plan, clean(plan.window))
                    .await
                    .unwrap_err(),
                "reconciliation_user: the audit of 2025-01-01T00:00:00Z to \
                 2025-02-01T00:00:00Z failed at the session (the connection was lost), \
                 after the session restart"
            );
        });
    }
}
