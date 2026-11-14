pub mod auth;
pub mod jobs;
pub mod tanks;
pub mod tickets;

use axum::routing::{delete, get, patch, post};
use axum::Json;
use serde_json::{json, Value};
use tower_http::trace::{DefaultMakeSpan, DefaultOnFailure, DefaultOnResponse, TraceLayer};
use tracing::Level;

use crate::state::AppState;

async fn health() -> Json<Value> {
    Json(json!({ "status": "ok" }))
}

pub fn router(state: AppState) -> axum::Router {
    axum::Router::new()
        .route("/health", get(health))
        .route("/api/auth/login", post(auth::login))
        .route("/api/auth/challenge/{challenge_id}", get(auth::challenge))
        .route("/api/auth/logout", delete(auth::logout))
        .route("/api/jobs/refuel", post(jobs::create_refuel))
        .route("/api/jobs/tank-fill", post(jobs::create_tank_fill))
        .route("/api/jobs/current", get(jobs::current_status))
        .route(
            "/api/jobs/current/dispenser/totaliser",
            get(jobs::get_totaliser),
        )
        .route(
            "/api/jobs/{job_id}",
            patch(jobs::patch_bdn).delete(jobs::cancel),
        )
        .route(
            "/api/jobs/{job_id}/dispenser/start",
            post(jobs::start_dispense),
        )
        .route("/api/tanks/{tank_id}/level", get(tanks::get_level))
        .route("/api/delivery-tickets", post(tickets::create_ticket))
        .with_state(state)
        // HTTP access logging (see the FMS router for the level rationale).
        // Successful requests at DEBUG, 5xx at ERROR via on_failure. The reason
        // for any 4xx/5xx is logged by HmiError::into_response.
        .layer(
            TraceLayer::new_for_http()
                .make_span_with(DefaultMakeSpan::new().level(Level::DEBUG))
                .on_response(DefaultOnResponse::new().level(Level::DEBUG))
                .on_failure(DefaultOnFailure::new().level(Level::ERROR)),
        )
}
