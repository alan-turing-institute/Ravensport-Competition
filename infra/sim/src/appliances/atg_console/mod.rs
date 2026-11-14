//! ATG console appliance (Veeder-Root Tank Level System class).
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Serves the Veeder-Root TLS serial protocol over TCP. The exact wire format
//! is specified in [tls-protocol.md](../tls-protocol.md); this module emits
//! computer-format responses for `i101`, `i201`, and `i205` and latches the
//! bund liquid-sensor alarm.
//!
//! Owns: `AtgConsoleState`.

mod internals;

use std::net::SocketAddr;

use serde::{Deserialize, Serialize};

use tokio::sync::{mpsc, oneshot, watch};

use crate::appliances::atg_console::internals::Config;
use crate::clock::SimClock;
use crate::physical::atg_probe::ProbeReading;
use crate::physical::bund_leak_sensor::BundReading;

/// The console's latched alarm state, saved at a stop (sim-impl.md, "State
/// types").
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AtgConsoleState {
    /// The bund liquid-sensor fuel alarm is latched.
    pub bund_alarm_latched: bool,
}

/// Spawn the ATG console emulator task.
///
/// Binds a TCP listener on `listen_addr` (synchronously, so callers may connect
/// as soon as `spawn` returns) and serves Veeder-Root TLS computer-format
/// responses. On an `i201` poll it requests a `ProbeReading` via
/// `probe_request_tx` and awaits the reply before responding; `i101` and `i205`
/// need no probe. Each connection is served by its own task, so multiple
/// clients (the FMS and the fuel supplier via `atg-relay-01`) are handled
/// concurrently. A wet `BundReading` latches the bund alarm internally
/// (reported in `i101` as `030301`), retained until an `alarm_clear_rx` signal
/// clears it.
///
/// The latch starts from `start`. The console runs for the life of the sim,
/// through a stop, and answers each request on `state_rx` with its latch.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    k_l_per_mm: f64,
    tank_capacity_l: f64,
    start: AtgConsoleState,
    state_rx: mpsc::Receiver<oneshot::Sender<AtgConsoleState>>,
    probe_request_tx: mpsc::Sender<oneshot::Sender<ProbeReading>>,
    bund_rx: watch::Receiver<BundReading>,
    alarm_clear_rx: mpsc::Receiver<()>,
    listen_addr: SocketAddr,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        Config {
            k_l_per_mm,
            tank_capacity_l,
        },
        start,
        state_rx,
        probe_request_tx,
        bund_rx,
        alarm_clear_rx,
        listen_addr,
    )
}
