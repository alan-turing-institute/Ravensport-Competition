//! Integration tests for `sim::appliances::dispenser`.
//!
//! Exercises the dispenser through its public `spawn()` interface only, driven
//! by a real Modbus TCP client (`tokio-modbus`) over loopback — proving a
//! standard client cannot tell it is talking to a simulation rather than a real
//! controller. The behaviours and register map are specified in
//! dispenser-modbus-tcp-spec.md and the test plan in sim-tests.md.
//!
//! The test holds the channel ends neighbouring entities own: `pulse_tx` (the
//! flow meter's role) and `dispenser_ctrl_rx` (the fuel tank's role). The
//! dispenser takes a `SimClock` to guard its Modbus exchanges, so each test runs
//! on an isolated runtime via `SimClock::isolated` + `block_on` (the standard
//! harness). These tests do not advance simulated time — they drive everything
//! by pulses and Modbus operations — so the clock simply stays at the start.

use std::net::SocketAddr;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, oneshot, watch};

use tokio_modbus::client::Context;
use tokio_modbus::prelude::*;

use sim::appliances::dispenser::{self, DispenserControlState, DispenserState};
use sim::clock::{SimClock, TimeModel};
use sim::physical::flow_meter::Pulse;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

// Construction parameters used in tests (sim-tests.md).
const K_FACTOR: f64 = 10.0; // pulses per litre
const PRESET_REG: u16 = 1000; // 100 L written as litres × 10
const AUTO_STOP_PULSES: u32 = 1000; // 100 L × 10 pulses/L

// Register addresses (dispenser-modbus-tcp-spec.md §8).
const PRESET: u16 = 0x0001;
const ENABLE: u16 = 0x0002;
const CUMULATIVE_HI: u16 = 0x0003;
const TRANSACTION_HI: u16 = 0x0005;
const STATUS: u16 = 0x0007;

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/// Counterpart channel ends held by the test.
struct Harness {
    pulse_tx: mpsc::Sender<Pulse>,
    ctrl_rx: watch::Receiver<DispenserControlState>,
    // Retained so the dispenser's state request channel stays open.
    _state_tx: mpsc::Sender<oneshot::Sender<DispenserState>>,
}

fn free_addr() -> SocketAddr {
    // Bind an ephemeral port, read it back, drop the listener so the dispenser
    // can re-bind it.
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
}

fn spawn_dispenser(clock: &SimClock, addr: SocketAddr) -> Harness {
    spawn_dispenser_at(clock, addr, 0)
}

/// A dispenser whose cumulative totaliser starts at `initial_cumulative_pulses`
/// (sim-impl.md, "Start state").
fn spawn_dispenser_at(
    clock: &SimClock,
    addr: SocketAddr,
    initial_cumulative_pulses: u32,
) -> Harness {
    // Generous pulse-channel capacity so a whole transaction's pulses buffer
    // without back-pressure; the accumulator drains them between awaits.
    let (pulse_tx, pulse_rx) = mpsc::channel(4096);
    let (ctrl_tx, ctrl_rx) = watch::channel(DispenserControlState::default());
    let (state_tx, state_rx) = mpsc::channel(1);
    dispenser::spawn(
        clock.clone(),
        DispenserState {
            cumulative_pulses: initial_cumulative_pulses,
            transaction_pulses: 0,
        },
        state_rx,
        pulse_rx,
        ctrl_tx,
        addr,
    );
    Harness {
        pulse_tx,
        ctrl_rx,
        _state_tx: state_tx,
    }
}

async fn connect(addr: SocketAddr) -> Context {
    tokio_modbus::client::tcp::connect(addr).await.unwrap()
}

async fn send_pulses(harness: &Harness, n: u32) {
    for _ in 0..n {
        harness
            .pulse_tx
            .send(Pulse { k_factor: K_FACTOR })
            .await
            .unwrap();
    }
}

/// Read a 32-bit totaliser from its consecutive register pair.
async fn read_u32(ctx: &mut Context, hi_addr: u16) -> u32 {
    let regs = ctx
        .read_holding_registers(hi_addr, 2)
        .await
        .unwrap()
        .unwrap();
    (u32::from(regs[0]) << 16) | u32::from(regs[1])
}

/// Read a single 16-bit register.
async fn read_u16(ctx: &mut Context, addr: u16) -> u16 {
    ctx.read_holding_registers(addr, 1).await.unwrap().unwrap()[0]
}

