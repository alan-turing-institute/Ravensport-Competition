pub mod auth;
pub mod cards;
pub mod health;
pub mod jobs;
pub mod tanks;

use axum::{
    extract::Request,
    http::{Method, Uri},
    middleware::{self, Next},
    response::Response,
};
use tower_http::trace::{DefaultMakeSpan, DefaultOnFailure, DefaultOnResponse, TraceLayer};
use tracing::Level;

use crate::{error::AppError, state::AppState};

// ---------------------------------------------------------------------------
// Bearer-token auth middleware
// ---------------------------------------------------------------------------

pub async fn require_auth(
    axum::extract::State(state): axum::extract::State<AppState>,
    mut req: Request,
    next: Next,
) -> std::result::Result<Response, AppError> {
    let token = req
        .headers()
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::to_owned)
        .ok_or(AppError::Unauthorised)?;

    let session = state
        .get_session(&token)
        .await
        .ok_or(AppError::Unauthorised)?;

    req.extensions_mut().insert(session);
    Ok(next.run(req).await)
}

pub async fn require_admin(
    axum::extract::State(state): axum::extract::State<AppState>,
    mut req: Request,
    next: Next,
) -> std::result::Result<Response, AppError> {
    let token = req
        .headers()
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::to_owned)
        .ok_or(AppError::Unauthorised)?;

    let session = state
        .get_session(&token)
        .await
        .ok_or(AppError::Unauthorised)?;

    if session.role != "admin" {
        return Err(AppError::Forbidden);
    }

    req.extensions_mut().insert(session);
    Ok(next.run(req).await)
}

/// Catch-all for request paths no route matches.
///
/// Axum's built-in response is an empty-bodied 404 that constructs no
/// `AppError`, so a request for a path that does not exist is recorded nowhere,
/// while a 404 for a missing resource on a real route is logged. A deployed
/// system records both. Logging the method and path separates endpoint probing
/// from the authentication failures it would otherwise be filed alongside.
async fn unknown_route(method: Method, uri: Uri) -> AppError {
    tracing::warn!(method = %method, path = %uri.path(), "request to unknown endpoint");
    AppError::NotFound
}

/// Catch-all for a known path addressed with a verb it does not serve.
///
/// Axum's method router answers these with an empty-bodied 405 that reaches no
/// handler. A 405 confirms to the caller that the path exists, so it is more
/// informative to someone mapping the interface than a 404 is, and is recorded
/// here for the same reason.
async fn method_not_allowed(method: Method, uri: Uri) -> AppError {
    tracing::warn!(method = %method, path = %uri.path(), "request with unsupported method");
    AppError::MethodNotAllowed
}

pub fn router(state: AppState) -> axum::Router {
    use axum::routing::{delete, get, post};

    let auth_routes = axum::Router::new().route("/api/v1/auth/login", post(auth::login));

    let admin_routes = axum::Router::new()
        .route("/api/v1/cards", post(cards::enrol).get(cards::list))
        .route("/api/v1/cards/{card_id}", delete(cards::revoke))
        // `route_layer`, not `layer`: middleware attached with `layer` also runs
        // for requests matching no route, so an unknown path would be rejected
        // as unauthenticated (401) before routing concludes, rather than
        // reported as unknown (404) and logged as probing.
        .route_layer(middleware::from_fn_with_state(state.clone(), require_admin));

    let operator_routes = axum::Router::new()
        .route("/api/v1/auth/logout", delete(auth::logout))
        .route("/api/v1/jobs", post(jobs::create).get(jobs::list))
        .route("/api/v1/jobs/current", get(jobs::current_status))
        .route("/api/v1/jobs/current/totaliser", get(jobs::get_totaliser))
        .route("/api/v1/jobs/{job_id}", delete(jobs::cancel))
        .route("/api/v1/jobs/{job_id}/rfid", post(jobs::validate_rfid))
        .route("/api/v1/jobs/{job_id}/start", post(jobs::start_dispense))
        .route("/api/v1/jobs/{job_id}/close", post(jobs::close_fill))
        .route("/api/v1/jobs/{job_id}/bdn", post(jobs::link_bdn))
        .route("/api/v1/tanks/{tank_id}/level", get(tanks::get_level))
        // See the note on `admin_routes` above.
        .route_layer(middleware::from_fn_with_state(state.clone(), require_auth));

    let public_routes = axum::Router::new().route("/api/v1/health", get(health::health));

    axum::Router::new()
        .merge(public_routes)
        .merge(auth_routes)
        .merge(admin_routes)
        .merge(operator_routes)
        .fallback(unknown_route)
        .method_not_allowed_fallback(method_not_allowed)
        .with_state(state)
        // HTTP access logging. Successful requests are recorded at DEBUG (silent
        // under the default `info` filter, so health polls do not flood the
        // journal); a 5xx is recorded at ERROR by on_failure, so a server fault
        // is never silent even when it bypasses AppError. The specific reason for
        // any 4xx/5xx is logged separately by AppError::into_response.
        .layer(
            TraceLayer::new_for_http()
                .make_span_with(DefaultMakeSpan::new().level(Level::DEBUG))
                .on_response(DefaultOnResponse::new().level(Level::DEBUG))
                .on_failure(DefaultOnFailure::new().level(Level::ERROR)),
        )
}
