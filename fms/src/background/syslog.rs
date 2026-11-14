//! RFC 5424 syslog frame construction for the security audit events.
//!
//! `fms.md` §2 specifies that the FMS builds these messages itself over a TCP
//! socket rather than depending on a syslog crate or the host's syslog service,
//! consistent with the self-contained-binary design. This module is that
//! construction, and nothing else: it is pure, so the wire format can be
//! asserted without a socket.
//!
//! The format is `hist.md` §4.3, and the reference implementation the hist suite
//! already pins is `tests/integration/hist/stubs/fms_pusher.py`:
//!
//! ```text
//! <PRI>1 TIMESTAMP HOSTNAME APP-NAME - MSGID [fms@99999 name="value" …] MSG\n
//! ```
//!
//! The PROCID field is `-` (unused). Messages are LF-terminated because they
//! travel on a stream rather than as datagrams (§5 path 3).

use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, SecondsFormat, Utc};
use rustls::pki_types::ServerName;
use tokio::{io::AsyncWriteExt, net::TcpStream, time};
use tokio_rustls::{client::TlsStream, TlsConnector};

use crate::log_queue::EventQueue;
use crate::state::{EventKind, LogEvent};

/// Delay before reconnecting after a failure — the fixed delay `fms.md` §5.4
/// specifies, matching the vessel RFID reader task (§5.3).
const RECONNECT_DELAY: Duration = Duration::from_secs(2);

/// Facility `user` (1), per `hist.md` §4.3.
pub(crate) const FACILITY_USER: u8 = 1;
/// Structured-data identifier. Illustrative rather than IANA-registered: this is
/// a synthetic environment (`hist.md` §4.3). VictoriaLogs stores each parameter
/// prefixed with it, so `username` is queried as `fms@99999.username`.
pub(crate) const SD_ID: &str = "fms@99999";
/// APP-NAME, matching `hist.md` §4.3's envelope.
pub(crate) const APP_NAME: &str = "fms";

impl EventKind {
    /// MSGID for the syslog classes, per `hist.md` §4.3. `None` for the classes
    /// that travel as metrics or JSON lines instead.
    pub fn msgid(self) -> Option<&'static str> {
        match self {
            EventKind::Login => Some("LOGIN"),
            EventKind::LoginFailed => Some("LOGIN_FAILED"),
            EventKind::CardChange => Some("CARD-CHANGE"),
            EventKind::AtgReading
            | EventKind::AtgAlarm
            | EventKind::JobOpened
            | EventKind::TransactionOpen
            | EventKind::TransactionClosed
            | EventKind::BdnLinked
            | EventKind::JobCancelled => None,
        }
    }

    /// Syslog severity, per `hist.md` §4.3: informational (6) for a successful
    /// login, warning (4) for a failed one, notice (5) for a card-table change.
    pub fn severity(self) -> u8 {
        match self {
            EventKind::LoginFailed => 4,
            EventKind::CardChange => 5,
            _ => 6,
        }
    }
}

