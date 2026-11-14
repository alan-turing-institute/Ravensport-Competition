use axum::extract::State;
use axum::http::StatusCode;
use axum::Json;
use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::error::{HmiError, Result};
use crate::fms::FillClose;
use crate::routes::auth::extract_bearer;
use crate::state::AppState;

/// The delivery ticket as the operator transcribes it (hmi.md §7.4). Every field
/// is required: a ticket without its start time cannot be assigned to an audit
/// window, so it is refused here, before anything reaches the FMS.
#[derive(Deserialize)]
pub struct DeliveryTicketRequest {
    pub job_id: String,
    pub ticket_number: String,
    pub delivery_commenced_at: DateTime<Utc>,
    pub delivered_volume_litres: f64,
    pub temperature_c: f64,
    pub density_kg_per_l: f64,
    pub temp_corrected_volume_litres: f64,
    pub supplier: String,
    pub tanker_registration: String,
}

pub async fn create_ticket(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Json(body): Json<DeliveryTicketRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    let ticket = FillClose {
        ticket_number: &body.ticket_number,
        delivery_commenced_at: body.delivery_commenced_at,
        delivered_volume_l: body.delivered_volume_litres,
        temperature_c: body.temperature_c,
        density_kg_per_l: body.density_kg_per_l,
        temp_corrected_volume_l: body.temp_corrected_volume_litres,
        supplier: &body.supplier,
        tanker_registration: &body.tanker_registration,
    };
    let tx_id = state.fms.close_fill(token, &body.job_id, &ticket).await?;

    Ok((
        StatusCode::CREATED,
        Json(json!({
            "ticket_id": Uuid::new_v4(),
            "job_id": body.job_id,
            "recorded_at": Utc::now().to_rfc3339(),
            "transaction_id": tx_id,
        })),
    ))
}
