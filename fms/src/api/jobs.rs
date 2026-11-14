use axum::{
    extract::{Extension, Path, State},
    http::StatusCode,
    Json,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::{
    db::is_card_active_for,
    error::{AppError, RefusalCode, Result},
    state::{AppState, Envelope, EventKind, Job, JobStatus, JobType, LogEvent, Session},
};

// ---------------------------------------------------------------------------
// Request / response types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct CreateJobRequest {
    #[serde(rename = "type")]
    job_type: String,
    counterparty: String,
    grade: String,
    preset_volume: Option<f64>,
    #[allow(dead_code)]
    expected_volume: Option<f64>,
}

#[derive(Serialize)]
pub struct JobResponse {
    job_id: Uuid,
    #[serde(rename = "type")]
    job_type: String,
    status: String,
    counterparty: String,
    grade: String,
    operator: String,
    rfid_validated: bool,
}

/// Response for `GET /jobs/current`: the live job's identity and state only.
/// `rfid_validated` is retained because the HMI's status translation needs it.
#[derive(Serialize)]
pub struct CurrentJobResponse {
    job_id: Uuid,
    #[serde(rename = "type")]
    job_type: String,
    status: String,
    rfid_validated: bool,
    /// The dispense start, the instant `transaction_open` publishes. `None`
    /// before the start and for a fill (fms.md §7.4).
    opened_at: Option<DateTime<Utc>>,
}

#[derive(Deserialize)]
pub struct ValidateRfidRequest {
    card_number: i32,
}

#[derive(Deserialize)]
pub struct StartDispenseRequest {
    preset_volume: f64,
}

#[derive(Deserialize)]
pub struct BdnRequest {
    bdn_number: String,
}

/// The delivery ticket as transcribed by the operator (fms.md §7): every field
/// is the road tanker's own, so the FMS records rather than computes them.
#[derive(Deserialize)]
pub struct CloseFillRequest {
    ticket_number: String,
    /// The start time printed on the ticket. Recorded as the transaction's
    /// `opened_at`: the FMS does not observe the tanker's pump, and the paper
    /// and the record must assign the transfer to an audit window by the same
    /// instant (sim.md §8.3).
    delivery_commenced_at: DateTime<Utc>,
    delivered_volume_l: f64,
    temperature_c: f64,
    density_kg_per_l: f64,
    temp_corrected_volume_l: f64,
    supplier: String,
    tanker_registration: String,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn job_type_str(job_type: &JobType) -> &'static str {
    match job_type {
        JobType::Dispense => "dispense",
        JobType::Fill => "fill",
    }
}

fn job_status_str(status: &JobStatus) -> &'static str {
    match status {
        JobStatus::Active => "active",
        JobStatus::Running => "running",
        JobStatus::AwaitingBdn => "awaiting_bdn",
        JobStatus::Closed => "closed",
        JobStatus::Cancelled => "cancelled",
    }
}

