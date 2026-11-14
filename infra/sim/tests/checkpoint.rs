//! Tests for `sim::checkpoint` (sim-tests.md, "Stopping and warm start",
//! "Checkpoint"): the document's round trip, its atomic write, and the
//! refusal rules on reading it. Each test reads from and writes to temporary
//! state and run directories.

use std::fs;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::time::Duration;

use time::macros::datetime;
use time::PrimitiveDateTime;

use sim::actors::dock_operator::{DockOperatorState, BDN};
use sim::actors::fuel_supplier::{FuelSupplierState, SupplierOrder};
use sim::actors::reconciliation_user::{
    AuditPeriod, ReconciliationRecord, ReconciliationUserState,
};
use sim::actors::road_tanker_driver::{DeliveryTicket, RoadTankerDriverState};
use sim::actors::systems_administrator::SystemsAdministratorState;
use sim::actors::tug_engineer::VesselId;
use sim::appliances::atg_console::AtgConsoleState;
use sim::appliances::dispenser::DispenserState;
use sim::checkpoint::{
    self, Checkpoint, Parameters, VesselParameters, CHECKPOINT_FILE, FORMAT_VERSION, LOADED_MARKER,
    RESEED_FILE,
};
use sim::physical::atg_probe::AtgProbeState;
use sim::physical::fuel_tank::{FuelTankRows, FuelTankState};
use sim::physical::tug_arrival_generator::{DispenseJob, TugArrivalState, TugPhase, TugState};
use sim::shared::RfidCardNumber;

/// The clock's start in every test.
const START: PrimitiveDateTime = datetime!(2025-03-01 12:00);
const MAX_START_GAP: Duration = Duration::from_secs(5 * 60);

fn parameters() -> Parameters {
    Parameters {
        tank_capacity_l: 50_000.0,
        k_l_per_mm: 7.0,
        inflow_rate_l_per_min: 600.0,
        outflow_rate_l_per_min: 300.0,
        initial_volume_l: 35_000.0,
        initial_temperature_c: 15.0,
        flow_meter_k_factor: 10.0,
        probe_noise_enabled: false,
        overfill_interlock_pct: 95.0,
        vessels: vec![
            VesselParameters {
                vessel_id: 9_876_543,
                name: "Tug Alpha".to_string(),
                rfid_card: 99_999,
                mean_time_away: Duration::from_secs(4 * 60 * 60),
            },
            VesselParameters {
                vessel_id: 9_876_555,
                name: "Tug Bravo".to_string(),
                rfid_card: 99_998,
                mean_time_away: Duration::from_secs(6 * 60 * 60),
            },
        ],
        refuel_volume_min_l: 500.0,
        refuel_volume_max_l: 3_000.0,
        driver_id: "driver-01".to_string(),
        tanker_registration: "AB12 CDE".to_string(),
        poll_interval: Duration::from_secs(15 * 60),
        reorder_threshold_l: 15_000.0,
        delivery_volume_l: 18_000.0,
        lead_time: Duration::from_secs(24 * 60 * 60),
        audit_period: AuditPeriod::Month,
        settling_delay: Duration::from_secs(6 * 60 * 60),
        initial_window: Duration::from_secs(7 * 24 * 60 * 60),
        boundary_margin: Duration::from_secs(2 * 60 * 60),
        clipboard_deadline: Duration::from_secs(60),
        desktop_settle: Duration::from_secs(10),
        terminal_settle: Duration::from_secs(3),
        key_interval: Duration::from_millis(30),
        connect_deadline: Duration::from_secs(30),
        admin_check_interval: Duration::from_secs(24 * 60 * 60),
        admin_step_deadline: Duration::from_secs(30),
    }
}

fn job(arrived_at: PrimitiveDateTime, volume_l: f64) -> DispenseJob {
    DispenseJob {
        arrived_at,
        vessel_id: VesselId(9_876_543),
        vessel_name: "Tug Alpha",
        volume_l,
        rfid_card: RfidCardNumber(99_999),
    }
}

fn bdn() -> BDN {
    BDN {
        bdn_number: "BDN-000004".to_string(),
        delivery_commenced_at: datetime!(2025-02-27 9:00),
        vessel_id: VesselId(9_876_543),
        vessel_name: "Tug Alpha",
        port: "Ravensport",
        supplier_name: "Ravensport Harbour Authority",
        supplier_address: "Fuel Dock, East Quay, Ravensport RV1 4QD",
        supplier_telephone: "01632 960482",
        grade: "MGO".to_string(),
        volume_l: 1_250.5,
        temperature_c: 15.0,
        temp_corrected_volume_l: 1_250.5,
        quantity_t: 1.050_42,
        density_kg_per_m3: 840.0,
        sulphur_pct_mm: 0.1,
        flashpoint_c: 66.0,
        sulphur_limit_declared: true,
        start_totaliser_l: 0.0,
        end_totaliser_l: 1_250.5,
        operator_id: "jrenard".to_string(),
        engineer_id: "tug-engineer".to_string(),
        signed_by_operator: true,
        signed_by_engineer: true,
    }
}

