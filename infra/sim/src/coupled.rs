//! The configuration of a run in coupled mode (sim.md §2.1, operating modes):
//! the boundary addresses that HAProxy fronts (sim-network.md §4), the
//! constants that `build.rs` compiles in from `world.yaml`, the sim's own
//! literals, and a start state (sim.md §2.3, start state). The sim's binary and
//! the competition's entry point both build their configuration here, so the
//! two differ only in where the start state comes from.

use std::net::SocketAddr;
use std::time::Duration;

use crate::actors::fuel_supplier::SupplierStart;
use crate::actors::reconciliation_user::{self, AuditPeriod};
use crate::orchestrator::{Config, ReconciliationUserConfig};
use crate::physical::tug_arrival_generator::Vessel;
use crate::serial::SerialConnection;

/// The systems administrator's egress through HAProxy, which applies the sim
/// host's `vnet-ot` address (sim-network.md §5).
pub const SYSTEMS_ADMINISTRATOR_EGRESS: &str = "127.0.0.1:20004";

/// The hosts that the systems administrator checks.
pub fn managed_hosts() -> Vec<String> {
    ["fms-01", "hist-01", "hmi-01"]
        .into_iter()
        .map(str::to_string)
        .collect()
}

/// The tank's volume at the start of a run from the simulator's own start state,
/// in litres.
pub const INITIAL_VOLUME_L: f64 = 35_000.0;

/// The level at or below which the fuel supplier's poll places an order, in
/// litres, as `world.yaml` gives it (`fuel_supplier.reorder_threshold_l`).
pub const REORDER_THRESHOLD_L: f64 = crate::constants::REORDER_THRESHOLD_L;

/// The volume of each road tanker delivery, in litres, as `world.yaml` gives
/// it (`fuel_supplier.delivery_volume_l`).
pub const DELIVERY_VOLUME_L: f64 = crate::constants::DELIVERY_VOLUME_L;

/// The roster's arrival rate in arrivals an hour: the sum, over the roster, of
/// one over each tug's mean time away (sim.md §6, arrival rate). Time spent
/// waiting and refuelling lowers the rate observed at the dock, appreciably
/// only when that time is not small compared with the mean time away.
pub fn roster_arrival_rate_per_h(vessels: &[Vessel]) -> f64 {
    vessels
        .iter()
        .map(|vessel| 3600.0 / vessel.mean_time_away.as_secs_f64())
        .sum()
}

/// The audit period that `world.yaml` gives, as `render.py` projects it.
pub fn audit_period() -> AuditPeriod {
    crate::constants::AUDIT_PERIOD
        .parse::<AuditPeriod>()
        .expect("world_schema.py validates audit.period, and render.py projects it")
}

/// Where the run starts in the port's operation (sim-impl.md, "Start state"),
/// with the seed of the tug arrival generator.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Start {
    /// The seed of the generator `sim_tug_arrivals`.
    pub tug_seed: u64,
    /// Litres in the tank at the start.
    pub initial_volume_l: f64,
    /// How the fuel supplier starts: with an order pending, or with its first
    /// poll due.
    pub supplier_start: SupplierStart,
    /// From the start to the systems administrator's first check.
    pub first_check_after: Duration,
    /// From the start to the reconciliation user's first audit: `Some` exactly
    /// for an audit period in hours.
    pub first_audit_after: Option<Duration>,
    /// The dispenser's cumulative totaliser at the start, in pulses.
    pub initial_cumulative_pulses: u32,
    /// The road tanker's meter at the start, in litres.
    pub initial_meter_totaliser_l: f64,
    /// The number of the run's first bunker delivery note.
    pub first_bdn_number: u32,
    /// The number of the run's first delivery ticket.
    pub first_ticket_number: u32,
}

