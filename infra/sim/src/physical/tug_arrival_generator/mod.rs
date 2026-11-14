//! Tug arrival generator.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `Vessel`, `DispenseJob`, `TugArrival`, `TugArrivalState`.

mod internals;

use std::time::Duration;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::actors::tug_engineer::VesselId;
use crate::clock::SimClock;
use crate::shared::RfidCardNumber;
use crate::stop::StopRequest;

/// A tug on the roster, compiled in from the numbered vessel keys of
/// `sim-constants.toml` (`deployment-config.md` §4.1).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Vessel {
    /// IMO number of the vessel. Also selects the vessel's random stream.
    pub vessel_id: VesselId,
    /// Name of the vessel, written on the BDN alongside its IMO number.
    pub name: &'static str,
    /// RFID card number for the vessel.
    pub rfid_card: RfidCardNumber,
    /// Mean time between the tug leaving the dock and its next arrival
    /// (`roster.vessels[].mean_time_away_s`).
    pub mean_time_away: Duration,
}

/// A tug refuelling job.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct DispenseJob {
    /// The instant the tug arrived. A tug saved at the dock is sent to the tug
    /// engineer again on a warm start, in the order of these times (sim.md
    /// §9.1).
    #[serde(with = "crate::checkpoint::utc")]
    pub arrived_at: PrimitiveDateTime,
    /// IMO number of the arriving vessel.
    pub vessel_id: VesselId,
    /// Name of the vessel, written on the BDN alongside its IMO number.
    #[serde(deserialize_with = "crate::checkpoint::static_str::deserialize")]
    pub vessel_name: crate::checkpoint::StaticStr,
    /// Volume of fuel requested, drawn uniformly between the refuel volume
    /// bounds.
    pub volume_l: f64,
    /// RFID card number for the vessel, carried on the job so the tug engineer
    /// can present the correct card at the dispenser reader.
    pub rfid_card: RfidCardNumber,
}

/// A tug's arrival at the dock: its job, and the signal by which the tug
/// engineer reports that the tug has left. The tug engineer forwards the job
/// to the dock operator and keeps the signal.
#[derive(Debug)]
pub struct TugArrival {
    pub job: DispenseJob,
    /// Fired by the tug engineer once the tug has left the dock, after it has
    /// handed back the co-signed BDN. The tug's next time away starts on
    /// receipt.
    pub departed: oneshot::Sender<()>,
}

/// The target of the generator's `INFO` events: each tug's scheduled arrival
/// with its requested volume, logged as the two are drawn, and each arrival.
/// The simulator's own filter of `WARN` holds them back; the competition's
/// entry point admits them for the episode runner (`competition/sim`).
pub const LOG_TARGET: &str = "tug_arrivals";

/// The generator's state at a stop (sim-impl.md, "State types"): the seed
/// every tug's stream derives from, and each roster vessel's state, in roster
/// order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TugArrivalState {
    pub seed: u64,
    pub tugs: Vec<TugState>,
}

/// One tug's state at a stop.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TugState {
    pub vessel_id: VesselId,
    /// The tug's random generator's position in its stream just after the
    /// draw of its current time away and before the draw of its requested
    /// volume (sim.md §9.3). A warm start draws the volume again from it.
    #[serde(with = "crate::checkpoint::decimal")]
    pub word_pos: u128,
    pub phase: TugPhase,
}

/// Where a tug is in its cycle (sim.md §6) at a stop.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum TugPhase {
    /// Away from the dock. `next_arrival_at` is the due time of its next
    /// arrival, once drawn; `None` on a cold start, when the first time away
    /// is drawn at spawn.
    Away {
        #[serde(with = "crate::checkpoint::utc::option")]
        next_arrival_at: Option<PrimitiveDateTime>,
    },
    /// At the dock, arrived and not taken up by the tug engineer. On a warm
    /// start the arrival is sent to the tug engineer again before any other.
    AtDock { job: DispenseJob },
}

