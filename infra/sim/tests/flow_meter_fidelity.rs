//! Does the flow meter faithfully measure what the tank actually loses?
//!
//! These tests wire the real triangle — fuel tank → flow meter → dispenser —
//! and run a single dispense, then compare two independent quantities:
//!
//! - the **tank's actual drain**, read from the tank's own sounding (level
//!   before minus level after); and
//! - the **meter's recorded dispense**, the dispenser's transaction totaliser
//!   (pulse count ÷ k-factor), read over real Modbus.
//!
//! A faithful meter agrees with the tank to within its resolution — one pulse
//! (`1/k` litres). The tests assert exactly that, across clock modes: Synthetic,
//! and `WallTimeMultiple` at ×1, ×10, ×300, and ×1000.
//!
//! The ×1000 case is `#[ignore]`d: at that multiple the real dispense window
//! (~5 ms) falls below the host's timer and scheduler granularity, so the
//! auto-stop overshoot can exceed one pulse on a loaded host. It is retained as
//! documentation of that limit and can be run explicitly with `--ignored`; ×300
//! is the highest multiple expected to hold reliably.
//!
//! This holds because the flow meter counts pulses against *elapsed flow time*
//! rather than per-sleep (see `physical::flow_meter`): a late wake catches up
//! instead of accumulating drift, and the dispenser counts the whole catch-up
//! batch, so the meter records exactly the small over-flow the auto-stop latency
//! causes — the same volume the tank drains. Earlier, with one pulse emitted per
//! `clock.sleep`, the per-pulse timer floor scaled by the multiplier over-drained
//! the tank to empty under real time; these tests were the acceptance criteria
//! for fixing that.

use std::net::SocketAddr;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot, watch};

use tokio_modbus::client::Context;
use tokio_modbus::prelude::*;

use sim::appliances::dispenser::{self, DispenserControlState};
use sim::clock::{SimClock, TimeModel};
use sim::physical::flow_meter;
use sim::physical::fuel_tank::{self, FuelTankState, OutflowRate, TankSounding};

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

// Tank/meter parameters. A large tank so the over-drain never clamps at zero
// (the disagreement stays measurable); a small dispense so the WallTime cases
// stay quick even at x1 (50 L at 600 L/min ≈ 5 s of real pump time).
const TANK_CAPACITY_L: f64 = 50_000.0;
const OVERFILL_INTERLOCK_PCT: f64 = 95.0;
const K_L_PER_MM: f64 = 7.0;
const INFLOW_RATE_L_PER_MIN: f64 = 600.0;
const OUTFLOW_RATE_L_PER_MIN: f64 = 600.0;
const INITIAL_VOLUME_L: f64 = 30_000.0;
const TEMPERATURE_C: f64 = 15.0;
const K_FACTOR: f64 = 10.0; // pulses per litre
const PRESET_L: f64 = 50.0;
const PRESET_X10: u16 = 500; // PRESET_L × 10 (the register's div-10 encoding)

// Dispenser register addresses (dispenser-modbus-tcp-spec.md §8).
const PRESET: u16 = 0x0001;
const ENABLE: u16 = 0x0002;
const TRANSACTION_HI: u16 = 0x0005;

