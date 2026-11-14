use axum::extract::{Path, State};
use axum::Json;

use crate::error::{AppError, RefusalCode, Result};
use crate::state::AppState;

pub async fn get_level(
    State(state): State<AppState>,
    Path(tank_id): Path<String>,
) -> Result<Json<serde_json::Value>> {
    if tank_id != "01" {
        return Err(AppError::NotFound);
    }
    let reading = state.last_atg.read().await;
    let r = reading.as_ref().ok_or_else(|| {
        AppError::Refused(
            RefusalCode::NoAtgReading,
            "no ATG reading is available yet".to_owned(),
        )
    })?;
    Ok(Json(serde_json::json!({
        "tank_id": tank_id,
        "level_mm": r.level_mm(),
        "volume_litres": r.volume_l(),
        "ullage_litres": r.ullage_l(),
        "temperature_c": r.temperature_c(),
        "timestamp": r.recorded_at().to_rfc3339(),
    })))
}
