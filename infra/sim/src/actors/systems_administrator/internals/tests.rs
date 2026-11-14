use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use time::macros::datetime;
use tokio::sync::oneshot;

use super::*;
use crate::actors::systems_administrator::SystemsAdministratorState;
use crate::clock::{SimInstant, TimeModel};
use crate::stop::{sleep_until, stop_channel, StopSender};

const SIX_HOURS: Duration = Duration::from_secs(6 * 60 * 60);
const TEST_DEADLINE: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, Copy)]
enum ConnectOutcome {
    Success,
    RejectedCredential,
    UnexpectedHostKey,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Event {
    Connect(SimInstant),
    HostKey,
    Password,
    Pty,
    Shell,
    Command { command: String, prompt: String },
    Recover(String),
    Disconnect,
}

#[derive(Clone)]
struct Recorder {
    events: Arc<Mutex<Vec<Event>>>,
    tx: tokio::sync::mpsc::UnboundedSender<Event>,
}

impl Recorder {
    fn push(&self, event: Event) {
        self.events.lock().unwrap().push(event.clone());
        let _ = self.tx.send(event);
    }
}

/// Replies held back until the test releases them, keyed by the command and
/// the prompt it was typed for.
type HeldReplies = Arc<Mutex<HashMap<(String, String), oneshot::Receiver<()>>>>;

struct MockConnector {
    clock: SimClock,
    recorder: Recorder,
    outcomes: Arc<Mutex<VecDeque<ConnectOutcome>>>,
    fallback_commands: Arc<Mutex<HashSet<String>>>,
    error_commands: Arc<Mutex<HashSet<String>>>,
    held: HeldReplies,
}

impl MockConnector {
    /// Hold the reply to `command` typed for `host`'s prompt until the
    /// returned sender fires.
    fn hold(&self, command: &str, host: &str) -> oneshot::Sender<()> {
        let (release, held) = oneshot::channel();
        self.held
            .lock()
            .unwrap()
            .insert((command.to_string(), host.to_string()), held);
        release
    }
}

#[async_trait::async_trait]
impl SessionConnector for MockConnector {
    type Session = MockSession;

    async fn connect(
        &self,
        _clock: &SimClock,
        _deadline: Duration,
    ) -> Result<Self::Session, SessionError> {
        self.recorder.push(Event::Connect(self.clock.now()));
        self.recorder.push(Event::HostKey);
        let outcome = self
            .outcomes
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or(ConnectOutcome::Success);
        if matches!(outcome, ConnectOutcome::UnexpectedHostKey) {
            return Err(SessionError("unexpected host key".to_string()));
        }

        self.recorder.push(Event::Password);
        if matches!(outcome, ConnectOutcome::RejectedCredential) {
            return Err(SessionError("credential rejected".to_string()));
        }

        self.recorder.push(Event::Pty);
        self.recorder.push(Event::Shell);
        Ok(MockSession {
            recorder: self.recorder.clone(),
            fallback_commands: Arc::clone(&self.fallback_commands),
            error_commands: Arc::clone(&self.error_commands),
            held: Arc::clone(&self.held),
        })
    }
}

struct MockSession {
    recorder: Recorder,
    fallback_commands: Arc<Mutex<HashSet<String>>>,
    error_commands: Arc<Mutex<HashSet<String>>>,
    held: HeldReplies,
}

#[async_trait::async_trait]
impl AdministrationSession for MockSession {
    async fn type_command(
        &mut self,
        command: &str,
        primary_prompt: &str,
        fallback_prompt: Option<&str>,
        _deadline: Duration,
    ) -> Result<PromptMatch, SessionError> {
        self.recorder.push(Event::Command {
            command: command.to_string(),
            prompt: primary_prompt.to_string(),
        });
        let held = self
            .held
            .lock()
            .unwrap()
            .remove(&(command.to_string(), primary_prompt.to_string()));
        if let Some(release) = held {
            let _ = release.await;
        }
        if self.error_commands.lock().unwrap().remove(command) {
            return Err(SessionError("scripted step failure".to_string()));
        }
        if self.fallback_commands.lock().unwrap().remove(command) {
            assert!(fallback_prompt.is_some());
            return Ok(PromptMatch::Fallback);
        }
        Ok(PromptMatch::Primary)
    }

    async fn recover_to_admin(
        &mut self,
        managed_host: &str,
        _deadline: Duration,
    ) -> Result<(), SessionError> {
        self.recorder.push(Event::Recover(managed_host.to_string()));
        Ok(())
    }

