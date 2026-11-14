//! The reconciliation user through `spawn()` (sim-tests.md, "Reconciliation
//! user"). Only what `spawn()` does before any session is tested here; the
//! audit loop is driven in-module through `run`, with the connector double.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::time::Duration;

use time::macros::datetime;
use tokio::sync::{mpsc, oneshot};

use sim::actors::reconciliation_user::{retrieval_widths, spawn, AuditPeriod};
use sim::clock::{SimClock, TimeModel};
use sim::stop::stop_channel;

const HOUR: Duration = Duration::from_secs(60 * 60);
const DAY: Duration = Duration::from_secs(24 * 60 * 60);

/// Whether `spawn()` accepts a day period with `settling_delay` and
/// `boundary_margin`, or panics as a startup fault.
fn spawn_accepts(settling_delay: Duration, boundary_margin: Duration) -> bool {
    spawn_accepts_period(AuditPeriod::Day, None, settling_delay, boundary_margin)
}

/// Whether `spawn()` accepts `period` with `first_audit_after`, `settling_delay`
/// and `boundary_margin`, or panics as a startup fault.
fn spawn_accepts_period(
    period: AuditPeriod,
    first_audit_after: Option<Duration>,
    settling_delay: Duration,
    boundary_margin: Duration,
) -> bool {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, datetime!(2025-01-15 12:00));
    runtime.block_on(async {
        let (_record_tx, record_rx) = mpsc::unbounded_channel();
        let (_stop, stop) = stop_channel();
        let (_dock_operator_report, dock_operator_at_rest) = oneshot::channel();
        let (report, _report_rx) = oneshot::channel();
        catch_unwind(AssertUnwindSafe(|| {
            spawn(
                clock.clone(),
                None,
                stop,
                dock_operator_at_rest,
                report,
                record_rx,
                "127.0.0.1:0".parse().unwrap(),
                "127.0.0.1:20003".parse().unwrap(),
                period,
                first_audit_after,
                settling_delay,
                7 * DAY,
                DAY,
                boundary_margin,
                Duration::from_secs(60),
                Duration::from_secs(10),
                Duration::from_secs(3),
                Duration::from_millis(30),
                Duration::from_secs(30),
            )
        }))
        .is_ok()
    })
}

/// `world.yaml` names the period (`audit.period`), compiled in as text; the
/// orchestrator reads the actor's period from that name.
#[test]
fn the_audit_period_is_read_from_its_world_yaml_name() {
    assert_eq!("month".parse(), Ok(AuditPeriod::Month));
    assert_eq!("week".parse(), Ok(AuditPeriod::Week));
    assert_eq!("day".parse(), Ok(AuditPeriod::Day));
    assert!("fortnight".parse::<AuditPeriod>().is_err());
    assert!("Month".parse::<AuditPeriod>().is_err());
}

/// In a compressed scenario `render.py` projects the period as a number of
/// hours, which may be fractional, such as a week of 168 hours at a
/// compression factor of 5.
#[test]
fn a_period_in_hours_is_read_from_its_projected_number() {
    assert_eq!("144h".parse(), Ok(AuditPeriod::Interval(144 * HOUR)));
    assert_eq!(
        "33.6h".parse(),
        Ok(AuditPeriod::Interval(Duration::from_secs(120_960)))
    );
    for refused in ["0h", "-1h", "h", "144", "inf h", "NaNh"] {
        assert!(
            refused.parse::<AuditPeriod>().is_err(),
            "accepted {refused:?}"
        );
    }
}

/// The start state places the first audit for a period in hours, and the
/// calendar places it for a month, a week or a day. Any other
/// pairing is a startup fault.
#[test]
fn a_first_audit_is_given_exactly_for_a_period_in_hours() {
    let hours = AuditPeriod::Interval(144 * HOUR);
    assert!(spawn_accepts_period(hours, Some(3 * HOUR), 2 * HOUR, HOUR));
    assert!(!spawn_accepts_period(hours, None, 2 * HOUR, HOUR));
    assert!(!spawn_accepts_period(
        AuditPeriod::Day,
        Some(HOUR),
        6 * HOUR,
        2 * HOUR
    ));
}

/// A period in hours bounds the settling delay by the period itself.
#[test]
fn a_period_in_hours_bounds_the_settling_delay_by_its_length() {
    let hours = AuditPeriod::Interval(4 * HOUR);
    assert!(spawn_accepts_period(hours, Some(4 * HOUR), 3 * HOUR, HOUR));
    assert!(!spawn_accepts_period(hours, Some(5 * HOUR), 4 * HOUR, HOUR));
}

/// The first window of a period in hours ends one settling delay before the
/// first audit, and the actor holds no paper from before its spawn, so a
/// first window that ends at or before the spawn is refused as a startup
/// fault (sim-impl.md, "A period in hours").
#[test]
fn a_first_window_that_ends_at_or_before_the_spawn_is_refused() {
    let hours = AuditPeriod::Interval(144 * HOUR);
    assert!(!spawn_accepts_period(hours, Some(HOUR), 2 * HOUR, HOUR));
    assert!(!spawn_accepts_period(hours, Some(2 * HOUR), 2 * HOUR, HOUR));
    assert!(spawn_accepts_period(
        hours,
        Some(2 * HOUR + Duration::from_secs(1)),
        2 * HOUR,
        HOUR
    ));
}

/// The widths fill half and a sixteenth of the clipboard's ceiling at the
/// arrival rate: about 8.4 days and about 1 day at one arrival an hour, and a
/// fifth of each at five arrivals an hour.
#[test]
fn the_retrieval_widths_follow_the_arrival_rate() {
    let (initial, floor) = retrieval_widths(1.0);
    assert!(
        (initial.as_secs_f64() / 86_400.0 - 8.38).abs() < 0.01,
        "{initial:?}"
    );
    assert!(
        (floor.as_secs_f64() / 86_400.0 - 1.05).abs() < 0.01,
        "{floor:?}"
    );
    let (initial_5, floor_5) = retrieval_widths(5.0);
    assert!((initial_5.as_secs_f64() * 5.0 - initial.as_secs_f64()).abs() < 1e-3);
    assert!((floor_5.as_secs_f64() * 5.0 - floor.as_secs_f64()).abs() < 1e-3);
}

/// A delay of one period or more would start each audit after the following
/// window had ended; one shorter than the margin would let an artefact arrive
/// dated before the window being audited. Either is refused before any session
/// is attempted.
#[test]
fn a_settling_delay_outside_its_bounds_is_refused_at_spawn() {
    assert!(
        !spawn_accepts(DAY, 2 * HOUR),
        "a one-day delay on a day period"
    );
    assert!(
        !spawn_accepts(HOUR, 2 * HOUR),
        "a delay shorter than the boundary margin"
    );
    assert!(
        spawn_accepts(6 * HOUR, 2 * HOUR),
        "a delay within its bounds"
    );
}
