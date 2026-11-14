/// Vessel RFID reader: maintains a persistent TCP connection to the reader's
/// networked address (the sim's HAProxy front end for the vessel RFID reader).
/// Reads card swipes in the format "{card_number:08d}\r\n" and validates them
/// against the FMS card table and the open dispense job's vessel; sets
/// rfid_validated on that job.
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    net::TcpStream,
    time,
};

use crate::{
    db::is_card_active_for,
    state::{AppState, Job, JobStatus, JobType},
};

pub async fn run(state: AppState) {
    let config = state.config.clone();
    let addr = format!(
        "{}:{}",
        config.vessel_rfid_host, config.vessel_rfid_tcp_port
    );

    loop {
        match TcpStream::connect(&addr).await {
            Ok(stream) => {
                tracing::info!("vessel RFID connected to {addr}");
                if let Err(e) = read_loop(&state, stream).await {
                    tracing::warn!("vessel RFID read error: {e}");
                }
                time::sleep(std::time::Duration::from_secs(2)).await;
            }
            Err(e) => {
                tracing::warn!("vessel RFID connect failed ({addr}): {e}; retrying in 2 s");
                time::sleep(std::time::Duration::from_secs(2)).await;
            }
        }
    }
}

async fn read_loop(state: &AppState, stream: TcpStream) -> anyhow::Result<()> {
    let mut lines = BufReader::new(stream).lines();

    while let Some(line) = lines.next_line().await? {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let card_number: i32 = match trimmed.parse() {
            Ok(n) => n,
            Err(_) => {
                tracing::warn!("vessel RFID: unparseable card data: {trimmed:?}");
                continue;
            }
        };

        tracing::debug!("vessel RFID swipe: card_number={card_number}");

        // The job the card is presented for, and the vessel it names. Read and
        // released before the card lookup so the job lock is not held across it.
        let open_job = {
            let guard = state.current_job.read().await;
            guard
                .as_ref()
                .filter(|j| awaits_vessel_tag(j))
                .map(|j| (j.job_id(), j.counterparty().to_owned()))
        };
        let Some((job_id, vessel)) = open_job else {
            tracing::debug!("vessel RFID: card {card_number} presented with no dispense job open");
            continue;
        };

        // The card must be active and enrolled to the job's vessel, so another
        // vessel's tag or an operator's card does not unlock the dispenser.
        let matches = match is_card_active_for(&state.db, card_number, &vessel).await {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!("vessel RFID: card lookup failed: {e}");
                continue;
            }
        };

        if !matches {
            tracing::info!(
                "vessel RFID: card {card_number} is not an active card enrolled to \
                 {vessel:?} (job {job_id}) — ignored"
            );
            continue;
        }

        // Validate only the job the card was checked against: if it closed or
        // was replaced during the lookup, the card validates nothing.
        let mut guard = state.current_job.write().await;
        if let Some(j) = guard
            .as_mut()
            .filter(|j| j.job_id() == job_id && awaits_vessel_tag(j))
        {
            j.set_rfid_validated(true);
            tracing::info!("vessel RFID validated: card={card_number} job={job_id}");
        }
    }

    Ok(())
}

/// A dispense job that is open, either awaiting its vessel tag or dispensing.
fn awaits_vessel_tag(job: &Job) -> bool {
    job.job_type() == JobType::Dispense
        && (job.status() == JobStatus::Active || job.status() == JobStatus::Running)
}
