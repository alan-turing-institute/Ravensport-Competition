//! Confirms the FMS's three pushes to `hist-01` validate the log server's
//! certificate against the CA root in the host's system trust store, and reach
//! a server whose certificate chains to it (fms.md §3, §5.4; digital.md §2.7).
//!
//! The deployment installs the RavensPort root with `update-ca-certificates`
//! (`_trust_ca.sh.j2`). A client carrying its own compiled-in root list doesn't
//! read that store and rejects every internally issued certificate, so these
//! tests stand the system store in with `SSL_CERT_FILE`, which the platform
//! loader reads in place of the operating system's store. A client that
//! consults the store accepts the test root; one that does not rejects it.
//!
//! `SSL_CERT_FILE` is process-wide, which is why these tests are a binary of
//! their own: one root is installed once, before any client is built, and every
//! test in the binary shares it.
//!
//! Certificates are generated in-process with `rcgen`, in the shape
//! `build-ca.sh` issues, following `hmi/tests/fms_tls.rs`. That the real
//! authority's chain is well-formed is `deploy/tests/unit/test_ca_provisioning.py`'s;
//! that the deployed FMS reaches the deployed `hist-01` needs the deployment.

use std::net::TcpListener as StdTcpListener;
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};
use std::time::Duration;

use axum::{extract::State, http::StatusCode, routing::post, Router};
use axum_server::tls_rustls::RustlsConfig;
use chrono::Utc;
use fms::background::log_pusher::{self, PushFailure};
use fms::background::syslog;
use fms::log_queue::EventQueue;
use fms::state::{EventKind, LogEvent};
use rcgen::{
    BasicConstraints, CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa,
    KeyPair, KeyUsagePurpose,
};
use serde_json::json;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_rustls::TlsAcceptor;

/// Long enough for several attempts under the pusher's backoff (500 ms, 1 s,
/// 2 s …), so a delivery that has not happened by then is not merely slow.
const DELIVERY_WAIT: Duration = Duration::from_secs(4);

/// A throwaway root CA, generated entirely in-process.
struct TestCa {
    cert: rcgen::Certificate,
    key: KeyPair,
}

impl TestCa {
    fn new() -> Self {
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        // CA:TRUE and keyCertSign explicitly, as build-ca.sh's root carries
        // them: rustls rejects a chain whose issuer does not assert both.
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        // A distinct name per authority. Two roots sharing a name are matched
        // by name and then fail on the signature (BadSignature); distinct names
        // make an unrelated root present as the untrusted issuer it is
        // (UnknownIssuer).
        let mut dn = DistinguishedName::new();
        dn.push(
            DnType::CommonName,
            format!("Test CA {}", uuid::Uuid::new_v4()),
        );
        params.distinguished_name = dn;

        let key = KeyPair::generate().unwrap();
        let cert = params.self_signed(&key).unwrap();
        Self { cert, key }
    }

    /// A leaf for `hostname` with a DNS SAN of `hostname` only, matching
    /// build-ca.sh's leaf profile (digital.md §2.7).
    fn sign_leaf(&self, hostname: &str) -> (Vec<u8>, Vec<u8>) {
        let mut params = CertificateParams::new(vec![hostname.to_string()]).unwrap();
        params.is_ca = IsCa::NoCa;
        params.key_usages = vec![
            KeyUsagePurpose::DigitalSignature,
            KeyUsagePurpose::KeyEncipherment,
        ];
        params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        let mut dn = DistinguishedName::new();
        dn.push(DnType::CommonName, hostname);
        params.distinguished_name = dn;

        let leaf_key = KeyPair::generate().unwrap();
        let leaf_cert = params.signed_by(&leaf_key, &self.cert, &self.key).unwrap();
        (
            leaf_cert.pem().into_bytes(),
            leaf_key.serialize_pem().into_bytes(),
        )
    }
}

/// The root standing in for the system trust store. Written to a file and
/// named by `SSL_CERT_FILE` exactly once, before any test builds a client.
fn system_root() -> &'static TestCa {
    static ROOT: OnceLock<(TestCa, PathBuf)> = OnceLock::new();
    let (ca, _) = ROOT.get_or_init(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
        let ca = TestCa::new();
        let path =
            std::env::temp_dir().join(format!("fms-hist-push-trust-{}.pem", uuid::Uuid::new_v4()));
        std::fs::write(&path, ca.cert.pem()).unwrap();
        std::env::set_var("SSL_CERT_FILE", &path);
        (ca, path)
    });
    ca
}