    async fn disconnect(&mut self, _deadline: Duration) {
        self.recorder.push(Event::Command {
            command: "exit".to_string(),
            prompt: ADMIN_HOST.to_string(),
        });
        self.recorder.push(Event::Disconnect);
    }
}

fn managed_hosts() -> Vec<String> {
    ["fms-01", "hist-01", "hmi-01"]
        .into_iter()
        .map(str::to_string)
        .collect()
}

async fn wait_for_disconnect(rx: &mut tokio::sync::mpsc::UnboundedReceiver<Event>) {
    while let Some(event) = rx.recv().await {
        if event == Event::Disconnect {
            return;
        }
    }
    panic!("systems-administrator test event channel closed");
}

/// A connector with no scripted failures or held replies, with the record of
/// its events and a receiver that sees each event as it happens.
fn connector(
    clock: &SimClock,
) -> (
    MockConnector,
    Arc<Mutex<Vec<Event>>>,
    tokio::sync::mpsc::UnboundedReceiver<Event>,
) {
    let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
    let events = Arc::new(Mutex::new(Vec::new()));
    let connector = MockConnector {
        clock: clock.clone(),
        recorder: Recorder {
            events: Arc::clone(&events),
            tx,
        },
        outcomes: Arc::default(),
        fallback_commands: Arc::default(),
        error_commands: Arc::default(),
        held: Arc::default(),
    };
    (connector, events, rx)
}

/// The cold-start state: the first session is due one `check_interval` after
/// the start, and there is no previous session (sim-impl.md, "State types").
fn cold_start(clock: &SimClock) -> SystemsAdministratorState {
    SystemsAdministratorState {
        next_check_at: clock.now_datetime() + SIX_HOURS,
        previous_check_at: None,
    }
}

/// Spawn the actor from `start` with a six-hour `check_interval`, returning
/// the stop request's sender and the receiver of the actor's report.
fn spawn_actor(
    clock: SimClock,
    start: SystemsAdministratorState,
    connector: MockConnector,
) -> (
    StopSender,
    oneshot::Receiver<SystemsAdministratorState>,
    tokio::task::JoinHandle<()>,
) {
    let (stop_tx, stop) = stop_channel();
    let (report_tx, report_rx) = oneshot::channel();
    let handle = spawn_with_connector(
        clock,
        start,
        stop,
        report_tx,
        managed_hosts(),
        SIX_HOURS,
        TEST_DEADLINE,
        connector,
    );
    (stop_tx, report_rx, handle)
}

/// The time at which each session connected.
fn connections(events: &Mutex<Vec<Event>>) -> Vec<SimInstant> {
    events
        .lock()
        .unwrap()
        .iter()
        .filter_map(|event| match event {
            Event::Connect(at) => Some(*at),
            _ => None,
        })
        .collect()
}

/// Every journal command typed, in order.
fn journal_commands(events: &Mutex<Vec<Event>>) -> Vec<String> {
    events
        .lock()
        .unwrap()
        .iter()
        .filter_map(|event| match event {
            Event::Command { command, .. } if command.starts_with("sudo journalctl") => {
                Some(command.clone())
            }
            _ => None,
        })
        .collect()
}

#[test]
fn one_session_each_configured_interval() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder { events, tx },
            outcomes: Arc::new(Mutex::new(VecDeque::new())),
            fallback_commands: Arc::new(Mutex::new(HashSet::new())),
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);

        let mut starts = Vec::new();
        while starts.len() < 4 {
            if let Event::Connect(start) = rx.recv().await.unwrap() {
                starts.push(start);
            }
        }
        handle.abort();

        for pair in starts.windows(2) {
            assert_eq!(pair[1].duration_since(pair[0]), SIX_HOURS);
        }
    });
}

#[test]
fn the_first_session_comes_after_the_drawn_delay() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder { events, tx },
            outcomes: Arc::new(Mutex::new(VecDeque::new())),
            fallback_commands: Arc::new(Mutex::new(HashSet::new())),
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        // The start state's first check after the start, as Start::cold
        // places it (sim-impl.md, "Start state").
        let first_check_after = Duration::from_secs(90 * 60);
        let spawned = clock.now();
        let start = SystemsAdministratorState {
            next_check_at: clock.now_datetime() + first_check_after,
            previous_check_at: None,
        };
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);

        let mut starts = Vec::new();
        while starts.len() < 2 {
            if let Event::Connect(start) = rx.recv().await.unwrap() {
                starts.push(start);
            }
        }
        handle.abort();

        assert_eq!(starts[0].duration_since(spawned), first_check_after);
        assert_eq!(starts[1].duration_since(starts[0]), SIX_HOURS);
    });
}

