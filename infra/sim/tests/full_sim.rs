//! Full self-running integration test.
//!
//! The whole sim (every entity, real transport between them) runs against a
//! mocked digital world (FMS + HMI + ATG relay, talking to each other by
//! channel and to the sim by real Modbus/TLS/HTTP/serial). Nothing in the sim
//! is mocked. With tugs arriving, the tank drains; the fuel supplier reorders;
//! road tankers refill — all on the Synthetic clock, which advances only when
//! no boundary I/O is in flight, so the run is as fast as the host allows.
//!
//! Three tests share one run harness, parameterised by `TimeModel`:
//!
//! - [`sim_runs_itself_until_two_refills`] is a smoke test (Synthetic): the
//!   closed loop sustains itself far enough to complete two refills without any
//!   entity dying.
//! - [`sim_conserves_fuel_and_only_reorders_below_threshold`] is the thorough
//!   one (Synthetic). The mock HMI's request/response log is the digital world's
//!   entire view of the sim, and because the dock operator processes jobs
//!   strictly sequentially it is a clean, ordered timeline. From it we (a) check
//!   every job's request sequence and field values, and (b) read the
//!   ATG-reported tank level at each delivery and assert the control law: a
//!   tanker only ever arrives when the level is at or below the reorder
//!   threshold, and the level stays physically bounded.
//! - [`sim_evolves_correctly_under_wall_time_pacing`] runs the same checks under
//!   `WallTimeMultiple` — the mode `main` uses against a real digital world —
//!   covering the real-timer clock path. (Meter fidelity itself is covered
//!   directly by `tests/flow_meter_fidelity.rs`.)

mod mock_digital;

use std::net::SocketAddr;
use std::time::Duration;

use serde_json::Value;
use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_modbus::prelude::Reader;

use sim::actors::fuel_supplier::SupplierStart;
use sim::actors::reconciliation_user::AuditPeriod;
use sim::checkpoint::{self, Checkpoint};
use sim::clock::{SimClock, TimeModel};
use sim::orchestrator::{self, AtRest, Config, ReconciliationUserConfig, SimHandles};
use sim::physical::tug_arrival_generator::Vessel;
use sim::serial::SerialConnection;

use mock_digital::fms;
use mock_digital::hmi::{MockHmi, Recorded};
use mock_digital::relay::MockRelay;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);
/// The systems administrator's interval: no run here reaches its first check.
const ADMIN_CHECK_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);
// Pulses per litre. Low (vs the real 100) so a dispense costs few clock ticks
// under Synthetic time, keeping the run fast; the loop is otherwise unaffected.
const K_FACTOR: f64 = 1.0;

// Physical parameters that the fuel-level reconstruction also depends on, so
// the test and the sim share one source of truth.
const TANK_CAPACITY_L: f64 = 50_000.0;
const INITIAL_VOLUME_L: f64 = 19_000.0;
const REORDER_THRESHOLD_L: f64 = 18_000.0;
const DELIVERY_VOLUME_L: f64 = 4_000.0;

// Tug dispense volumes are drawn from Uniform(500, 3000) L (narrative §2.2),
// the refuel range the sim is configured with below.
const DISPENSE_MIN_L: f64 = 500.0;
const DISPENSE_MAX_L: f64 = 3000.0;

/// The roster of `world.yaml`, each tug away for long enough on average that
/// together the tugs arrive about five times an hour, which draws the tank down
/// within the run.
fn test_roster() -> &'static [Vessel] {
    let roster = sim::constants::VESSELS;
    let mean_time_away = Duration::from_secs(12 * 60) * roster.len() as u32;
    let vessels: Vec<Vessel> = roster
        .iter()
        .map(|vessel| Vessel {
            mean_time_away,
            ..*vessel
        })
        .collect();
    Box::leak(vessels.into_boxed_slice())
}

