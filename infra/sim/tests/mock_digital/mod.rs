//! Representative mock of the digital world for the full-sim integration test.
//!
//! Each digital-world entity we need is an individual Tokio task: the FMS
//! ([`fms`]), the HMI, and the ATG relay. Communication *between* mocked
//! entities uses Tokio channels (the [`FmsCommand`] protocol below);
//! communication between a mocked entity and a *sim* entity uses the real
//! transport (HTTP, Modbus TCP, Veeder-Root TLS, RFID serial).
//!
//! Lives under `tests/mock_digital/` (a subdirectory module) so Cargo does not
//! compile it as its own test binary.

// Shared test fixtures: each test crate includes the whole module but uses only
// the entities it needs (e.g. the FMS test does not use the HMI), so unused
// items are expected per-crate.
#[allow(dead_code)]
pub mod fms;
#[allow(dead_code)]
pub mod hmi;
#[allow(dead_code)]
pub mod relay;

use tokio::sync::oneshot;

/// The dispenser totaliser state the FMS reports to the HMI.
#[derive(Debug, Clone)]
pub struct Totaliser {
    pub job_id: String,
    pub dispensed_litres: f64,
    /// `"dispensing"` until the transaction totaliser reaches the preset, then
    /// `"complete"`.
    pub status: String,
}

/// The current-job state the FMS reports to the HMI for `GET /api/jobs/current`.
#[derive(Debug, Clone)]
pub struct JobStatus {
    pub job_id: String,
    pub status: String,
    /// The dispense start, `None` until it starts (fms.md §7.4).
    pub opened_at: Option<String>,
}

/// Commands the HMI sends to the FMS (the only digital↔digital channel). Each
/// carries a `oneshot` reply, modelling a request/response call.
///
/// This is a shared protocol type: different mock entities and test crates use
/// different parts of it (the FMS trusts the HMI's forwarding and so ignores
/// some fields; some variants are exercised only by the HMI or the integration
/// test), so not every including crate reads every field.
#[allow(dead_code)]
pub enum FmsCommand {
    /// Validate operator credentials; returns a challenge id.
    Login {
        username: String,
        password: String,
        reply: oneshot::Sender<String>,
    },
    /// Validate the operator RFID card (already read off the HMI serial port);
    /// returns a session token.
    ValidateCard {
        card: u32,
        reply: oneshot::Sender<String>,
    },
    /// Create a refuel (tug dispense) job; returns the job id.
    CreateRefuelJob {
        vessel_id: String,
        requested_volume_litres: f64,
        reply: oneshot::Sender<String>,
    },
    /// Current status of the singleton live job (`pending_vessel_rfid`,
    /// `rfid_validated`, `dispensing`, `complete`, `open`). The HMI addresses
    /// `GET /api/jobs/current` with no job id; the FMS resolves the current job
    /// and returns its id alongside the status. `None` when no job is live.
    GetJobStatus {
        reply: oneshot::Sender<Option<JobStatus>>,
    },
    /// Write the preset and enable the dispenser pump (Modbus).
    StartDispense {
        job_id: String,
        preset_volume_litres: f64,
        reply: oneshot::Sender<()>,
    },
    /// Read the dispenser transaction totaliser (Modbus) and report progress for
    /// the singleton live job. As with [`GetJobStatus`](Self::GetJobStatus) the
    /// HMI addresses `current`; the FMS resolves the live job. `None` when no
    /// job is live.
    GetTotaliser {
        reply: oneshot::Sender<Option<Totaliser>>,
    },
    /// Record the BDN number against a completed dispense.
    EnterBdn {
        job_id: String,
        bdn_number: String,
        reply: oneshot::Sender<()>,
    },
    /// Read the current tank level from the ATG console (TLS).
    GetTankLevel { reply: oneshot::Sender<f64> },
    /// Create a tank-fill job; returns the job id.
    CreateFillJob {
        tanker_registration: String,
        delivery_volume_litres: f64,
        reply: oneshot::Sender<String>,
    },
    /// Transcribe a delivery ticket against a fill job.
    TranscribeTicket {
        job_id: String,
        ticket_number: String,
        reply: oneshot::Sender<()>,
    },
}