impl Start {
    /// The simulator's own start state (sim-impl.md, "Start state"), with the
    /// tug arrival generator's seed `tug_seed`: 35,000 litres in the tank, no
    /// order placed, both totalisers at zero, and the first bunker delivery note
    /// and the first delivery ticket numbered 1. The fuel supplier's first poll
    /// and the systems administrator's first check come one interval after the
    /// start. The calendar audit period that `world.yaml` gives places the
    /// first audit, so the start state gives none.
    pub fn defaults(tug_seed: u64) -> Start {
        Start {
            tug_seed,
            initial_volume_l: INITIAL_VOLUME_L,
            supplier_start: SupplierStart::FirstPollAfter(crate::constants::SUPPLIER_POLL_INTERVAL),
            first_check_after: crate::constants::ADMIN_CHECK_INTERVAL,
            first_audit_after: None,
            initial_cumulative_pulses: 0,
            initial_meter_totaliser_l: 0.0,
            first_bdn_number: 1,
            first_ticket_number: 1,
        }
    }
}

/// The configuration of a coupled run from `start`.
///
/// Call inside the Tokio runtime: the transport of each radio-frequency
/// identification reader binds its listener and spawns its accept loop here.
pub fn config(start: Start) -> Config {
    let audit_period = audit_period();
    let (initial_window, width_floor) =
        reconciliation_user::retrieval_widths(roster_arrival_rate_per_h(crate::constants::VESSELS));
    // The readers' serial transport: a TCP listener per reader at the
    // sim<->digital boundary (loopback port map, sim-network.md §4). HAProxy
    // fronts these as the readers' OT addresses.
    let vessel_rfid_serial = SerialConnection::tcp(loopback(19001));
    let operator_rfid_serial = SerialConnection::tcp(loopback(19002));
    Config {
        // Physical constants shared with the digital world (tank capacity,
        // flow-meter K-factor) and the scenario's rates and timings are
        // projected from world.yaml via sim-constants.toml and compiled in by
        // build.rs, so world.yaml is their single source (sim-network.md §7).
        // render.py projects each value a compressed scenario scales already
        // scaled (sim.md §2.2). The remaining values are sim-only and stay as
        // literals.
        tank_capacity_l: crate::constants::TANK_CAPACITY_L,
        overfill_interlock_pct: crate::constants::OVERFILL_INTERLOCK_PCT,
        k_l_per_mm: 7.0,
        inflow_rate_l_per_min: crate::constants::INFLOW_RATE_L_PER_MIN,
        outflow_rate_l_per_min: crate::constants::OUTFLOW_RATE_L_PER_MIN,
        initial_volume_l: start.initial_volume_l,
        initial_temperature_c: 15.0,
        flow_meter_k_factor: crate::constants::FLOW_METER_K_FACTOR,
        probe_noise_enabled: false,
        probe_noise_seed: 0,
        initial_cumulative_pulses: start.initial_cumulative_pulses,
        first_bdn_number: start.first_bdn_number,
        // The roster, each tug with its mean time away (sim.md §6).
        vessels: crate::constants::VESSELS,
        refuel_volume_min_l: crate::constants::REFUEL_VOLUME_MIN_L,
        refuel_volume_max_l: crate::constants::REFUEL_VOLUME_MAX_L,
        tug_seed: start.tug_seed,
        driver_id: "driver-01".to_string(),
        tanker_registration: "AB12 CDE".to_string(),
        first_ticket_number: start.first_ticket_number,
        initial_meter_totaliser_l: start.initial_meter_totaliser_l,
        poll_interval: crate::constants::SUPPLIER_POLL_INTERVAL,
        reorder_threshold_l: REORDER_THRESHOLD_L,
        delivery_volume_l: DELIVERY_VOLUME_L,
        lead_time: crate::constants::LEAD_TIME,
        supplier_start: start.supplier_start,
        // Boundary endpoints: loopback ports HAProxy fronts (sim-network.md
        // §4). The inbound servers bind their own ports; the egress clients
        // dial the proxy, which applies the zone source address, so the
        // outbound `*_local_addr` fields are unbound (`:0`).
        atg_console_listen_addr: loopback(10001),
        dispenser_listen_addr: loopback(10502),
        dock_operator_local_addr: loopback(0),
        hmi_addr: loopback(20002),
        fuel_supplier_local_addr: loopback(0),
        atg_relay_addr: loopback(20001),
        systems_administrator_egress_addr: SYSTEMS_ADMINISTRATOR_EGRESS
            .parse()
            .expect("a literal socket address"),
        systems_administrator_managed_hosts: managed_hosts(),
        admin_first_check_after: start.first_check_after,
        admin_check_interval: crate::constants::ADMIN_CHECK_INTERVAL,
        admin_step_deadline: Duration::from_secs(30),
        vessel_rfid_serial,
        operator_rfid_serial,
        // The reconciliation user (sim-impl.md, "The audit loop"). Its session
        // is dialled through HAProxy's egress port, which applies the sim
        // host's vnet-it address (sim-network.md §5). The audit's period,
        // settling delay and boundary margin are the port's procedure,
        // projected from world.yaml (deployment-config.md §3); the retrieval
        // widths follow the arrival rate; the rest are the sim's own.
        reconciliation_user: ReconciliationUserConfig {
            local_addr: loopback(0),
            recon_addr: loopback(20003),
            audit_period,
            first_audit_after: start.first_audit_after,
            settling_delay: crate::constants::SETTLING_DELAY,
            boundary_margin: crate::constants::BOUNDARY_MARGIN,
            initial_window,
            width_floor,
            // More than a hundred times the measured near-ceiling read.
            clipboard_deadline: Duration::from_secs(10),
            // A Ctrl+Alt+T 0.6 s after the connection did not open a terminal
            // and one at 2.6 s did, at a first login and a later one; ten
            // seconds is about four times 2.6 s.
            desktop_settle: Duration::from_secs(10),
            terminal_settle: Duration::from_secs(3),
            // The interval the probe proved.
            key_interval: Duration::from_millis(30),
            connect_deadline: Duration::from_secs(30),
        },
    }
}

