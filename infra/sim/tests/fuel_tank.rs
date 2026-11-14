//! Integration tests for `sim::physical::fuel_tank`.
//!
//! Exercises the entity through its public `spawn()` interface only, holding
//! the channel ends that neighbouring entities would own (see sim-tests.md).
//! All tests run under `SimClock::isolated(TimeModel::Synthetic, SIM_START)`.

use std::time::Duration;

use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;

use sim::appliances::dispenser::DispenserControlState;
use sim::clock::{SimClock, TimeModel};
use sim::physical::fuel_tank::{
    self, FuelTankReport, FuelTankState, InterlockCircuit, InterlockPlug, OutflowRate, TankSounding,
};
use time::macros::datetime;
use time::PrimitiveDateTime;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);
const CAPACITY_L: f64 = 10_000.0;
// An interlock level of 9 500 L, for tests that do not state another.
const OVERFILL_INTERLOCK_PCT: f64 = 95.0;
const K_L_PER_MM: f64 = 7.0;
const INFLOW_L_PER_MIN: f64 = 200.0;
const OUTFLOW_L_PER_MIN: f64 = 100.0;
const TEMPERATURE_C: f64 = 15.0;
const DEFAULT_INITIAL_L: f64 = 5_000.0;
const EPS_MM: f64 = 1e-9;
// The tank's boundary tolerance, 1e-6 L, as a level.
const TOL_MM: f64 = 1e-6 / K_L_PER_MM;

fn level_for(volume_l: f64) -> f64 {
    volume_l / K_L_PER_MM
}

fn active_dispenser() -> DispenserControlState {
    DispenserControlState {
        solenoid_open: true,
        pump_energised: true,
    }
}

/// Counterpart channel ends held by the test (the roles of neighbouring
/// entities), plus the tank's own task handle (for the cavitation panic test).
struct Harness {
    sounding_tx: mpsc::Sender<oneshot::Sender<TankSounding>>,
    interlock_plug_tx: mpsc::Sender<InterlockPlug>,
    bund_wet_rx: watch::Receiver<bool>,
    outflow_rate_rx: watch::Receiver<OutflowRate>,
    dispenser_ctrl_tx: watch::Sender<DispenserControlState>,
    esd_tx: watch::Sender<bool>,
    tanker_pump_tx: watch::Sender<bool>,
    handle: JoinHandle<()>,
    // Retained so the tank's state request channel stays open.
    _state_tx: mpsc::Sender<oneshot::Sender<FuelTankReport>>,
}

fn spawn_tank(clock: SimClock, initial_volume_l: f64) -> Harness {
    spawn_tank_with_interlock(clock, initial_volume_l, OVERFILL_INTERLOCK_PCT)
}

fn spawn_tank_with_interlock(
    clock: SimClock,
    initial_volume_l: f64,
    overfill_interlock_pct: f64,
) -> Harness {
    let (sounding_tx, sounding_rx) = mpsc::channel(16);
    let (interlock_plug_tx, interlock_plug_rx) = mpsc::channel(16);
    let (bund_wet_tx, bund_wet_rx) = watch::channel(false);
    let (outflow_rate_tx, outflow_rate_rx) = watch::channel(None);
    let (dispenser_ctrl_tx, dispenser_ctrl_rx) = watch::channel(DispenserControlState::default());
    let (esd_tx, esd_rx) = watch::channel(false);
    let (tanker_pump_tx, tanker_pump_rx) = watch::channel(false);
    let (state_tx, state_rx) = mpsc::channel(1);

    let handle = fuel_tank::spawn(
        clock,
        CAPACITY_L,
        overfill_interlock_pct,
        K_L_PER_MM,
        INFLOW_L_PER_MIN,
        OUTFLOW_L_PER_MIN,
        FuelTankState {
            volume_l: initial_volume_l,
            temperature_c: TEMPERATURE_C,
            bund_wet: false,
        },
        state_rx,
        sounding_rx,
        bund_wet_tx,
        interlock_plug_rx,
        outflow_rate_tx,
        dispenser_ctrl_rx,
        esd_rx,
        tanker_pump_rx,
    );

    Harness {
        sounding_tx,
        interlock_plug_tx,
        bund_wet_rx,
        outflow_rate_rx,
        dispenser_ctrl_tx,
        esd_tx,
        tanker_pump_tx,
        handle,
        _state_tx: state_tx,
    }
}

