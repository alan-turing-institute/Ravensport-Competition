//! Fuel supplier actor.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `FillJob`, `SupplierStart`, `FuelSupplierState`.

mod internals;

use std::net::SocketAddr;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot};

use crate::clock::SimClock;
use crate::stop::StopRequest;

/// A tank fill job, sent by the fuel supplier to the road tanker driver when it
/// dispatches a delivery.
///
/// The driver forwards only the volume on to the dock operator (as
/// `DockJob::Fill`); the completion oneshot stays with the driver.
#[derive(Debug)]
pub struct FillJob {
    /// Volume of fuel the arriving tanker carries, per narrative §2.1.
    pub delivery_volume_l: f64,
    /// Fired by the road tanker driver once the delivery has ended, however it
    /// ended: the full volume delivered, the delivery stopped part-way by the
    /// interlock circuit, or the pump never started. It ends the supplier's
    /// pending order. The supplier keeps polling meanwhile but places no new
    /// order until this fires (sim.md §5.3).
    pub completed: oneshot::Sender<()>,
}

/// The target of the supplier's `INFO` events: each poll, with the volume and
/// alarm state it read and whether it placed an order; each order, with the
/// volume that the ordering poll read and the tanker's scheduled arrival, one
/// lead time after the poll, or, for an order pending at the start, its
/// tanker's scheduled arrival alone; each tanker's arrival; and the end of
/// each delivery. The simulator's own filter of `WARN` holds
/// them back; the competition's entry point admits them for the episode runner
/// (`competition/sim`).
pub const LOG_TARGET: &str = "fuel_supplier";

/// How the fuel supplier starts a run, set by the start state (sim-impl.md,
/// "Start state"). On a cold start the orchestrator turns it into the
/// supplier's starting state with [`FuelSupplierState::cold`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SupplierStart {
    /// No order placed: the first poll comes this long after the start.
    FirstPollAfter(Duration),
    /// An order placed and pending: the road tanker arrives this long after
    /// the start, the remaining lead time.
    OrderPending(Duration),
}

/// The fuel supplier's state at a stop (sim-impl.md, "State types"): its next
/// poll and its order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct FuelSupplierState {
    /// The next poll of the tank is due at this time. Polls fall due one
    /// `poll_interval` apart, whether or not an order is pending.
    #[serde(with = "crate::checkpoint::utc")]
    pub next_poll_at: PrimitiveDateTime,
    pub order: SupplierOrder,
}

/// The supplier's order at a stop.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SupplierOrder {
    /// No order is pending.
    None,
    /// An order is pending and the road tanker driver has not taken it up:
    /// its tanker is due, or arrived, at this time.
    TankerDueAt(#[serde(with = "crate::checkpoint::utc")] PrimitiveDateTime),
}

impl FuelSupplierState {
    /// The cold-start state: no order, and the first poll one `poll_interval`
    /// after `now`.
    pub fn new(now: PrimitiveDateTime, poll_interval: Duration) -> Self {
        Self {
            next_poll_at: now + poll_interval,
            order: SupplierOrder::None,
        }
    }

    /// The cold-start state that the start state `start` gives at `now`
    /// (sim-impl.md, "Start state"). With no order placed, the first poll comes
    /// the time `start` gives after `now`. With an order pending, the tanker is
    /// due the remaining lead time after `now`, and the first poll comes one
    /// `poll_interval` after `now`.
    pub fn cold(now: PrimitiveDateTime, poll_interval: Duration, start: SupplierStart) -> Self {
        match start {
            SupplierStart::FirstPollAfter(after) => Self {
                next_poll_at: now + after,
                order: SupplierOrder::None,
            },
            SupplierStart::OrderPending(remaining) => Self {
                next_poll_at: now + poll_interval,
                order: SupplierOrder::TankerDueAt(now + remaining),
            },
        }
    }
}

/// Spawn the fuel supplier actor.
///
/// Runs under a vendor-managed-inventory arrangement, beginning from `start`,
/// polling at every `poll_interval` whether or not an order is pending (sim.md
/// §5.3). It waits concurrently on the next poll, the end of a pending order's
/// lead time, and the `completed` signal of a dispatched `FillJob`:
///
/// - On each poll: opens a TCP connection to `atg_relay_addr` (source-bound to
///   `local_addr`) and reads the tank volume (`i201`) and alarm state (`i101`)
///   using the Veeder-Root TLS protocol (tls-protocol.md). If no order is
///   pending, the volume is at or below `reorder_threshold_l` and no critical
///   alarm is active, it places an order: the order becomes pending and its
///   lead time starts. A poll while an order is pending places no order.
/// - When the lead time ends: sends a `FillJob`, carrying a fresh `completed`
///   oneshot, to the road tanker driver.
/// - When `completed` fires: the order is no longer pending. The driver fires
///   it on every outcome of the delivery, so an order cannot remain pending
///   indefinitely. A dropped `completed` sender is fatal.
///
/// Polls keep their interval through the lead time and the delivery: the
/// order does not move the poll schedule. Saved due times are kept on a warm
/// start; one already passed is acted on at once.
///
/// On a stop it begins no further poll or dispatch. A delivery it is awaiting
/// is waited for until it ends, when no order is pending, or until the driver
/// reports without taking it up (`driver_at_rest`), when the tanker is due at
/// the time the job was sent. It then reports its next poll and its order on
/// `report` and holds its channel ends until the process exits.
///
/// The actor begins as `start` sets. On a cold start the start state gives it
/// ([`FuelSupplierState::cold`]): with no order placed, the first poll comes
/// the time the start state gives after the start, one poll interval in the
/// simulator's start state (sim-impl.md, "Start state"); with an order
/// pending, the tanker is due the remaining lead time after the start, and the
/// first poll comes one poll interval after the start. An order pending at the
/// start is logged with its tanker's scheduled arrival.
///
/// Transient connection failures are retried with backoff (sim-impl.md); a
/// malformed response from the ATG console is fatal.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    start: FuelSupplierState,
    stop: StopRequest,
    driver_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<FuelSupplierState>,
    local_addr: SocketAddr,
    atg_relay_addr: SocketAddr,
    poll_interval: Duration,
    reorder_threshold_l: f64,
    delivery_volume_l: f64,
    lead_time: Duration,
    tanker_arrival_tx: mpsc::Sender<FillJob>,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        start,
        stop,
        driver_at_rest,
        report,
        local_addr,
        atg_relay_addr,
        poll_interval,
        reorder_threshold_l,
        delivery_volume_l,
        lead_time,
        tanker_arrival_tx,
    )
}
