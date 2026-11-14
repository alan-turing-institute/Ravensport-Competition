//! Reconciliation user actor.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `ReconciliationRecord`, `ReconciliationUserState`.

mod internals;

use std::net::SocketAddr;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot};

use crate::actors::dock_operator::BDN;
use crate::actors::road_tanker_driver::DeliveryTicket;
use crate::clock::SimClock;
use crate::stop::StopRequest;

/// A paper artefact posted by the dock operator for the reconciliation user to
/// cross-check against the log-server replica at month-end.
///
/// Defined here ahead of the reconciliation user's own implementation because
/// it is the record type the dock operator posts on each completed transaction.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum ReconciliationRecord {
    Bdn(BDN),
    DeliveryTicket(DeliveryTicket),
}

/// The period one audit covers, set by the port (narrative.md §3), in UTC: a
/// calendar month; an ISO week, 00:00 on Monday to the end of Sunday; or a day,
/// 00:00 to the end of that day, including the whole of 23:59:59. Each
/// period's successor begins at 00:00. In a compressed scenario the period is
/// a fixed interval measured in hours (narrative.md §6, item 3, compressed
/// scenarios), whose windows' boundaries the first audit fixes (sim-impl.md,
/// "A period in hours").
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AuditPeriod {
    Month,
    Week,
    Day,
    Interval(Duration),
}

/// The period by its name in `world.yaml` (`audit.period`), or, as `render.py`
/// projects it in a compressed scenario, as a number of hours such as `144h`.
impl std::str::FromStr for AuditPeriod {
    type Err = String;

    fn from_str(name: &str) -> Result<AuditPeriod, String> {
        let hours = name
            .strip_suffix('h')
            .and_then(|hours| hours.parse::<f64>().ok())
            .filter(|hours| hours.is_finite() && *hours > 0.0);
        match (name, hours) {
            ("month", _) => Ok(AuditPeriod::Month),
            ("week", _) => Ok(AuditPeriod::Week),
            ("day", _) => Ok(AuditPeriod::Day),
            (_, Some(hours)) => Ok(AuditPeriod::Interval(Duration::from_secs_f64(
                hours * 3600.0,
            ))),
            (other, None) => Err(format!(
                "{other:?} is not an audit period: month, week, day or a positive number of hours"
            )),
        }
    }
}

/// `initial_window` and `width_floor` at the dock's arrival rate of
/// `arrivals_per_h`: the times whose expected records fill half and a
/// sixteenth of the clipboard's ceiling (sim-impl.md, "Splitting the window").
pub fn retrieval_widths(arrivals_per_h: f64) -> (Duration, Duration) {
    internals::retrieval_widths(arrivals_per_h)
}

/// The reconciliation user's state at a stop (sim-impl.md, "State types"): the
/// window it audits next, and every paper artefact held for that window or a
/// later one. The audit is due `settling_delay` after `window_end`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReconciliationUserState {
    #[serde(with = "crate::checkpoint::utc")]
    pub window_start: PrimitiveDateTime,
    #[serde(with = "crate::checkpoint::utc")]
    pub window_end: PrimitiveDateTime,
    pub records: Vec<ReconciliationRecord>,
}

/// Spawn the reconciliation user actor.
///
/// Sleeps until `settling_delay` past the end of each audit period, then audits
/// that period (sim-impl.md, "The audit loop"): drains `record_rx` and
/// partitions the artefacts by their own commencement, holding any dated in a
/// later window; connects to `recon-01` through HAProxy's egress port; confirms
/// the session through the clipboard latch, retrieves the window, records the
/// audit's summary entry, closes the terminal and disconnects; then emits the
/// result, or panics on a fatal one. Both credentials and the deployment root
/// are the compiled world constants.
///
/// It starts from `start`'s window and artefacts on a warm start, and from the
/// window beginning at its spawn on a cold start (`None`). On a stop it begins
/// no further audit and finishes an audit in progress; once the dock operator
/// has reported (`dock_operator_at_rest`) it drains `record_rx`, reports its
/// window and the artefacts held on `report`, and holds its channel ends until
/// the process exits.
///
/// # Panics
///
/// As a startup fault, on a settling delay outside its bounds, on a first
/// audit given for a calendar period or missing for a period in hours, or on a
/// first window of a period in hours that ends at or before the spawn.
#[allow(clippy::too_many_arguments)]
pub fn spawn(
    clock: SimClock,
    start: Option<ReconciliationUserState>,
    stop: StopRequest,
    dock_operator_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<ReconciliationUserState>,
    record_rx: mpsc::UnboundedReceiver<ReconciliationRecord>,
    // The loopback address the session is dialled from, unbound: HAProxy
    // applies the sim host's `vnet-it` address (sim-network.md §5).
    local_addr: SocketAddr,
    // HAProxy's egress port to `recon-01:3389`, in TCP mode.
    recon_addr: SocketAddr,
    audit_period: AuditPeriod,
    // From the actor's spawn to its first audit, set by the start state.
    // `Some` exactly when `audit_period` is an interval, and then longer than
    // `settling_delay`, or `spawn` refuses the configuration as a startup
    // fault.
    first_audit_after: Option<Duration>,
    // At least `boundary_margin` and shorter than the shortest period of its
    // kind, or `spawn` refuses it.
    settling_delay: Duration,
    // Width of the first sub-retrieval, and the width below which halving
    // stops, both derived from the arrival rate (`retrieval_widths`).
    initial_window: Duration,
    width_floor: Duration,
    boundary_margin: Duration,
    clipboard_deadline: Duration,
    // After connecting, before the session's first `Ctrl+Alt+T`.
    desktop_settle: Duration,
    terminal_settle: Duration,
    key_interval: Duration,
    connect_deadline: Duration,
) -> tokio::task::JoinHandle<()> {
    internals::spawn(
        clock,
        record_rx,
        internals::Settings {
            local_addr,
            recon_addr,
            audit_period,
            first_audit_after,
            settling_delay,
            initial_window,
            width_floor,
            boundary_margin,
            clipboard_deadline,
            desktop_settle,
            terminal_settle,
            key_interval,
            connect_deadline,
        },
        start,
        stop,
        dock_operator_at_rest,
        report,
    )
}
