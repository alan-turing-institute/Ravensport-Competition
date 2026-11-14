//! The competition's entry point for `sim-host` (episodes.md §3.4, competition
//! image; episode-runner-impl.md §1, competition images).
//!
//! The competition image runs this binary in place of the simulator's own. It
//! reads the start-up input once, before any entity of the simulator starts,
//! passes the value of each field to the simulator as a parameter, and logs one
//! `INFO` event, `start_state`, whose time the episode runner takes as the
//! simulator's start (episodes.md §4.3, start-up input). Every parameter that
//! the start-up input leaves out takes the value that the simulator's own
//! binary gives it, through the simulator's [`coupled::config`].

mod input;

use std::path::Path;
use std::time::Duration;

use tracing_subscriber::EnvFilter;

use sim::actors::fuel_supplier::SupplierStart;
use sim::actors::reconciliation_user::AuditPeriod;
use sim::actors::systems_administrator;
use sim::clock::SimClock;
use sim::coupled::{self, Start};
use sim::orchestrator;

/// The in-memory directory the service creates, to which the simulator writes
/// its checkpoint on a stop (sim-network.md §7).
const RUN_DIR: &str = "/run/sim";

/// The entry point's log filter: the simulator's `WARN`, and the `INFO` events
/// that the episode runner reads.
const LOG_FILTER: &str = "warn,start_state=info,tug_arrivals=info,fuel_supplier=info";

fn main() {
    // The simulator's own filter, WARN and ERROR, admits at INFO too the
    // start_state event, the tug arrival generator's scheduled arrivals and
    // arrivals, and the fuel supplier's polls and deliveries, from which the
    // episode runner reads the start state at a fork (sim-impl.md, "Logging";
    // episode-runner-impl.md §2, start-up input).
    tracing_subscriber::fmt()
        .with_ansi(false)
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new(LOG_FILTER)),
        )
        .init();

    let (clock, runtime) = SimClock::coupled();
    let args: Vec<String> = std::env::args().skip(1).collect();

    // The systems administrator's one-off check, as the simulator's own binary
    // offers it.
    if args.iter().any(|arg| arg == "--admin-check-once") {
        let succeeded = runtime.block_on(systems_administrator::run_once(
            clock,
            coupled::SYSTEMS_ADMINISTRATOR_EGRESS
                .parse()
                .expect("a literal socket address"),
            coupled::managed_hosts(),
            Duration::from_secs(30),
        ));
        std::process::exit(if succeeded { 0 } else { 1 });
    }

    let path = args.first().map_or(input::PATH, String::as_str);
    let now = runtime.block_on(async { clock.now_datetime() });
    let start = read_start(path, now).unwrap_or_else(|error| {
        tracing::error!(%error, "sim cannot start");
        std::process::exit(1);
    });
    log(&start);
    runtime.block_on(async move {
        // The entry point starts only from the start-up input, so it passes no
        // checkpoint: every run is a cold start.
        orchestrator::run(clock, coupled::config(start), None, Path::new(RUN_DIR)).await;
    });
}

/// The start state that the start-up input at `path` gives, at the simulator's
/// start `now`.
fn read_start(path: &str, now: time::PrimitiveDateTime) -> Result<Start, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|error| format!("the start-up input {path} cannot be read: {error}"))?;
    let read = input::parse(&text, sim::constants::TANK_CAPACITY_L).map_err(|e| e.to_string())?;
    let period_in_hours = matches!(coupled::audit_period(), AuditPeriod::Interval(_));
    read.start(now, period_in_hours).map_err(|e| e.to_string())
}

/// The one INFO event `start_state`, with the start state.
fn log(start: &Start) {
    let (delivery_state, remaining_lead_time_s, first_poll_after_s) = match start.supplier_start {
        SupplierStart::OrderPending(remaining) => {
            ("order placed", Some(remaining.as_secs_f64()), None)
        }
        SupplierStart::FirstPollAfter(after) => {
            ("no order placed", None, Some(after.as_secs_f64()))
        }
    };
    tracing::info!(
        target: "start_state",
        sim_tug_arrivals = start.tug_seed,
        delivery_state,
        remaining_lead_time_s = ?remaining_lead_time_s,
        initial_volume_l = start.initial_volume_l,
        first_poll_after_s = ?first_poll_after_s,
        first_check_after_s = start.first_check_after.as_secs_f64(),
        first_audit_after_s = ?start.first_audit_after.map(|after| after.as_secs_f64()),
        initial_cumulative_pulses = start.initial_cumulative_pulses,
        initial_meter_totaliser_l = start.initial_meter_totaliser_l,
        first_bdn_number = start.first_bdn_number,
        first_ticket_number = start.first_ticket_number,
        "start_state"
    );
}

#[cfg(test)]
mod tests {
    use super::LOG_FILTER;
    use sim::actors::fuel_supplier;
    use sim::physical::tug_arrival_generator;

    #[test]
    fn the_filter_admits_the_events_the_episode_runner_reads() {
        for target in [
            "start_state",
            tug_arrival_generator::LOG_TARGET,
            fuel_supplier::LOG_TARGET,
        ] {
            assert!(
                LOG_FILTER.split(',').any(|d| d == format!("{target}=info")),
                "the filter holds back {target}"
            );
        }
    }
}