async fn request_sounding(h: &Harness) -> TankSounding {
    let (reply_tx, reply_rx) = oneshot::channel();
    h.sounding_tx.send(reply_tx).await.unwrap();
    reply_rx.await.unwrap()
}

/// Await the tank task and assert it terminated with a cavitation panic.
async fn assert_cavitation_panic(handle: JoinHandle<()>) {
    let err = handle.await.unwrap_err();
    assert!(err.is_panic());
    let payload = err.into_panic();
    let message = payload
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| payload.downcast_ref::<&str>().copied())
        .unwrap_or("");
    assert!(
        message.contains("cavitation"),
        "unexpected panic message: {message}"
    );
}

/// Plug the overfill cable in: establish an interlock lease and return the
/// receiver carrying its live `InterlockCircuit` state.
async fn plug_in(h: &Harness) -> watch::Receiver<InterlockCircuit> {
    let (reply_tx, reply_rx) = oneshot::channel();
    h.interlock_plug_tx
        .send(InterlockPlug { reply: reply_tx })
        .await
        .unwrap();
    reply_rx.await.unwrap()
}

// ---------------------------------------------------------------------------
// Outflow activation (watch-channel observation)
// ---------------------------------------------------------------------------

#[test]
fn outflow_active_when_solenoid_and_pump_open() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_some());
    });
}

#[test]
fn outflow_inactive_when_esd_pressed() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_some());

        h.esd_tx.send(true).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_none());
    });
}

#[test]
fn outflow_resumes_after_esd_released() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_some());

        h.esd_tx.send(true).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_none());

        h.esd_tx.send(false).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_some());
    });
}

#[test]
fn outflow_inactive_when_dispenser_clears() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_some());

        h.dispenser_ctrl_tx
            .send(DispenserControlState::default())
            .unwrap();
        h.outflow_rate_rx.changed().await.unwrap();
        assert!(h.outflow_rate_rx.borrow().is_none());
    });
}

// ---------------------------------------------------------------------------
// Overfill interlock lease (plug-in observation)
// ---------------------------------------------------------------------------

#[test]
fn interlock_live_when_plugged_in_below_threshold() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        let circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);
    });
}

#[test]
fn interlock_dead_at_plug_in_above_threshold() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), 9_600.0);
        let circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);
    });
}

#[test]
fn interlock_trips_dead_at_threshold() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), 9_400.0);
        let mut circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);

        h.tanker_pump_tx.send(true).unwrap();
        // 100 L of ullage at 200 L/min: the scheduler wakes at the 9 500 L
        // crossing and drives the lease Dead.
        circuit.changed().await.unwrap();
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);
    });
}

/// The interlock level is `overfill_interlock_pct` of the capacity, taken from
/// the configuration: at 90 % the lease trips at 9 000 L, below the default
/// 9 500 L.
#[test]
fn interlock_level_taken_from_configuration() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank_with_interlock(clock.clone(), 8_900.0, 90.0);
        let mut circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);

        // 100 L below the 9 000 L level at 200 L/min: the scheduler wakes at
        // the crossing and drives the lease Dead.
        h.tanker_pump_tx.send(true).unwrap();
        circuit.changed().await.unwrap();
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);

        // No simulated time passes before the pump stops, so the sounding
        // reads the volume at which the lease tripped.
        h.tanker_pump_tx.send(false).unwrap();
        let s = request_sounding(&h).await;
        assert!(
            (s.level_mm - level_for(9_000.0)).abs() < TOL_MM,
            "the lease tripped at {} mm, not at the 9 000 L level ({} mm)",
            s.level_mm,
            level_for(9_000.0)
        );

        // Above the configured level and below the default: dead at plug-in.
        let above = spawn_tank_with_interlock(clock.clone(), 9_200.0, 90.0);
        assert_eq!(*plug_in(&above).await.borrow(), InterlockCircuit::Dead);
    });
}

