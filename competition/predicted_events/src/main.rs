//! Each tug's first arrival as the simulator draws it, for the episode runner's
//! plan of fork 0 (`competition/episodes.md` §10.2, choosing the chain).
//!
//! The run command plans fork 0 before anything is deployed, and may try many
//! seeds, so it has no simulator log to read. Every tug starts away from the
//! dock, and its first arrival falls one drawn time away after the simulator's
//! start (`sim.md` §6). The helper prints one JSON object for a seed of the
//! generator `sim_tug_arrivals`: the fuel supplier's delivery volume, from
//! the simulator's own configuration, and each tug's
//! first arrival and requested volume, in the order of the arrivals. Each
//! tug's mean time away and the bounds of a request arrive as arguments: the
//! episode runner takes them from the render's projection of `world.yaml` at
//! the competition's compression factor, so the helper is independent of the
//! constants of the last render on disk. Forks after fork 0 read each tug's
//! scheduled arrival from the simulator's log instead.
//!
//! The tug arrival generator keeps its draws private, so its first two draws
//! are repeated here with the crates the simulator locks. Each tug's stream is
//! `ChaCha8Rng::seed_from_u64(seed)` with its stream set to the tug's IMO
//! number. It draws a unit exponential, which scales the tug's mean time away,
//! and then the requested volume from `Uniform(min, max)` in litres
//! (`infra/sim/src/physical/tug_arrival_generator/internals.rs`).

use rand::distr::{Distribution, Uniform};
use rand::SeedableRng;
use rand_chacha::ChaCha8Rng;
use rand_distr::Exp1;
use sim::coupled::DELIVERY_VOLUME_L;

const USAGE: &str = "usage: predicted-events <sim_tug_arrivals seed> <smallest request L> \
                     <largest request L> <IMO number>:<mean time away s>...";

fn refuse() -> ! {
    eprintln!("{USAGE}");
    std::process::exit(2);
}

