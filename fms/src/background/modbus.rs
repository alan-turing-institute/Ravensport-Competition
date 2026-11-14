/// Modbus TCP poller: monitors the dispenser during an active dispense job.
/// Writes preset and enable registers when a job transitions to Running,
/// then polls the transaction totaliser until the preset volume is reached.
/// On completion, inserts one complete transaction row (append-only design).
use chrono::{DateTime, Utc};
use tokio::time;
use tokio_modbus::prelude::*;
use uuid::Uuid;

use crate::state::{
    AppState, Envelope, EventKind, Job, JobStatus, JobType, LogEvent, DISPENSE_PRESET_MAX_L,
};

// Register map (dispenser-modbus-tcp-spec.md §8): 0-based PDU addresses. Both
// totalisers are 32-bit pulse counts occupying a consecutive register pair,
// high word at the lower address, and are read together (quantity 2).
const PRESET_REG: u16 = 0x0001;
const ENABLE_REG: u16 = 0x0002;
const CUMULATIVE_REG: u16 = 0x0003; // 0x0003 high word, 0x0004 low word
const TRANSACTION_REG: u16 = 0x0005; // 0x0005 high word, 0x0006 low word
const TOTALISER_PAIR: u16 = 2;

/// Status register (dispenser-modbus-tcp-spec.md §7): bit 0 set while the pump
/// is running, cleared when the dispenser auto-stops at its preset target.
const STATUS_RUNNING_BIT: u16 = 0x0001;
/// The transaction totaliser pair (0x0005-0x0006) and the adjacent status
/// register (0x0007), read together in one request.
const TRANSACTION_AND_STATUS: u16 = 3;

/// Reconstruct a 32-bit totaliser from its register pair (high word first).
fn combine_u32(regs: &[u16]) -> u32 {
    let hi = regs.first().copied().unwrap_or(0);
    let lo = regs.get(1).copied().unwrap_or(0);
    (u32::from(hi) << 16) | u32::from(lo)
}

/// Encode a preset volume (litres) into the 16-bit "unsigned div10" register,
/// or `None` if it exceeds the representable range (spec §7). The value is
/// rounded to the register's 0.1 L resolution rather than truncated.
fn encode_preset(preset_l: f64) -> Option<u16> {
    let raw = (preset_l * 10.0).round();
    if (0.0..=f64::from(u16::MAX)).contains(&raw) {
        Some(raw as u16)
    } else {
        None
    }
}

/// Advance a completed dispense towards `AwaitingBdn`, recording the metered
/// volume.
///
/// The metering transaction row is inserted into the durable ledger before this
/// in-memory write, and the `current_job` lock is released in between. A
/// concurrent `link_bdn` (which keys off the durable row, not the in-memory
/// status) can therefore set `Closed` in the intervening window. That is a
/// terminal state the poller must not revert, so the status is only advanced
/// when it is still `Running`. `cancel` refuses a running dispense (fms.md
/// §7.4), and the same guard would keep `Cancelled`. The metered volume is
/// recorded regardless of status.
fn complete_dispense(job: &mut Job, volume_dispensed_l: f64) {
    if job.status() == JobStatus::Running {
        job.set_status(JobStatus::AwaitingBdn);
    }
    if let Err(e) = job.update_current_dispensed_l(volume_dispensed_l) {
        tracing::error!("invalid final dispensed volume {volume_dispensed_l}: {e}");
    }
}

/// What the poll loop needs from a running dispense, copied out of `current_job`
/// so the lock is not held across Modbus I/O.
struct DispenseSnapshot {
    job_id: Uuid,
    preset_l: f64,
    /// The envelope travels with the snapshot: this task has no request, so the
    /// job is its only source of the session the dispense was authorised under.
    envelope: Envelope,
    atg_open: Option<f64>,
    auth_id: Option<Uuid>,
    start_pulses: Option<i64>,
    /// The transfer's start, recorded by `start_dispense` — not the job's
    /// creation (fms.md §8.4).
    opened_at: DateTime<Utc>,
}

