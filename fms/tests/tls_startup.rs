//! Confirms the FMS binary has no self-signed-certificate fallback: a missing
//! TLS certificate/key at the configured path is a hard startup failure
//! (digital.md §8 decision 7), not a silently generated stand-in.
//!
//! Requires a reachable Postgres, the same as fms/tests/api.rs (`make
//! test-fms` starts an ephemeral local cluster for this). This is deliberately
//! the only test in this binary: `fms::run()` installs the process-wide
//! rustls crypto provider on every call, which panics on a second install, so
//! a second `run()`-calling test in the same binary (tests run in parallel by
//! default) would be unsound.

use std::path::Path;
use std::time::Duration;

use sqlx::postgres::PgPoolOptions;
use uuid::Uuid;

fn pg_base_url() -> String {
    std::env::var("FMS_TEST_DATABASE_URL")
        .unwrap_or_else(|_| "postgres://postgres:postgres@127.0.0.1:5432".to_string())
}

async fn fresh_test_database_url() -> String {
    let base = pg_base_url();
    let admin_url = format!("{base}/postgres");
    let admin_pool = PgPoolOptions::new()
        .max_connections(1)
        .connect(&admin_url)
        .await
        .unwrap_or_else(|e| {
            panic!(
                "failed to connect to Postgres at {admin_url}: {e}\n\
                 This test requires a running Postgres. Run it via `make test-fms` \
                 (starts an ephemeral local cluster), or set $FMS_TEST_DATABASE_URL."
            )
        });
    let db_name = format!("fms_test_{}", Uuid::new_v4().to_string().replace('-', ""));
    sqlx::query(&format!("CREATE DATABASE \"{db_name}\""))
        .execute(&admin_pool)
        .await
        .expect("failed to create test database");
    format!("{base}/{db_name}")
}

#[tokio::test]
async fn run_fails_fast_when_the_tls_certificate_is_missing() {
    let db_url = fresh_test_database_url().await;

    // A writable directory that legitimately has no cert/key in it yet — not
    // an unwritable path — so that if a self-sign fallback were still present
    // it would succeed in writing there (and this test would time out below,
    // reporting exactly that), rather than failing for an unrelated reason
    // (e.g. a missing parent directory).
    let dir = std::env::temp_dir().join(format!("fms-tls-startup-test-{}", Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let cert_path = dir.join("cert.pem");
    let key_path = dir.join("key.pem");

    std::env::set_var("FMS_DATABASE_URL", &db_url);
    std::env::set_var("FMS_LISTEN_PORT", "0");
    std::env::set_var("FMS_TLS_CERT", cert_path.to_str().unwrap());
    std::env::set_var("FMS_TLS_KEY", key_path.to_str().unwrap());
    std::env::set_var("FMS_ADMIN_PASSWORD_HASH", "unused-in-this-test");
    std::env::set_var("FMS_LDAP_URI", "ldap://127.0.0.1:389");
    std::env::set_var("FMS_ATG_HOST", "127.0.0.1");
    std::env::set_var("FMS_DISPENSER_HOST", "127.0.0.1");
    std::env::set_var("FMS_VESSEL_RFID_HOST", "127.0.0.1");

    let outcome = tokio::time::timeout(Duration::from_secs(5), fms::run()).await;

    let _ = std::fs::remove_dir_all(&dir);

    match outcome {
        Ok(result) => assert!(
            result.is_err(),
            "expected fms::run() to fail with a missing TLS certificate, but it returned Ok"
        ),
        Err(_elapsed) => panic!(
            "fms::run() did not return within 5s with a missing TLS certificate — it likely \
             started serving anyway (self-sign fallback still present?)"
        ),
    }

    assert!(
        !Path::new(&cert_path).exists(),
        "a certificate was written to the configured path — the self-sign fallback is still \
         present; digital.md §8 decision 7 requires no fallback at all"
    );
}