fn ticket() -> DeliveryTicket {
    DeliveryTicket {
        ticket_number: "TICKET-000002".to_string(),
        delivery_commenced_at: datetime!(2025-02-28 14:30),
        grade: "MGO".to_string(),
        gross_volume_l: 18_000.0,
        temperature_c: 15.0,
        temp_corrected_volume_l: 18_000.0,
        density_kg_per_l: 0.84,
        start_totaliser_l: 18_000.0,
        end_totaliser_l: 36_000.0,
        tanker_registration: "AB12 CDE".to_string(),
        driver_id: "driver-01".to_string(),
        signed_by_driver: true,
        signed_by_operator: true,
    }
}

/// A checkpoint with a value in every field, written at `written_at`.
fn checkpoint(written_at: PrimitiveDateTime) -> Checkpoint {
    Checkpoint {
        format_version: FORMAT_VERSION,
        written_at,
        parameters: parameters(),
        fuel_tank: FuelTankState {
            volume_l: 21_345.25,
            temperature_c: 15.0,
            bund_wet: true,
        },
        atg_probe: AtgProbeState {
            noise_seed: 3,
            noise_word_pos: 1 << 70,
        },
        atg_console: AtgConsoleState {
            bund_alarm_latched: true,
        },
        dispenser: DispenserState {
            cumulative_pulses: 123_456,
            transaction_pulses: 12_505,
        },
        tug_arrivals: TugArrivalState {
            seed: 42,
            tugs: vec![
                TugState {
                    vessel_id: VesselId(9_876_543),
                    word_pos: (1 << 64) + 17,
                    phase: TugPhase::AtDock {
                        job: job(datetime!(2025-03-01 11:58), 2_100.0),
                    },
                },
                TugState {
                    vessel_id: VesselId(9_876_555),
                    word_pos: 40,
                    phase: TugPhase::Away {
                        next_arrival_at: Some(datetime!(2025-03-01 15:10)),
                    },
                },
            ],
        },
        dock_operator: DockOperatorState { bdn_seq: 5 },
        road_tanker_driver: RoadTankerDriverState {
            meter_totaliser_l: 36_000.0,
            ticket_seq: 3,
        },
        fuel_supplier: FuelSupplierState {
            next_poll_at: datetime!(2025-03-01 12:10),
            order: SupplierOrder::TankerDueAt(datetime!(2025-03-02 8:15)),
        },
        reconciliation_user: ReconciliationUserState {
            window_start: datetime!(2025-02-01 0:00),
            window_end: datetime!(2025-03-01 0:00),
            records: vec![
                ReconciliationRecord::Bdn(bdn()),
                ReconciliationRecord::DeliveryTicket(ticket()),
            ],
        },
        systems_administrator: SystemsAdministratorState {
            next_check_at: datetime!(2025-03-02 9:00),
            previous_check_at: Some(datetime!(2025-03-01 9:00)),
        },
    }
}

/// Temporary state and run directories.
struct Directories {
    state: tempfile::TempDir,
    run: tempfile::TempDir,
}

impl Directories {
    fn new() -> Self {
        Self {
            state: tempfile::tempdir().unwrap(),
            run: tempfile::tempdir().unwrap(),
        }
    }

    fn with_checkpoint(checkpoint: &Checkpoint) -> Self {
        let directories = Self::new();
        directories.write_state(CHECKPOINT_FILE, &serde_json::to_string(checkpoint).unwrap());
        directories
    }

    fn write_state(&self, name: &str, text: &str) {
        fs::write(self.state.path().join(name), text).unwrap();
    }

    fn read(&self) -> Result<Option<Checkpoint>, String> {
        checkpoint::read(
            self.state.path(),
            self.run.path(),
            START,
            &parameters(),
            MAX_START_GAP,
        )
    }

    fn marker_exists(&self) -> bool {
        self.run.path().join(LOADED_MARKER).exists()
    }
}

fn refusal(result: Result<Option<Checkpoint>, String>) -> String {
    result.expect_err("the checkpoint should be refused")
}

#[test]
fn checkpoint_round_trips() {
    let original = checkpoint(START);
    let text = serde_json::to_string_pretty(&original).unwrap();
    assert!(
        text.contains("\"18446744073709551633\""),
        "the word position is a decimal string"
    );
    assert!(
        text.contains("\"2025-03-01T12:00:00Z\""),
        "date-times are RFC 3339 UTC"
    );
    let read: Checkpoint = serde_json::from_str(&text).unwrap();
    assert_eq!(read, original);
}