fn free_addr() -> SocketAddr {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

fn count_requests(log: &[Recorded], method: &str, path: &str) -> usize {
    log.iter()
        .filter(|r| r.method == method && r.path == path)
        .count()
}

/// Advance the sim on the shared clock until `n` refills have been transcribed,
/// bounded by a generous sim-time budget so a stalled sim fails rather than
/// spins.
async fn wait_for_refills(clock: &SimClock, hmi: &MockHmi, n: usize) {
    let step = Duration::from_secs(300);
    let budget = Duration::from_secs(30 * 24 * 60 * 60); // 30 sim-days
    let mut elapsed = Duration::ZERO;
    while elapsed < budget {
        if count_requests(&hmi.requests(), "POST", "/api/delivery-tickets") >= n {
            return;
        }
        clock.sleep(step).await;
        elapsed += step;
    }
    panic!(
        "only {} refill(s) after {:?} of sim time (wanted {n})",
        count_requests(&hmi.requests(), "POST", "/api/delivery-tickets"),
        budget
    );
}

/// Wait until a refuel's BDN number has been transcribed to `hmi`.
async fn wait_for_refuel(clock: &SimClock, hmi: &MockHmi) {
    let step = Duration::from_secs(300);
    let budget = Duration::from_secs(30 * 24 * 60 * 60); // 30 sim-days
    let mut elapsed = Duration::ZERO;
    while elapsed < budget {
        if first_field(&hmi.requests(), "PATCH", "bdn_number").is_some() {
            return;
        }
        clock.sleep(step).await;
        elapsed += step;
    }
    panic!("no refuel after {budget:?} of sim time");
}

/// A real-wall-clock backstop: Synthetic time advances independently, so this
/// fires only if the sim makes no real progress (a deadlock), failing the test
/// instead of hanging.
async fn real_timeout() {
    tokio::time::sleep(Duration::from_secs(120)).await;
}

/// The mocked digital world (FMS, HMI, ATG relay), and the sim's ends of the
/// RFID serial lines until the sim takes them.
struct DigitalWorld {
    hmi: MockHmi,
    relay: MockRelay,
    atg_addr: SocketAddr,
    dispenser_addr: SocketAddr,
    vessel_serial: Option<SerialConnection>,
    operator_serial: Option<SerialConnection>,
}

/// Start the mocked digital world. Must be called within the runtime.
fn start_digital_world() -> DigitalWorld {
    let (vessel_serial, vessel_handle) = SerialConnection::loopback();
    let (operator_serial, operator_handle) = SerialConnection::loopback();
    let (fms_tx, fms_rx) = mpsc::channel(64);

    let atg_addr = free_addr();
    let dispenser_addr = free_addr();

    fms::spawn(fms_rx, vessel_handle, dispenser_addr, atg_addr, K_FACTOR);
    let hmi = MockHmi::start(operator_handle, fms_tx);
    let relay = MockRelay::start(atg_addr);
    DigitalWorld {
        hmi,
        relay,
        atg_addr,
        dispenser_addr,
        vessel_serial: Some(vessel_serial),
        operator_serial: Some(operator_serial),
    }
}

/// The sim's configuration against `world`, taking its serial lines.
fn sim_config(world: &mut DigitalWorld) -> Config {
    Config {
        tank_capacity_l: TANK_CAPACITY_L,
        overfill_interlock_pct: 95.0,
        k_l_per_mm: 7.0,
        inflow_rate_l_per_min: 600.0,
        outflow_rate_l_per_min: 300.0,
        initial_volume_l: INITIAL_VOLUME_L,
        initial_temperature_c: 15.0,
        flow_meter_k_factor: K_FACTOR,
        probe_noise_enabled: false,
        probe_noise_seed: 0,
        initial_cumulative_pulses: 0,
        first_bdn_number: 1,
        vessels: test_roster(),
        refuel_volume_min_l: DISPENSE_MIN_L,
        refuel_volume_max_l: DISPENSE_MAX_L,
        tug_seed: 42,
        driver_id: "driver-01".to_string(),
        tanker_registration: "AB12 CDE".to_string(),
        first_ticket_number: 1,
        initial_meter_totaliser_l: 0.0,
        poll_interval: Duration::from_secs(600),
        reorder_threshold_l: REORDER_THRESHOLD_L,
        delivery_volume_l: DELIVERY_VOLUME_L,
        lead_time: Duration::from_secs(300),
        supplier_start: SupplierStart::FirstPollAfter(Duration::from_secs(600)),
        atg_console_listen_addr: world.atg_addr,
        dispenser_listen_addr: world.dispenser_addr,
        dock_operator_local_addr: "127.0.0.1:0".parse().unwrap(),
        hmi_addr: world.hmi.addr,
        fuel_supplier_local_addr: "127.0.0.1:0".parse().unwrap(),
        atg_relay_addr: world.relay.addr,
        systems_administrator_egress_addr: "127.0.0.1:9".parse().unwrap(),
        systems_administrator_managed_hosts: ["fms-01", "hist-01", "hmi-01"]
            .into_iter()
            .map(str::to_string)
            .collect(),
        admin_first_check_after: ADMIN_CHECK_INTERVAL,
        admin_check_interval: ADMIN_CHECK_INTERVAL,
        admin_step_deadline: Duration::from_millis(100),
        vessel_rfid_serial: world.vessel_serial.take().expect("one sim per world"),
        operator_rfid_serial: world.operator_serial.take().expect("one sim per world"),
        // A monthly audit, which a run of a few refills does not reach, so
        // the reconciliation user never dials its unanswered port.
        reconciliation_user: ReconciliationUserConfig {
            local_addr: "127.0.0.1:0".parse().unwrap(),
            recon_addr: free_addr(),
            audit_period: AuditPeriod::Month,
            first_audit_after: None,
            settling_delay: Duration::from_secs(6 * 60 * 60),
            boundary_margin: Duration::from_secs(2 * 60 * 60),
            initial_window: Duration::from_secs(7 * 24 * 60 * 60),
            width_floor: Duration::from_secs(24 * 60 * 60),
            clipboard_deadline: Duration::from_secs(60),
            desktop_settle: Duration::from_secs(10),
            terminal_settle: Duration::from_secs(3),
            key_interval: Duration::from_millis(30),
            connect_deadline: Duration::from_secs(30),
        },
    }
}

/// Run `sim` until `deliveries` refills have been transcribed to `hmi`,
/// failing on the first entity to stop or on the real-time backstop.
async fn run_until_refills(
    clock: &SimClock,
    sim: &mut SimHandles,
    hmi: &MockHmi,
    deliveries: usize,
) {
    tokio::select! {
        () = wait_for_refills(clock, hmi, deliveries) => {}
        cause = sim.await_first_failure() => panic!("a sim entity stopped: {cause}"),
        () = real_timeout() => panic!(
            "timed out before {deliveries} refills (delivery tickets: {})",
            count_requests(&hmi.requests(), "POST", "/api/delivery-tickets")
        ),
    }
}

/// Build and run the full sim under `model` until `deliveries` refills have
/// completed, then return the mock HMI's recorded request log (the integration
/// test's sole observation surface).
fn run_full_sim(deliveries: usize, model: TimeModel) -> Vec<Recorded> {
    let (clock, runtime) = SimClock::isolated(model, SIM_START);
    runtime.block_on(async move {
        let mut world = start_digital_world();
        let config = sim_config(&mut world);
        let mut sim = orchestrator::build(clock.clone(), config, None);
        run_until_refills(&clock, &mut sim, &world.hmi, deliveries).await;
        world.hmi.requests()
    })
}

/// One run of the sim, in a runtime of its own as a process is, from
/// `checkpoint` or cold: run under Synthetic time until `deliveries` refills
/// have been transcribed, stop, and bring the sim to rest. Returns the sim at
/// rest and the mock HMI's log.
fn run_then_stop(checkpoint: Option<Checkpoint>, deliveries: usize) -> (AtRest, Vec<Recorded>) {
    let start = checkpoint
        .as_ref()
        .map_or(SIM_START, |saved| saved.written_at);
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, start);
    runtime.block_on(async move {
        let mut world = start_digital_world();
        let config = sim_config(&mut world);
        let mut sim = orchestrator::build(clock.clone(), config, checkpoint);
        run_until_refills(&clock, &mut sim, &world.hmi, deliveries).await;
        (stop(&mut sim).await, world.hmi.requests())
    })
}