/// Spin (yielding to let the pulse accumulator drain) until a 32-bit totaliser
/// reaches `expected`. Totalisers are monotonic, so this cannot overshoot a
/// value we sent exactly; the bound guards against a stalled accumulator.
async fn await_u32(ctx: &mut Context, hi_addr: u16, expected: u32) {
    for _ in 0..10_000 {
        if read_u32(ctx, hi_addr).await == expected {
            return;
        }
        tokio::task::yield_now().await;
    }
    panic!("totaliser at {hi_addr:#06x} never reached {expected}");
}

/// Write the standard preset and enable the pump, then consume the resulting
/// activation on the control channel.
async fn arm_and_run(ctx: &mut Context, harness: &mut Harness) {
    ctx.write_single_register(PRESET, PRESET_REG)
        .await
        .unwrap()
        .unwrap();
    ctx.write_single_register(ENABLE, 1).await.unwrap().unwrap();
    harness.ctrl_rx.changed().await.unwrap();
    let state = *harness.ctrl_rx.borrow_and_update();
    assert!(
        state.solenoid_open && state.pump_energised,
        "pump should be running after enable"
    );
}

/// Send a raw MBAP+PDU frame on a fresh connection and return the response
/// `(transaction_id, protocol_id, unit_id, response_pdu)`.
async fn raw_round_trip(
    addr: SocketAddr,
    txid: [u8; 2],
    unit_id: u8,
    pdu: &[u8],
) -> ([u8; 2], [u8; 2], u8, Vec<u8>) {
    let mut stream = TcpStream::connect(addr).await.unwrap();
    let length = u16::try_from(pdu.len() + 1).unwrap();
    let mut request = Vec::new();
    request.extend_from_slice(&txid);
    request.extend_from_slice(&[0x00, 0x00]); // protocol identifier
    request.extend_from_slice(&length.to_be_bytes());
    request.push(unit_id);
    request.extend_from_slice(pdu);
    stream.write_all(&request).await.unwrap();

    let mut header = [0u8; 7];
    stream.read_exact(&mut header).await.unwrap();
    let resp_len = usize::from(u16::from_be_bytes([header[4], header[5]]));
    let mut resp_pdu = vec![0u8; resp_len - 1];
    stream.read_exact(&mut resp_pdu).await.unwrap();
    (
        [header[0], header[1]],
        [header[2], header[3]],
        header[6],
        resp_pdu,
    )
}

// ---------------------------------------------------------------------------
// Control state and pump enable
// ---------------------------------------------------------------------------

#[test]
fn initial_control_state_is_inactive() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let harness = spawn_dispenser(&clock, addr);
        let state = *harness.ctrl_rx.borrow();
        assert!(!state.solenoid_open);
        assert!(!state.pump_energised);
    });
}

#[test]
fn pump_enable_activates_solenoid_and_pump() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        ctx.write_single_register(PRESET, PRESET_REG)
            .await
            .unwrap()
            .unwrap();
        ctx.write_single_register(ENABLE, 1).await.unwrap().unwrap();

        harness.ctrl_rx.changed().await.unwrap();
        let state = *harness.ctrl_rx.borrow_and_update();
        assert!(state.solenoid_open);
        assert!(state.pump_energised);
    });
}

#[test]
fn pump_disable_clears_solenoid_and_pump() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;

        ctx.write_single_register(ENABLE, 0).await.unwrap().unwrap();
        harness.ctrl_rx.changed().await.unwrap();
        let state = *harness.ctrl_rx.borrow_and_update();
        assert!(!state.solenoid_open);
        assert!(!state.pump_energised);
    });
}

// ---------------------------------------------------------------------------
// Totalisers and auto-stop
// ---------------------------------------------------------------------------

#[test]
fn totalisers_accumulate_received_pulses() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, 50).await;

        await_u32(&mut ctx, CUMULATIVE_HI, 50).await;
        assert_eq!(read_u32(&mut ctx, TRANSACTION_HI).await, 50);
    });
}

#[test]
fn cumulative_totaliser_starts_at_zero_by_default() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        assert_eq!(read_u32(&mut ctx, CUMULATIVE_HI).await, 0);
    });
}