/// A tug's first arrival after the simulator's start, in seconds, and its
/// requested volume, from the seed and the tug's IMO number.
fn first_draw(seed: u64, imo: u32, mean_time_away_s: f64, volumes: &Uniform<f64>) -> (f64, f64) {
    let mut rng = ChaCha8Rng::seed_from_u64(seed);
    rng.set_stream(u64::from(imo));
    let unit: f64 = Exp1.sample(&mut rng);
    let volume_l: f64 = volumes.sample(&mut rng);
    (unit * mean_time_away_s, volume_l)
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [seed, low, high, roster @ ..] = args.as_slice() else {
        refuse()
    };
    let (Ok(seed), Ok(low), Ok(high)) =
        (seed.parse::<u64>(), low.parse::<f64>(), high.parse::<f64>())
    else {
        refuse()
    };
    if roster.is_empty() {
        refuse()
    }
    let Ok(volumes) = Uniform::new(low, high) else {
        refuse()
    };
    let mut arrivals = Vec::with_capacity(roster.len());
    for tug in roster {
        let Some((imo, mean)) = tug.split_once(':') else {
            refuse()
        };
        let (Ok(imo), Ok(mean)) = (imo.parse::<u32>(), mean.parse::<f64>()) else {
            refuse()
        };
        if !(mean.is_finite() && mean > 0.0) {
            refuse()
        }
        let (at_s, volume_l) = first_draw(seed, imo, mean, &volumes);
        arrivals.push((at_s, imo, volume_l));
    }
    arrivals.sort_by(|a, b| a.0.total_cmp(&b.0));
    let printed: Vec<String> = arrivals
        .iter()
        .map(|(at_s, imo, volume_l)| {
            format!("{{\"vessel\": {imo}, \"at_s\": {at_s:?}, \"volume_l\": {volume_l:?}}}")
        })
        .collect();
    println!(
        "{{\"delivery_volume_l\": {:?}, \"tug_arrivals\": [{}]}}",
        DELIVERY_VOLUME_L,
        printed.join(", "),
    );
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use sim::actors::tug_engineer::VesselId;
    use sim::clock::{SimClock, TimeModel};
    use sim::physical::tug_arrival_generator::{self, TugArrivalState, Vessel};
    use sim::shared::RfidCardNumber;
    use sim::stop::stop_channel;
    use time::macros::datetime;

    use super::*;

    static ROSTER: [Vessel; 3] = [
        Vessel {
            vessel_id: VesselId(1999997),
            name: "Tug Alpha",
            rfid_card: RfidCardNumber(1000001),
            mean_time_away: Duration::from_secs(14_400),
        },
        Vessel {
            vessel_id: VesselId(1999985),
            name: "Tug Bravo",
            rfid_card: RfidCardNumber(1000003),
            mean_time_away: Duration::from_secs(14_400),
        },
        Vessel {
            vessel_id: VesselId(1999973),
            name: "Tug Charlie",
            rfid_card: RfidCardNumber(1000004),
            mean_time_away: Duration::from_secs(14_400),
        },
    ];

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

    /// The helper's first draw of each tug is the one that the simulator logs
    /// in its scheduled-arrival event for the same seed: the same time away
    /// after the start, to within a microsecond, and the same volume. Fork 0's
    /// admission, and the search for a seed that admits a named chain, rely on
    /// the two agreeing.
    #[test]
    fn the_first_draws_are_the_simulators_logged_scheduled_arrivals() {
        const SEED: u64 = 4_651_467_801_475_421_837;
        let volumes = Uniform::new(500.0, 3000.0).unwrap();
        let start = datetime!(2025-01-01 0:00);
        let captured = Captured::default();
        let subscriber = tracing_subscriber::fmt()
            .with_ansi(false)
            .with_writer(captured.clone())
            .with_env_filter(tracing_subscriber::EnvFilter::new(format!(
                "{}=info",
                tug_arrival_generator::LOG_TARGET
            )))
            .finish();
        tracing::subscriber::with_default(subscriber, || {
            let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, start);
            runtime.block_on(async {
                let (tx, _rx) = tokio::sync::mpsc::channel(ROSTER.len());
                // A cold start, never stopped.
                let (_stop, stop_rx) = stop_channel();
                let (_engineer_at_rest, engineer_at_rest_rx) = tokio::sync::oneshot::channel();
                let (report_tx, _report_rx) = tokio::sync::oneshot::channel();
                let _tasks = tug_arrival_generator::spawn(
                    clock.clone(),
                    &ROSTER,
                    500.0,
                    3000.0,
                    TugArrivalState::new(SEED, &ROSTER),
                    stop_rx,
                    engineer_at_rest_rx,
                    report_tx,
                    tx,
                );
                // Each task draws and logs its first arrival as it starts.
                tokio::task::yield_now().await;
            });
        });
        let text = String::from_utf8(captured.0.lock().unwrap().clone()).unwrap();
        let mut logged = 0;
        for line in text.lines().filter(|l| l.contains(" scheduled arrival ")) {
            let field = |name: &str| {
                let prefix = format!("{name}=");
                line.split_whitespace()
                    .find_map(|w| w.strip_prefix(prefix.as_str()).map(str::to_string))
                    .unwrap_or_else(|| panic!("no {name} in {line}"))
            };
            let imo: u32 = field("vessel_id").parse().unwrap();
            let due = time::OffsetDateTime::parse(
                &field("scheduled_arrival"),
                &time::format_description::well_known::Rfc3339,
            )
            .unwrap();
            let volume_l: f64 = field("volume_l").parse().unwrap();
            let after = (due - start.assume_utc()).as_seconds_f64();
            let (at_s, drawn_l) = first_draw(SEED, imo, 14_400.0, &volumes);
            assert!(
                (at_s - after).abs() < 1e-6,
                "vessel {imo}: {at_s} and {after}"
            );
            assert_eq!(drawn_l, volume_l, "vessel {imo}");
            logged += 1;
        }
        assert_eq!(
            logged,
            ROSTER.len(),
            "the log holds {logged} first draws:\n{text}"
        );
    }
}
