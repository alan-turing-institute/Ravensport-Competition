//! The syslog pusher chooses its own rustls crypto provider rather than relying
//! on the process-wide default.
//!
//! The FMS compiles in both of rustls's providers: `ring`, which `Cargo.toml`
//! asks for, and `aws-lc-rs`, which `axum-server`'s `tls-rustls` feature
//! enables. With both present and no process default installed,
//! `rustls::ClientConfig::builder()` panics. `fms::run` installs `ring` before
//! spawning anything, so the binary is unaffected, but any other caller of
//! `syslog::run` loses the task at startup — the sim<->HMI<->FMS harness is one.
//! The two HTTP pushers are unaffected because reqwest chooses its provider
//! itself.
//!
//! This file is a separate test binary so that its process starts with no
//! default provider. No test here may install one: `hist_push_trust.rs` does,
//! which is why its syslog test cannot observe this dependency.

use std::time::Duration;

use chrono::Utc;
use rustls::crypto::CryptoProvider;
use tokio::net::TcpListener;
use tokio::task::JoinError;

use fms::background::syslog;
use fms::log_queue::EventQueue;
use fms::state::{EventKind, LogEvent};

/// How a task that should still be running ended.
fn describe(ended: Result<(), JoinError>) -> String {
    match ended {
        Ok(()) => "returned".to_owned(),
        Err(e) if e.is_panic() => {
            let payload = e.into_panic();
            let message = payload
                .downcast_ref::<&str>()
                .map(|s| (*s).to_owned())
                .or_else(|| payload.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "non-string payload".to_owned());
            format!("panicked: {message}")
        }
        Err(e) => e.to_string(),
    }
}

#[tokio::test]
async fn syslog_pusher_dials_without_a_process_default_crypto_provider() {
    assert!(
        CryptoProvider::get_default().is_none(),
        "a process-default crypto provider is already installed, so this test cannot observe the pusher depending on one"
    );

    // The pusher builds its TLS connector before it dials, so a TCP connection
    // arriving here shows the connector was built. No handshake is needed.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();

    let queue = EventQueue::new(8);
    queue.push(LogEvent::new(
        EventKind::Login,
        Utc::now(),
        serde_json::json!({ "username": "jrenard" }),
    ));
    let mut task = tokio::spawn(syslog::run(
        queue,
        "127.0.0.1".to_owned(),
        port,
        "fms-01".to_owned(),
    ));

    tokio::select! {
        accepted = listener.accept() => {
            accepted.expect("accept the pusher's connection");
        }
        ended = &mut task => panic!("syslog pusher stopped before dialling: {}", describe(ended)),
        () = tokio::time::sleep(Duration::from_secs(10)) => {
            panic!("syslog pusher did not dial within 10 s")
        }
    }
    task.abort();

    // `ClientConfig::builder()` installs the process default itself when exactly
    // one provider is compiled in, rather than panicking. Checking the default
    // is still absent therefore detects the dependency whichever providers the
    // dependency graph enables.
    assert!(
        CryptoProvider::get_default().is_none(),
        "the syslog pusher installed the process-default crypto provider, so it still depends on it"
    );
}