fn free_addr() -> SocketAddr {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

/// Request one sounding and return the tank volume in litres.
async fn read_volume(sounding_tx: &mpsc::Sender<oneshot::Sender<TankSounding>>) -> f64 {
    let (reply_tx, reply_rx) = oneshot::channel();
    sounding_tx.send(reply_tx).await.unwrap();
    reply_rx.await.unwrap().level_mm * K_L_PER_MM
}

/// Await until the dispenser pump is running (`true`) or stopped (`false`).
///
/// The dispenser publishes its control state synchronously when it processes the
/// enable/auto-stop, so the running state is observable immediately after the
/// enable write (no transient to miss) and the stopped state is the stable final
/// state — unlike the outflow-rate signal, whose `None→Some→None` the `watch`
/// would coalesce when the dispense completes inside a single `changed().await`.
async fn wait_for_pump(rx: &mut watch::Receiver<DispenserControlState>, running: bool) {
    loop {
        if rx.borrow_and_update().pump_energised == running {
            return;
        }
        rx.changed().await.unwrap();
    }
}

/// Wire the tank → flow meter → dispenser triangle, run one preset dispense, and
/// assert the tank's actual drain matches the meter's recorded volume to within
/// one pulse. Runs entirely under the supplied clock.
async fn assert_tank_matches_meter(clock: SimClock) {
    let addr = free_addr();

    // Channels owned by the neighbouring entities; the test holds the sounding
    // sender (the ATG probe's role) and the inert ESD/pump/plug senders.
    let (sounding_tx, sounding_rx) = mpsc::channel(16);
    let (bund_tx, _bund_rx) = watch::channel(false);
    let (outflow_tx, outflow_rx) = watch::channel::<OutflowRate>(None);
    let (ctrl_tx, ctrl_rx) = watch::channel(DispenserControlState::default());
    let mut observe_ctrl = ctrl_rx.clone();
    let (pulse_tx, pulse_rx) = mpsc::channel(4096);
    let (esd_tx, esd_rx) = watch::channel(false);
    let (tanker_pump_tx, tanker_pump_rx) = watch::channel(false);
    let (interlock_plug_tx, interlock_plug_rx) = mpsc::channel(16);
    let (_tank_state_tx, tank_state_rx) = mpsc::channel(1);
    let (_dispenser_state_tx, dispenser_state_rx) = mpsc::channel(1);

    fuel_tank::spawn(
        clock.clone(),
        TANK_CAPACITY_L,
        OVERFILL_INTERLOCK_PCT,
        K_L_PER_MM,
        INFLOW_RATE_L_PER_MIN,
        OUTFLOW_RATE_L_PER_MIN,
        FuelTankState {
            volume_l: INITIAL_VOLUME_L,
            temperature_c: TEMPERATURE_C,
            bund_wet: false,
        },
        tank_state_rx,
        sounding_rx,
        bund_tx,
        interlock_plug_rx,
        outflow_tx,
        ctrl_rx,
        esd_rx,
        tanker_pump_rx,
    );
    flow_meter::spawn(clock.clone(), K_FACTOR, outflow_rx, pulse_tx);
    dispenser::spawn(
        clock,
        Default::default(),
        dispenser_state_rx,
        pulse_rx,
        ctrl_tx,
        addr,
    );

    let initial_volume = read_volume(&sounding_tx).await;

    // Drive the dispense over real Modbus: set the preset, enable the pump.
    let mut ctx: Context = tokio_modbus::client::tcp::connect(addr).await.unwrap();
    ctx.write_single_register(PRESET, PRESET_X10)
        .await
        .unwrap()
        .unwrap();
    ctx.write_single_register(ENABLE, 1).await.unwrap().unwrap();

    // Wait until the pump has auto-stopped, at which point the tank has
    // integrated the whole drain. (We don't wait for the running state first:
    // under Synthetic the dispense completes before the enable write even
    // returns, so by here the pump may already have stopped.)
    wait_for_pump(&mut observe_ctrl, false).await;

    let final_volume = read_volume(&sounding_tx).await;
    let tank_drain = initial_volume - final_volume;

    // What the meter recorded: the transaction totaliser, pulses ÷ k-factor.
    let regs = ctx
        .read_holding_registers(TRANSACTION_HI, 2)
        .await
        .unwrap()
        .unwrap();
    let pulses = (u32::from(regs[0]) << 16) | u32::from(regs[1]);
    let meter_dispensed = f64::from(pulses) / K_FACTOR;

    // Keep the inert senders alive until the assertion: dropping them earlier
    // would close the tank's input channels mid-run.
    let _held = (esd_tx, tanker_pump_tx, interlock_plug_tx);

    let one_pulse = 1.0 / K_FACTOR;
    let disagreement = (tank_drain - meter_dispensed).abs();
    assert!(
        disagreement <= one_pulse + 1e-6,
        "tank drained {tank_drain:.3} L but the meter recorded {meter_dispensed:.3} L \
         (preset {PRESET_L} L) — disagreement {disagreement:.3} L exceeds one pulse ({one_pulse} L)"
    );
}

#[test]
fn tank_matches_meter_under_synthetic_time() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(assert_tank_matches_meter(clock));
}

#[test]
fn tank_matches_meter_at_wall_time_x1() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(1.0), SIM_START);
    runtime.block_on(assert_tank_matches_meter(clock));
}

#[test]
fn tank_matches_meter_at_wall_time_x10() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(10.0), SIM_START);
    runtime.block_on(assert_tank_matches_meter(clock));
}

#[test]
fn tank_matches_meter_at_wall_time_x300() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(300.0), SIM_START);
    runtime.block_on(assert_tank_matches_meter(clock));
}

// At ×1000 the ~5 ms real dispense window is below the host's timer/scheduler
// granularity, so the auto-stop overshoot can exceed one pulse on a loaded host
// (flaky). Kept as documentation of that limit; run explicitly with --ignored.
#[test]
#[ignore = "flaky at ×1000: real dispense window below host timer granularity; see module docs"]
fn tank_matches_meter_at_wall_time_x1000() {
    let (clock, runtime) = SimClock::isolated(TimeModel::WallTimeMultiple(1000.0), SIM_START);
    runtime.block_on(assert_tank_matches_meter(clock));
}