#[test]
fn checkpoint_written_by_rename() {
    let run = tempfile::tempdir().unwrap();
    checkpoint::write_at_rest(run.path(), &FuelTankRows::default(), &checkpoint(START)).unwrap();

    let text = fs::read_to_string(run.path().join(CHECKPOINT_FILE)).unwrap();
    let read: Checkpoint = serde_json::from_str(&text).unwrap();
    assert_eq!(read, checkpoint(START));
    assert!(!run.path().join(format!("{CHECKPOINT_FILE}.tmp")).exists());
}

#[test]
fn absent_checkpoint_is_a_cold_start() {
    let directories = Directories::new();
    assert_eq!(directories.read(), Ok(None));
    assert!(!directories.marker_exists());
}

#[test]
fn accepted_checkpoint_creates_marker() {
    let accepted = Directories::with_checkpoint(&checkpoint(START));
    assert_eq!(accepted.read(), Ok(Some(checkpoint(START))));
    assert!(accepted.marker_exists());

    let refused = Directories::with_checkpoint(&checkpoint(START + Duration::from_secs(1)));
    refusal(refused.read());
    assert!(!refused.marker_exists());
}

#[test]
fn unparseable_checkpoint_is_refused() {
    let directories = Directories::new();
    let text = serde_json::to_string(&checkpoint(START)).unwrap();
    directories.write_state(CHECKPOINT_FILE, &text[..text.len() / 2]);

    let message = refusal(directories.read());
    assert!(message.contains(CHECKPOINT_FILE), "{message}");
}

#[test]
fn format_version_mismatch_is_refused() {
    let directories = Directories::with_checkpoint(&Checkpoint {
        format_version: FORMAT_VERSION + 1,
        ..checkpoint(START)
    });

    let message = refusal(directories.read());
    assert!(
        message.contains(&(FORMAT_VERSION + 1).to_string()),
        "{message}"
    );
    assert!(message.contains(&FORMAT_VERSION.to_string()), "{message}");
}

#[test]
fn parameter_mismatch_is_refused_naming_the_field() {
    let directories = Directories::with_checkpoint(&Checkpoint {
        parameters: Parameters {
            tank_capacity_l: 60_000.0,
            ..parameters()
        },
        ..checkpoint(START)
    });

    let message = refusal(directories.read());
    assert!(message.contains("tank_capacity_l"), "{message}");
}

#[test]
fn checkpoint_written_after_start_is_refused() {
    let directories = Directories::with_checkpoint(&checkpoint(START + Duration::from_secs(1)));
    refusal(directories.read());
}

#[test]
fn checkpoint_older_than_max_start_gap_is_refused() {
    let written_at = START - MAX_START_GAP - Duration::from_secs(1);
    let directories = Directories::with_checkpoint(&checkpoint(written_at));

    let message = refusal(directories.read());
    assert!(message.contains("2025-03-01T11:54:59Z"), "{message}");
    assert!(message.contains("2025-03-01T12:00:00Z"), "{message}");
}

#[test]
fn checkpoint_within_max_start_gap_is_accepted() {
    for written_at in [START, START - MAX_START_GAP] {
        let directories = Directories::with_checkpoint(&checkpoint(written_at));
        assert_eq!(directories.read(), Ok(Some(checkpoint(written_at))));
    }
}

#[test]
fn reseed_replaces_seed_and_resets_position() {
    let saved = checkpoint(START);
    let directories = Directories::with_checkpoint(&saved);
    directories.write_state(RESEED_FILE, r#"{"tug_arrival_seed": 7}"#);

    let read = directories.read().unwrap().unwrap();
    let mut expected = saved.tug_arrivals.clone();
    expected.seed = 7;
    for tug in &mut expected.tugs {
        tug.word_pos = 0;
    }
    assert_eq!(
        read.tug_arrivals, expected,
        "every tug's position is reset and its saved phase kept"
    );
    assert_eq!(
        read.atg_probe, saved.atg_probe,
        "the probe's sequence is kept"
    );
}

#[test]
fn reseed_with_unknown_field_is_refused() {
    let directories = Directories::with_checkpoint(&checkpoint(START));
    directories.write_state(RESEED_FILE, r#"{"tug_seed": 7}"#);

    let message = refusal(directories.read());
    assert!(message.contains("tug_seed"), "{message}");
}

#[test]
fn reseed_without_checkpoint_is_refused() {
    let directories = Directories::new();
    directories.write_state(RESEED_FILE, r#"{"tug_arrival_seed": 7}"#);
    refusal(directories.read());
}

#[test]
fn sim_not_at_rest_writes_no_checkpoint() {
    let run = tempfile::tempdir().unwrap();
    let rows = FuelTankRows {
        dispenser_solenoid_open: true,
        ..FuelTankRows::default()
    };

    let written = catch_unwind(AssertUnwindSafe(|| {
        checkpoint::write_at_rest(run.path(), &rows, &checkpoint(START))
    }));
    assert!(written.is_err(), "an invariant violation panics");
    assert!(directory_is_empty(run.path()));
}

fn directory_is_empty(path: &Path) -> bool {
    fs::read_dir(path).unwrap().next().is_none()
}