fn job_to_response(job: &Job) -> JobResponse {
    JobResponse {
        job_id: job.job_id(),
        job_type: job_type_str(&job.job_type()).to_owned(),
        status: job_status_str(&job.status()).to_owned(),
        counterparty: job.counterparty().to_owned(),
        grade: job.grade().to_owned(),
        operator: job.operator().to_owned(),
        rfid_validated: job.rfid_validated(),
    }
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

pub async fn create(
    State(state): State<AppState>,
    Extension(session): Extension<Session>,
    Json(body): Json<CreateJobRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    // The tank holds one grade (fms.md §7.4). A job for any other is refused
    // before any other check: no ATG read is taken and no job is created.
    if body.grade != state.config.grade {
        return Err(AppError::UnprocessableEntity(format!(
            "grade {:?} is not the grade the tank holds ({:?})",
            body.grade, state.config.grade,
        )));
    }

    let job_type = match body.job_type.as_str() {
        "dispense" => JobType::Dispense,
        "fill" => JobType::Fill,
        _ => return Err(AppError::UnprocessableEntity("invalid job type".to_owned())),
    };

    // The rest of the request is validated next, as the start validates its
    // preset first: an invalid request answers 422 without a code, before the
    // job slot or the gauge is consulted. `Job::new` and `set_preset_volume_l`
    // apply the same checks below.
    Job::validate_fields(&body.counterparty, &body.grade, &session.username)?;
    let preset = match body.preset_volume {
        // A dispense's preset must fit the dispenser's register, and is held at
        // its resolution; the start's preset replaces it (fms.md §7.4).
        Some(requested) if job_type == JobType::Dispense => {
            Some(crate::state::register_preset_l(requested)?)
        }
        Some(requested) => {
            Job::validate_preset_volume_l(job_type, requested)?;
            Some(requested)
        }
        None => None,
    };

    // A job in progress refuses the request before any ATG read (digital.md
    // §4.1, step 2). The claim below still decides under one write lock.
    if state.job_in_progress().await {
        return Err(AppError::Conflict("a job is already active".to_owned()));
    }

    // Fresh tank volume, captured on-demand so the open reading is taken
    // identically to the close reading (fms.md §5.1) and the cavitation gate
    // sees the current level rather than one up to a poll interval stale. Used
    // for both the gate below and the recorded open volume; the tank is static
    // before flow starts, so the read is accurate. A dispense's check takes a
    // fresh reading only, never the cached one, so a failed read refuses the
    // dispense. A fill is exempt from the check, and an unavailable reading
    // must not block it: a failed read records null (fms.md §5.1, §8.4).
    let atg_volume = if job_type == JobType::Dispense {
        let v = fresh_volume_for_check(&state, "job open").await?;
        // Cavitation threshold check: the current volume must be strictly
        // above the threshold.
        let threshold = state.config.cavitation_volume_l();
        if v <= threshold {
            return Err(AppError::Refused(
                RefusalCode::CavitationThreshold,
                format!(
                    "tank level {v:.0} L is at or below the cavitation threshold ({threshold:.0} L)"
                ),
            ));
        }
        Some(v)
    } else {
        crate::background::atg::read_tc_volume_or_null(&state, "job open").await
    };

    let mut job = Job::new(
        job_type,
        body.counterparty.clone(),
        body.grade.clone(),
        session.username.clone(),
        session.session_id,
        atg_volume,
    )?;
    let job_id = job.job_id();
    let job_created_at = job.created_at();
    let envelope = Envelope::from_job(&job);

    if let Some(preset) = preset {
        job.set_preset_volume_l(preset)?;
    }

    // Atomically claim the single-job slot. The check and the set occur under
    // one write lock (AppState::try_set_current_job), so two concurrent creates
    // cannot both observe an empty slot and proceed.
    if !state.try_set_current_job(job).await {
        return Err(AppError::Conflict("a job is already active".to_owned()));
    }

    // Persist the immutable creation record. The slot is already claimed, so no
    // other create can interleave; if the insert fails we release the slot so a
    // failed create does not strand it. `session_id` is the session whose token
    // made this request — the one the job was created under (fms.md §8.3).
    if let Err(e) = sqlx::query!(
        "INSERT INTO jobs (job_id, type, counterparty, grade, operator, session_id, atg_volume_at_open_l, created_at)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        job_id,
        body.job_type,
        body.counterparty,
        body.grade,
        session.username,
        session.session_id,
        atg_volume,
        job_created_at,
    )
    .execute(&state.db)
    .await
    {
        state.clear_current_job(job_id).await;
        return Err(e.into());
    }

    state.emit_log_event(LogEvent::bunkering(
        EventKind::JobOpened,
        job_created_at,
        &envelope,
        json!({
            "atg_volume_at_open_l": atg_volume,
            "created_at": job_created_at,
        }),
    ));

    Ok((
        StatusCode::CREATED,
        Json(json!({ "job_id": job_id, "type": body.job_type, "status": "active" })),
    ))
}

pub async fn list(State(state): State<AppState>) -> Json<Vec<JobResponse>> {
    let guard = state.current_job.read().await;
    let jobs: Vec<JobResponse> = guard.as_ref().map(job_to_response).into_iter().collect();
    Json(jobs)
}

/// GET /jobs/current — the single in-flight job, or 404 if none.
///
/// There is only ever one current job (narrative §273, no concurrent
/// transactions), so it is addressed as a fixed resource rather than by id.
pub async fn current_status(State(state): State<AppState>) -> Result<Json<CurrentJobResponse>> {
    let guard = state.current_job.read().await;
    let job = guard.as_ref().ok_or(AppError::NotFound)?;
    Ok(Json(CurrentJobResponse {
        job_id: job.job_id(),
        job_type: job_type_str(&job.job_type()).to_owned(),
        status: job_status_str(&job.status()).to_owned(),
        rfid_validated: job.rfid_validated(),
        opened_at: job.tx_opened_at(),
    }))
}

/// Cancels an active job (fms.md §7.4): the job the dock operator abandons after
/// the FMS refuses its start, before any fuel has moved. A running dispense, one
/// awaiting its BDN, and a closed job answer 409. The cancellation is recorded
/// in `job_cancellations` under the user and session that made it, which need
/// not be the creator's (§8.5), and published as `job_cancelled`.
///
/// The status check, the record and the status change are made under one hold
/// of the job slot, so no start or create interleaves with them. Freeing the
/// slot before the record committed would let a create replace the job, leaving
/// a cancellation with no durable record. A job already cancelled answers 204
/// and records nothing further, so a cancel whose response was lost can be sent
/// again.
pub async fn cancel(
    State(state): State<AppState>,
    Extension(session): Extension<Session>,
    Path(job_id): Path<Uuid>,
) -> Result<StatusCode> {
    let now = Utc::now();
    let envelope = {
        let mut guard = state.current_job.write().await;
        let job = guard
            .as_mut()
            .filter(|j| j.job_id() == job_id)
            .ok_or(AppError::NotFound)?;
        match job.status() {
            JobStatus::Cancelled => return Ok(StatusCode::NO_CONTENT),
            JobStatus::Active => {}
            JobStatus::Running | JobStatus::AwaitingBdn | JobStatus::Closed => {
                return Err(AppError::Conflict("job is not in active state".to_owned()));
            }
        }
        sqlx::query!(
            "INSERT INTO job_cancellations (job_id, operator, session_id, cancelled_at)
             VALUES ($1, $2, $3, $4)",
            job_id,
            session.username,
            session.session_id,
            now,
        )
        .execute(&state.db)
        .await?;
        job.set_status(JobStatus::Cancelled);
        Envelope::from_job(job)
    };

    state.emit_log_event(LogEvent::bunkering(
        EventKind::JobCancelled,
        now,
        &envelope,
        json!({
            "cancelled_by": session.username,
            "cancelled_session_id": session.session_id,
            "cancelled_at": now,
        }),
    ));
    Ok(StatusCode::NO_CONTENT)
}

pub async fn validate_rfid(
    State(state): State<AppState>,
    Path(job_id): Path<Uuid>,
    Json(body): Json<ValidateRfidRequest>,
) -> Result<StatusCode> {
    // The vessel the job names. Read and released before the card lookup so the
    // job lock is not held across it.
    let vessel = {
        let guard = state.current_job.read().await;
        guard
            .as_ref()
            .filter(|j| j.job_id() == job_id)
            .map(|j| j.counterparty().to_owned())
            .ok_or(AppError::NotFound)?
    };
    // The card must be active and enrolled to that vessel, so another vessel's
    // tag or an operator's card is refused.
    if !is_card_active_for(&state.db, body.card_number, &vessel).await? {
        return Err(AppError::Forbidden);
    }
    let mut guard = state.current_job.write().await;
    if let Some(j) = guard.as_mut().filter(|j| j.job_id() == job_id) {
        j.set_rfid_validated(true);
        return Ok(StatusCode::OK);
    }
    Err(AppError::NotFound)
}

/// A fresh on-demand ATG reading for a cavitation check, which never falls
/// back to the cached reading (fms.md §5.1). A failed read means the check
/// cannot be evaluated, so the request is refused with `atg_read_failed`; the
/// reason is logged here, and the response does not carry it.
async fn fresh_volume_for_check(state: &AppState, label: &str) -> Result<f64> {
    crate::background::atg::read_tc_volume_on_demand(&state.config)
        .await
        .map_err(|e| {
            tracing::warn!(
                "on-demand ATG read at {label} failed ({e}); the cavitation check cannot be \
                 evaluated"
            );
            AppError::Refused(
                RefusalCode::AtgReadFailed,
                "the on-demand ATG read failed, so the cavitation check cannot be evaluated"
                    .to_owned(),
            )
        })
}

/// Checks that `job` is the dispense job `job_id` and may start: `404` if it is
/// not that job, `409` if its vessel card is not yet validated or it is not
/// active.
fn check_startable(job: Option<&Job>, job_id: Uuid) -> Result<()> {
    let job = job
        .filter(|j| j.job_id() == job_id && j.job_type() == JobType::Dispense)
        .ok_or(AppError::NotFound)?;
    if !job.rfid_validated() {
        return Err(AppError::Conflict(
            "vessel RFID not yet validated".to_owned(),
        ));
    }
    if job.status() != JobStatus::Active {
        return Err(AppError::Conflict("job is not in active state".to_owned()));
    }
    Ok(())
}

/// Transitions a dispense job to Running, after the cavitation check at the
/// start (fms.md §5.1): on a fresh on-demand reading, the current volume and
/// the projected closing volume, the current volume less the preset, must both
/// be strictly above the threshold. The check runs before the job changes, so a
/// refusal leaves the job `active`, with no `transaction_open` event and no
/// Modbus write. No DB row is written here; the Modbus background task writes
/// preset/enable and inserts the complete transaction row when the preset
/// volume is reached.
pub async fn start_dispense(
    State(state): State<AppState>,
    Path(job_id): Path<Uuid>,
    Json(body): Json<StartDispenseRequest>,
) -> Result<StatusCode> {
    // The preset as the dispenser's register holds it, rounded to 0.1 L: the
    // volume checked, recorded and written (fms.md §7.4).
    let preset = crate::state::register_preset_l(body.preset_volume)?;

    // A job that cannot start is refused before the gauge is read.
    check_startable(state.current_job.read().await.as_ref(), job_id)?;

    // On-demand read: the pump has not started, so the level is static and this
    // captures the true start-of-flow volume for the ±1% cross-check (fms.md §5.1).
    let atg_volume = fresh_volume_for_check(&state, "dispense open").await?;
    let auth_id = Uuid::new_v4();
    let now = Utc::now();

    let envelope = {
        let mut guard = state.current_job.write().await;
        // The job may have changed while the gauge was read.
        check_startable(guard.as_ref(), job_id)?;
        let job = guard
            .as_mut()
            .filter(|j| j.job_id() == job_id)
            .ok_or(AppError::NotFound)?;

        let threshold = state.config.cavitation_volume_l();
        if atg_volume <= threshold {
            return Err(AppError::Refused(
                RefusalCode::CavitationThreshold,
                format!(
                    "tank level {atg_volume:.0} L is at or below the cavitation threshold \
                     ({threshold:.0} L)"
                ),
            ));
        }
        let projected = atg_volume - preset;
        if projected <= threshold {
            return Err(AppError::Refused(
                RefusalCode::CavitationThreshold,
                format!(
                    "projected closing volume {projected:.0} L (tank level {atg_volume:.0} L \
                     less the preset {preset} L) is at or below the cavitation threshold \
                     ({threshold:.0} L)"
                ),
            ));
        }

        job.set_preset_volume_l(preset)?;
        job.set_atg_volume_at_tx_open(atg_volume)?;
        job.set_status(JobStatus::Running);
        job.set_authorisation_id(auth_id);
        // The transfer's start: the dispense close records it as `opened_at`,
        // and `transaction_open` below publishes the same instant.
        job.set_tx_opened_at(now);

        Envelope::from_job(job)
    };

    state.emit_log_event(LogEvent::bunkering(
        EventKind::TransactionOpen,
        now,
        &envelope,
        json!({
            "authorisation_id": auth_id,
            "preset_volume_l": preset,
            "atg_volume_at_open_l": atg_volume,
            "opened_at": now,
        }),
    ));

    Ok(StatusCode::OK)
}

/// Closes a fill job and records the delivery ticket transcription.
pub async fn close_fill(
    State(state): State<AppState>,
    Path(job_id): Path<Uuid>,
    Json(body): Json<CloseFillRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    // The tanker pump has stopped and the level is now static, so an on-demand
    // ATG read gives an accurate close volume for the ±1% reconciliation; the
    // periodic cache would be up to one poll interval stale, so a failed read
    // records null rather than the cache (fms.md §5.1).
    let atg_close = crate::background::atg::read_tc_volume_or_null(&state, "fill close").await;
    let tx_id = Uuid::new_v4();
    let now = Utc::now();

    let (envelope, atg_open) = {
        let mut guard = state.current_job.write().await;
        let job = guard
            .as_mut()
            .filter(|j| j.job_id() == job_id && j.job_type() == JobType::Fill)
            .ok_or(AppError::NotFound)?;

        if job.status() != JobStatus::Active {
            return Err(AppError::Conflict("fill job is not active".to_owned()));
        }

        let data = (Envelope::from_job(job), job.atg_volume_at_job_open());
        job.set_status(JobStatus::Closed);
        data
    };
    // The transfer's start is the ticket's, not the creation of the fill job.
    let opened_at = body.delivery_commenced_at;

    sqlx::query!(
        r#"INSERT INTO transactions
           (transaction_id, job_id, operator, counterparty, grade,
            ticket_number, delivered_volume_l, supplier, tanker_registration,
            temperature_c, density_kg_per_l, temp_corrected_volume_l,
            atg_volume_at_open_l, atg_volume_at_close_l,
            opened_at, closed_at, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)"#,
        tx_id,
        job_id,
        envelope.operator,
        envelope.counterparty,
        envelope.grade,
        body.ticket_number,
        body.delivered_volume_l,
        body.supplier,
        body.tanker_registration,
        body.temperature_c,
        body.density_kg_per_l,
        body.temp_corrected_volume_l,
        atg_open,
        atg_close,
        opened_at,
        now,
        now,
    )
    .execute(&state.db)
    .await?;

    state.emit_log_event(LogEvent::bunkering(
        EventKind::TransactionClosed,
        now,
        &envelope,
        json!({
            "transaction_id": tx_id,
            "ticket_number": body.ticket_number,
            "delivered_volume_l": body.delivered_volume_l,
            // The tanker's own figures, mirroring the paper ticket so the audit
            // can recompute the correction rather than accept it (hist.md §4.2).
            "temperature_c": body.temperature_c,
            "density_kg_per_l": body.density_kg_per_l,
            "temp_corrected_volume_l": body.temp_corrected_volume_l,
            "supplier": body.supplier,
            "tanker_registration": body.tanker_registration,
            "atg_volume_at_open_l": atg_open,
            "atg_volume_at_close_l": atg_close,
            "opened_at": opened_at,
            "closed_at": now,
        }),
    ));

    Ok((
        StatusCode::CREATED,
        Json(json!({ "transaction_id": tx_id })),
    ))
}

/// Links a Bunker Delivery Note to a dispense and closes the job.
///
/// The dispense transaction is resolved from the durable ledger keyed by
/// `job_id`, not from `current_job`, so a BDN can be linked even after the job
/// is no longer the live one. A BDN is valid only for a dispense (narrative
/// §2.2). The linkage is an appended row referencing the original transaction
/// (append-only, narrative §281); recording it closes the job (§279).
pub async fn link_bdn(
    State(state): State<AppState>,
    Path(job_id): Path<Uuid>,
    Json(body): Json<BdnRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    // The job comes from the durable jobs table, not current_job: its type
    // distinguishes 404 from 422, and its row supplies the event envelope, which
    // must describe the job even when it is no longer the live one.
    let job = sqlx::query!(
        "SELECT type, operator, counterparty, grade, session_id FROM jobs WHERE job_id = $1",
        job_id
    )
    .fetch_optional(&state.db)
    .await?
    .ok_or(AppError::NotFound)?;
    if job.r#type != "dispense" {
        return Err(AppError::UnprocessableEntity(
            "a BDN may only be linked to a dispense".to_owned(),
        ));
    }
    let envelope = Envelope {
        job_id,
        job_type: JobType::Dispense,
        operator: job.operator,
        session_id: job.session_id,
        counterparty: job.counterparty,
        grade: job.grade,
    };

    // The original (metering) transaction for this job: the row with no
    // references_transaction_id. Absent until the dispense has completed.
    let tx = sqlx::query!(
        "SELECT transaction_id FROM transactions
         WHERE job_id = $1 AND references_transaction_id IS NULL",
        job_id
    )
    .fetch_optional(&state.db)
    .await?
    .ok_or(AppError::Conflict(
        "dispense transaction not yet recorded".to_owned(),
    ))?;

    // One BDN per dispense.
    let existing = sqlx::query!(
        "SELECT transaction_id FROM transactions
         WHERE references_transaction_id = $1 AND bdn_number IS NOT NULL
         LIMIT 1",
        tx.transaction_id
    )
    .fetch_optional(&state.db)
    .await?;
    if existing.is_some() {
        return Err(AppError::Conflict("a BDN is already linked".to_owned()));
    }

    // Append the BDN linkage record (append-only).
    let new_tx_id = Uuid::new_v4();
    let now = Utc::now();
    sqlx::query!(
        r#"INSERT INTO transactions
           (transaction_id, job_id, references_transaction_id, bdn_number, created_at)
           VALUES ($1, $2, $3, $4, $5)"#,
        new_tx_id,
        job_id,
        tx.transaction_id,
        body.bdn_number,
        now,
    )
    .execute(&state.db)
    .await?;

    // Close the job if it is still the live one. The linkage above is the
    // durable record and stands regardless of in-memory state.
    if let Some(j) = state
        .current_job
        .write()
        .await
        .as_mut()
        .filter(|j| j.job_id() == job_id)
    {
        j.set_status(JobStatus::Closed);
    }

    state.emit_log_event(LogEvent::bunkering(
        EventKind::BdnLinked,
        now,
        &envelope,
        json!({
            "transaction_id": new_tx_id,
            "references_transaction_id": tx.transaction_id,
            "bdn_number": body.bdn_number,
        }),
    ));

    Ok((
        StatusCode::CREATED,
        Json(json!({ "transaction_id": new_tx_id })),
    ))
}

pub async fn get_totaliser(State(state): State<AppState>) -> Result<Json<Value>> {
    let guard = state.current_job.read().await;
    let job = guard.as_ref().ok_or(AppError::NotFound)?;
    let status = match job.status() {
        JobStatus::Running => "dispensing",
        JobStatus::AwaitingBdn | JobStatus::Closed => "complete",
        _ => return Err(AppError::NotFound),
    };
    Ok(Json(json!({
        "job_id": job.job_id(),
        "dispensed_litres": job.current_dispensed_l().unwrap_or(0.0),
        "preset_litres": job.preset_volume_l().unwrap_or(0.0),
        "status": status,
    })))
}