/// An HTTPS server on 127.0.0.1 presenting `cert_pem`, standing in for vmagent's
/// import endpoint. Each request body is forwarded on the returned channel.
async fn start_https_intake(
    cert_pem: Vec<u8>,
    key_pem: Vec<u8>,
) -> (u16, mpsc::UnboundedReceiver<String>) {
    let (tx, rx) = mpsc::unbounded_channel::<String>();
    let router = Router::new()
        .route(
            "/api/v1/import",
            post(
                |State(tx): State<mpsc::UnboundedSender<String>>, body: String| async move {
                    let _ = tx.send(body);
                    StatusCode::NO_CONTENT
                },
            ),
        )
        .with_state(tx);
    let tls_config = RustlsConfig::from_pem(cert_pem, key_pem).await.unwrap();
    let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum_server::from_tcp_rustls(listener, tls_config)
            .serve(router.into_make_service())
            .await
            .unwrap();
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    (port, rx)
}

/// A TLS listener on 127.0.0.1 presenting `cert_pem`, standing in for vlagent's
/// syslog port (RFC 5425). Each line read from a completed handshake is
/// forwarded on the returned channel; a connection that fails the handshake —
/// including one that sends plaintext — contributes nothing.
async fn start_syslog_tls_listener(
    cert_pem: Vec<u8>,
    key_pem: Vec<u8>,
) -> (u16, mpsc::UnboundedReceiver<String>) {
    let certs = rustls_pemfile::certs(&mut cert_pem.as_slice())
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    let key = rustls_pemfile::private_key(&mut key_pem.as_slice())
        .unwrap()
        .unwrap();
    let config = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .unwrap();
    let acceptor = TlsAcceptor::from(Arc::new(config));

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (tx, rx) = mpsc::unbounded_channel::<String>();
    tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else {
                return;
            };
            let acceptor = acceptor.clone();
            let tx = tx.clone();
            tokio::spawn(async move {
                let Ok(tls) = acceptor.accept(stream).await else {
                    return;
                };
                let mut lines = BufReader::new(tls);
                let mut line = String::new();
                while matches!(lines.read_line(&mut line).await, Ok(n) if n > 0) {
                    let _ = tx.send(std::mem::take(&mut line));
                }
            });
        }
    });
    (port, rx)
}

fn atg_reading() -> LogEvent {
    LogEvent::new(
        EventKind::AtgReading,
        Utc::now(),
        json!({"volume_l": 42_350.0, "temperature_c": 12.5}),
    )
}

fn login() -> LogEvent {
    LogEvent::new(
        EventKind::Login,
        Utc::now(),
        json!({"username": "jrenard", "role": "operator", "session_id": "0a98829b", "result": "success"}),
    )
}

/// Runs the HTTP pusher for one event against `url` and returns what the
/// server received within [`DELIVERY_WAIT`], if anything.
async fn push_over_https(url: String, rx: &mut mpsc::UnboundedReceiver<String>) -> Option<String> {
    let queue = EventQueue::new(8);
    let task = tokio::spawn(log_pusher::run(Arc::clone(&queue), url));
    queue.push(atg_reading());
    queue.close();
    let received = tokio::time::timeout(DELIVERY_WAIT, rx.recv())
        .await
        .ok()
        .flatten();
    task.abort();
    received
}

/// Runs the syslog pusher for `event` against `host:port` and returns the
/// first line the listener received within [`DELIVERY_WAIT`], if any.
async fn push_over_syslog(
    host: &str,
    port: u16,
    event: LogEvent,
    rx: &mut mpsc::UnboundedReceiver<String>,
) -> Option<String> {
    let queue = EventQueue::new(8);
    let task = tokio::spawn(syslog::run(
        Arc::clone(&queue),
        host.to_owned(),
        port,
        "fms-01".to_owned(),
    ));
    queue.push(event);
    queue.close();
    let received = tokio::time::timeout(DELIVERY_WAIT, rx.recv())
        .await
        .ok()
        .flatten();
    task.abort();
    received
}

// ── HTTPS: vmagent and vlagent's JSON-line endpoint share log_pusher::run ────

#[tokio::test]
async fn https_push_is_delivered_to_a_server_chaining_to_the_system_store_root() {
    let (cert_pem, key_pem) = system_root().sign_leaf("localhost");
    let (port, mut rx) = start_https_intake(cert_pem, key_pem).await;

    let body = push_over_https(format!("https://localhost:{port}/api/v1/import"), &mut rx).await;

    let body = body.expect(
        "no push reached a server whose certificate chains to the root in the system \
         trust store — the FMS's HTTP client is not consulting that store",
    );
    assert!(body.contains("atg_tank_volume_litres"), "{body}");
}

