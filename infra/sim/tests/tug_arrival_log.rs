//! The tug arrival generator's log (`sim::physical::tug_arrival_generator`):
//! each tug's scheduled arrival and requested volume, logged as they are
//! drawn, from which the episode runner reads the arrivals still to come at a
//! fork (sim.md §6, step 3).
//!
//! The test captures the generator's events with a subscriber of its own. It
//! lives in a test binary of its own, because tracing caches each event's
//! interest across the threads of one process, and a test on another thread
//! that reaches the generator first, with no subscriber, would hide the
//! events from this one.

use std::collections::HashMap;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use sim::actors::tug_engineer::VesselId;
use sim::clock::{SimClock, TimeModel};
use sim::physical::tug_arrival_generator::{self, TugArrival, TugArrivalState, Vessel};
use sim::shared::RfidCardNumber;
use sim::stop::stop_channel;
use time::macros::datetime;
use time::PrimitiveDateTime;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

static ROSTER: [Vessel; 2] = [
    Vessel {
        vessel_id: VesselId(1999997),
        name: "Tug A",
        rfid_card: RfidCardNumber(99999),
        mean_time_away: Duration::from_secs(15 * 60),
    },
    Vessel {
        vessel_id: VesselId(1999985),
        name: "Tug B",
        rfid_card: RfidCardNumber(99998),
        mean_time_away: Duration::from_secs(60 * 60),
    },
];

/// Spawn the generator for the roster from a cold start, with a refuel range
/// of 200 to 800 L and a fixed seed, on a channel with one slot per roster
/// vessel. The generator is never stopped: the stop request and the tug
/// engineer's report are kept for the life of the test process.
fn spawn_generator(clock: SimClock) -> (mpsc::Receiver<TugArrival>, Vec<JoinHandle<()>>) {
    let (tx, rx) = mpsc::channel(ROSTER.len());
    let (stop, stop_rx) = stop_channel();
    let (engineer_at_rest, engineer_at_rest_rx) = oneshot::channel();
    let (report_tx, report_rx) = oneshot::channel();
    let handles = tug_arrival_generator::spawn(
        clock,
        &ROSTER,
        200.0,
        800.0,
        TugArrivalState::new(42, &ROSTER),
        stop_rx,
        engineer_at_rest_rx,
        report_tx,
        tx,
    );
    std::mem::forget((stop, engineer_at_rest, report_rx));
    (rx, handles)
}

/// A writer that keeps every line the test's subscriber formats.
#[derive(Clone, Default)]
struct Captured(std::sync::Arc<std::sync::Mutex<Vec<u8>>>);

impl std::io::Write for Captured {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0.lock().unwrap().extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for Captured {
    type Writer = Captured;

    fn make_writer(&'a self) -> Self::Writer {
        self.clone()
    }
}

/// The generator logs each tug's scheduled arrival and requested volume as it
/// draws them, in the line that the episode runner reads (episodes-tests.md,
/// `each_tug_arrives_at_its_logged_scheduled_arrival`). Over 12 hours, with
/// each tug waiting and refuelling for a while before it leaves, every tug
/// arrives at the scheduled arrival logged last before it, and its job carries
/// the logged volume.
#[test]
fn each_tug_arrives_at_its_logged_scheduled_arrival() {
    use time::format_description::well_known::Rfc3339;
    use time::OffsetDateTime;

    const SPAN: Duration = Duration::from_secs(12 * 60 * 60);
    const AT_THE_DOCK: Duration = Duration::from_secs(10 * 60);

    let captured = Captured::default();
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .with_writer(captured.clone())
        .with_env_filter(tracing_subscriber::EnvFilter::new(format!(
            "{}=info",
            tug_arrival_generator::LOG_TARGET
        )))
        .finish();
    let arrivals = tracing::subscriber::with_default(subscriber, || {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let (mut rx, _handles) = spawn_generator(clock.clone());
            let mut arrivals: Vec<(u32, OffsetDateTime, f64)> = Vec::new();
            loop {
                let arrival = rx.recv().await.expect("the generator closed its channel");
                let at = clock.now_datetime();
                if at - SIM_START > SPAN {
                    break;
                }
                arrivals.push((
                    arrival.job.vessel_id.0,
                    at.assume_utc(),
                    arrival.job.volume_l,
                ));
                // The tug waits and refuels, then leaves; the next arrival is
                // taken meanwhile.
                let clock = clock.clone();
                tokio::spawn(async move {
                    clock.sleep(AT_THE_DOCK).await;
                    let _ = arrival.departed.send(());
                });
            }
            arrivals
        })
    });

    let text = String::from_utf8(captured.0.lock().unwrap().clone()).unwrap();
    let line = regex_lite_scheduled();
    let mut scheduled: HashMap<u32, Vec<(OffsetDateTime, f64)>> = HashMap::new();
    for row in text.lines().filter(|l| l.contains(" scheduled arrival ")) {
        let fields = line(row).unwrap_or_else(|| panic!("an unexpected line: {row}"));
        let at = OffsetDateTime::parse(&fields.1, &Rfc3339).expect("an RFC 3339 instant");
        scheduled.entry(fields.0).or_default().push((at, fields.2));
    }

    assert!(
        arrivals.len() >= 10,
        "too few arrivals to test: {}",
        arrivals.len()
    );
    let mut taken: HashMap<u32, usize> = HashMap::new();
    for (vessel, at, volume_l) in arrivals {
        let k = taken.entry(vessel).or_default();
        let (due, logged_volume_l) = scheduled[&vessel][*k];
        *k += 1;
        assert!(
            (at - due).abs() <= time::Duration::SECOND,
            "vessel {vessel} arrived at {at}, and its logged scheduled arrival is {due}"
        );
        assert_eq!(
            volume_l, logged_volume_l,
            "vessel {vessel}'s job carries another volume"
        );
    }
}

/// The vessel, the scheduled arrival and the volume of a `scheduled arrival`
/// line, in the form that the episode runner parses (competition/episode_runner
/// fork.py): the line's time, `INFO`, the target, the message, then
/// `vessel_id=`, `scheduled_arrival=` and `volume_l=`.
fn regex_lite_scheduled() -> impl Fn(&str) -> Option<(u32, String, f64)> {
    |row: &str| {
        let mut words = row.split_whitespace();
        let _time = words.next()?;
        if words.next()? != "INFO" || words.next()? != "tug_arrivals:" {
            return None;
        }
        if words.next()? != "scheduled" || words.next()? != "arrival" {
            return None;
        }
        let vessel = words.next()?.strip_prefix("vessel_id=")?.parse().ok()?;
        let at = words
            .next()?
            .strip_prefix("scheduled_arrival=")?
            .to_string();
        let volume = words.next()?.strip_prefix("volume_l=")?.parse().ok()?;
        words.next().is_none().then_some((vessel, at, volume))
    }
}