/// Stop `sim` and bring it to rest, failing on the real-time backstop.
async fn stop(sim: &mut SimHandles) -> AtRest {
    sim.stop();
    tokio::select! {
        at_rest = sim.come_to_rest() => at_rest.unwrap_or_else(|cause| {
            panic!("a sim entity stopped while coming to rest: {cause}")
        }),
        () = real_timeout() => panic!("the sim did not come to rest"),
    }
}

#[test]
fn sim_runs_itself_until_two_refills() {
    let log = run_full_sim(2, TimeModel::Synthetic);
    assert!(
        count_requests(&log, "POST", "/api/delivery-tickets") >= 2,
        "two refills should have completed"
    );
    assert!(
        count_requests(&log, "POST", "/api/jobs/refuel") >= 1,
        "tugs should have dispensed to drain the tank"
    );
}

#[test]
fn sim_conserves_fuel_and_only_reorders_below_threshold() {
    const DELIVERIES: usize = 10;
    let log = run_full_sim(DELIVERIES, TimeModel::Synthetic);
    let log = truncate_after_nth_delivery(&log, DELIVERIES);
    let timeline = validate_lifecycles(&log, DELIVERIES);
    assert_reorders_below_threshold(&timeline);
}

/// The same validation under `WallTimeMultiple` — the mode `main` uses against a
/// real digital world — covering the real-timer clock path. Now that the flow
/// meter counts pulses against elapsed flow time (see `physical::flow_meter`),
/// the tank stays healthy under real time rather than over-draining to empty, so
/// the reported ATG level at each fill is a genuine below-threshold reading and
/// the check is meaningful.
#[test]
fn sim_evolves_correctly_under_wall_time_pacing() {
    const DELIVERIES: usize = 2;
    // 1 simulated hour per real second: fast, but real timers still drive it.
    let log = run_full_sim(DELIVERIES, TimeModel::WallTimeMultiple(3600.0));
    let log = truncate_after_nth_delivery(&log, DELIVERIES);
    let timeline = validate_lifecycles(&log, DELIVERIES);
    assert_reorders_below_threshold(&timeline);
}

