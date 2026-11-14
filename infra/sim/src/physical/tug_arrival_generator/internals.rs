//! Private implementation of the tug arrival generator. Not visible to other
//! modules.
//!
//! One task per roster vessel follows its tug through the cycle of sim.md §6:
//! away from the dock, arrive, wait for the tug engineer's signal that the tug
//! has left, and away again. A further task collects the tugs' states at a
//! stop.

use rand::distr::{Distribution, Uniform};
use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use rand_distr::Exp1;
use time::format_description::well_known::Rfc3339;
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;

use super::{DispenseJob, TugArrival, TugArrivalState, TugPhase, TugState, Vessel, LOG_TARGET};
use crate::clock::SimClock;
use crate::stop::{after_peer, rest, sleep_until, StopRequest};

const ENTITY: &str = "tug_arrival_generator";

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    vessels: &'static [Vessel],
    refuel_volume_min_l: f64,
    refuel_volume_max_l: f64,
    start: TugArrivalState,
    stop: StopRequest,
    engineer_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<TugArrivalState>,
    tug_arrival_tx: mpsc::Sender<TugArrival>,
) -> Vec<JoinHandle<()>> {
    // Checked at construction so a bad roster, range or starting state fails
    // fast.
    assert!(
        !vessels.is_empty(),
        "tug_arrival_generator: the roster is empty; it needs at least one vessel"
    );
    for vessel in vessels {
        assert!(
            !vessel.mean_time_away.is_zero(),
            "tug_arrival_generator: vessel {} has a mean_time_away of zero; it would \
             arrive again the instant it left",
            vessel.vessel_id.0
        );
    }
    let is_volume = |v: f64| v.is_finite() && v > 0.0;
    assert!(
        is_volume(refuel_volume_min_l)
            && is_volume(refuel_volume_max_l)
            && refuel_volume_min_l < refuel_volume_max_l,
        "tug_arrival_generator: refuel_volume_min_l {refuel_volume_min_l} and \
         refuel_volume_max_l {refuel_volume_max_l} must be positive and finite, \
         with the minimum below the maximum"
    );
    let saved: Vec<_> = start.tugs.iter().map(|tug| tug.vessel_id).collect();
    let roster: Vec<_> = vessels.iter().map(|vessel| vessel.vessel_id).collect();
    assert!(
        saved == roster,
        "tug_arrival_generator: the starting state's tugs {saved:?} are not the roster's \
         vessels {roster:?}"
    );
    let volume_dist = Uniform::new(refuel_volume_min_l, refuel_volume_max_l)
        .expect("tug_arrival_generator: the refuel_volume range was checked above");

    // The tugs saved at the dock arrive again first, in the order in which
    // they arrived; the channel holds one arrival per tug, so none blocks.
    let mut at_dock: Vec<(usize, DispenseJob)> = start
        .tugs
        .iter()
        .enumerate()
        .filter_map(|(i, tug)| match tug.phase {
            TugPhase::AtDock { job } => Some((i, job)),
            TugPhase::Away { .. } => None,
        })
        .collect();
    at_dock.sort_by_key(|(_, job)| job.arrived_at);
    let mut departures: Vec<Option<oneshot::Receiver<()>>> =
        start.tugs.iter().map(|_| None).collect();
    for (i, job) in at_dock {
        let (departed, departed_rx) = oneshot::channel();
        tug_arrival_tx
            .try_send(TugArrival { job, departed })
            .unwrap_or_else(|error| {
                panic!("tug_arrival_generator: cannot resend a saved arrival: {error}")
            });
        departures[i] = Some(departed_rx);
    }

    // The tug engineer's report, passed to every tug.
    let (engineer_rested_tx, engineer_rested_rx) = watch::channel(false);
    let mut reports = Vec::with_capacity(vessels.len());
    let mut handles = Vec::with_capacity(vessels.len() + 1);
    for ((vessel, tug), departure) in vessels.iter().zip(start.tugs).zip(departures) {
        let (tug_report, tug_report_rx) = oneshot::channel();
        reports.push(tug_report_rx);
        handles.push(tokio::spawn(run_tug(
            clock.clone(),
            *vessel,
            volume_dist,
            start.seed,
            tug,
            departure,
            stop.clone(),
            engineer_rested_rx.clone(),
            tug_report,
            tug_arrival_tx.clone(),
        )));
    }
    handles.push(tokio::spawn(collect(
        start.seed,
        engineer_at_rest,
        engineer_rested_tx,
        reports,
        report,
    )));
    handles
}

/// Pass the tug engineer's report to every tug, then report every tug's state
/// in roster order once all have reported.
async fn collect(
    seed: u64,
    engineer_at_rest: oneshot::Receiver<()>,
    engineer_rested: watch::Sender<bool>,
    reports: Vec<oneshot::Receiver<TugState>>,
    report: oneshot::Sender<TugArrivalState>,
) {
    let tugs = async {
        let mut tugs = Vec::with_capacity(reports.len());
        for tug in reports {
            tugs.push(
                tug.await
                    .unwrap_or_else(|_| panic!("{ENTITY}: a tug task dropped its report")),
            );
        }
        tugs
    };
    let forward = async {
        after_peer(ENTITY, "tug engineer", engineer_at_rest).await;
        engineer_rested.send_replace(true);
        // Held so that the tugs' receivers stay open.
        std::future::pending::<()>().await
    };
    let tugs = tokio::select! {
        tugs = tugs => tugs,
        _ = forward => unreachable!(),
    };
    rest(ENTITY, report, TugArrivalState { seed, tugs }).await
}