fn loopback(port: u16) -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], port))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// At the roster's arrival rate, three tugs each away for 20 hours on
    /// average, the audit's first query covers a whole month with its margins,
    /// and the floor is wider than a day. A day's or a week's window is
    /// therefore one query of exactly its bounds, and a query of it that
    /// reaches its deadline is at the floor. A compression factor of 5 raises
    /// the rate fivefold and divides the period, so the same holds then.
    #[test]
    fn the_audit_queries_a_whole_period_at_the_rosters_arrival_rate() {
        const HOUR: f64 = 3600.0;
        let rate = roster_arrival_rate_per_h(crate::constants::VESSELS);
        assert!((rate - 0.15).abs() < 1e-9, "{rate}");
        for factor in [1.0, 5.0] {
            let (initial, floor) = reconciliation_user::retrieval_widths(rate * factor);
            let month_with_margins = (720.0 + 2.0 * 2.0) * HOUR / factor;
            assert!(initial.as_secs_f64() > month_with_margins, "{initial:?}");
            assert!(floor.as_secs_f64() > 24.0 * HOUR / factor, "{floor:?}");
        }
    }

    #[test]
    fn the_defaults_give_the_simulators_own_start_state() {
        let start = Start::defaults(7);
        assert_eq!(start.tug_seed, 7);
        assert_eq!(start.initial_volume_l, 35_000.0);
        assert_eq!(
            start.supplier_start,
            SupplierStart::FirstPollAfter(crate::constants::SUPPLIER_POLL_INTERVAL)
        );
        assert_eq!(
            start.first_check_after,
            crate::constants::ADMIN_CHECK_INTERVAL
        );
        assert_eq!(start.first_audit_after, None);
        assert_eq!(start.initial_cumulative_pulses, 0);
        assert_eq!(start.initial_meter_totaliser_l, 0.0);
        assert_eq!((start.first_bdn_number, start.first_ticket_number), (1, 1));
    }
}