/// The snapshot of `job` if it is a running dispense, otherwise `None`.
///
/// `start_dispense` records the start under the same lock that sets `Running`,
/// so a running dispense always has one. Should that ever fail to hold, the
/// close still proceeds — stopping the pump matters more than the timestamp —
/// using job creation, and says so at ERROR.
fn running_dispense(job: &Job) -> Option<DispenseSnapshot> {
    if job.job_type() != JobType::Dispense || job.status() != JobStatus::Running {
        return None;
    }
    let opened_at = job.tx_opened_at().unwrap_or_else(|| {
        tracing::error!(
            job = %job.job_id(),
            "running dispense has no recorded start; using job creation as opened_at"
        );
        job.created_at()
    });
    Some(DispenseSnapshot {
        job_id: job.job_id(),
        preset_l: job.preset_volume_l().unwrap_or(0.0),
        envelope: Envelope::from_job(job),
        atg_open: job.atg_volume_at_tx_open(),
        auth_id: job.authorisation_id(),
        start_pulses: job.start_totaliser_pulses(),
        opened_at,
    })
}

/// The dispense row's three measurement fields, populated by the FMS rather
/// than received: a dispense has no transcription route (fms.md §5.2).
#[derive(Debug, Clone, Copy, PartialEq)]
struct DispenseMeasurements {
    /// From the close `i201` read; `None` when neither a fresh nor a cached
    /// reading exists — recorded as unknown, not invented.
    temperature_c: Option<f64>,
    /// `FMS_DENSITY_KG_PER_L`: a property of the grade, not a measurement.
    density_kg_per_l: f64,
    temp_corrected_volume_l: f64,
}

/// The volume correction to 15 °C is identity: the fuel is held at the
/// reference temperature and no thermal-expansion model is defined (sim.md
/// §1.3). It therefore does not depend on the temperature reading, and is
/// recorded even when that reading is missing.
fn dispense_measurements(
    volume_dispensed_l: f64,
    temperature_c: Option<f64>,
    density_kg_per_l: f64,
) -> DispenseMeasurements {
    DispenseMeasurements {
        temperature_c,
        density_kg_per_l,
        temp_corrected_volume_l: volume_dispensed_l,
    }
}

/// Everything the dispense close records, gathered once so the transaction row
/// and the `transaction_closed` event are built from the same values.
struct DispenseClose {
    snapshot: DispenseSnapshot,
    transaction_id: Uuid,
    start_totaliser_pulses: i64,
    end_totaliser_pulses: i64,
    volume_dispensed_l: f64,
    atg_close: Option<f64>,
    measurements: DispenseMeasurements,
    closed_at: DateTime<Utc>,
}

impl DispenseClose {
    /// The dispense `transaction_closed` (hist.md §4.2): the job's envelope plus
    /// the dispense list, including the measurement fields that mirror the BDN
    /// and the preset repeated from `transaction_open`.
    fn transaction_closed_event(&self) -> LogEvent {
        let s = &self.snapshot;
        LogEvent::bunkering(
            EventKind::TransactionClosed,
            self.closed_at,
            &s.envelope,
            serde_json::json!({
                "transaction_id": self.transaction_id,
                "authorisation_id": s.auth_id,
                "preset_volume_l": s.preset_l,
                "start_totaliser_pulses": self.start_totaliser_pulses,
                "end_totaliser_pulses": self.end_totaliser_pulses,
                "volume_dispensed_l": self.volume_dispensed_l,
                "temperature_c": self.measurements.temperature_c,
                "density_kg_per_l": self.measurements.density_kg_per_l,
                "temp_corrected_volume_l": self.measurements.temp_corrected_volume_l,
                "atg_volume_at_open_l": s.atg_open,
                "atg_volume_at_close_l": self.atg_close,
                "opened_at": s.opened_at,
                "closed_at": self.closed_at,
            }),
        )
    }
}
/// Stage of a dispense as seen through the Running status bit (spec §7).
#[derive(Debug, PartialEq, Eq)]
enum DispenseProgress {
    /// Enabled, but the pump has not yet asserted Running. A real dispenser
    /// needs time to spin up after the enable write, so a clear bit here means
    /// "not started", not "finished".
    AwaitingStart,
    /// The pump is running.
    Running,
    /// The pump ran and has now auto-stopped at its preset target (spec §9).
    Complete,
}

/// Latches the observation that the pump has run, so that a cleared Running bit
/// is only accepted as completion once the bit has been seen set. Without this
/// a dispenser that has not yet asserted Running is mistaken for a completed,
/// zero-volume dispense, advancing the job to AwaitingBdn as though it
/// succeeded. The latch is reset each time a new dispense is enabled.
#[derive(Debug, Default)]
struct RunningLatch {
    observed_running: bool,
}

impl RunningLatch {
    fn observe(&mut self, running: bool) -> DispenseProgress {
        if running {
            self.observed_running = true;
            DispenseProgress::Running
        } else if self.observed_running {
            DispenseProgress::Complete
        } else {
            DispenseProgress::AwaitingStart
        }
    }
}

