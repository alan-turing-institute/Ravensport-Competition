//! Tests for the mock FMS (`tests/mock_digital/fms.rs`), exercised against the
//! real sim appliances it drives — the dispenser (Modbus) and the ATG console
//! (TLS) — plus a real RFID serial line. The FMS must work on its own before
//! the full-sim integration test depends on it.

mod mock_digital;

use std::net::SocketAddr;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot};

use sim::appliances::atg_console;
use sim::appliances::dispenser;
use sim::clock::{SimClock, TimeModel};
use sim::physical::atg_probe::ProbeReading;
use sim::physical::bund_leak_sensor::BundReading;
use sim::physical::flow_meter::Pulse;
use sim::serial::SerialConnection;

use mock_digital::{fms, FmsCommand, Totaliser};

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);
const K_FACTOR: f64 = 10.0; // pulses per litre
const VESSEL_CARD: &[u8] = b"1000001\n";

fn free_addr() -> SocketAddr {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

fn dummy_addr() -> SocketAddr {
    // Never connected by the commands a given test issues.
    "127.0.0.1:1".parse().unwrap()
}

/// Spawn the FMS; return its command sender and the vessel RFID write end.
fn spawn_fms(
    dispenser_addr: SocketAddr,
    atg_addr: SocketAddr,
) -> (mpsc::Sender<FmsCommand>, SerialConnection) {
    let (cmd_tx, cmd_rx) = mpsc::channel(16);
    let (vessel_serial, vessel_handle) = SerialConnection::loopback();
    fms::spawn(cmd_rx, vessel_handle, dispenser_addr, atg_addr, K_FACTOR);
    (cmd_tx, vessel_serial)
}

// --- command helpers --------------------------------------------------------

async fn create_refuel(cmd_tx: &mpsc::Sender<FmsCommand>) -> String {
    let (reply, rx) = oneshot::channel();
    cmd_tx
        .send(FmsCommand::CreateRefuelJob {
            vessel_id: "1999997".to_string(),
            requested_volume_litres: 100.0,
            reply,
        })
        .await
        .unwrap();
    rx.await.unwrap()
}

/// Query the singleton current job, asserting it is the expected one, and return
/// its status. The HMI addresses `GET /api/jobs/current` with no job id; the FMS
/// resolves the current job and echoes its id.
async fn job_status(cmd_tx: &mpsc::Sender<FmsCommand>, job_id: &str) -> String {
    let (reply, rx) = oneshot::channel();
    cmd_tx
        .send(FmsCommand::GetJobStatus { reply })
        .await
        .unwrap();
    let status = rx.await.unwrap().expect("no current job");
    assert_eq!(status.job_id, job_id, "current job id mismatch");
    status.status
}

async fn start_dispense(cmd_tx: &mpsc::Sender<FmsCommand>, job_id: &str, preset: f64) {
    let (reply, rx) = oneshot::channel();
    cmd_tx
        .send(FmsCommand::StartDispense {
            job_id: job_id.to_string(),
            preset_volume_litres: preset,
            reply,
        })
        .await
        .unwrap();
    rx.await.unwrap();
}

async fn totaliser(cmd_tx: &mpsc::Sender<FmsCommand>, job_id: &str) -> Totaliser {
    let (reply, rx) = oneshot::channel();
    cmd_tx
        .send(FmsCommand::GetTotaliser { reply })
        .await
        .unwrap();
    let totaliser = rx.await.unwrap().expect("no current job");
    assert_eq!(totaliser.job_id, job_id, "current job id mismatch");
    totaliser
}

// ---------------------------------------------------------------------------
// Authentication
// ---------------------------------------------------------------------------

#[test]
fn login_and_card_validation_return_credentials() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let _ = clock;
        let (cmd_tx, _vessel) = spawn_fms(dummy_addr(), dummy_addr());

        let (reply, rx) = oneshot::channel();
        cmd_tx
            .send(FmsCommand::Login {
                username: "dock-operator".to_string(),
                password: "operator-password".to_string(),
                reply,
            })
            .await
            .unwrap();
        assert_eq!(rx.await.unwrap(), "chal-1");

        let (reply, rx) = oneshot::channel();
        cmd_tx
            .send(FmsCommand::ValidateCard {
                card: 1_000_002,
                reply,
            })
            .await
            .unwrap();
        assert_eq!(rx.await.unwrap(), "token-1");
    });
}

// ---------------------------------------------------------------------------
// Refuel job + RFID gate
// ---------------------------------------------------------------------------

#[test]
fn refuel_job_starts_pending_vessel_rfid() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let _ = clock;
        let (cmd_tx, _vessel) = spawn_fms(dummy_addr(), dummy_addr());

        let job_id = create_refuel(&cmd_tx).await;
        assert_eq!(job_status(&cmd_tx, &job_id).await, "pending_vessel_rfid");
    });
}