// ---------------------------------------------------------------------------
// Stopping and warm start (sim-tests.md, "Stopping and warm start")
// ---------------------------------------------------------------------------

#[test]
fn stop_does_not_close_any_channel() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let mut world = start_digital_world();
        let config = sim_config(&mut world);
        let mut sim = orchestrator::build(clock.clone(), config, None);
        run_until_refills(&clock, &mut sim, &world.hmi, 1).await;

        let at_rest = stop(&mut sim).await;
        // Every entity is at rest and holds its channel ends: none finishes.
        tokio::select! {
            cause = sim.await_first_failure() => panic!("a sim entity stopped at rest: {cause}"),
            () = clock.sleep(Duration::from_secs(24 * 60 * 60)) => {}
        }

        let run = tempfile::tempdir().unwrap();
        checkpoint::write_at_rest(run.path(), &at_rest.rows, &at_rest.checkpoint).unwrap();
    });
}

#[test]
fn whole_sim_checkpoint_carries_the_administrator_state() {
    // Stopped at once, before the systems administrator's first check is due.
    let (at_rest, _) = run_then_stop(None, 0);
    let state = at_rest.checkpoint.systems_administrator;
    assert_eq!(state.next_check_at, SIM_START + ADMIN_CHECK_INTERVAL);
    assert_eq!(state.previous_check_at, None);
}

#[test]
fn warm_start_continues_counters_and_totalisers() {
    let (first, first_log) = run_then_stop(None, 1);
    let saved = first.checkpoint;
    let last_bdn =
        last_field(&first_log, "PATCH", "bdn_number").expect("a refuel in the first run");
    let last_ticket =
        last_field(&first_log, "POST", "ticket_number").expect("a fill in the first run");

    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, saved.written_at);
    runtime.block_on(async move {
        let mut world = start_digital_world();
        let config = sim_config(&mut world);
        let mut sim = orchestrator::build(clock.clone(), config, Some(saved.clone()));

        let volume_l = read_atg_volume(world.atg_addr).await;
        assert!(
            (volume_l - saved.fuel_tank.volume_l).abs() < 0.01,
            "the first ATG reading, {volume_l} L, is the volume at the stop, {} L",
            saved.fuel_tank.volume_l
        );
        assert_eq!(
            read_cumulative_pulses(world.dispenser_addr).await,
            saved.dispenser.cumulative_pulses,
            "the dispenser's cumulative totaliser continues"
        );

        // A fill can complete before any tug is refuelled, so the run
        // continues until it has both.
        run_until_refills(&clock, &mut sim, &world.hmi, 1).await;
        tokio::select! {
            () = wait_for_refuel(&clock, &world.hmi) => {}
            cause = sim.await_first_failure() => panic!("a sim entity stopped: {cause}"),
            () = real_timeout() => panic!("timed out before a refuel in the second run"),
        }
        let log = world.hmi.requests();
        assert_eq!(
            first_field(&log, "PATCH", "bdn_number").expect("a refuel in the second run"),
            next_number(&last_bdn)
        );
        assert_eq!(
            first_field(&log, "POST", "ticket_number").expect("a fill in the second run"),
            next_number(&last_ticket)
        );
    });
}

