/// Log-server event pusher: consumes LogEvents from the in-memory queue and
/// POSTs them to the configured log-server endpoint.
///
/// Delivery is best-effort and must never stall the producers (fms.md §5.4). A
/// failed POST is retried indefinitely with exponential backoff capped at 30
/// seconds, so a transient log-server failure loses nothing. Loss happens at
/// the queue instead: `AppState::emit_log_event` drops on a full queue rather
/// than back-pressuring the operational path. A sustained outage therefore
/// degrades to loss at the rate the queue fills, without ever slowing fuel
/// movement — narrative.md §5 "Temporal & procedural" condition 4 resolves
/// record completeness against availability in favour of availability.
use std::time::Duration;

use std::sync::Arc;

use tokio::time;

use serde_json::json;

use crate::log_queue::EventQueue;
use crate::state::{EventKind, LogEvent};

/// First wait after a failed attempt (fms.md §5.4).
const INITIAL_BACKOFF: Duration = Duration::from_millis(500);
/// Ceiling the backoff doubles up to and then holds at (fms.md §5.4).
const MAX_BACKOFF: Duration = Duration::from_secs(30);

/// Double the wait, holding at the cap. Pure, so the schedule can be asserted
/// without sleeping through it.
pub(crate) fn next_backoff(current: Duration) -> Duration {
    (current * 2).min(MAX_BACKOFF)
}

/// Only one tank in this scenario (hist.md §4.1); the label is kept for realism
/// and forward-compatibility with a multi-tank site.
const TANK_ID: &str = "01";

/// Serialise an event into the body its target accepts.
///
/// Two formats, because the two stores are different products: vmagent takes
/// VictoriaMetrics import-format JSON lines, one per metric per sample
/// (hist.md §4.1, §5 path 1); vlagent takes one flat JSON object per line, with
/// `_time` and `_msg` alongside the fields (§4.2, §5 path 2).
pub(crate) fn body_for(event: &LogEvent) -> Option<String> {
    match event.kind {
        EventKind::AtgReading | EventKind::AtgAlarm => Some(metrics_body(event)),
        EventKind::JobOpened
        | EventKind::TransactionOpen
        | EventKind::TransactionClosed
        | EventKind::BdnLinked
        | EventKind::JobCancelled => Some(jsonline_body(event)),
        // Authentication and card-table events travel as RFC 5424 syslog on
        // their own persistent connection (background::syslog), and are queued
        // separately — so an HTTP pusher task never receives one.
        EventKind::Login | EventKind::LoginFailed | EventKind::CardChange => None,
    }
}

fn import_line(name: &str, labels: &[(&str, &str)], value: f64, ts_ms: i64) -> String {
    let mut metric = serde_json::Map::new();
    metric.insert("__name__".to_owned(), json!(name));
    for (k, v) in labels {
        metric.insert((*k).to_owned(), json!(v));
    }
    json!({"metric": metric, "values": [value], "timestamps": [ts_ms]}).to_string()
}

fn metrics_body(event: &LogEvent) -> String {
    let ts = event.time.timestamp_millis();
    let num = |key: &str| event.fields.get(key).and_then(|v| v.as_f64());
    let mut lines = Vec::new();

    match event.kind {
        EventKind::AtgReading => {
            if let Some(v) = num("volume_l") {
                lines.push(import_line(
                    "atg_tank_volume_litres",
                    &[("tank_id", TANK_ID)],
                    v,
                    ts,
                ));
            }
            if let Some(t) = num("temperature_c") {
                lines.push(import_line(
                    "atg_tank_temperature_celsius",
                    &[("tank_id", TANK_ID)],
                    t,
                    ts,
                ));
            }
        }
        // Written on transition only, so the series is sparse by design.
        EventKind::AtgAlarm => {
            let alarm = event
                .fields
                .get("alarm")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            lines.push(import_line(
                "atg_alarm_active",
                &[("tank_id", TANK_ID), ("alarm", alarm)],
                num("active").unwrap_or(1.0),
                ts,
            ));
        }
        _ => {}
    }

    lines.join("\n")
}

fn jsonline_body(event: &LogEvent) -> String {
    let mut record = serde_json::Map::new();
    record.insert("_time".to_owned(), json!(event.time.to_rfc3339()));
    record.insert("_msg".to_owned(), json!(event.kind.msg()));
    record.insert("event_type".to_owned(), json!(event.kind.name()));
    // Fields are already named as hist.md §4.2 specifies and go at the top
    // level; nesting them under a payload key is what §4.2's selectors would
    // fail to match.
    for (k, v) in &event.fields {
        record.insert(k.clone(), v.clone());
    }
    serde_json::Value::Object(record).to_string()
}

