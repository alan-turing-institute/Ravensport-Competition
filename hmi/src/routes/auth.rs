use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};

use crate::error::{HmiError, Result};
use crate::fms::expires_at_from_now;
use crate::state::AppState;

#[derive(Deserialize)]
pub struct LoginRequest {
    pub username: String,
    pub password: String,
}

pub async fn login(
    State(state): State<AppState>,
    Json(body): Json<LoginRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    let challenge_id = state.challenges.insert(body.username, body.password).await;
    Ok((
        StatusCode::ACCEPTED,
        Json(json!({ "challenge_id": challenge_id })),
    ))
}

pub async fn challenge(
    State(state): State<AppState>,
    Path(challenge_id): Path<String>,
) -> Result<Json<Value>> {
    let pending = state
        .challenges
        .take(&challenge_id)
        .await
        .ok_or(HmiError::ChallengeExpired)?;

    let card = state
        .rfid
        .read_card()
        .await
        .map_err(|_| HmiError::RfidMismatch)?;

    let login_resp = state
        .fms
        .login(&pending.username, &pending.password, card)
        .await?;

    let expires_at = expires_at_from_now(1800);
    Ok(Json(json!({
        "token": login_resp.session_token,
        "expires_at": expires_at.to_rfc3339(),
    })))
}

pub async fn logout(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
) -> Result<StatusCode> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    state.fms.logout(token).await?;
    Ok(StatusCode::NO_CONTENT)
}

pub fn extract_bearer(headers: &axum::http::HeaderMap) -> Option<&str> {
    headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
}
