//! Systems administrator actor.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private SSH implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `SystemsAdministratorState`.

mod internals;

use std::net::SocketAddr;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;
use tokio::sync::oneshot;

use crate::clock::SimClock;
use crate::stop::StopRequest;

/// The systems administrator's schedule, which is also its state at a stop
/// (sim-impl.md, "State types").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct SystemsAdministratorState {
    /// The due time of the next session: the end of the previous session plus
    /// `check_interval`, or, on a cold start, the start plus the start state's
    /// first check after the start (sim-impl.md, "Start state").
    #[serde(with = "crate::checkpoint::utc")]
    pub next_check_at: PrimitiveDateTime,
    /// The start time of the previous session, from which the next session
    /// reads the journal. `None` before the first session after a cold start,
    /// which reads the journal since boot.
    #[serde(with = "crate::checkpoint::utc::option")]
    pub previous_check_at: Option<PrimitiveDateTime>,
}

/// Spawn the systems administrator actor.
///
/// Sleeps until `start.next_check_at`, then signs in to `admin-01` through the
/// systems-administrator egress, reaches each managed host through the
/// configured SSH bastion, and runs the read-only routine check. On a cold
/// start the first session is due the start state's first check after the
/// start (sim-impl.md, "Start state"); on a warm start, at the checkpoint's due
/// time. The next session is due `check_interval` after the session ends.
/// Failures are recorded and retried at the next interval; an unavailable
/// managed host is skipped without preventing checks on the remaining hosts.
///
/// On a stop it begins no further session; a session in progress runs to its
/// end, through the exit from `admin-01` and the disconnect. It then reports
/// its schedule on `report` and holds its channel ends until the process
/// exits.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    start: SystemsAdministratorState,
    stop: StopRequest,
    report: oneshot::Sender<SystemsAdministratorState>,
    egress_addr: SocketAddr,
    managed_hosts: Vec<String>,
    check_interval: Duration,
    step_deadline: Duration,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        start,
        stop,
        report,
        egress_addr,
        managed_hosts,
        check_interval,
        step_deadline,
    )
}

/// Run one administration session immediately.
///
/// This is the deployed integration-test entry point used by the sim binary's
/// `--admin-check-once` diagnostic mode. It uses the same implementation and
/// credentials as the scheduled actor and returns true only when every managed
/// host completes every check.
#[doc(hidden)]
pub async fn run_once(
    clock: SimClock,
    egress_addr: SocketAddr,
    managed_hosts: Vec<String>,
    step_deadline: Duration,
) -> bool {
    internals::run_once(clock, egress_addr, managed_hosts, step_deadline).await
}
