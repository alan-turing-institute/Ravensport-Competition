//! Bounded, drop-oldest queues feeding the log-server pusher tasks.
//!
//! One queue per target class (`fms.md` §5.4). The classes do not share a queue:
//! so an event bound for an unreachable target will not hold up every
//! event behind it — including events whose own target is healthy.
//!
//! When a queue is full the **oldest** event is discarded, not the incoming one:
//! under an outage longer than the queue can absorb, the records nearest the
//! present are the ones an audit needs - Note this is a DECISION not in narrative and
//! should be reviewed as part of the PR. That is why this is a `VecDeque` behind a
//! mutex rather than a `tokio::sync::mpsc` channel — a bounded channel rejects
//! the incoming event and gives the producer no way to evict from the front.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use tokio::sync::Notify;

use crate::state::{EventKind, LogEvent};

/// Events per queue (`fms.md` §5.4).
pub const QUEUE_DEPTH: usize = 256;

/// Which pusher task an event belongs to. The mapping from `EventKind` lives in
/// [`EventKind::class`] so that producer-side routing and task construction
/// cannot disagree about it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EventClass {
    /// ATG readings and alarms, to vmagent's import endpoint.
    Metrics,
    /// Job and transaction events, to vlagent's JSON-line endpoint.
    Events,
    /// Authentication and card-table events, to vlagent's syslog listener over a
    /// persistent TCP connection. Its own class because that listener can fail
    /// independently of the JSON-line endpoint beside it (`fms.md` §5.4).
    Syslog,
}

impl EventKind {
    pub fn class(self) -> EventClass {
        match self {
            EventKind::AtgReading | EventKind::AtgAlarm => EventClass::Metrics,
            EventKind::JobOpened
            | EventKind::TransactionOpen
            | EventKind::TransactionClosed
            | EventKind::BdnLinked
            | EventKind::JobCancelled => EventClass::Events,
            EventKind::Login | EventKind::LoginFailed | EventKind::CardChange => EventClass::Syslog,
        }
    }
}

struct Inner {
    items: VecDeque<LogEvent>,
    closed: bool,
}

pub struct EventQueue {
    inner: Mutex<Inner>,
    notify: Notify,
    capacity: usize,
}