#[test]
fn fuel_conserved_across_a_stop() {
    let (first, first_log) = run_then_stop(None, 1);
    let (second, second_log) = run_then_stop(Some(first.checkpoint), 1);

    let logs = [first_log, second_log];
    let dispensed: Vec<f64> = logs.iter().flat_map(|log| dispensed_volumes(log)).collect();
    let filled_l: f64 = logs
        .iter()
        .flatten()
        .filter(|r| r.method == "POST" && r.path == "/api/delivery-tickets")
        .map(|r| body_f64(r, "delivered_volume_litres"))
        .sum();
    let expected_l = INITIAL_VOLUME_L - dispensed.iter().sum::<f64>() + filled_l;
    let volume_l = second.checkpoint.fuel_tank.volume_l;

    // The tank drains continuously and the meter counts whole pulses, so each
    // dispense may differ from its metered volume by up to one pulse.
    let tolerance_l = dispensed.len() as f64 / K_FACTOR + 1e-6;
    assert!(
        (volume_l - expected_l).abs() <= tolerance_l,
        "tank at {volume_l} L after the second delivery; {expected_l} L expected from \
         {} dispenses and {filled_l} L filled",
        dispensed.len()
    );
}

/// The `field` of the last request body of `method` that carries it.
fn last_field(log: &[Recorded], method: &str, field: &str) -> Option<String> {
    fields(log, method, field).last().cloned()
}

/// The `field` of the first request body of `method` that carries it.
fn first_field(log: &[Recorded], method: &str, field: &str) -> Option<String> {
    fields(log, method, field).first().cloned()
}

fn fields(log: &[Recorded], method: &str, field: &str) -> Vec<String> {
    log.iter()
        .filter(|r| r.method == method)
        .filter_map(|r| {
            let body: Value = serde_json::from_str(&r.body).ok()?;
            Some(body[field].as_str()?.to_string())
        })
        .collect()
}

/// The number following a form number such as `BDN-000004`.
fn next_number(number: &str) -> String {
    let (prefix, digits) = number.rsplit_once('-').unwrap();
    format!("{prefix}-{:06}", digits.parse::<u32>().unwrap() + 1)
}

/// The metered volume of every completed dispense, from the last totaliser
/// poll of each job.
fn dispensed_volumes(log: &[Recorded]) -> Vec<f64> {
    let mut by_job: Vec<(String, f64)> = Vec::new();
    for r in log
        .iter()
        .filter(|r| r.path == "/api/jobs/current/dispenser/totaliser")
    {
        let response: Value = serde_json::from_str(&r.response).unwrap();
        if response["status"] != "complete" {
            continue;
        }
        let job = response["job_id"].as_str().unwrap().to_string();
        let litres = response["dispensed_litres"].as_f64().unwrap();
        match by_job.iter_mut().find(|(id, _)| *id == job) {
            Some(entry) => entry.1 = litres,
            None => by_job.push((job, litres)),
        }
    }
    by_job.into_iter().map(|(_, litres)| litres).collect()
}

/// Read the tank volume from the ATG console's `i201` inventory report
/// (tls-protocol.md §6.2).
async fn read_atg_volume(atg_addr: SocketAddr) -> f64 {
    let mut stream = TcpStream::connect(atg_addr).await.unwrap();
    stream.write_all(b"\x01i20100").await.unwrap();
    let mut response = Vec::new();
    loop {
        let byte = stream.read_u8().await.unwrap();
        response.push(byte);
        if byte == 0x03 {
            break;
        }
    }
    // The body follows `SOH echo(6) timestamp(10)`; the Volume field is its
    // eight hex characters at offset 9.
    let hex = std::str::from_utf8(&response[17 + 9..17 + 17]).unwrap();
    f64::from(f32::from_bits(u32::from_str_radix(hex, 16).unwrap()))
}