#[tokio::test]
async fn https_push_is_withheld_from_a_server_chaining_to_an_unrelated_root() {
    system_root();
    let (cert_pem, key_pem) = TestCa::new().sign_leaf("localhost");
    let (port, mut rx) = start_https_intake(cert_pem, key_pem).await;

    let body = push_over_https(format!("https://localhost:{port}/api/v1/import"), &mut rx).await;

    assert!(
        body.is_none(),
        "a push reached a server whose certificate chains to no trusted root: {body:?}"
    );
}

// ── HTTPS failure reporting (fms.md §3) ──────────────────────────────────────
//
// A rejected certificate must be logged with rustls's reason and distinctly from
// a connection refused or timed out. These classify one real attempt made with
// the production client; the WARN that carries the classification is
// push_until_delivered's.

/// One POST with the production client, which must fail; its classification.
async fn failed_attempt(url: String) -> PushFailure {
    let err = log_pusher::client()
        .post(url)
        .body("probe")
        .send()
        .await
        .expect_err("the attempt was expected to fail");
    PushFailure::from_error(&err)
}

#[tokio::test]
async fn an_untrusted_certificate_is_reported_as_a_rejection_with_the_rustls_reason() {
    system_root();
    let (cert_pem, key_pem) = TestCa::new().sign_leaf("localhost");
    let (port, _rx) = start_https_intake(cert_pem, key_pem).await;

    let failure = failed_attempt(format!("https://localhost:{port}/api/v1/import")).await;

    assert!(matches!(failure, PushFailure::Certificate(_)), "{failure}");
    assert!(failure.to_string().contains("UnknownIssuer"), "{failure}");
}

#[tokio::test]
async fn a_certificate_for_another_host_is_reported_as_a_rejection_with_the_rustls_reason() {
    let (cert_pem, key_pem) = system_root().sign_leaf("hist-01");
    let (port, _rx) = start_https_intake(cert_pem, key_pem).await;

    let failure = failed_attempt(format!("https://localhost:{port}/api/v1/import")).await;

    assert!(matches!(failure, PushFailure::Certificate(_)), "{failure}");
    let text = failure.to_string();
    assert!(
        text.contains("not valid for name") || text.contains("NotValidForName"),
        "{text}"
    );
}

#[tokio::test]
async fn a_refused_connection_is_reported_as_unreachable_not_as_a_rejection() {
    system_root();
    // Bound and released, so nothing is listening on it.
    let port = StdTcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();

    let failure = failed_attempt(format!("https://localhost:{port}/api/v1/import")).await;

    assert!(matches!(failure, PushFailure::Unreachable(_)), "{failure}");
}

// ── Syslog over TLS (RFC 5425) ───────────────────────────────────────────────

#[tokio::test]
async fn syslog_push_is_delivered_over_tls_to_a_listener_chaining_to_the_system_store_root() {
    let (cert_pem, key_pem) = system_root().sign_leaf("localhost");
    let (port, mut rx) = start_syslog_tls_listener(cert_pem, key_pem).await;
    let event = login();
    let expected = syslog::frame(&event, "fms-01").unwrap();

    let line = push_over_syslog("localhost", port, event, &mut rx).await;

    let line = line.expect(
        "no syslog frame arrived over a completed TLS handshake — the sender is not \
         speaking TLS, not trusting the system store root, or not flushing the stream",
    );
    assert_eq!(line, expected);
}

#[tokio::test]
async fn syslog_push_is_withheld_from_a_listener_chaining_to_an_unrelated_root() {
    system_root();
    let (cert_pem, key_pem) = TestCa::new().sign_leaf("localhost");
    let (port, mut rx) = start_syslog_tls_listener(cert_pem, key_pem).await;

    let line = push_over_syslog("localhost", port, login(), &mut rx).await;

    assert!(
        line.is_none(),
        "a syslog frame reached a listener whose certificate chains to no trusted root: {line:?}"
    );
}

#[tokio::test]
async fn syslog_push_is_withheld_when_the_certificate_names_another_host() {
    // hist-01's certificate carries a DNS SAN of "hist-01" only. Reached by any
    // other name the chain is still trusted but the name is not, and the
    // connection must fail — the check a hand-built TLS client configuration
    // is most easily written without.
    let (cert_pem, key_pem) = system_root().sign_leaf("hist-01");
    let (port, mut rx) = start_syslog_tls_listener(cert_pem, key_pem).await;

    let line = push_over_syslog("localhost", port, login(), &mut rx).await;

    assert!(
        line.is_none(),
        "a syslog frame reached a listener whose certificate names another host: {line:?}"
    );
}
