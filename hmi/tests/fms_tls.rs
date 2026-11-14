//! Confirms `FmsClient` (src/fms.rs) validates the FMS's TLS certificate
//! against a configured trust root instead of accepting anything, as
//! `danger_accept_invalid_certs` used to (digital.md §8 decision 7).
//!
//! Certificates are generated in-process with `rcgen` rather than by shelling
//! out to the `openssl` CLI, so this test carries no dependency on tools
//! installed on whatever machine runs `cargo test` — `openssl` is a real
//! dependency of the build host running `deploy/scripts/build-ca.sh`
//! (digital.md §2.7), but has no reason to be a dependency of running this
//! crate's own test suite, and hmi-01's deployed image never needs it at all.

use std::net::TcpListener as StdTcpListener;
use std::time::Duration;

use axum::{routing::post, Json, Router};
use axum_server::tls_rustls::RustlsConfig;
use hmi::fms::FmsClient;
use rcgen::{
    BasicConstraints, CertificateParams, DistinguishedName, DnType, ExtendedKeyUsagePurpose, IsCa,
    KeyPair, KeyUsagePurpose,
};
use serde_json::json;

/// A throwaway root CA, generated entirely in-process.
struct TestCa {
    cert: rcgen::Certificate,
    key: KeyPair,
}

impl TestCa {
    fn new() -> Self {
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        // basicConstraints/keyUsage set explicitly, not left to rcgen's
        // NoCa default: a strict TLS handshake verifier requires a CA acting
        // as a CA to assert CA:TRUE and keyCertSign, or it rejects the chain
        // — the same shape build-ca.sh's real root must have.
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        let mut dn = DistinguishedName::new();
        dn.push(DnType::CommonName, "Test CA");
        params.distinguished_name = dn;

        let key = KeyPair::generate().unwrap();
        let cert = params.self_signed(&key).unwrap();
        Self { cert, key }
    }

    fn ca_pem(&self) -> Vec<u8> {
        self.cert.pem().into_bytes()
    }

    /// Signs a leaf certificate for `hostname`, carrying a DNS SAN of
    /// `hostname` only — matching build-ca.sh's hostname-only SAN scheme
    /// (digital.md §2.7).
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

/// Starts a real HTTPS server on 127.0.0.1, presenting `cert_pem`/`key_pem`,
/// with a single route mirroring the FMS's login endpoint. Returns the port.
async fn start_test_fms(cert_pem: Vec<u8>, key_pem: Vec<u8>) -> u16 {
    // Idempotent: multiple tests in this binary each reach this, and a second
    // install attempt is an error we deliberately ignore.
    let _ = rustls::crypto::ring::default_provider().install_default();

    let router = Router::new().route(
        "/api/v1/auth/login",
        post(|| async { Json(json!({ "session_token": "test-token" })) }),
    );
    let tls_config = RustlsConfig::from_pem(cert_pem, key_pem).await.unwrap();
    let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        axum_server::from_tcp_rustls(listener, tls_config)
            .serve(router.into_make_service())
            .await
            .unwrap();
    });
    // Let the accept loop start before the first connection attempt.
    tokio::time::sleep(Duration::from_millis(50)).await;
    port
}

#[tokio::test]
async fn accepts_a_certificate_signed_by_the_configured_ca() {
    let ca = TestCa::new();
    let (cert_pem, key_pem) = ca.sign_leaf("localhost");
    let port = start_test_fms(cert_pem, key_pem).await;

    let client = FmsClient::new(format!("https://localhost:{port}"), Some(&ca.ca_pem())).unwrap();
    let result = client.login("operator", "password", 1234).await;

    if let Err(e) = &result {
        panic!("expected a trusted connection to succeed, got {e:?}");
    }
}

#[tokio::test]
async fn rejects_a_certificate_signed_by_an_unrelated_ca() {
    let server_ca = TestCa::new();
    let (cert_pem, key_pem) = server_ca.sign_leaf("localhost");
    let port = start_test_fms(cert_pem, key_pem).await;

    let other_ca = TestCa::new();
    let client = FmsClient::new(
        format!("https://localhost:{port}"),
        Some(&other_ca.ca_pem()),
    )
    .unwrap();
    let result = client.login("operator", "password", 1234).await;

    assert!(
        result.is_err(),
        "expected a certificate trust failure against an unrelated CA, got Ok"
    );
}

#[tokio::test]
async fn rejects_a_certificate_whose_san_does_not_match_the_requested_host() {
    // fms-01's real certificate carries a DNS SAN of "fms-01" only (digital.md
    // §2.7). A client connecting to that same key material by a different name
    // must fail hostname verification even though the chain of trust is valid —
    // this is the property `verifyhost`/reqwest's default hostname check gives
    // HAProxy and the HMI's own client alike, and is distinct from CA trust.
    let ca = TestCa::new();
    let (cert_pem, key_pem) = ca.sign_leaf("fms-01");
    let port = start_test_fms(cert_pem, key_pem).await;

    // Connect via "localhost", not "fms-01" — the cert is valid and CA-trusted,
    // but for the wrong name.
    let client = FmsClient::new(format!("https://localhost:{port}"), Some(&ca.ca_pem())).unwrap();
    let result = client.login("operator", "password", 1234).await;

    assert!(
        result.is_err(),
        "expected a hostname mismatch to fail verification, got Ok"
    );
}

#[tokio::test]
async fn rejects_an_unvalidated_certificate_when_no_ca_is_configured() {
    let ca = TestCa::new();
    let (cert_pem, key_pem) = ca.sign_leaf("localhost");
    let port = start_test_fms(cert_pem, key_pem).await;

    // No CA configured: FmsClient must not fall back to trusting anything,
    // proving danger_accept_invalid_certs is gone, not merely defaulted away.
    let client = FmsClient::new(format!("https://localhost:{port}"), None).unwrap();
    let result = client.login("operator", "password", 1234).await;

    assert!(
        result.is_err(),
        "expected a certificate trust failure with no CA configured, got Ok"
    );
}