/// Read the dispenser's cumulative totaliser over Modbus
/// (dispenser-modbus-tcp-spec.md §8).
async fn read_cumulative_pulses(dispenser_addr: SocketAddr) -> u32 {
    let mut context = tokio_modbus::client::tcp::connect(dispenser_addr)
        .await
        .unwrap();
    let words = context
        .read_holding_registers(0x0003, 2)
        .await
        .unwrap()
        .unwrap();
    (u32::from(words[0]) << 16) | u32::from(words[1])
}

// ---------------------------------------------------------------------------
// Event-log validation
// ---------------------------------------------------------------------------

/// One HMI request, classified by endpoint. Endpoints still addressed by job id
/// (`dispenser/start`, the BDN `PATCH`) carry it from the URL so a lifecycle's
/// requests can be checked for consistency; the `current`-addressed polls carry
/// the resolved job id in their response body instead (checked in
/// [`check_dispense`]).
#[derive(Debug, PartialEq, Eq)]
enum Req {
    Login,
    Challenge,
    Refuel,
    JobStatus,
    Start(String),
    Totaliser,
    Bdn(String),
    TankLevel,
    TankFill,
    DeliveryTicket,
}

fn classify(r: &Recorded) -> Req {
    let segments: Vec<&str> = r.path.trim_matches('/').split('/').collect();
    match (r.method.as_str(), segments.as_slice()) {
        ("POST", ["api", "auth", "login"]) => Req::Login,
        ("GET", ["api", "auth", "challenge", _]) => Req::Challenge,
        ("POST", ["api", "jobs", "refuel"]) => Req::Refuel,
        ("POST", ["api", "jobs", "tank-fill"]) => Req::TankFill,
        ("POST", ["api", "delivery-tickets"]) => Req::DeliveryTicket,
        ("GET", ["api", "tanks", _, "level"]) => Req::TankLevel,
        ("POST", ["api", "jobs", id, "dispenser", "start"]) => Req::Start((*id).to_string()),
        ("GET", ["api", "jobs", "current", "dispenser", "totaliser"]) => Req::Totaliser,
        ("GET", ["api", "jobs", "current"]) => Req::JobStatus,
        ("PATCH", ["api", "jobs", id]) => Req::Bdn((*id).to_string()),
        _ => panic!("unexpected HMI request: {} {}", r.method, r.path),
    }
}

/// Extract an `f64` field from a recorded request's JSON body.
fn body_f64(r: &Recorded, field: &str) -> f64 {
    let body: Value = serde_json::from_str(&r.body)
        .unwrap_or_else(|e| panic!("invalid JSON body for {} {}: {e}", r.method, r.path));
    body[field]
        .as_f64()
        .unwrap_or_else(|| panic!("missing field {field} in body of {} {}", r.method, r.path))
}

/// Extract a string field from a recorded response's JSON body.
fn response_str(r: &Recorded, field: &str) -> String {
    let body: Value = serde_json::from_str(&r.response)
        .unwrap_or_else(|e| panic!("invalid JSON response for {} {}: {e}", r.method, r.path));
    body[field]
        .as_str()
        .unwrap_or_else(|| {
            panic!(
                "missing field {field} in response of {} {}",
                r.method, r.path
            )
        })
        .to_string()
}

/// Extract an `f64` field from a recorded response's JSON body.
fn response_f64(r: &Recorded, field: &str) -> f64 {
    let body: Value = serde_json::from_str(&r.response)
        .unwrap_or_else(|e| panic!("invalid JSON response for {} {}: {e}", r.method, r.path));
    body[field].as_f64().unwrap_or_else(|| {
        panic!(
            "missing field {field} in response of {} {}",
            r.method, r.path
        )
    })
}