/// The client every HTTP push uses. reqwest's `rustls-tls-native-roots` feature
/// gives it the system trust store's roots, so `hist-01`'s certificate is
/// validated against the CA root the deployment installs there (fms.md §3).
pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .expect("failed to build HTTP client")
}

/// Why one push attempt failed, in the classes fms.md §3 requires the FMS's log
/// to keep apart. A certificate rustls rejected is a trust problem; a connection
/// refused or timed out is an outage. Reported alike, a rotated or compromised
/// `hist-01` certificate would read as an outage.
#[derive(Debug)]
pub enum PushFailure {
    /// rustls rejected the certificate `hist-01` presented; carries its reason.
    Certificate(String),
    /// Refused, timed out, or otherwise not reached.
    Unreachable(String),
    /// Reached, and answered with a non-success status.
    Status(u16),
}

impl PushFailure {
    pub fn from_error(e: &reqwest::Error) -> Self {
        match find_rustls_error(e) {
            Some(tls) => PushFailure::Certificate(tls.to_string()),
            None => PushFailure::Unreachable(error_chain(e)),
        }
    }
}

/// The rustls error behind `e`, if the attempt failed in the TLS handshake.
///
/// Searched rather than read at a fixed depth: rustls's error reaches reqwest
/// inside two nested `io::Error`s (tokio-rustls's, then the connector's). An
/// `io::Error`'s `source()` returns the source *of* the error it wraps, skipping
/// that error itself, so the walk steps into each `io::Error` with `get_ref()`.
fn find_rustls_error<'a>(e: &'a (dyn std::error::Error + 'static)) -> Option<&'a rustls::Error> {
    let mut cur = Some(e);
    while let Some(err) = cur {
        if let Some(tls) = err.downcast_ref::<rustls::Error>() {
            return Some(tls);
        }
        cur = match err.downcast_ref::<std::io::Error>() {
            Some(io) => io
                .get_ref()
                .map(|inner| inner as &(dyn std::error::Error + 'static)),
            None => err.source(),
        };
    }
    None
}

impl std::fmt::Display for PushFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PushFailure::Certificate(reason) => {
                write!(f, "log server certificate rejected: {reason}")
            }
            PushFailure::Unreachable(reason) => write!(f, "log server unreachable: {reason}"),
            PushFailure::Status(code) => write!(f, "log server returned HTTP {code}"),
        }
    }
}