/// Render `value` safely inside a PARAM-VALUE.
///
/// Two separate hazards, both reachable from the unauthenticated `username` of a
/// `LOGIN_FAILED`, so neither is theoretical:
///
/// 1. RFC 5424 §6.3.3: `\`, `"` and `]` are backslash-escaped. Without this a
///    username containing `]` closes the structured-data element early and the
///    rest of the message is misparsed.
/// 2. Control characters are replaced by a printable `\xNN` escape. Messages are
///    framed by the LF that terminates them (§5 path 3), so a raw LF in a value
///    ends the record and everything after it is read as a *second*, complete
///    record whose content the caller chose. Because RFC 5424 permits `-` in
///    place of structured data, a forged record needs none of the three
///    characters escaped above — only spaces and hyphens — so escaping those
///    alone does not close this. CR and NUL are escaped for the same reason:
///    a reader that frames on either would split the record there.
///
/// The escape is unambiguous rather than lossy: a literal backslash in the input
/// is already doubled by rule 1, so a single `\x` can only have been produced
/// here. That keeps "the username contained a newline" in the audit record,
/// which stripping the character would discard.
fn escape_sd_value(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        if c.is_control() {
            out.push_str(&format!("\\x{:02x}", c as u32));
            continue;
        }
        if matches!(c, '\\' | '"' | ']') {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn timestamp(time: DateTime<Utc>) -> String {
    // `Z` rather than `+00:00`, matching the reference implementation.
    time.to_rfc3339_opts(SecondsFormat::Micros, true)
}

/// Render `event` as one LF-terminated RFC 5424 message.
///
/// Returns `None` for an event that does not travel by syslog, which cannot
/// happen for a queued event because the class decides the transport — the
/// signature records the partiality rather than panicking on it.
pub fn frame(event: &LogEvent, hostname: &str) -> Option<String> {
    let msgid = event.kind.msgid()?;
    let pri = u16::from(FACILITY_USER) * 8 + u16::from(event.kind.severity());

    // Field order follows the map's iteration order, which serde_json keeps
    // sorted by key — so the frame is deterministic for a given event.
    let sd: String = event
        .fields
        .iter()
        .map(|(name, value)| {
            let text = match value {
                serde_json::Value::String(s) => s.clone(),
                other => other.to_string(),
            };
            format!(" {name}=\"{}\"", escape_sd_value(&text))
        })
        .collect();

    Some(format!(
        "<{pri}>1 {} {hostname} {APP_NAME} - {msgid} [{SD_ID}{sd}] {}\n",
        timestamp(event.time),
        event.kind.msg(),
    ))
}

/// The HOSTNAME field of the envelope (`hist.md` §4.3 gives `fms-01`).
///
/// Read from the host rather than configured. A syslog client identifies itself
/// by its own name, and a separately configured copy is a second source of truth
/// that can disagree with it — a mismatch that would be invisible until someone
/// tried to correlate log-server records with a host.
pub fn local_hostname() -> String {
    std::fs::read_to_string("/etc/hostname")
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "fms-01".to_owned())
}

/// A TLS connector whose only trust anchors are the system trust store's
/// (`fms.md` §3), where the deployment installs the RavensPort CA root.
///
/// The crypto provider is named here rather than taken from the process
/// default. Both of rustls's providers are compiled in (`axum-server`'s
/// `tls-rustls` feature enables `aws-lc-rs` alongside this crate's `ring`), so
/// `ClientConfig::builder()` panics unless a default has been installed first,
/// and a caller that spawns this task without `fms::run` would lose it.
fn system_store_connector() -> TlsConnector {
    let native = rustls_native_certs::load_native_certs();
    for e in &native.errors {
        tracing::warn!("syslog: could not read part of the system trust store: {e}");
    }
    let mut roots = rustls::RootCertStore::empty();
    roots.add_parsable_certificates(native.certs);
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .expect("ring supports rustls's default protocol versions")
        .with_root_certificates(roots)
        .with_no_client_auth();
    TlsConnector::from(Arc::new(config))
}

/// Drain the syslog queue to vlagent's listener over one persistent TLS
/// connection (RFC 5425, `fms.md` §5.4).
///
/// `host` is both the address dialled and the name the certificate is verified
/// against, so it must be the name `hist-01`'s certificate carries rather than an
/// address (`digital.md` §2.7).
///
/// Delivery is retried until it succeeds, as on the two HTTP paths: a message
/// that cannot be written while the connection is down is sent once it is
/// re-established, giving this path the same guarantee as the others
/// (`fms.md` §5.4). Loss happens at the queue, not here.
pub async fn run(queue: Arc<EventQueue>, host: String, port: u16, hostname: String) {
    let addr = format!("{host}:{port}");
    let server_name = match ServerName::try_from(host.clone()) {
        Ok(name) => name,
        Err(e) => {
            tracing::error!(
                "syslog host {host:?} is not a valid TLS server name: {e}; \
                 authentication events will not be delivered"
            );
            return;
        }
    };
    let connector = system_store_connector();
    let mut conn: Option<TlsStream<TcpStream>> = None;
    let mut warned = false;

    while let Some(event) = queue.pop().await {
        let Some(line) = frame(&event, &hostname) else {
            tracing::error!(
                event_type = event.kind.name(),
                "non-syslog event on the syslog queue; discarding"
            );
            continue;
        };

        loop {
            if conn.is_none() {
                // The two stages are reported separately (`fms.md` §3): a
                // refused or timed-out connection is an outage, whereas a
                // rejected certificate is a trust failure, and the reason rustls
                // gives is the only record of which.
                let attempt = match TcpStream::connect(&addr).await {
                    Ok(tcp) => connector
                        .connect(server_name.clone(), tcp)
                        .await
                        .map_err(|e| format!("TLS handshake with {addr} failed: {e}")),
                    Err(e) => Err(format!("connect to {addr} failed: {e}")),
                };
                match attempt {
                    Ok(stream) => {
                        tracing::info!("syslog connected to {addr}");
                        conn = Some(stream);
                        warned = false;
                    }
                    Err(reason) => {
                        // One warning per outage rather than one per attempt:
                        // this is the only indication that the audit trail has
                        // stopped reaching the log server.
                        if !warned {
                            tracing::warn!(
                                "syslog {reason}; retrying every {}s. \
                                 Authentication events will be discarded once the queue fills",
                                RECONNECT_DELAY.as_secs()
                            );
                            warned = true;
                        }
                        time::sleep(RECONNECT_DELAY).await;
                        continue;
                    }
                }
            }

            let stream = conn.as_mut().expect("connection established above");
            // TLS buffers records, so a write is not on the wire until flushed.
            let written = match stream.write_all(line.as_bytes()).await {
                Ok(()) => stream.flush().await,
                Err(e) => Err(e),
            };
            match written {
                Ok(()) => break,
                Err(e) => {
                    tracing::debug!("syslog write failed: {e}; reconnecting");
                    conn = None;
                    time::sleep(RECONNECT_DELAY).await;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn at(kind: EventKind, fields: serde_json::Value) -> LogEvent {
        LogEvent::new(
            kind,
            Utc.with_ymd_and_hms(2026, 8, 6, 9, 30, 0).unwrap(),
            fields,
        )
    }

    #[test]
    fn login_frame_matches_the_specified_envelope() {
        let line = frame(
            &at(
                EventKind::Login,
                serde_json::json!({
                    "username": "jrenard",
                    "role": "operator",
                    "session_id": "0a98829b",
                    "result": "success",
                }),
            ),
            "fms-01",
        )
        .unwrap();

        // PRI = facility 1 * 8 + severity 6 = 14.
        assert!(
            line.starts_with("<14>1 2026-08-06T09:30:00.000000Z fms-01 fms - LOGIN ["),
            "{line}"
        );
        assert!(line.contains(r#"[fms@99999 result="success" role="operator" session_id="0a98829b" username="jrenard"]"#), "{line}");
        assert!(line.ends_with(" login\n"), "{line}");
    }

    #[test]
    fn login_failed_is_warning_severity() {
        // PRI = 1 * 8 + 4 = 12. A wrong severity would put failed logins at the
        // same level as successes, which is what an audit filters on.
        let line = frame(
            &at(EventKind::LoginFailed, serde_json::json!({"username": "x"})),
            "fms-01",
        )
        .unwrap();
        assert!(line.starts_with("<12>1 "), "{line}");
        assert!(line.contains(" LOGIN_FAILED ["), "{line}");
    }

    #[test]
    fn card_change_is_notice_severity_and_keeps_its_hyphen() {
        // PRI = 1 * 8 + 5 = 13. The MSGID is `CARD-CHANGE`, not `CARD_CHANGE`.
        let line = frame(
            &at(
                EventKind::CardChange,
                serde_json::json!({"action": "enrol"}),
            ),
            "fms-01",
        )
        .unwrap();
        assert!(line.starts_with("<13>1 "), "{line}");
        assert!(line.contains(" CARD-CHANGE ["), "{line}");
    }

    #[test]
    fn structured_data_values_are_escaped() {
        // A username is attacker-controlled on a failed login. An unescaped `]`
        // would close the structured-data element early and the remainder of the
        // message would parse as something else entirely.
        let line = frame(
            &at(
                EventKind::LoginFailed,
                serde_json::json!({"username": r#"a]b"c\d"#}),
            ),
            "fms-01",
        )
        .unwrap();
        assert!(line.contains(r#"username="a\]b\"c\\d""#), "{line}");
        // Exactly one closing bracket: the element's own.
        assert_eq!(line.matches("] ").count(), 1, "{line}");
    }

    #[test]
    fn a_newline_in_a_value_cannot_forge_a_second_record() {
        // The message is framed by its terminating LF, so a raw LF in an
        // attacker-supplied value ends the record early and everything after it
        // is read as a second, complete record. The payload below is a valid
        // RFC 5424 message using `-` for structured data, so it needs none of
        // the three characters escaped for §6.3.3 — escaping those alone would
        // let it through.
        let forged = "\n<14>1 2026-08-06T09:30:00.000000Z fms-01 fms - LOGIN - login";
        let line = frame(
            &at(
                EventKind::LoginFailed,
                serde_json::json!({ "username": format!("bob{forged}") }),
            ),
            "fms-01",
        )
        .unwrap();

        // One LF only: the terminator. Two would be two records on the wire.
        assert_eq!(
            line.matches('\n').count(),
            1,
            "embedded LF survived escaping, forging a second record: {line}"
        );
        assert!(line.ends_with('\n'), "{line}");
        // The text is kept, in printable form, so the attempt stays auditable.
        assert!(line.contains(r"bob\x0a<14>1 "), "{line}");
    }

    #[test]
    fn control_characters_are_escaped_printably() {
        let line = frame(
            &at(
                EventKind::LoginFailed,
                serde_json::json!({"username": "a\rb\tc\u{0}d\u{7f}e"}),
            ),
            "fms-01",
        )
        .unwrap();
        assert!(
            line.contains(r#"username="a\x0db\x09c\x00d\x7fe""#),
            "{line}"
        );
        assert_eq!(line.matches('\n').count(), 1, "{line}");
    }

    #[test]
    fn every_message_carries_a_msg() {
        // hist.md §4.3: VictoriaLogs substitutes a placeholder pointing at its own
        // documentation when MSG is absent, and that placeholder then becomes what
        // every security event reads as in Grafana.
        for kind in [
            EventKind::Login,
            EventKind::LoginFailed,
            EventKind::CardChange,
        ] {
            let line = frame(&at(kind, serde_json::json!({})), "fms-01").unwrap();
            let after_sd = line.split_once("] ").expect("no structured data").1;
            assert!(
                after_sd.trim().len() > 1,
                "{kind:?} produced an empty MSG: {line}"
            );
        }
    }

    #[test]
    fn non_syslog_classes_have_no_frame() {
        for kind in [EventKind::AtgReading, EventKind::JobOpened] {
            assert!(frame(&at(kind, serde_json::json!({})), "fms-01").is_none());
        }
    }

    #[test]
    fn message_is_lf_terminated() {
        // The listener frames on LF (hist.md §5 path 3); without it two messages
        // on the persistent connection would arrive as one.
        let line = frame(&at(EventKind::Login, serde_json::json!({})), "fms-01").unwrap();
        assert!(line.ends_with('\n'));
        assert_eq!(line.matches('\n').count(), 1);
    }
}