pub async fn run(state: AppState) {
    let config = state.config.clone();
    let mut interval = time::interval(config.modbus_poll_interval);
    interval.set_missed_tick_behavior(time::MissedTickBehavior::Skip);

    let mut started_job: Option<Uuid> = None;
    let mut latch = RunningLatch::default();

    loop {
        interval.tick().await;

        let snapshot = {
            let guard = state.current_job.read().await;
            guard.as_ref().and_then(running_dispense)
        };

        let Some(snapshot) = snapshot else {
            started_job = None;
            continue;
        };
        // Copied out for the start and metering steps; the snapshot itself is
        // kept whole for the close, which records it.
        let DispenseSnapshot {
            job_id,
            preset_l,
            start_pulses,
            ..
        } = snapshot;

        let addr = format!("{}:{}", config.dispenser_host, config.dispenser_port);
        let socket_addr: std::net::SocketAddr = match addr.parse() {
            Ok(a) => a,
            Err(e) => {
                tracing::warn!("invalid Modbus address {addr}: {e}");
                continue;
            }
        };

        let mut ctx = match tcp::connect(socket_addr).await {
            Ok(c) => c,
            Err(e) => {
                tracing::warn!("Modbus connect failed: {e}");
                continue;
            }
        };

        // Write preset and enable on the first poll for this job.
        if started_job != Some(job_id) {
            let preset_raw = match encode_preset(preset_l) {
                Some(raw) => raw,
                None => {
                    tracing::error!(
                        "preset {preset_l:.1} L exceeds the dispenser's representable range \
                         ({DISPENSE_PRESET_MAX_L} L); skipping dispense for job={job_id}"
                    );
                    continue;
                }
            };

            let start_total = match ctx
                .read_holding_registers(CUMULATIVE_REG, TOTALISER_PAIR)
                .await
            {
                Ok(v) => i64::from(combine_u32(&v)),
                Err(e) => {
                    tracing::warn!("failed to read start totaliser: {e}");
                    continue;
                }
            };

            if let Err(e) = ctx.write_single_register(PRESET_REG, preset_raw).await {
                tracing::warn!("failed to write preset: {e}");
                continue;
            }
            if let Err(e) = ctx.write_single_register(ENABLE_REG, 1).await {
                tracing::warn!("failed to write enable: {e}");
                continue;
            }

            if let Some(j) = state.current_job.write().await.as_mut() {
                if let Err(e) = j.set_start_totaliser_pulses(start_total) {
                    tracing::error!("invalid start totaliser value {start_total}: {e}");
                    continue;
                }
            }

            started_job = Some(job_id);
            latch = RunningLatch::default();
            tracing::info!(
                "dispense started: job={job_id} preset={preset_l:.0} L start_pulses={start_total}"
            );
        }

        // Poll the transaction totaliser and the adjacent status register in one
        // request. Completion is signalled by the dispenser itself: it auto-stops
        // at its own preset target (dispenser-modbus-tcp-spec.md §9) and clears
        // the Running status bit (§7). The FMS must not infer completion by
        // recomputing volume against `preset_l`: the dispenser meters in whole
        // pulses and stops at `round(preset × k)`, so a fractional preset would
        // leave `volume_from_pulses` short of `preset_l` by up to one pulse and
        // the dispense would never be observed as complete.
        let regs = match ctx
            .read_holding_registers(TRANSACTION_REG, TRANSACTION_AND_STATUS)
            .await
        {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("failed to read transaction totaliser/status: {e}");
                continue;
            }
        };
        let tx_pulses = combine_u32(&regs);
        let running = regs.get(2).is_some_and(|s| s & STATUS_RUNNING_BIT != 0);

        let volume_from_pulses = f64::from(tx_pulses) / config.k_factor;

        if let Some(j) = state.current_job.write().await.as_mut() {
            if let Err(e) = j.update_current_dispensed_l(volume_from_pulses) {
                tracing::warn!("invalid dispensed volume {volume_from_pulses}: {e}");
            }
        }

        // Only accept a cleared Running bit as completion once the pump has been
        // observed running; a clear bit before that means the dispenser has not
        // yet spun up after the enable write (spec §7), not that it has finished.
        match latch.observe(running) {
            DispenseProgress::AwaitingStart | DispenseProgress::Running => continue,
            DispenseProgress::Complete => {}
        }

        // Preset reached — read the final cumulative totaliser pair and disable pump.
        let end_total = match ctx
            .read_holding_registers(CUMULATIVE_REG, TOTALISER_PAIR)
            .await
        {
            Ok(v) => i64::from(combine_u32(&v)),
            Err(e) => {
                tracing::warn!("failed to read end totaliser: {e}");
                continue;
            }
        };

        if let Err(e) = ctx.write_single_register(ENABLE_REG, 0).await {
            tracing::warn!("failed to write enable=0: {e}");
        }

        // Re-read start_totaliser_pulses from in-memory job; it may have been
        // written in this same poll cycle (started_job was just set above).
        let start_total_final = {
            let guard = state.current_job.read().await;
            guard
                .as_ref()
                .and_then(|j| j.start_totaliser_pulses())
                .unwrap_or(start_pulses.unwrap_or(0))
        };

        let volume_dispensed = (end_total - start_total_final) as f64 / config.k_factor;
        if let Err(e) = Job::validate_dispensed_volume(volume_dispensed) {
            tracing::error!(
                "invalid final metering for job={job_id} \
                 (start={start_total_final}, end={end_total}): {e}"
            );
            continue;
        }
        // Flow has stopped and the level is now static, so an on-demand ATG read
        // gives an accurate close volume for the ±1% reconciliation; the periodic
        // cache would be up to one poll interval stale (fms.md §5.1).
        // The same read supplies the fuel temperature the row records (fms.md
        // §5.2), so volume and temperature come from one reading.
        let close_reading =
            crate::background::atg::read_tc_reading_or_cached(&state, "dispense close").await;
        let atg_close = close_reading.map(|r| r.tc_volume_l);
        let measurements = dispense_measurements(
            volume_dispensed,
            close_reading.map(|r| r.temperature_c),
            config.density_kg_per_l,
        );
        let close = DispenseClose {
            snapshot,
            transaction_id: Uuid::new_v4(),
            start_totaliser_pulses: start_total_final,
            end_totaliser_pulses: end_total,
            volume_dispensed_l: volume_dispensed,
            atg_close,
            measurements,
            closed_at: Utc::now(),
        };
        let s = &close.snapshot;

        // Insert one complete, immutable transaction row (append-only design).
        if let Err(e) = sqlx::query!(
            r#"INSERT INTO transactions
               (transaction_id, job_id, authorisation_id,
                operator, counterparty, grade,
                preset_volume_l, start_totaliser_pulses, end_totaliser_pulses,
                volume_dispensed_l, temperature_c, density_kg_per_l, temp_corrected_volume_l,
                atg_volume_at_open_l, atg_volume_at_close_l,
                opened_at, closed_at, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)"#,
            close.transaction_id,
            s.job_id,
            s.auth_id,
            s.envelope.operator,
            s.envelope.counterparty,
            s.envelope.grade,
            s.preset_l,
            close.start_totaliser_pulses,
            close.end_totaliser_pulses,
            close.volume_dispensed_l,
            close.measurements.temperature_c,
            close.measurements.density_kg_per_l,
            close.measurements.temp_corrected_volume_l,
            s.atg_open,
            close.atg_close,
            s.opened_at,
            close.closed_at,
            close.closed_at,
        )
        .execute(&state.db)
        .await
        {
            tracing::error!("failed to insert dispense transaction: {e}");
        }

        // The job is not closed here: it moves to AwaitingBdn until the operator
        // records the BDN (narrative §279). The transaction itself is the durable
        // row inserted above, keyed by job_id.
        if let Some(j) = state.current_job.write().await.as_mut() {
            complete_dispense(j, volume_dispensed);
        }
        started_job = None;

        state.emit_log_event(close.transaction_closed_event());

        tracing::info!(
            "dispense complete: job={job_id} transaction={} volume={volume_dispensed:.1} L",
            close.transaction_id
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn combine_u32_reconstructs_high_word_first() {
        // 0x0001_1170 = 70000 split across the register pair.
        assert_eq!(combine_u32(&[0x0001, 0x1170]), 70_000);
        assert_eq!(combine_u32(&[0x0000, 0x0000]), 0);
        assert_eq!(combine_u32(&[0xFFFF, 0xFFFF]), u32::MAX);
    }

    #[test]
    fn combine_u32_tolerates_short_slices() {
        assert_eq!(combine_u32(&[]), 0);
        assert_eq!(combine_u32(&[0x0002]), 0x0002_0000);
    }

    #[test]
    fn encode_preset_rounds_to_register_resolution() {
        assert_eq!(encode_preset(300.0), Some(3000));
        assert_eq!(encode_preset(100.04), Some(1000)); // rounds to 0.1 L
        assert_eq!(encode_preset(100.06), Some(1001));
        assert_eq!(encode_preset(0.0), Some(0));
    }

    #[test]
    fn encode_preset_accepts_register_ceiling() {
        assert_eq!(encode_preset(DISPENSE_PRESET_MAX_L), Some(u16::MAX)); // 6553.5 L
    }

    #[test]
    fn encode_preset_rejects_out_of_range() {
        assert_eq!(encode_preset(6553.6), None);
        assert_eq!(encode_preset(-1.0), None);
    }

    fn make_job(status: JobStatus) -> Job {
        let mut job = Job::new(
            JobType::Dispense,
            "MV Harrier".to_owned(),
            "MGO".to_owned(),
            "test-operator".to_owned(),
            Uuid::nil(),
            None,
        )
        .expect("valid job");
        job.set_status(status);
        job.set_preset_volume_l(1000.0).expect("valid preset");
        job.set_rfid_validated(true);
        job
    }

    /// The dispense close records the transfer's start as `opened_at`, not the
    /// job's creation (fms.md §8.4: "when the dispense or fill started"). The
    /// paper BDN's `delivery_commenced_at` is the dispenser start, and both
    /// assign the transaction to an audit window (sim.md §8.3): with job
    /// creation here, a job created before midnight on the last day of a month
    /// whose dispense starts after it would put record and paper in different
    /// months.
    #[test]
    fn dispense_close_takes_opened_at_from_the_transfer_start_not_job_creation() {
        let mut job = make_job(JobStatus::Running);
        let started = job.created_at() + chrono::Duration::seconds(30);
        job.set_tx_opened_at(started);

        let snapshot = running_dispense(&job).expect("a running dispense");

        assert_eq!(snapshot.opened_at, started);
        assert_ne!(snapshot.opened_at, job.created_at());
    }

    /// A job that is not a running dispense yields nothing for the close path
    /// to act on.
    #[test]
    fn only_a_running_dispense_is_snapshotted() {
        let mut job = make_job(JobStatus::Active);
        job.set_tx_opened_at(job.created_at());
        assert!(running_dispense(&job).is_none());
    }

    /// The dispense row's three measurement fields are populated by the FMS, not
    /// received (fms.md §5.2): temperature from the close `i201` read, density
    /// from configuration, and the meter volume corrected to 15 °C. The
    /// correction factor is 1 while the fuel is held at 15 °C (sim.md §1.3).
    #[test]
    fn dispense_measurements_record_temperature_density_and_corrected_volume() {
        let m = dispense_measurements(500.0, Some(15.0), 0.84);
        assert_eq!(m.temperature_c, Some(15.0));
        assert_eq!(m.density_kg_per_l, 0.84);
        assert_eq!(m.temp_corrected_volume_l, 500.0);
    }

    /// With no ATG reading at the close (and no cached one), the temperature is
    /// recorded as unknown rather than invented; density is configuration and
    /// is always known, and the identity correction does not depend on the
    /// temperature.
    #[test]
    fn dispense_measurements_without_a_reading_record_no_temperature() {
        let m = dispense_measurements(500.0, None, 0.84);
        assert_eq!(m.temperature_c, None);
        assert_eq!(m.density_kg_per_l, 0.84);
        assert_eq!(m.temp_corrected_volume_l, 500.0);
    }

    /// The dispense `transaction_closed` carries exactly hist.md §4.2's fields:
    /// the envelope plus the dispense list, including the three measurement
    /// fields and the repeated preset. Built from the same `DispenseClose` the
    /// poll loop emits, so this is the Modbus close's own event, not a copy.
    #[test]
    fn dispense_transaction_closed_carries_exactly_the_specified_fields() {
        let mut job = make_job(JobStatus::Running);
        job.set_tx_opened_at(job.created_at());
        let close = DispenseClose {
            snapshot: running_dispense(&job).expect("a running dispense"),
            transaction_id: Uuid::from_u128(2),
            start_totaliser_pulses: 1_000,
            end_totaliser_pulses: 101_000,
            volume_dispensed_l: 1_000.0,
            atg_close: Some(34_000.0),
            measurements: dispense_measurements(1_000.0, Some(15.0), 0.84),
            closed_at: Utc::now(),
        };

        let event = close.transaction_closed_event();
        assert_eq!(event.kind, EventKind::TransactionClosed);

        let mut actual: Vec<&str> = event.fields.keys().map(String::as_str).collect();
        actual.sort_unstable();
        let mut expected = vec![
            // envelope
            "job_id",
            "type",
            "operator",
            "session_id",
            "counterparty",
            "grade",
            // hist.md §4.2, transaction_closed (dispense)
            "transaction_id",
            "authorisation_id",
            "preset_volume_l",
            "start_totaliser_pulses",
            "end_totaliser_pulses",
            "volume_dispensed_l",
            "temperature_c",
            "density_kg_per_l",
            "temp_corrected_volume_l",
            "atg_volume_at_open_l",
            "atg_volume_at_close_l",
            "opened_at",
            "closed_at",
        ];
        expected.sort_unstable();
        assert_eq!(actual, expected);

        assert_eq!(event.fields["temperature_c"], serde_json::json!(15.0));
        assert_eq!(event.fields["density_kg_per_l"], serde_json::json!(0.84));
        assert_eq!(
            event.fields["temp_corrected_volume_l"],
            serde_json::json!(1_000.0)
        );
    }

    /// Normal sequencing: a Running dispense advances to AwaitingBdn and records
    /// its metered volume.
    #[test]
    fn complete_dispense_advances_running_to_awaiting_bdn() {
        let mut job = make_job(JobStatus::Running);
        complete_dispense(&mut job, 987.6);
        assert_eq!(job.status(), JobStatus::AwaitingBdn);
        assert_eq!(job.current_dispensed_l(), Some(987.6));
    }

    /// Regression: a `link_bdn` that wins the race in the window between the
    /// metering INSERT and this write sets `Closed`. The poller must not revert
    /// that terminal state to `AwaitingBdn`, which would strand the slot (a
    /// second `link_bdn` returns 409 before it can re-close the job).
    #[test]
    fn complete_dispense_does_not_clobber_closed() {
        let mut job = make_job(JobStatus::Closed);
        complete_dispense(&mut job, 987.6);
        assert_eq!(
            job.status(),
            JobStatus::Closed,
            "a job closed by a racing link_bdn must stay Closed"
        );
        // The metered volume is still recorded regardless of status.
        assert_eq!(job.current_dispensed_l(), Some(987.6));
    }

    /// The poller must not revert a `Cancelled` status either. `cancel` refuses
    /// a running dispense (fms.md §7.4), and this test keeps the guard
    /// independent of that rule.
    #[test]
    fn complete_dispense_does_not_clobber_cancelled() {
        let mut job = make_job(JobStatus::Cancelled);
        complete_dispense(&mut job, 500.0);
        assert_eq!(job.status(), JobStatus::Cancelled);
        assert_eq!(job.current_dispensed_l(), Some(500.0));
    }

    // RunningLatch: completion must only be inferred from a cleared Running bit
    // once the pump has actually been observed running. Without the latch, a
    // dispenser that has not yet asserted Running after the enable write is
    // mistaken for a completed, zero-volume dispense.

    #[test]
    fn cleared_bit_before_start_is_awaiting_not_complete() {
        // The dispenser has been enabled but has not yet spun up: the Running
        // bit is still clear. This must not be read as completion.
        let mut latch = RunningLatch::default();
        assert_eq!(latch.observe(false), DispenseProgress::AwaitingStart);
        // Still spinning up on the next poll — still not complete.
        assert_eq!(latch.observe(false), DispenseProgress::AwaitingStart);
    }

    #[test]
    fn set_bit_reports_running() {
        let mut latch = RunningLatch::default();
        assert_eq!(latch.observe(true), DispenseProgress::Running);
    }

    #[test]
    fn cleared_bit_after_running_is_complete() {
        let mut latch = RunningLatch::default();
        assert_eq!(latch.observe(false), DispenseProgress::AwaitingStart);
        assert_eq!(latch.observe(true), DispenseProgress::Running);
        assert_eq!(latch.observe(true), DispenseProgress::Running);
        assert_eq!(latch.observe(false), DispenseProgress::Complete);
    }

    #[test]
    fn running_asserted_synchronously_still_completes() {
        // Matches the test stub, which asserts Running on the enable write: the
        // first poll already sees it set, the next sees it cleared.
        let mut latch = RunningLatch::default();
        assert_eq!(latch.observe(true), DispenseProgress::Running);
        assert_eq!(latch.observe(false), DispenseProgress::Complete);
    }
}
