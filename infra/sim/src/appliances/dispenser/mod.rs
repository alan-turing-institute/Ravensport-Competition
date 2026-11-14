//! Dispenser appliance.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Presents a Modbus TCP server (slave) to the FMS. The exact wire protocol,
//! addressing, function codes, register map, and exception behaviour are
//! specified in [dispenser-modbus-tcp-spec.md](../dispenser-modbus-tcp-spec.md).
//!
//! Owns: `DispenserControlState`, `DispenserState`, `spawn()`.

mod internals;

use std::net::SocketAddr;

use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, oneshot, watch};

use crate::clock::SimClock;
use crate::physical::flow_meter::Pulse;

/// Solenoid and pump state published by the dispenser to the physical model.
/// The fuel tank uses these to compute the derived quantity `outflow_active`.
///
/// Defined here ahead of the dispenser's own implementation because it is the
/// dispenser-owned message type the fuel tank consumes on its
/// `dispenser_ctrl_rx` channel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct DispenserControlState {
    /// True when the dispenser solenoid is open.
    pub solenoid_open: bool,

    /// True when the dispenser pump is energised. Transitions in lockstep with
    /// `solenoid_open` in the current model.
    pub pump_energised: bool,
}

/// The dispenser's totalisers, in flow-meter pulses, saved at a stop
/// (sim-impl.md, "State types"). A real dispenser's totalisers survive a power
/// cycle; the cumulative totaliser is never cleared.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DispenserState {
    pub cumulative_pulses: u32,
    pub transaction_pulses: u32,
}

/// Spawn the dispenser emulator task.
///
/// Binds a Modbus TCP listener on `listen_addr` (synchronously, so callers may
/// connect as soon as `spawn` returns) and serves the FMS as a Modbus server.
/// Concurrently accumulates `Pulse` events from the flow meter on `pulse_rx`.
///
/// On each pulse it increments the cumulative and transaction totalisers and,
/// while the pump is enabled, auto-stops when the transaction pulse count
/// reaches `(preset_litres) × k_factor` — clearing the solenoid and pump and
/// publishing the change on `dispenser_ctrl_tx`. A write of the pump-enable
/// register toggles the solenoid and pump and is likewise published; a write of
/// the preset register resets the transaction totaliser. The cumulative
/// totaliser is never cleared.
///
/// The `clock` is used only for its [`io_guard`](SimClock::io_guard): while
/// serving a Modbus request the dispenser holds a guard so simulated time does
/// not advance mid-exchange (the FMS may be a mocked component, which is not
/// permitted to guard). All timing is otherwise driven by incoming pulses and
/// FMS register operations.
///
/// The totalisers start from `start`: on a cold start the cumulative totaliser
/// reads the start state's reading (sim-impl.md, "Start state"), on a warm
/// start the checkpoint's. The dispenser runs for the life of the sim, through
/// a stop, and answers each request on `state_rx` with its totalisers.
pub fn spawn(
    clock: SimClock,
    start: DispenserState,
    state_rx: mpsc::Receiver<oneshot::Sender<DispenserState>>,
    pulse_rx: mpsc::Receiver<Pulse>,
    dispenser_ctrl_tx: watch::Sender<DispenserControlState>,
    listen_addr: SocketAddr,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        start,
        state_rx,
        pulse_rx,
        dispenser_ctrl_tx,
        listen_addr,
    )
}