/// Return the prefix of `log` up to and including the `n`th delivery ticket.
/// Because lifecycles are sequential and a delivery ticket ends a fill, that
/// prefix is always a whole number of complete lifecycles — no trailing partial
/// job to confuse the parser.
fn truncate_after_nth_delivery(log: &[Recorded], n: usize) -> Vec<Recorded> {
    let mut seen = 0;
    for (idx, r) in log.iter().enumerate() {
        if r.method == "POST" && r.path == "/api/delivery-tickets" {
            seen += 1;
            if seen == n {
                return log[..=idx].to_vec();
            }
        }
    }
    panic!("log has only {seen} delivery tickets, wanted {n}");
}

/// A completed lifecycle in the log, in order. A fill carries the ATG-reported
/// tank level (`volume_litres`) the dock operator read at the start of the
/// delivery — the physical ground truth, not a reconstruction.
enum Lifecycle {
    Dispense,
    Fill(f64),
}

/// Walk the request log, verifying the login prologue and every lifecycle's
/// request sequence and field values, and return the ordered timeline. This is
/// mode-independent: it makes no timing assumptions, so it holds under both
/// Synthetic and WallTime.
fn validate_lifecycles(log: &[Recorded], deliveries: usize) -> Vec<Lifecycle> {
    let kinds: Vec<Req> = log.iter().map(classify).collect();

    assert_eq!(
        kinds.first(),
        Some(&Req::Login),
        "first request must be login"
    );
    assert_eq!(
        kinds.get(1),
        Some(&Req::Challenge),
        "login must be followed by the RFID challenge"
    );

    let mut timeline = Vec::new();
    let mut i = 2;
    while i < kinds.len() {
        match &kinds[i] {
            Req::Refuel => {
                check_dispense(&kinds, log, &mut i);
                timeline.push(Lifecycle::Dispense);
            }
            Req::TankLevel => timeline.push(Lifecycle::Fill(check_fill(&kinds, log, &mut i))),
            other => panic!("unexpected start of a lifecycle at index {i}: {other:?}"),
        }
    }

    let fills = timeline
        .iter()
        .filter(|l| matches!(l, Lifecycle::Fill(_)))
        .count();
    let dispenses = timeline.len() - fills;
    assert_eq!(
        fills, deliveries,
        "expected exactly {deliveries} completed refills"
    );
    assert!(
        dispenses >= deliveries,
        "only {dispenses} dispenses across {deliveries} deliveries — the loop is not sustaining drains"
    );
    timeline
}

/// Assert the reorder control law against the ATG ground truth: at every
/// delivery, the tank level the dock operator read (`volume_litres` from the
/// `GET .../level` response) is at or below the reorder threshold — the supplier
/// dispatches a tanker only below threshold and has one order pending at a time, so
/// the level it reads can only be at or below it. Also checks the level stays
/// physically bounded.
///
/// This uses the *reported* level, not a preset-based reconstruction, so it is
/// exact under both Synthetic and WallTime: it reads the actual integrated tank
/// volume and so carries none of the per-pulse timer drift a reconstruction
/// would (see the discussion of meter-vs-tank reconciliation).
fn assert_reorders_below_threshold(timeline: &[Lifecycle]) {
    let mut fills = 0usize;
    for lifecycle in timeline {
        if let Lifecycle::Fill(level) = lifecycle {
            assert!(
                (0.0..=TANK_CAPACITY_L).contains(level),
                "refill #{} read an unphysical tank level of {level} L",
                fills + 1
            );
            assert!(
                *level <= REORDER_THRESHOLD_L + 1e-6,
                "refill #{} began with the tank at {level} L, above the reorder threshold {REORDER_THRESHOLD_L} L",
                fills + 1
            );
            fills += 1;
        }
    }
}

