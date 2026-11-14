pub mod api;
pub mod background;
pub mod config;
pub mod db;
pub mod error;
pub mod log_queue;
pub mod state;

use std::{net::SocketAddr, sync::Arc};

use axum_server::tls_rustls::RustlsConfig;
use tracing_subscriber::EnvFilter;

use config::Config;
use db::{connect, run_migrations};
use state::AppState;

pub async fn run() -> anyhow::Result<()> {
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("failed to install rustls ring crypto provider");

    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::from_default_env()
                .add_directive("fms=info".parse().unwrap())
                // Surface the TraceLayer's on_failure (5xx) events, which are
                // emitted under the tower_http target.
                .add_directive("tower_http=info".parse().unwrap()),
        )
        .init();

    let config = Config::from_env()?;

    let pool = connect(&config.database_url).await?;
    run_migrations(&pool).await?;

    // Enrol the configured RFID cards (operator + vessels) that are not yet in
    // the card_table, so a freshly-deployed FMS accepts the operator login and
    // vessel RFID gate without a manual enrolment step. No-op when the cards are
    // already present or FMS_SEED_CARDS is unset.
    let seeded = db::seed_cards(&pool, &config.seed_cards).await?;
    if seeded > 0 {
        tracing::info!("enrolled {seeded} seed RFID card(s)");
    }

    // One queue and one pusher task per target class, so an unreachable target
    // cannot hold up a class whose own target is healthy (fms.md §5.4).
    let queues = log_queue::LogQueues::new(log_queue::QUEUE_DEPTH);
    let state = AppState::new(pool, Arc::new(config.clone()), queues.clone());

    tokio::spawn(background::atg::run(state.clone()));
    tokio::spawn(background::modbus::run(state.clone()));
    tokio::spawn(background::rfid::run(state.clone()));
    tokio::spawn(background::log_pusher::run(
        Arc::clone(&queues.metrics),
        config.vmagent_url.clone(),
    ));
    tokio::spawn(background::log_pusher::run(
        Arc::clone(&queues.events),
        config.vlagent_jsonline_url.clone(),
    ));
    tokio::spawn(background::syslog::run(
        Arc::clone(&queues.syslog),
        config.vlagent_syslog_host.clone(),
        config.vlagent_syslog_port,
        background::syslog::local_hostname(),
    ));

    let router = api::router(state);
    let addr = SocketAddr::from(([0, 0, 0, 0], config.listen_port));
    // No self-sign fallback (digital.md §8 decision 7): a missing or unreadable
    // certificate at the configured path is a startup failure, surfaced here by
    // simply letting that error propagate rather than working around it.
    let tls_config =
        RustlsConfig::from_pem_file(&config.tls_cert_path, &config.tls_key_path).await?;

    tracing::info!("listening on https://{addr}");
    axum_server::bind_rustls(addr, tls_config)
        .serve(router.into_make_service_with_connect_info::<SocketAddr>())
        .await?;

    Ok(())
}