#[test]
fn each_managed_host_is_checked_in_order() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder {
                events: Arc::clone(&events),
                tx,
            },
            outcomes: Arc::new(Mutex::new(VecDeque::new())),
            fallback_commands: Arc::new(Mutex::new(HashSet::new())),
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let commands: Vec<_> = events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|event| match event {
                Event::Command { command, .. } => Some(command.clone()),
                _ => None,
            })
            .collect();
        let check = [
            "systemctl --failed",
            "df -h",
            "chronyc tracking",
            "sudo journalctl --priority=err --boot",
        ];
        let mut expected = Vec::new();
        for host in managed_hosts() {
            expected.push(format!("ssh {host}"));
            expected.extend(check.iter().map(|command| (*command).to_string()));
            expected.push("exit".to_string());
        }
        expected.push("exit".to_string());
        assert_eq!(commands, expected);
    });
}

#[test]
fn the_check_is_read_only() {
    let commands = routine_commands(Some(datetime!(2025-01-01 6:00)));
    assert_eq!(
        commands,
        [
            "systemctl --failed",
            "df -h",
            "chronyc tracking",
            "sudo journalctl --priority=err --since '2025-01-01 6:00:00.0'",
        ]
        .map(str::to_string)
    );
}

#[test]
fn only_a_shell_is_requested() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder {
                events: Arc::clone(&events),
                tx,
            },
            outcomes: Arc::new(Mutex::new(VecDeque::new())),
            fallback_commands: Arc::new(Mutex::new(HashSet::new())),
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let requests: Vec<_> = events
            .lock()
            .unwrap()
            .iter()
            .filter(|event| {
                matches!(
                    event,
                    Event::HostKey | Event::Password | Event::Pty | Event::Shell
                )
            })
            .cloned()
            .collect();
        assert_eq!(
            requests,
            [Event::HostKey, Event::Password, Event::Pty, Event::Shell]
        );
    });
}

#[test]
fn an_unreachable_host_is_skipped() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let fallback_commands = Arc::new(Mutex::new(HashSet::from(["ssh hist-01".to_string()])));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder {
                events: Arc::clone(&events),
                tx,
            },
            outcomes: Arc::new(Mutex::new(VecDeque::new())),
            fallback_commands,
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let events = events.lock().unwrap();
        assert!(events.iter().any(|event| {
            matches!(
                event,
                Event::Command { command, prompt }
                    if command == "ssh hmi-01" && prompt == "hmi-01"
            )
        }));
        assert!(!events.iter().any(|event| {
            matches!(
                event,
                Event::Command { command, prompt }
                    if command == "systemctl --failed" && prompt == "hist-01"
            )
        }));
    });
}

#[test]
fn a_rejected_sign_in_waits_for_the_next_interval() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder {
                events: Arc::clone(&events),
                tx,
            },
            outcomes: Arc::new(Mutex::new(VecDeque::from([
                ConnectOutcome::RejectedCredential,
                ConnectOutcome::Success,
            ]))),
            fallback_commands: Arc::new(Mutex::new(HashSet::new())),
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let starts: Vec<_> = events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|event| match event {
                Event::Connect(start) => Some(*start),
                _ => None,
            })
            .collect();
        assert_eq!(starts.len(), 2);
        assert_eq!(starts[1].duration_since(starts[0]), SIX_HOURS);
    });
}

#[test]
fn an_unexpected_host_key_ends_the_session_before_the_credential() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let events = Arc::new(Mutex::new(Vec::new()));
        let connector = MockConnector {
            clock: clock.clone(),
            recorder: Recorder {
                events: Arc::clone(&events),
                tx,
            },
            outcomes: Arc::new(Mutex::new(VecDeque::from([
                ConnectOutcome::UnexpectedHostKey,
                ConnectOutcome::Success,
            ]))),
            fallback_commands: Arc::new(Mutex::new(HashSet::new())),
            error_commands: Arc::new(Mutex::new(HashSet::new())),
            held: Arc::default(),
        };
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let events = events.lock().unwrap();
        let second_connect = events
            .iter()
            .enumerate()
            .filter(|(_, event)| matches!(event, Event::Connect(_)))
            .nth(1)
            .unwrap()
            .0;
        assert_eq!(
            events[..second_connect]
                .iter()
                .filter(|event| matches!(event, Event::HostKey))
                .count(),
            1
        );
        assert!(!events[..second_connect]
            .iter()
            .any(|event| matches!(event, Event::Password)));
    });
}