/// Verify one dispense lifecycle starting at `*i`, advancing `*i` past it,
/// including that the dispenser preset equals the tug's requested volume.
fn check_dispense(kinds: &[Req], log: &[Recorded], i: &mut usize) {
    // POST /api/jobs/refuel — its response carries the job id the rest of the
    // lifecycle resolves to (the `current`-addressed polls echo it in their
    // response body).
    let requested = body_f64(&log[*i], "requested_volume_litres");
    assert!(
        (DISPENSE_MIN_L..=DISPENSE_MAX_L).contains(&requested),
        "tug requested volume {requested} L outside Uniform({DISPENSE_MIN_L}, {DISPENSE_MAX_L})"
    );
    let job = response_str(&log[*i], "job_id");
    *i += 1;

    // One or more RFID-gate status polls, all resolving to this job.
    let mut polled_status = false;
    while let Some(Req::JobStatus) = kinds.get(*i) {
        assert_eq!(
            response_str(&log[*i], "job_id"),
            job,
            "GET /api/jobs/current resolved to a different job than the live dispense {job}"
        );
        polled_status = true;
        *i += 1;
    }
    assert!(
        polled_status,
        "dispense {job} never polled job status before starting the pump"
    );

    // POST /api/jobs/{job}/dispenser/start, preset == requested.
    match kinds.get(*i) {
        Some(Req::Start(id)) if *id == job => {
            let preset = body_f64(&log[*i], "preset_volume_litres");
            assert!(
                (preset - requested).abs() < 1e-6,
                "dispenser preset {preset} L != tug requested {requested} L for {job}"
            );
        }
        other => panic!("expected dispenser/start for {job} after the RFID gate, got {other:?}"),
    }
    *i += 1;

    // One or more totaliser polls, all resolving to this job.
    let mut polled = false;
    while let Some(Req::Totaliser) = kinds.get(*i) {
        assert_eq!(
            response_str(&log[*i], "job_id"),
            job,
            "totaliser poll resolved to a different job than the live dispense {job}"
        );
        polled = true;
        *i += 1;
    }
    assert!(polled, "dispense for {job} never polled the totaliser");

    // GET /api/jobs/current: the FMS's dispense start for the BDN, read once the
    // dispense is complete (sim.md §8.3).
    match kinds.get(*i) {
        Some(Req::JobStatus) => {
            assert_eq!(
                response_str(&log[*i], "job_id"),
                job,
                "the dispense-start read resolved to a different job than the live dispense {job}"
            );
            response_str(&log[*i], "opened_at");
        }
        other => {
            panic!("expected the BDN's dispense-start read after {job} completed, got {other:?}")
        }
    }
    *i += 1;

    // GET /api/tanks/01/level: the tank temperature for the BDN, read once the
    // dispense is complete.
    match kinds.get(*i) {
        Some(Req::TankLevel) => {}
        other => panic!("expected the BDN's temperature read after {job} completed, got {other:?}"),
    }
    *i += 1;

    // PATCH /api/jobs/{job} (BDN) closes the lifecycle.
    match kinds.get(*i) {
        Some(Req::Bdn(id)) if *id == job => {}
        other => panic!("expected the BDN patch to close {job}, got {other:?}"),
    }
    *i += 1;
}

/// Verify one fill lifecycle starting at `*i` (the `GET .../level` already
/// matched by the caller), advancing `*i` past it. Returns the ATG-reported tank
/// level (`volume_litres`) from the level-check response.
fn check_fill(kinds: &[Req], log: &[Recorded], i: &mut usize) -> f64 {
    // GET /api/tanks/01/level (already classified as TankLevel by the caller);
    // its response carries the ATG-reported tank volume.
    let atg_level = response_f64(&log[*i], "volume_litres");
    *i += 1;

    // POST /api/jobs/tank-fill, volume == configured delivery.
    match kinds.get(*i) {
        Some(Req::TankFill) => {
            let volume = body_f64(&log[*i], "delivery_volume_litres");
            assert!(
                (volume - DELIVERY_VOLUME_L).abs() < 1e-6,
                "tank-fill volume {volume} L != configured delivery {DELIVERY_VOLUME_L} L"
            );
        }
        other => panic!("expected tank-fill after the level check, got {other:?}"),
    }
    *i += 1;

    // POST /api/delivery-tickets, delivered volume == configured delivery.
    match kinds.get(*i) {
        Some(Req::DeliveryTicket) => {
            let delivered = body_f64(&log[*i], "delivered_volume_litres");
            assert!(
                (delivered - DELIVERY_VOLUME_L).abs() < 1e-6,
                "delivered volume {delivered} L != configured delivery {DELIVERY_VOLUME_L} L"
            );
        }
        other => panic!("expected the delivery ticket to close the fill, got {other:?}"),
    }
    *i += 1;

    atg_level
}