#[test]
fn interlock_dead_when_esd_pressed() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        let mut circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);

        h.esd_tx.send(true).unwrap();
        circuit.changed().await.unwrap();
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);
    });
}

#[test]
fn interlock_stays_dead_after_draining_below_threshold() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), 9_400.0);
        let mut circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);

        // Fill past 95%: the circuit trips Dead.
        h.tanker_pump_tx.send(true).unwrap();
        circuit.changed().await.unwrap();
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);

        // Drain back below the threshold while holding the same lease — a
        // concurrent fill and drain, not reachable in normal operation but
        // enforced regardless.
        h.tanker_pump_tx.send(false).unwrap();
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(Duration::from_secs(60)).await;

        // The sounding forces a re-evaluation below the threshold. The circuit
        // has latched Dead: a broken interlock never returns to Live within a
        // lease; only a fresh plug-in resets it.
        let s = request_sounding(&h).await;
        assert!(s.level_mm < level_for(OVERFILL_INTERLOCK_PCT / 100.0 * CAPACITY_L));
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);
    });
}

#[test]
fn bund_wets_when_defeated_tanker_ignores_dead_interlock() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        // A tanker whose pump controller ignores the Dead interlock: it holds
        // the lease and keeps the pump running past 95%. Modelled here by
        // holding the lease receiver and never clearing `tanker_pump_tx`.
        let mut h = spawn_tank(clock.clone(), 9_400.0);
        let mut circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);

        h.tanker_pump_tx.send(true).unwrap();
        // The interlock trips Dead at the 95% threshold; the defeated pump
        // controller does not cut the pump.
        circuit.changed().await.unwrap();
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);

        // Inflow continues past the threshold to the overflow point: the tank
        // re-schedules its wake from the threshold to capacity and the bund
        // latches wet.
        h.bund_wet_rx.changed().await.unwrap();
        assert!(*h.bund_wet_rx.borrow());
    });
}
#[test]
fn bund_stays_dry_when_pump_obeys_tripped_interlock() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        // A working fill: the pump controller cuts the pump the instant the
        // interlock trips `Dead` at 95%. The tank stops short of capacity and
        // the bund stays dry — the overfill interlock doing its job. This is
        // the counterpart to `bund_wets_when_defeated_tanker_ignores_dead_interlock`.
        let h = spawn_tank(clock.clone(), 9_400.0);
        let mut circuit = plug_in(&h).await;
        assert_eq!(*circuit.borrow(), InterlockCircuit::Live);

        h.tanker_pump_tx.send(true).unwrap();
        // The interlock trips Dead at the 9 500 L threshold (100 L at 200 L/min).
        circuit.changed().await.unwrap();
        assert_eq!(*circuit.borrow(), InterlockCircuit::Dead);

        // A working pump controller cuts the pump on the trip.
        h.tanker_pump_tx.send(false).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(Duration::from_secs(60)).await;

        // The tank stopped at the threshold, below capacity, with no fuel
        // having escaped: the bund is dry.
        let s = request_sounding(&h).await;
        assert!(s.level_mm < level_for(CAPACITY_L));
        assert!(!*h.bund_wet_rx.borrow());
    });
}
// ---------------------------------------------------------------------------
// Volume dynamics over elapsed time (sounding observation)
// ---------------------------------------------------------------------------

#[test]
fn sounding_reflects_outflow_over_elapsed_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(Duration::from_secs(60)).await;
        let s = request_sounding(&h).await;
        let expected = level_for(DEFAULT_INITIAL_L - OUTFLOW_L_PER_MIN);
        assert!((s.level_mm - expected).abs() < EPS_MM);
    });
}