#[test]
fn the_journal_is_read_since_boot_then_since_the_previous_check() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (connector, events, mut rx) = connector(&clock);
        let start = cold_start(&clock);
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        // One journal command per managed host in each session; the first
        // session began at 06:00.
        let boot = "sudo journalctl --priority=err --boot";
        let since = "sudo journalctl --priority=err --since '2025-01-01 6:00:00.0'";
        assert_eq!(
            journal_commands(&events),
            [boot, boot, boot, since, since, since]
        );
    });
}

// ---------------------------------------------------------------------------
// Stopping and warm start (sim-tests.md, "Stopping and warm start")
// ---------------------------------------------------------------------------

#[test]
fn administrator_begins_no_session_after_stop() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (connector, events, _rx) = connector(&clock);
        let start = cold_start(&clock);
        let (stop, report, _handle) = spawn_actor(clock.clone(), start, connector);
        tokio::task::yield_now().await;
        stop.stop();

        assert_eq!(report.await.unwrap(), start);
        sleep_until(&clock, start.next_check_at + Duration::from_secs(60 * 60)).await;
        assert!(
            connections(&events).is_empty(),
            "no session begins after the stop"
        );
    });
}

#[test]
fn administrator_finishes_session_in_progress_before_report() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (connector, events, mut rx) = connector(&clock);
        let release = connector.hold("systemctl --failed", "hist-01");
        let start = cold_start(&clock);
        let (stop, report, _handle) = spawn_actor(clock.clone(), start, connector);

        // The session, begun at 06:00, awaits the first prompt on hist-01.
        let held = Event::Command {
            command: "systemctl --failed".to_string(),
            prompt: "hist-01".to_string(),
        };
        while rx.recv().await.unwrap() != held {}
        stop.stop();
        // A minute passes before the prompt returns, so that the session ends
        // later than it began.
        clock.sleep(Duration::from_secs(60)).await;
        release.send(()).unwrap();

        let report = report.await.unwrap();
        let events = events.lock().unwrap();
        assert!(
            events.contains(&Event::Command {
                command: "sudo journalctl --priority=err --boot".to_string(),
                prompt: "hmi-01".to_string(),
            }),
            "the check on hmi-01 completes after the stop"
        );
        assert_eq!(
            events[events.len() - 2..],
            [
                Event::Command {
                    command: "exit".to_string(),
                    prompt: ADMIN_HOST.to_string(),
                },
                Event::Disconnect,
            ],
            "the shell on admin-01 is exited and the session disconnected before the report"
        );
        assert_eq!(report.previous_check_at, Some(datetime!(2025-01-01 6:00)));
        assert_eq!(report.next_check_at, datetime!(2025-01-01 12:01));
    });
}

#[test]
fn administrator_waits_for_its_saved_check_time() {
    const TWO_HOURS: Duration = Duration::from_secs(2 * 60 * 60);
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (connector, events, mut rx) = connector(&clock);
        let began = clock.now();
        let start = SystemsAdministratorState {
            next_check_at: clock.now_datetime() + TWO_HOURS,
            previous_check_at: None,
        };
        let (_stop, _report, handle) = spawn_actor(clock.clone(), start, connector);

        clock.sleep(TWO_HOURS - Duration::from_secs(1)).await;
        assert!(
            connections(&events).is_empty(),
            "no session before the saved due time"
        );
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let after_start: Vec<_> = connections(&events)
            .iter()
            .map(|at| at.duration_since(began))
            .collect();
        assert_eq!(after_start, [TWO_HOURS]);
    });
}

#[test]
fn administrator_check_already_due_runs_at_once() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (connector, events, mut rx) = connector(&clock);
        let began = clock.now();
        let start = SystemsAdministratorState {
            next_check_at: datetime!(2024-12-31 23:59),
            previous_check_at: None,
        };
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        assert_eq!(
            connections(&events),
            [began],
            "the session begins without the clock advancing"
        );
    });
}

#[test]
fn administrator_reads_the_journal_from_its_saved_previous_check() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-01 0:00));
    runtime.block_on(async {
        let (connector, events, mut rx) = connector(&clock);
        let start = SystemsAdministratorState {
            next_check_at: datetime!(2024-12-31 23:59),
            previous_check_at: Some(datetime!(2024-12-31 18:00)),
        };
        let (_stop, _report, handle) = spawn_actor(clock, start, connector);
        wait_for_disconnect(&mut rx).await;
        handle.abort();

        let since = "sudo journalctl --priority=err --since '2024-12-31 18:00:00.0'";
        assert_eq!(journal_commands(&events), [since, since, since]);
    });
}
