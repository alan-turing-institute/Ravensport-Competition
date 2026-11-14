use std::{net::SocketAddr, sync::Arc};

use tracing::info;
use tracing_subscriber::EnvFilter;

mod challenge;
mod config;
mod error;
mod fms;
mod rfid;
mod routes;
mod state;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::from_default_env()
                .add_directive("hmi=info".parse().unwrap())
                // Surface the TraceLayer's on_failure (5xx) events, emitted under
                // the tower_http target.
                .add_directive("tower_http=info".parse().unwrap()),
        )
        .init();

    let config = config::Config::from_env()?;

    // HAProxy sits in front of this service and terminates TLS (sim-network.md
    // §1); it listens on port 443, handles TLS and source IP binding, and
    // forwards plain HTTP here. This binary never terminates TLS itself.

    let ca_cert_pem = std::fs::read(&config.ca_cert_path)
        .map_err(|e| anyhow::anyhow!("reading CA certificate at {}: {e}", config.ca_cert_path))?;
    let fms_client = fms::FmsClient::new(config.fms_url.clone(), Some(&ca_cert_pem))?;
    let rfid = rfid::RfidReader::new(config.operator_rfid_addr);
    // Establish and maintain the operator-reader connection in the background so
    // the HTTP listener binds regardless of reader reachability, while the HMI
    // still attaches to the reader promptly (hmi.md §5).
    rfid.start();
    let app_state = state::AppState {
        fms: Arc::new(fms_client),
        challenges: challenge::ChallengeStore::new(config.challenge_timeout_secs),
        rfid,
    };

    let router = routes::router(app_state);
    let addr: SocketAddr = config.listen_addr;
    let listener = tokio::net::TcpListener::bind(addr).await?;

    info!("HMI listening on http://{addr} (TLS terminated at HAProxy)");
    axum::serve(listener, router).await?;

    Ok(())
}