/// One tug's cycle, for the duration of the sim.
#[allow(clippy::too_many_arguments)]
async fn run_tug(
    clock: SimClock,
    vessel: Vessel,
    volume_dist: Uniform<f64>,
    seed: u64,
    start: TugState,
    departure: Option<oneshot::Receiver<()>>,
    mut stop: StopRequest,
    mut engineer_rested: watch::Receiver<bool>,
    report: oneshot::Sender<TugState>,
    tug_arrival_tx: mpsc::Sender<TugArrival>,
) {
    // The tug's own stream: the shared seed, with the stream set to its IMO
    // number, so its draws do not depend on the other tugs.
    let mut rng = ChaCha8Rng::seed_from_u64(seed);
    rng.set_stream(u64::from(vessel.vessel_id.0));
    rng.set_word_pos(start.word_pos);
    // The position just after the draw of the tug's current time away and
    // before the draw of its requested volume, which a stop saves (sim.md
    // §9.3). On a warm start it is the saved position.
    let mut between_draws = start.word_pos;
    let state = |word_pos, phase| TugState {
        vessel_id: vessel.vessel_id,
        word_pos,
        phase,
    };

    // A warm start draws the tug's requested volume again from the saved
    // position, which repeats the uninterrupted run's draw and leaves the
    // stream where that run left it. A tug away keeps its saved due time and
    // arrives with that volume. A tug at the dock has its job, which carries
    // the volume, and the draw only moves its stream past it. A cold start
    // draws the first time away below.
    let (mut pending, mut at_dock) = match start.phase {
        TugPhase::Away {
            next_arrival_at: None,
        } => (None, None),
        TugPhase::Away {
            next_arrival_at: Some(due),
        } => (Some((due, volume_dist.sample(&mut rng))), None),
        TugPhase::AtDock { job } => {
            let _repeated: f64 = volume_dist.sample(&mut rng);
            (
                None,
                Some((
                    job,
                    departure.expect("tug_arrival_generator: a tug at the dock has no departure"),
                )),
            )
        }
    };

    loop {
        if let Some((job, mut departed_rx)) = at_dock.take() {
            // The tug is at the dock, waiting or refuelling, until it leaves.
            // After a stop, the tug engineer's report before the departure
            // means it did not take the tug up.
            tokio::select! {
                biased;
                left = &mut departed_rx => left.unwrap_or_else(|_| {
                    panic!(
                        "tug_arrival_generator: the tug engineer dropped the departure signal \
                         of vessel {}",
                        vessel.vessel_id.0
                    )
                }),
                () = engineer_reported(&mut engineer_rested) => {
                    rest(ENTITY, report, state(between_draws, TugPhase::AtDock { job })).await
                }
            }
        }

        // The next arrival is drawn as the tug leaves: a unit exponential
        // scaled by the tug's mean, so the sequence of draws does not depend on
        // the mean, then the requested volume. The stream draws the two in this
        // order on every cycle (sim.md §6). A warm start's arrival was drawn
        // above.
        let (due, volume_l) = pending.take().unwrap_or_else(|| {
            let unit: f64 = Exp1.sample(&mut rng);
            let due = clock.now_datetime() + vessel.mean_time_away.mul_f64(unit);
            between_draws = rng.get_word_pos();
            (due, volume_dist.sample(&mut rng))
        });

        // The tug's scheduled arrival, one time away after the draw, logged
        // with its requested volume as the two are drawn (sim.md §6, step 3).
        let scheduled_arrival = rfc3339(due);
        tracing::info!(
            target: LOG_TARGET,
            vessel_id = vessel.vessel_id.0,
            scheduled_arrival = %scheduled_arrival,
            volume_l,
            "scheduled arrival"
        );
        tokio::select! {
            biased;
            _ = stop.requested() => {
                let phase = TugPhase::Away { next_arrival_at: Some(due) };
                rest(ENTITY, report, state(between_draws, phase)).await
            }
            _ = sleep_until(&clock, due) => {}
        }
        tracing::info!(
            target: LOG_TARGET,
            vessel_id = vessel.vessel_id.0,
            scheduled_arrival = %scheduled_arrival,
            volume_l,
            "arrival"
        );

        let job = DispenseJob {
            arrived_at: due,
            vessel_id: vessel.vessel_id,
            vessel_name: vessel.name,
            volume_l,
            rfid_card: vessel.rfid_card,
        };
        let (departed, departed_rx) = oneshot::channel();
        tug_arrival_tx
            .send(TugArrival { job, departed })
            .await
            .expect("tug_arrival_generator: tug engineer arrival channel closed");
        at_dock = Some((job, departed_rx));
    }
}

/// Resolves once the tug engineer has reported, as the collector passes on.
async fn engineer_reported(engineer_rested: &mut watch::Receiver<bool>) {
    if engineer_rested.wait_for(|rested| *rested).await.is_err() {
        panic!("{ENTITY}: the collector dropped the tug engineer report signal");
    }
}

/// An instant of the simulator's calendar, which is in UTC, in RFC 3339.
fn rfc3339(at: time::PrimitiveDateTime) -> String {
    at.assume_utc()
        .format(&Rfc3339)
        .expect("a date in the simulator's calendar formats as RFC 3339")
}
