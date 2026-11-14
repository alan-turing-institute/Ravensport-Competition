//! The workstation and connector doubles the session, retrieval and audit tests
//! drive the audit's procedures against (sim-tests.md, "Session", "Retrieval"
//! and "Failure handling").

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use tokio::sync::oneshot;

use super::audit::Connector;
use super::session::Workstation;
use super::AuditFailure;
use crate::clock::{SimClock, SimInstant};

/// What a procedure did, in order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Recorded {
    Connected,
    AwaitedDesktop,
    OpenedTerminal,
    ClearedLatch,
    Typed(String),
    Read,
    Disconnected,
}

/// What every workstation a connector handed out did, in order.
pub(super) type Log = Arc<Mutex<Vec<Recorded>>>;

/// Records each action, and answers each read with the next scripted result.
#[derive(Default)]
pub(super) struct Double {
    pub(super) recorded: Vec<Recorded>,
    reads: VecDeque<Result<String, AuditFailure>>,
    /// The wait for the desktop ends with this failure.
    desktop_refused: Option<AuditFailure>,
    /// Every line typed is refused with this failure.
    typing_refused: Option<AuditFailure>,
    /// The read, by its index, held until the receiver fires.
    held_read: Option<(usize, oneshot::Receiver<()>)>,
    reads_made: usize,
    log: Option<Log>,
}

impl Double {
    pub(super) fn answering(
        reads: impl IntoIterator<Item = Result<String, AuditFailure>>,
    ) -> Double {
        Double {
            reads: reads.into_iter().collect(),
            ..Double::default()
        }
    }

    /// Ends the wait for the desktop with `failure`.
    pub(super) fn refusing_desktop(mut self, failure: AuditFailure) -> Double {
        self.desktop_refused = Some(failure);
        self
    }

    /// Refuses every line typed with `failure`.
    pub(super) fn refusing_typing(mut self, failure: AuditFailure) -> Double {
        self.typing_refused = Some(failure);
        self
    }

    /// Holds the read at `index`, counting from zero, until `release` fires:
    /// a procedure in progress at that read for as long as the test chooses.
    pub(super) fn holding_read(mut self, index: usize, release: oneshot::Receiver<()>) -> Double {
        self.held_read = Some((index, release));
        self
    }

    pub(super) fn typed(&self) -> Vec<&str> {
        self.recorded
            .iter()
            .filter_map(|recorded| match recorded {
                Recorded::Typed(line) => Some(line.as_str()),
                _ => None,
            })
            .collect()
    }

    fn record(&mut self, recorded: Recorded) {
        if let Some(log) = &self.log {
            log.lock().unwrap().push(recorded.clone());
        }
        self.recorded.push(recorded);
    }
}

impl Workstation for Double {
    async fn await_desktop(&mut self) -> Result<(), AuditFailure> {
        self.record(Recorded::AwaitedDesktop);
        match &self.desktop_refused {
            Some(failure) => Err(failure.clone()),
            None => Ok(()),
        }
    }

    async fn open_terminal(&mut self) -> Result<(), AuditFailure> {
        self.record(Recorded::OpenedTerminal);
        Ok(())
    }

    async fn clear_latch(&mut self) {
        self.record(Recorded::ClearedLatch);
    }

    async fn type_line(&mut self, line: &str) -> Result<(), AuditFailure> {
        self.record(Recorded::Typed(line.to_string()));
        match &self.typing_refused {
            Some(failure) => Err(failure.clone()),
            None => Ok(()),
        }
    }

    async fn read_clipboard(&mut self) -> Result<String, AuditFailure> {
        self.record(Recorded::Read);
        if self
            .held_read
            .as_ref()
            .is_some_and(|(index, _)| *index == self.reads_made)
        {
            let (_, release) = self.held_read.take().unwrap();
            release.await.expect("the test releases the held read");
        }
        self.reads_made += 1;
        self.reads
            .pop_front()
            .expect("the test scripts every read the procedure makes")
    }

    async fn disconnect(&mut self) {
        self.record(Recorded::Disconnected);
    }
}

/// One connection a connector double is scripted with.
pub(super) enum Connection {
    Refused(AuditFailure),
    Accepted(Double),
}

/// Hands out its scripted workstations in turn, into one shared log, and notes
/// the simulated instant of every connection attempted.
pub(super) struct Connections {
    clock: SimClock,
    script: VecDeque<Connection>,
    pub(super) log: Log,
    pub(super) attempted_at: Vec<SimInstant>,
}

impl Connections {
    pub(super) fn new(
        clock: &SimClock,
        script: impl IntoIterator<Item = Connection>,
    ) -> Connections {
        Connections {
            clock: clock.clone(),
            script: script.into_iter().collect(),
            log: Log::default(),
            attempted_at: Vec::new(),
        }
    }

    /// The log so far.
    pub(super) fn recorded(&self) -> Vec<Recorded> {
        self.log.lock().unwrap().clone()
    }

    /// The connections made.
    pub(super) fn made(&self) -> usize {
        self.recorded()
            .iter()
            .filter(|recorded| **recorded == Recorded::Connected)
            .count()
    }

    /// Every line typed, in every session.
    pub(super) fn typed(&self) -> Vec<String> {
        self.recorded()
            .into_iter()
            .filter_map(|recorded| match recorded {
                Recorded::Typed(line) => Some(line),
                _ => None,
            })
            .collect()
    }
}

impl Connector for Connections {
    type Workstation = Double;

    async fn connect(&mut self) -> Result<Double, AuditFailure> {
        self.attempted_at.push(self.clock.now());
        match self
            .script
            .pop_front()
            .expect("the test scripts every connection the procedure makes")
        {
            Connection::Refused(failure) => Err(failure),
            Connection::Accepted(mut double) => {
                self.log.lock().unwrap().push(Recorded::Connected);
                double.log = Some(Arc::clone(&self.log));
                Ok(double)
            }
        }
    }
}
