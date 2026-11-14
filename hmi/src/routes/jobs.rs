use axum::extract::rejection::PathRejection;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::Json;
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::error::{HmiError, Result};
use crate::routes::auth::extract_bearer;
use crate::state::AppState;

#[derive(Deserialize)]
pub struct RefuelRequest {
    pub vessel_id: String,
    pub product: String,
    #[allow(dead_code)]
    pub requested_volume_litres: f64,
}

#[derive(Deserialize)]
pub struct TankFillRequest {
    pub tanker_registration: String,
    pub product: String,
    #[allow(dead_code)]
    pub delivery_volume_litres: f64,
}

#[derive(Deserialize)]
pub struct DispenserStartRequest {
    pub preset_volume_litres: f64,
}

#[derive(Deserialize)]
pub struct BdnRequest {
    pub bdn_number: String,
}

pub async fn create_refuel(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Json(body): Json<RefuelRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    let job_id = state
        .fms
        .create_dispense_job(token, &body.vessel_id, &body.product)
        .await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({ "job_id": job_id, "status": "pending_vessel_rfid" })),
    ))
}

pub async fn create_tank_fill(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Json(body): Json<TankFillRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    let job_id = state
        .fms
        .create_fill_job(token, &body.tanker_registration, &body.product)
        .await?;
    Ok((
        StatusCode::CREATED,
        Json(json!({ "job_id": job_id, "status": "open" })),
    ))
}

pub async fn current_status(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
) -> Result<Json<Value>> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    let job = state.fms.current_status(token).await?;
    Ok(Json(json!({
        "job_id": job.job_id,
        "type": job.job_type,
        "status": job.status,
        "opened_at": job.opened_at,
    })))
}

pub async fn start_dispense(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Path(job_id): Path<String>,
    Json(body): Json<DispenserStartRequest>,
) -> Result<Json<Value>> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    state
        .fms
        .start_dispense(token, &job_id, body.preset_volume_litres)
        .await?;
    Ok(Json(json!({ "job_id": job_id, "status": "dispensing" })))
}

pub async fn patch_bdn(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Path(job_id): Path<String>,
    Json(body): Json<BdnRequest>,
) -> Result<Json<Value>> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    state
        .fms
        .link_bdn_by_job(token, &job_id, &body.bdn_number)
        .await?;
    Ok(Json(
        json!({ "job_id": job_id, "bdn_number": body.bdn_number }),
    ))
}

/// `DELETE /api/jobs/{job_id}`: cancels the job, forwarding the FMS's decision
/// (hmi.md §7.3). The dock operator cancels a job whose start the FMS refused,
/// which frees the single job slot. The FMS cancels an active job only, and
/// answers 409 once dispensing has started or metering is complete
/// (fms.md §7.4); that answer reaches the caller unchanged.
///
/// The FMS identifies every job by a UUID (fms.md §7.4). An identifier that is
/// not one is refused with `400` before any request is made. The FMS request's
/// path is built from the parsed UUID, so it addresses this job's cancel only.
pub async fn cancel(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    job_id: std::result::Result<Path<Uuid>, PathRejection>,
) -> Result<StatusCode> {
    let Path(job_id) = job_id.map_err(|_| HmiError::InvalidJobId)?;
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    state.fms.cancel_job(token, job_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

pub async fn get_totaliser(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
) -> Result<Json<Value>> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    let totaliser = state.fms.get_totaliser(token).await?;
    Ok(Json(totaliser))
}