impl EventQueue {
    pub fn new(capacity: usize) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(Inner {
                items: VecDeque::with_capacity(capacity),
                closed: false,
            }),
            notify: Notify::new(),
            capacity,
        })
    }

    /// Append `event`, discarding the oldest if the queue is already full.
    ///
    /// Never blocks and never fails: the producers are the ATG poller, the
    /// Modbus poller and the job endpoints, and none of them may stall on
    /// telemetry (`fms.md` §5.4). Returns the discarded event so the caller can
    /// record the loss — that record is the only trace it leaves.
    pub fn push(&self, event: LogEvent) -> Option<LogEvent> {
        let discarded = {
            let mut inner = self.inner.lock().expect("log queue mutex poisoned");
            let discarded = if inner.items.len() >= self.capacity {
                inner.items.pop_front()
            } else {
                None
            };
            inner.items.push_back(event);
            discarded
        };
        self.notify.notify_one();
        discarded
    }

    /// Remove the oldest event, waiting if the queue is empty.
    ///
    /// Returns `None` once the queue is closed and drained, which ends the
    /// pusher task.
    pub async fn pop(&self) -> Option<LogEvent> {
        loop {
            {
                let mut inner = self.inner.lock().expect("log queue mutex poisoned");
                if let Some(event) = inner.items.pop_front() {
                    return Some(event);
                }
                if inner.closed {
                    return None;
                }
            }
            self.notify.notified().await;
        }
    }

    /// Stop the consumer once the queue drains. Queued events are still
    /// delivered; this is not a discard.
    pub fn close(&self) {
        self.inner.lock().expect("log queue mutex poisoned").closed = true;
        self.notify.notify_waiters();
    }

    /// Take everything currently queued, without waiting. Used by tests that
    /// assert what a producer emitted rather than what was delivered.
    pub fn drain(&self) -> Vec<LogEvent> {
        self.inner
            .lock()
            .expect("log queue mutex poisoned")
            .items
            .drain(..)
            .collect()
    }

    pub fn len(&self) -> usize {
        self.inner
            .lock()
            .expect("log queue mutex poisoned")
            .items
            .len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// The per-class queues, held by `AppState` and by the pusher tasks.
#[derive(Clone)]
pub struct LogQueues {
    pub metrics: Arc<EventQueue>,
    pub events: Arc<EventQueue>,
    pub syslog: Arc<EventQueue>,
}

impl LogQueues {
    pub fn new(capacity: usize) -> Self {
        Self {
            metrics: EventQueue::new(capacity),
            events: EventQueue::new(capacity),
            syslog: EventQueue::new(capacity),
        }
    }

    pub fn for_class(&self, class: EventClass) -> &Arc<EventQueue> {
        match class {
            EventClass::Metrics => &self.metrics,
            EventClass::Events => &self.events,
            EventClass::Syslog => &self.syslog,
        }
    }

    pub fn close(&self) {
        self.metrics.close();
        self.events.close();
        self.syslog.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;

    fn event(kind: EventKind, marker: u64) -> LogEvent {
        LogEvent::new(kind, Utc::now(), serde_json::json!({ "marker": marker }))
    }

    fn marker(event: &LogEvent) -> u64 {
        event.fields["marker"].as_u64().expect("marker missing")
    }

    #[test]
    fn class_splits_metrics_from_domain_events() {
        assert_eq!(EventKind::AtgReading.class(), EventClass::Metrics);
        assert_eq!(EventKind::AtgAlarm.class(), EventClass::Metrics);
        for kind in [
            EventKind::JobOpened,
            EventKind::TransactionOpen,
            EventKind::TransactionClosed,
            EventKind::BdnLinked,
            EventKind::JobCancelled,
        ] {
            assert_eq!(kind.class(), EventClass::Events);
        }
    }

    #[tokio::test]
    async fn pops_in_order_of_arrival() {
        let q = EventQueue::new(4);
        for i in 0..3 {
            assert!(q.push(event(EventKind::JobOpened, i)).is_none());
        }
        for i in 0..3 {
            assert_eq!(marker(&q.pop().await.unwrap()), i);
        }
    }

    #[tokio::test]
    async fn full_queue_discards_the_oldest_and_keeps_the_newest() {
        let q = EventQueue::new(3);
        for i in 0..3 {
            assert!(q.push(event(EventKind::JobOpened, i)).is_none());
        }

        // The fourth push evicts marker 0 — the oldest — and is itself kept.
        let discarded = q
            .push(event(EventKind::JobOpened, 3))
            .expect("expected an eviction");
        assert_eq!(marker(&discarded), 0);
        assert_eq!(q.len(), 3);

        // What remains is the three most recent, in order. A drop-newest queue
        // would have kept 0,1,2 and rejected 3 — the distinction this asserts.
        let remaining: Vec<u64> = vec![
            marker(&q.pop().await.unwrap()),
            marker(&q.pop().await.unwrap()),
            marker(&q.pop().await.unwrap()),
        ];
        assert_eq!(remaining, vec![1, 2, 3]);
    }

    #[tokio::test]
    async fn push_never_reports_failure_when_full() {
        // The producers must not stall or error on a full queue; the only
        // observable effect is the discarded event handed back.
        let q = EventQueue::new(1);
        for i in 0..50 {
            q.push(event(EventKind::AtgReading, i));
        }
        assert_eq!(q.len(), 1);
        assert_eq!(marker(&q.pop().await.unwrap()), 49);
    }

    #[tokio::test]
    async fn pop_waits_then_returns_a_later_push() {
        let q = EventQueue::new(4);
        let waiter = {
            let q = Arc::clone(&q);
            tokio::spawn(async move { q.pop().await.map(|e| marker(&e)) })
        };
        tokio::task::yield_now().await;
        q.push(event(EventKind::JobOpened, 7));
        assert_eq!(waiter.await.unwrap(), Some(7));
    }

    #[tokio::test]
    async fn close_drains_before_ending() {
        let q = EventQueue::new(4);
        q.push(event(EventKind::JobOpened, 1));
        q.push(event(EventKind::JobOpened, 2));
        q.close();

        // Closing must not discard: both queued events are still delivered.
        assert_eq!(marker(&q.pop().await.unwrap()), 1);
        assert_eq!(marker(&q.pop().await.unwrap()), 2);
        assert!(q.pop().await.is_none());
    }

    #[tokio::test]
    async fn a_full_class_does_not_affect_the_other() {
        // The property the split exists for: saturating one class must leave the
        // other untouched. With a shared queue, metrics would have evicted the
        // domain event.
        let queues = LogQueues::new(2);
        queues.events.push(event(EventKind::JobOpened, 100));
        for i in 0..20 {
            queues.metrics.push(event(EventKind::AtgReading, i));
        }

        assert_eq!(queues.events.len(), 1);
        assert_eq!(marker(&queues.events.pop().await.unwrap()), 100);
        assert_eq!(queues.metrics.len(), 2);
    }

    #[test]
    fn for_class_selects_the_matching_queue() {
        let queues = LogQueues::new(4);
        assert!(Arc::ptr_eq(
            queues.for_class(EventClass::Metrics),
            &queues.metrics
        ));
        assert!(Arc::ptr_eq(
            queues.for_class(EventClass::Events),
            &queues.events
        ));
    }
}