impl TugArrivalState {
    /// The cold-start state: every tug away, its stream from the beginning.
    pub fn new(seed: u64, vessels: &[Vessel]) -> Self {
        Self {
            seed,
            tugs: vessels
                .iter()
                .map(|vessel| TugState {
                    vessel_id: vessel.vessel_id,
                    word_pos: 0,
                    phase: TugPhase::Away {
                        next_arrival_at: None,
                    },
                })
                .collect(),
        }
    }
}

/// Spawn the tug arrival generator: one task per roster vessel, and one that
/// collects their reports at a stop, whose handles are returned for the
/// orchestrator to supervise.
///
/// Each tug's task begins from its state in `start`. On each cycle it draws the
/// tug's time away, an Exp(1) sample multiplied by the vessel's
/// `mean_time_away`, and the requested volume from
/// Uniform(`refuel_volume_min_l`, `refuel_volume_max_l`); logs one `INFO`
/// event, `scheduled arrival`, with the vessel's IMO number, its scheduled
/// arrival, one time away after the draw, and the requested volume; sleeps
/// until the arrival is due on the sim clock; logs the `arrival`; sends a
/// `TugArrival` to the tug engineer; and awaits its `departed` signal before
/// drawing the next time away. A tug therefore has at most one arrival
/// outstanding, and a tug that is waiting or refuelling does not arrive again
/// (`sim.md` §6). The tugs saved at the dock are sent to the tug engineer at
/// spawn, before any other arrival, in the order in which they arrived.
///
/// Warm start. Each tug's saved position lies between the draws of its time
/// away and its requested volume, and the tug draws the volume again from it
/// at spawn (sim.md §9.3). The draw repeats the uninterrupted run's volume and
/// leaves the stream where that run left it, so a restored run repeats the
/// uninterrupted run's draws. A tug away keeps its saved due time and arrives
/// with that volume, logged with it at the start; a tug at the dock has its
/// job, which carries the volume.
///
/// Random streams. Each task's generator is a `ChaCha8Rng` seeded from
/// `start.seed`, with its stream set to the vessel's IMO number and its
/// position to the saved word position. Each tug's sequence of times away and
/// volumes is therefore reproducible from the seed and independent of the
/// other tugs, so adding or removing a vessel leaves the others' sequences
/// unchanged. Drawing a unit exponential and scaling it keeps the sequence of
/// draws independent of the mean.
///
/// On a stop no tug arrives again. A tug away reports its next due time at
/// once. A tug at the dock waits for its `departed` signal, after which it
/// draws its time away and requested volume and reports that due time, or for
/// the tug engineer's report (`engineer_at_rest`), which before `departed`
/// means the engineer did not take it up, when it reports itself at the dock.
/// Each tug reports the position between the draws of its current time away
/// and its volume. Once every tug has reported, the generator reports on
/// `report` and holds its channel ends until the process exits (sim-impl.md,
/// "Coming to rest").
///
/// Start-up faults, refused at spawn: an empty roster; a vessel whose
/// `mean_time_away` is zero; refuel volume bounds that are not positive and
/// finite, or whose minimum is not below its maximum; a starting state whose
/// tugs are not the roster's vessels in roster order. A dropped `departed`
/// sender is fatal: the task can no longer tell when its tug left.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    // The roster, one task per vessel. Projected from `world.yaml`
    // (`roster.vessels`).
    vessels: &'static [Vessel],
    // Bounds of each tug's requested volume in litres, the same for every tug
    // (`roster.refuel_volume_min_l`, `roster.refuel_volume_max_l`; narrative
    // §2.2, "~500–3,000 L", by default).
    refuel_volume_min_l: f64,
    refuel_volume_max_l: f64,
    start: TugArrivalState,
    stop: StopRequest,
    engineer_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<TugArrivalState>,
    // Channel to the tug engineer task. Its capacity is the roster size
    // (orchestrator), so a send never blocks.
    tug_arrival_tx: mpsc::Sender<TugArrival>,
) -> Vec<JoinHandle<()>> {
    internals::spawn(
        clock,
        vessels,
        refuel_volume_min_l,
        refuel_volume_max_l,
        start,
        stop,
        engineer_at_rest,
        report,
        tug_arrival_tx,
    )
}