#[test]
fn cumulative_totaliser_starts_at_the_start_state() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        // Above 65,535, so the high word carries part of the reading.
        let mut harness = spawn_dispenser_at(&clock, addr, 70_000);
        let mut ctx = connect(addr).await;

        assert_eq!(read_u32(&mut ctx, CUMULATIVE_HI).await, 70_000);
        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, 50).await;

        await_u32(&mut ctx, CUMULATIVE_HI, 70_050).await;
        assert_eq!(read_u32(&mut ctx, TRANSACTION_HI).await, 50);
    });
}

#[test]
fn auto_stop_triggers_at_preset_pulse_count() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, AUTO_STOP_PULSES).await;

        harness.ctrl_rx.changed().await.unwrap();
        let state = *harness.ctrl_rx.borrow_and_update();
        assert!(!state.solenoid_open);
        assert!(!state.pump_energised);
    });
}

#[test]
fn cumulative_totaliser_retains_count_after_auto_stop() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, AUTO_STOP_PULSES).await;
        harness.ctrl_rx.changed().await.unwrap();

        await_u32(&mut ctx, CUMULATIVE_HI, AUTO_STOP_PULSES).await;
    });
}

#[test]
fn transaction_totaliser_resets_on_new_preset_write() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, 200).await;
        await_u32(&mut ctx, TRANSACTION_HI, 200).await;

        ctx.write_single_register(PRESET, 2000)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(read_u32(&mut ctx, TRANSACTION_HI).await, 0);
    });
}

#[test]
fn cumulative_totaliser_not_reset_on_new_preset_write() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, 200).await;
        await_u32(&mut ctx, CUMULATIVE_HI, 200).await;

        ctx.write_single_register(PRESET, 2000)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(read_u32(&mut ctx, CUMULATIVE_HI).await, 200);
    });
}

#[test]
fn cumulative_totaliser_is_monotonically_non_decreasing_across_transactions() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        for expected in [1000u32, 2000, 3000] {
            ctx.write_single_register(PRESET, PRESET_REG)
                .await
                .unwrap()
                .unwrap();
            ctx.write_single_register(ENABLE, 1).await.unwrap().unwrap();
            harness.ctrl_rx.changed().await.unwrap();
            assert!(harness.ctrl_rx.borrow_and_update().pump_energised);

            send_pulses(&harness, AUTO_STOP_PULSES).await;
            harness.ctrl_rx.changed().await.unwrap();
            assert!(!harness.ctrl_rx.borrow_and_update().pump_energised);

            await_u32(&mut ctx, CUMULATIVE_HI, expected).await;
        }
    });
}

#[test]
fn dispenser_re_arms_after_auto_stop() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        send_pulses(&harness, AUTO_STOP_PULSES).await;
        harness.ctrl_rx.changed().await.unwrap();
        assert!(!harness.ctrl_rx.borrow_and_update().pump_energised);

        ctx.write_single_register(PRESET, PRESET_REG)
            .await
            .unwrap()
            .unwrap();
        ctx.write_single_register(ENABLE, 1).await.unwrap().unwrap();
        harness.ctrl_rx.changed().await.unwrap();
        let state = *harness.ctrl_rx.borrow_and_update();
        assert!(state.solenoid_open);
        assert!(state.pump_energised);
    });
}

#[test]
fn status_flag_set_when_running_cleared_when_stopped() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let mut harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        arm_and_run(&mut ctx, &mut harness).await;
        assert_eq!(read_u16(&mut ctx, STATUS).await, 0x0001);

        ctx.write_single_register(ENABLE, 0).await.unwrap().unwrap();
        harness.ctrl_rx.changed().await.unwrap();
        assert_eq!(read_u16(&mut ctx, STATUS).await, 0x0000);
    });
}

// ---------------------------------------------------------------------------
// Register map and holding-register read-back
// ---------------------------------------------------------------------------

#[test]
fn writable_registers_read_back() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        ctx.write_single_register(PRESET, 1500)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(read_u16(&mut ctx, PRESET).await, 1500);
        assert_eq!(read_u16(&mut ctx, ENABLE).await, 0);
        ctx.write_single_register(ENABLE, 1).await.unwrap().unwrap();
        assert_eq!(read_u16(&mut ctx, ENABLE).await, 1);
    });
}

#[test]
fn whole_mapped_range_reads_in_one_request() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        let regs = ctx
            .read_holding_registers(0x0001, 7)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(regs.len(), 7);
    });
}

