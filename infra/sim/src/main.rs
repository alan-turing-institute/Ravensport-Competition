//! Thin binary entry point for the sim.
//!
//! Initialises logging, builds the run's configuration ([`coupled::config`])
//! from the simulator's own start state ([`Start::defaults`]; sim-impl.md,
//! "Start state"), reads the checkpoint if one is present, and hands off to
//! [`orchestrator::run`], which wires and supervises every entity, from the
//! checkpoint on a warm start, and writes the checkpoint on a stop. The sim
//! connects to the digital world at the configured boundary addresses; with no
//! digital world present it fails fast (the supervisor logs and exits
//! non-zero).
//!
//! Configuration is fixed at compile time (sim-network.md §5): the boundary
//! endpoints are the loopback ports HAProxy fronts as the entities' zone
//! addresses. The sim binds and originates only on `127.0.0.1`; the proxy owns
//! every network interface.

use std::path::Path;
use std::time::Duration;

use tracing_subscriber::EnvFilter;

use sim::actors::systems_administrator;
use sim::checkpoint;
use sim::clock::SimClock;
use sim::coupled::{self, Start};
use sim::orchestrator;

/// The tug arrival generator's seed.
const TUG_SEED: u64 = 1;

/// The read-only directory the deployment mounts with the checkpoint and the
/// reseed file for a warm start (sim-network.md §7).
const STATE_DIR: &str = "/var/lib/sim/state";
/// The in-memory directory the service creates, to which the checkpoint is
/// written on a stop and in which the loaded marker is created.
const RUN_DIR: &str = "/run/sim";
/// The longest a warm start may follow the checkpoint it starts from (sim.md
/// §9.3). It governs the start, not the state, so it is not a checkpoint
/// parameter.
const MAX_START_GAP: Duration = Duration::from_secs(5 * 60);

fn main() {
    // Sparse logging: WARN and ERROR from dependencies, but INFO from the sim's
    // own crate so operational events it chooses to record (e.g. each ATG reading
    // the fuel supplier receives, an attack-chain observation point) reach the log
    // and the deployment monitor. Overridable with RUST_LOG. Plain text without
    // colour codes, since standard output is the file /var/log/sim.log.
    tracing_subscriber::fmt()
        .with_ansi(false)
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("warn,sim=info")),
        )
        .init();

    // Coupled mode: running against the real digital world, so 1:1 pace and a
    // calendar starting at the current UTC time (sim.md §2.1). A compressed
    // scenario runs in coupled mode too (sim.md §2.2).
    let (clock, runtime) = SimClock::coupled();

    let admin_check_once = std::env::args()
        .skip(1)
        .any(|arg| arg == "--admin-check-once");

    runtime.block_on(async move {
        if admin_check_once {
            let succeeded = systems_administrator::run_once(
                clock,
                coupled::SYSTEMS_ADMINISTRATOR_EGRESS
                    .parse()
                    .expect("a literal socket address"),
                coupled::managed_hosts(),
                Duration::from_secs(30),
            )
            .await;
            if !succeeded {
                std::process::exit(1);
            }
            return;
        }

        let config = coupled::config(Start::defaults(TUG_SEED));
        let checkpoint = checkpoint::read(
            Path::new(STATE_DIR),
            Path::new(RUN_DIR),
            clock.now_datetime(),
            &config.parameters(),
            MAX_START_GAP,
        )
        .unwrap_or_else(|fault| {
            tracing::error!(%fault, "checkpoint refused");
            std::process::exit(1);
        });

        orchestrator::run(clock, config, checkpoint, Path::new(RUN_DIR)).await;
    });
}
