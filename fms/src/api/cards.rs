use axum::{
    extract::{Path, State},
    http::StatusCode,
    Json,
};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::json;
use uuid::Uuid;

use crate::{
    error::{AppError, Result},
    state::{AppState, EventKind, LogEvent},
};

#[derive(Deserialize)]
pub struct EnrolRequest {
    card_number: i32,
    label: String,
}

#[derive(Serialize)]
pub struct EnrolResponse {
    card_id: Uuid,
}

#[derive(Serialize)]
pub struct CardEntry {
    card_id: Uuid,
    card_number: i32,
    label: String,
    enrolled_at: chrono::DateTime<chrono::Utc>,
}

pub async fn enrol(
    State(state): State<AppState>,
    Json(body): Json<EnrolRequest>,
) -> Result<(StatusCode, Json<EnrolResponse>)> {
    let card_id = Uuid::new_v4();
    sqlx::query!(
        "INSERT INTO card_table (card_id, card_number, label, active, action)
         VALUES ($1, $2, $3, true, 'enrol')",
        card_id,
        body.card_number,
        body.label,
    )
    .execute(&state.db)
    .await?;

    // After the write succeeds, per fms.md §7.5.
    state.emit_log_event(LogEvent::new(
        EventKind::CardChange,
        Utc::now(),
        json!({
            "card_id": card_id,
            "card_number": body.card_number,
            "label": body.label,
            "action": "enrol",
        }),
    ));

    Ok((StatusCode::CREATED, Json(EnrolResponse { card_id })))
}

pub async fn list(State(state): State<AppState>) -> Result<Json<Vec<CardEntry>>> {
    // Return the latest row per card_id where active = true.
    let rows = sqlx::query!(
        r#"
        SELECT card_id, card_number, label, enrolled_at
        FROM (
            SELECT card_id, card_number, label, active, enrolled_at,
                   ROW_NUMBER() OVER (PARTITION BY card_id ORDER BY enrolled_at DESC) AS rn
            FROM card_table
        ) t
        WHERE rn = 1 AND active = true
        ORDER BY enrolled_at DESC
        "#
    )
    .fetch_all(&state.db)
    .await?;

    let entries = rows
        .into_iter()
        .map(|r| CardEntry {
            card_id: r.card_id,
            card_number: r.card_number,
            label: r.label,
            enrolled_at: r.enrolled_at,
        })
        .collect();

    Ok(Json(entries))
}

pub async fn revoke(
    State(state): State<AppState>,
    Path(card_id): Path<Uuid>,
) -> Result<StatusCode> {
    // Fetch the card's current card_number and label from the most recent enrol row.
    let row = sqlx::query!(
        "SELECT card_number, label FROM card_table WHERE card_id = $1 ORDER BY enrolled_at DESC LIMIT 1",
        card_id
    )
    .fetch_optional(&state.db)
    .await?
    .ok_or(AppError::NotFound)?;

    sqlx::query!(
        "INSERT INTO card_table (card_id, card_number, label, active, action)
         VALUES ($1, $2, $3, false, 'revoke')",
        card_id,
        row.card_number,
        row.label,
    )
    .execute(&state.db)
    .await?;

    state.emit_log_event(LogEvent::new(
        EventKind::CardChange,
        Utc::now(),
        json!({
            "card_id": card_id,
            "card_number": row.card_number,
            "label": row.label,
            "action": "revoke",
        }),
    ));

    Ok(StatusCode::NO_CONTENT)
}