/// `e` and each of its sources, joined. reqwest's own message names only the
/// URL; the cause is further down the chain.
fn error_chain(e: &(dyn std::error::Error + 'static)) -> String {
    let mut out = e.to_string();
    let mut cur = e.source();
    while let Some(err) = cur {
        out.push_str(": ");
        out.push_str(&err.to_string());
        cur = err.source();
    }
    out
}

/// Drain one class's queue to its endpoint, for the lifetime of the process.
///
/// One task per class (fms.md §5.4): the endpoint is fixed at construction, so
/// an unreachable target stalls only its own class. Which queue feeds which
/// endpoint is decided in `lib.rs` from [`EventKind::class`].
pub async fn run(queue: Arc<EventQueue>, endpoint: String) {
    let client = client();

    while let Some(event) = queue.pop().await {
        push_until_delivered(&client, &endpoint, &event).await;
    }
}

/// Deliver `event`, retrying until it succeeds.
///
/// There is deliberately no attempt limit. A bounded schedule discards an event
/// after a few seconds of log-server unavailability, which is a loss the
/// specification accepts only once the queue is full — a transient blip should
/// cost nothing. Per-attempt failures are logged at DEBUG; one WARN is emitted
/// when the backoff first reaches its cap, so a sustained outage is visible
/// without a line per attempt. The WARN carries the latest attempt's
/// [`PushFailure`], so a rejected certificate is distinguishable from an
/// unreachable host (fms.md §3).
pub(crate) async fn push_until_delivered(
    client: &reqwest::Client,
    endpoint: &str,
    event: &LogEvent,
) {
    let Some(body) = body_for(event) else {
        tracing::error!(
            event_type = event.kind.name(),
            "syslog-class event reached an HTTP pusher task; discarding"
        );
        return;
    };
    let mut backoff = INITIAL_BACKOFF;
    let mut warned = false;
    let mut attempt: u32 = 0;
    loop {
        attempt += 1;
        let failure = match client
            .post(endpoint)
            .header("Content-Type", "application/stream+json")
            .body(body.to_owned())
            .send()
            .await
        {
            Ok(resp) if resp.status().is_success() || resp.status().as_u16() == 204 => {
                tracing::debug!("event pushed: {}", event.kind.name());
                return;
            }
            Ok(resp) => PushFailure::Status(resp.status().as_u16()),
            Err(e) => PushFailure::from_error(&e),
        };
        tracing::debug!("push attempt {attempt} failed: {failure}");

        if !warned && backoff >= MAX_BACKOFF {
            tracing::warn!(
                event_type = event.kind.name(),
                "{failure}; retrying at the {}s backoff cap. Events will be \
                 dropped once the queue fills",
                MAX_BACKOFF.as_secs()
            );
            warned = true;
        }

        time::sleep(backoff).await;
        backoff = next_backoff(backoff);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::LogEvent;
    use wiremock::matchers::method;
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn atg_reading(volume: f64, temp: f64) -> LogEvent {
        LogEvent::new(
            EventKind::AtgReading,
            chrono::DateTime::from_timestamp(1_752_761_525, 0).unwrap(),
            serde_json::json!({"volume_l": volume, "temperature_c": temp}),
        )
    }

    // -----------------------------------------------------------------------
    // Routing (hist.md §4.1 vs §4.2)
    // -----------------------------------------------------------------------

    #[test]
    fn body_format_follows_the_event_class() {
        // Routing to an endpoint is now the queue's job (log_queue::EventClass);
        // what this module still decides is which of the two wire formats an
        // event is serialised into, and the two must agree.
        assert!(body_for(&atg_reading(1.0, 2.0))
            .unwrap()
            .contains("__name__"));
        assert!(body_for(&test_event()).unwrap().contains("_msg"));
    }

    // -----------------------------------------------------------------------
    // Metric body (hist.md §4.1, §5 path 1)
    // -----------------------------------------------------------------------

    #[test]
    fn atg_reading_becomes_two_import_lines() {
        let body = body_for(&atg_reading(42_350.0, 12.5)).unwrap();
        let lines: Vec<&str> = body.lines().collect();
        assert_eq!(lines.len(), 2, "one line per metric per sample: {body}");

        let first: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(first["metric"]["__name__"], "atg_tank_volume_litres");
        assert_eq!(first["metric"]["tank_id"], "01");
        assert_eq!(first["values"][0], 42_350.0);
        // Milliseconds, not seconds: a factor-1000 error puts every sample in 1970.
        assert_eq!(first["timestamps"][0], 1_752_761_525_000i64);

        let second: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(second["metric"]["__name__"], "atg_tank_temperature_celsius");
        assert_eq!(second["values"][0], 12.5);
    }

    #[test]
    fn atg_alarm_carries_its_name_as_a_label() {
        let event = LogEvent::new(
            EventKind::AtgAlarm,
            chrono::Utc::now(),
            serde_json::json!({"alarm": "bund_leak", "active": 1}),
        );
        let v: serde_json::Value = serde_json::from_str(&body_for(&event).unwrap()).unwrap();
        assert_eq!(v["metric"]["__name__"], "atg_alarm_active");
        assert_eq!(v["metric"]["alarm"], "bund_leak");
        assert_eq!(v["values"][0], 1.0);
    }

    // -----------------------------------------------------------------------
    // Domain-event body (hist.md §4.2, §5 path 2)
    // -----------------------------------------------------------------------

    #[test]
    fn domain_event_body_is_flat_with_time_and_msg() {
        let event = LogEvent::new(
            EventKind::TransactionOpen,
            chrono::Utc::now(),
            serde_json::json!({"job_id": "j-1", "preset_volume_l": 500.0}),
        );
        let v: serde_json::Value = serde_json::from_str(&body_for(&event).unwrap()).unwrap();

        assert_eq!(v["event_type"], "transaction_open");
        assert_eq!(v["_msg"], "transaction opened");
        assert!(v["_time"].is_string(), "_time must be present: {v}");

        // Flat: §4.2's selectors address these at the top level. Nesting them
        // under `payload` is what the pre-existing pusher did, and no selector
        // in the hist suite would have matched.
        assert_eq!(v["job_id"], "j-1");
        assert_eq!(v["preset_volume_l"], 500.0);
        assert!(v.get("payload").is_none(), "fields must not be nested: {v}");
    }

    fn test_event() -> LogEvent {
        LogEvent::new(
            EventKind::JobOpened,
            chrono::Utc::now(),
            serde_json::json!({"key": "value"}),
        )
    }

    // -----------------------------------------------------------------------
    // Retry schedule (pure)
    // -----------------------------------------------------------------------

    #[test]
    fn backoff_doubles_from_500ms_and_holds_at_30s() {
        // fms.md §5.4: "exponential backoff starting at 500 ms and doubling to
        // a cap of 30 seconds, retried without an attempt limit".
        assert_eq!(INITIAL_BACKOFF, Duration::from_millis(500));
        assert_eq!(MAX_BACKOFF, Duration::from_secs(30));

        let mut waits = vec![INITIAL_BACKOFF];
        while *waits.last().unwrap() < MAX_BACKOFF {
            waits.push(next_backoff(*waits.last().unwrap()));
        }
        assert_eq!(
            waits.iter().map(|d| d.as_millis()).collect::<Vec<_>>(),
            vec![500, 1_000, 2_000, 4_000, 8_000, 16_000, 30_000],
            "the schedule must double and then clamp, not overshoot the cap"
        );
        // Once at the cap it stays there rather than growing without bound.
        assert_eq!(next_backoff(MAX_BACKOFF), MAX_BACKOFF);
    }

    // -----------------------------------------------------------------------
    // HTTP push behaviour (wiremock)
    // -----------------------------------------------------------------------

    #[tokio::test]
    async fn push_returns_on_200() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200))
            .expect(1)
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        push_until_delivered(&client, &server.uri(), &test_event()).await;
        // MockServer verifies the expected call count on drop.
    }

    #[tokio::test]
    async fn push_returns_on_204() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        push_until_delivered(&client, &server.uri(), &test_event()).await;
    }

    #[tokio::test]
    async fn push_retries_on_server_error_then_succeeds() {
        let server = MockServer::start().await;

        // wiremock matches in forward order: first registered = first checked.
        // Register the 500 mock first so it fires for the initial two attempts.
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(500))
            .up_to_n_times(2)
            .mount(&server)
            .await;

        // 200 fallback fires once the 500 mock is exhausted.
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200))
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        push_until_delivered(&client, &server.uri(), &test_event()).await;

        let requests = server.received_requests().await.unwrap();
        assert_eq!(
            requests.len(),
            3,
            "expected 2 failures then 1 success = 3 total requests"
        );
    }

    /// End-to-end through `run`: the two classes must reach different servers.
    /// The pure routing test above checks the URL choice; this checks that the
    /// task actually wires it, and that each target receives the format it
    /// accepts rather than the other one's.
    // Real time, not `start_paused`: both deliveries succeed first attempt, so
    // there is nothing to sleep through, and auto-advancing the clock trips the
    // client's own 10 s request timeout while a request is in flight.
    #[tokio::test]
    async fn each_class_is_delivered_to_its_own_target() {
        let metrics_server = MockServer::start().await;
        let events_server = MockServer::start().await;
        for server in [&metrics_server, &events_server] {
            Mock::given(method("POST"))
                .respond_with(ResponseTemplate::new(200))
                .mount(server)
                .await;
        }

        let queues = crate::log_queue::LogQueues::new(8);
        let handles = vec![
            tokio::spawn(run(Arc::clone(&queues.metrics), metrics_server.uri())),
            tokio::spawn(run(Arc::clone(&queues.events), events_server.uri())),
        ];

        queues.metrics.push(atg_reading(42_350.0, 12.5));
        queues.events.push(test_event());
        queues.close(); // each task returns once its own queue drains
        for handle in handles {
            handle.await.unwrap();
        }

        let to_metrics = metrics_server.received_requests().await.unwrap();
        assert_eq!(
            to_metrics.len(),
            1,
            "vmagent should receive the ATG reading only"
        );
        let body = String::from_utf8_lossy(&to_metrics[0].body);
        assert!(
            body.contains("atg_tank_volume_litres"),
            "vmagent got a non-metric body: {body}"
        );

        let to_events = events_server.received_requests().await.unwrap();
        assert_eq!(
            to_events.len(),
            1,
            "vlagent should receive the domain event only"
        );
        let v: serde_json::Value = serde_json::from_slice(&to_events[0].body).unwrap();
        assert_eq!(v["event_type"], "job_opened");
    }

    /// The old schedule gave up after 5 attempts, dropping the event about 8.5 s
    /// into an outage. Delivery must now outlast that: this fails 8 times —
    /// past the old limit and past the backoff cap — and still delivers.
    #[tokio::test(start_paused = true)]
    async fn delivery_outlasts_the_old_attempt_limit() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(500))
            .up_to_n_times(8)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200))
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        push_until_delivered(&client, &server.uri(), &test_event()).await;

        let requests = server.received_requests().await.unwrap();
        assert_eq!(
            requests.len(),
            9,
            "expected 8 failures then 1 success; a bounded schedule would have \
             dropped the event instead"
        );
    }
}
