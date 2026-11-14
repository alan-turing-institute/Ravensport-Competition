use axum::extract::{Path, State};
use axum::Json;
use serde_json::Value;

use crate::error::{HmiError, Result};
use crate::routes::auth::extract_bearer;
use crate::state::AppState;

pub async fn get_level(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
    Path(tank_id): Path<String>,
) -> Result<Json<Value>> {
    let token = extract_bearer(&headers).ok_or(HmiError::Unauthorised)?;
    let level = state.fms.get_tank_level(token, &tank_id).await?;
    Ok(Json(level))
}