#[test]
fn sounding_reflects_inflow_over_elapsed_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.tanker_pump_tx.send(true).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(Duration::from_secs(60)).await;
        let s = request_sounding(&h).await;
        let expected = level_for(DEFAULT_INITIAL_L + INFLOW_L_PER_MIN);
        assert!((s.level_mm - expected).abs() < EPS_MM);
    });
}

#[test]
fn sounding_reflects_net_rate_with_simultaneous_flows() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        h.tanker_pump_tx.send(true).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(Duration::from_secs(60)).await;
        let s = request_sounding(&h).await;
        let expected = level_for(DEFAULT_INITIAL_L + INFLOW_L_PER_MIN - OUTFLOW_L_PER_MIN);
        assert!((s.level_mm - expected).abs() < EPS_MM);
    });
}

#[test]
fn sounding_unchanged_when_all_flows_inactive() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        clock.sleep(Duration::from_secs(300)).await;
        let s = request_sounding(&h).await;
        assert!((s.level_mm - level_for(DEFAULT_INITIAL_L)).abs() < EPS_MM);
    });
}

// ---------------------------------------------------------------------------
// Boundary enforcement: overflow and cavitation
// ---------------------------------------------------------------------------

#[test]
fn bund_wets_on_overflow() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        // No interlock lease is held, so nothing cuts the pump: inflow carries
        // the tank to capacity and `bund_wet` latches (the scheduled overflow
        // wake lands the volume on the capacity boundary, within `TOL_L`).
        let mut h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.tanker_pump_tx.send(true).unwrap();
        h.bund_wet_rx.changed().await.unwrap();
        assert!(*h.bund_wet_rx.borrow());
    });
}

#[test]
fn bund_wets_when_filling_an_already_full_tank() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        // The tank is already at 100% capacity when the pump starts:
        // the first step exceeds the volume on the capacity boundary and
        // the bund latches wet immediately, without a boundary-crossing wake
        // to reach it first. Counterpart of the start-empty test
        // `cavitation_panics_when_dispensing_from_empty_tank`.
        let mut h = spawn_tank(clock.clone(), CAPACITY_L);
        h.tanker_pump_tx.send(true).unwrap();
        h.bund_wet_rx.changed().await.unwrap();
        assert!(*h.bund_wet_rx.borrow());
    });
}
#[test]
fn bund_stays_wet_after_draining_below_capacity() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);

        // Overflow the tank so the bund latches wet.
        h.tanker_pump_tx.send(true).unwrap();
        h.bund_wet_rx.changed().await.unwrap();
        assert!(*h.bund_wet_rx.borrow());

        // Stop filling and drain the primary tank back below capacity.
        h.tanker_pump_tx.send(false).unwrap();
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        tokio::task::yield_now().await;
        clock.sleep(Duration::from_secs(60)).await;

        // The escaped fuel remains in the bund: it stays wet even though the
        // level has fallen below capacity. The sounding forces a step at the
        // current time, re-evaluating (and not clearing) the latch.
        let s = request_sounding(&h).await;
        assert!(s.level_mm < level_for(CAPACITY_L));
        assert!(*h.bund_wet_rx.borrow());
    });
}

#[test]
fn cavitation_panics_when_outflow_drains_empty_tank() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let h = spawn_tank(clock.clone(), DEFAULT_INITIAL_L);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        // The scheduler wakes the tank at the 0 L crossing; outflow is still
        // active, so it panics.
        assert_cavitation_panic(h.handle).await;
    });
}

#[test]
fn cavitation_panics_when_dispensing_from_empty_tank() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        // The tank is already empty: opening the dispenser draws air at once,
        // so the panic fires on the boundary condition at t = 0 rather than
        // after a drain.
        let h = spawn_tank(clock.clone(), 0.0);
        h.dispenser_ctrl_tx.send(active_dispenser()).unwrap();
        assert_cavitation_panic(h.handle).await;
    });
}