#[test]
fn rfid_validates_after_card_presented() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let _ = clock;
        let (cmd_tx, mut vessel) = spawn_fms(dummy_addr(), dummy_addr());

        let job_id = create_refuel(&cmd_tx).await;
        assert_eq!(job_status(&cmd_tx, &job_id).await, "pending_vessel_rfid");

        // The tug engineer presents the vessel RFID tag.
        vessel.write_all(VESSEL_CARD).await.unwrap();
        assert_eq!(job_status(&cmd_tx, &job_id).await, "rfid_validated");
    });
}

// ---------------------------------------------------------------------------
// Dispense: drives the real dispenser over Modbus to completion
// ---------------------------------------------------------------------------

#[test]
fn dispense_drives_dispenser_to_completion() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let dispenser_addr = free_addr();
        let (pulse_tx, pulse_rx) = mpsc::channel(4096);
        let (ctrl_tx, mut ctrl_rx) = tokio::sync::watch::channel(Default::default());
        let (_dispenser_state_tx, dispenser_state_rx) = mpsc::channel(1);
        dispenser::spawn(
            clock.clone(),
            Default::default(),
            dispenser_state_rx,
            pulse_rx,
            ctrl_tx,
            dispenser_addr,
        );

        let (cmd_tx, mut vessel) = spawn_fms(dispenser_addr, dummy_addr());

        let job_id = create_refuel(&cmd_tx).await;
        vessel.write_all(VESSEL_CARD).await.unwrap();
        assert_eq!(job_status(&cmd_tx, &job_id).await, "rfid_validated");

        // FMS writes preset + enable over Modbus; the dispenser activates.
        start_dispense(&cmd_tx, &job_id, 100.0).await;
        ctrl_rx.changed().await.unwrap();
        assert!(
            ctrl_rx.borrow_and_update().pump_energised,
            "pump should run after start"
        );

        // Play the flow meter: 100 L × 10 pulses/L drives the auto-stop.
        for _ in 0..1000 {
            pulse_tx.send(Pulse { k_factor: K_FACTOR }).await.unwrap();
        }
        ctrl_rx.changed().await.unwrap();
        assert!(
            !ctrl_rx.borrow_and_update().pump_energised,
            "pump should auto-stop"
        );

        let totaliser = totaliser(&cmd_tx, &job_id).await;
        assert_eq!(totaliser.status, "complete");
        assert!((totaliser.dispensed_litres - 100.0).abs() < 0.5);
    });
}

// ---------------------------------------------------------------------------
// Tank level: reads the real ATG console over TLS
// ---------------------------------------------------------------------------

#[test]
fn tank_level_read_from_atg_console() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let atg_addr = free_addr();
        let (probe_tx, mut probe_rx) = mpsc::channel(16);
        let (_bund_tx, bund_rx) = tokio::sync::watch::channel(BundReading::default());
        let (_alarm_clear_tx, alarm_clear_rx) = mpsc::channel(16);
        let (_console_state_tx, console_state_rx) = mpsc::channel(1);
        // k_L_per_mm = 7, level 5000 mm → 35 000 L.
        atg_console::spawn(
            clock.clone(),
            7.0,
            50_000.0,
            Default::default(),
            console_state_rx,
            probe_tx,
            bund_rx,
            alarm_clear_rx,
            atg_addr,
        );

        let (cmd_tx, _vessel) = spawn_fms(dummy_addr(), atg_addr);

        let (reply, rx) = oneshot::channel();
        cmd_tx
            .send(FmsCommand::GetTankLevel { reply })
            .await
            .unwrap();

        // Service the probe request the console makes to satisfy the i201.
        let probe_reply = probe_rx.recv().await.unwrap();
        probe_reply
            .send(ProbeReading {
                level_mm: 5000.0,
                temperature_c: 15.0,
            })
            .unwrap();

        let level = rx.await.unwrap();
        assert!(
            (level - 35_000.0).abs() < 1.0,
            "expected ~35000 L, got {level}"
        );
    });
}

// ---------------------------------------------------------------------------
// Fill job + ticket transcription
// ---------------------------------------------------------------------------

#[test]
fn fill_job_and_ticket_transcription() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let _ = clock;
        let (cmd_tx, _vessel) = spawn_fms(dummy_addr(), dummy_addr());

        let (reply, rx) = oneshot::channel();
        cmd_tx
            .send(FmsCommand::CreateFillJob {
                tanker_registration: "AB12 CDE".to_string(),
                delivery_volume_litres: 15_000.0,
                reply,
            })
            .await
            .unwrap();
        let job_id = rx.await.unwrap();
        assert_eq!(job_status(&cmd_tx, &job_id).await, "open");

        let (reply, rx) = oneshot::channel();
        cmd_tx
            .send(FmsCommand::TranscribeTicket {
                job_id: job_id.clone(),
                ticket_number: "TICKET-000001".to_string(),
                reply,
            })
            .await
            .unwrap();
        rx.await.unwrap(); // acknowledged
    });
}