// ---------------------------------------------------------------------------
// Exception responses (via a real client)
// ---------------------------------------------------------------------------

#[test]
fn read_unmapped_base_address_is_illegal_data_address() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        let result = ctx.read_holding_registers(0x0000, 1).await.unwrap();
        assert_eq!(result, Err(ExceptionCode::IllegalDataAddress));
    });
}

#[test]
fn read_range_spilling_past_map_is_illegal_data_address() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        // 0x0007 is the last mapped register; quantity 2 spills into 0x0008.
        let result = ctx.read_holding_registers(0x0007, 2).await.unwrap();
        assert_eq!(result, Err(ExceptionCode::IllegalDataAddress));
    });
}

#[test]
fn write_to_read_only_register_is_illegal_data_address() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        let mut ctx = connect(addr).await;

        let result = ctx.write_single_register(CUMULATIVE_HI, 5).await.unwrap();
        assert_eq!(result, Err(ExceptionCode::IllegalDataAddress));
    });
}

#[test]
fn non_default_unit_id_is_accepted_and_echoed() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);
        // A successful exchange implies the server echoed the unit id; otherwise
        // the client rejects the response with a header mismatch.
        let mut ctx = tokio_modbus::client::tcp::connect_slave(addr, Slave(0x07))
            .await
            .unwrap();
        let regs = ctx
            .read_holding_registers(PRESET, 1)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(regs.len(), 1);
    });
}

// ---------------------------------------------------------------------------
// Wire-level framing (raw sockets)
// ---------------------------------------------------------------------------

#[test]
fn transaction_and_unit_id_are_echoed_protocol_id_zero() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);

        // Read holding register 0x0001, quantity 1.
        let pdu = [0x03, 0x00, 0x01, 0x00, 0x01];
        let (txid, proto, unit, resp_pdu) = raw_round_trip(addr, [0xBE, 0xEF], 0x07, &pdu).await;

        assert_eq!(txid, [0xBE, 0xEF], "transaction id must be echoed");
        assert_eq!(proto, [0x00, 0x00], "protocol id must be 0x0000");
        assert_eq!(unit, 0x07, "unit id must be echoed");
        assert_eq!(resp_pdu[0], 0x03, "function code echoed");
        assert_eq!(resp_pdu[1], 0x02, "byte count = 2 × quantity");
    });
}

#[test]
fn unsupported_function_code_is_illegal_function() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);

        // Function 0x05 (Write Single Coil) is not implemented.
        let pdu = [0x05, 0x00, 0x00, 0xFF, 0x00];
        let (_, _, _, resp_pdu) = raw_round_trip(addr, [0x00, 0x01], 0xFF, &pdu).await;

        assert_eq!(
            resp_pdu,
            vec![0x85, 0x01],
            "function | 0x80, IllegalFunction"
        );
    });
}

#[test]
fn read_quantity_zero_is_illegal_data_value() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);

        let pdu = [0x03, 0x00, 0x01, 0x00, 0x00]; // quantity 0
        let (_, _, _, resp_pdu) = raw_round_trip(addr, [0x00, 0x02], 0xFF, &pdu).await;

        assert_eq!(
            resp_pdu,
            vec![0x83, 0x03],
            "function | 0x80, IllegalDataValue"
        );
    });
}

#[test]
fn read_quantity_over_125_is_illegal_data_value() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);

        let pdu = [0x03, 0x00, 0x01, 0x00, 0x7E]; // quantity 126
        let (_, _, _, resp_pdu) = raw_round_trip(addr, [0x00, 0x03], 0xFF, &pdu).await;

        assert_eq!(
            resp_pdu,
            vec![0x83, 0x03],
            "function | 0x80, IllegalDataValue"
        );
    });
}

#[test]
fn read_device_identification_is_illegal_function() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async move {
        let addr = free_addr();
        let _harness = spawn_dispenser(&clock, addr);

        // A Read Device Identification (0x2B / 0x0E) probe; the minimal device
        // does not implement it (spec §10), so it returns Illegal Function.
        let pdu = [0x2B, 0x0E, 0x01, 0x00];
        let (_, _, _, resp_pdu) = raw_round_trip(addr, [0x00, 0x04], 0xFF, &pdu).await;

        assert_eq!(
            resp_pdu,
            vec![0xAB, 0x01],
            "function | 0x80, IllegalFunction"
        );
    });
}
