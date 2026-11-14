//! The sim binary's orchestrator: the single construction site that wires every
//! channel, spawns every sim entity, and supervises them.
//!
//! See sim-impl.md "Orchestrator (the sim binary)". `build` pre-creates all
//! channels and spawns the entities, from a checkpoint or cold; `run` builds,
//! supervises and, on `SIGTERM`, brings the sim to rest and writes the
//! checkpoint, and is the body of `main`. Integration tests call `build`
//! directly, supervise via [`SimHandles::await_first_failure`], and stop the
//! sim via [`SimHandles::stop`] and [`SimHandles::come_to_rest`].

use std::net::SocketAddr;
use std::path::Path;
use std::time::Duration;

use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::{JoinError, JoinHandle, JoinSet};

use crate::actors::dock_operator::DockOperatorState;
use crate::actors::fuel_supplier::FuelSupplierState;
use crate::actors::reconciliation_user::ReconciliationUserState;
use crate::actors::road_tanker_driver::RoadTankerDriverState;
use crate::actors::systems_administrator::SystemsAdministratorState;
use crate::actors::{
    dock_operator, fuel_supplier, reconciliation_user, road_tanker_driver, systems_administrator,
    tug_engineer,
};
use crate::appliances::atg_console::AtgConsoleState;
use crate::appliances::dispenser::{DispenserControlState, DispenserState};
use crate::appliances::{atg_console, dispenser, operator_rfid_reader, vessel_rfid_reader};
use crate::checkpoint::{self, Checkpoint, Parameters, VesselParameters};
use crate::clock::SimClock;
use crate::physical::atg_probe::AtgProbeState;
use crate::physical::bund_leak_sensor::BundReading;
use crate::physical::fuel_tank::{FuelTankReport, FuelTankRows, FuelTankState, OutflowRate};
use crate::physical::tug_arrival_generator::TugArrivalState;
use crate::physical::{atg_probe, bund_leak_sensor, flow_meter, fuel_tank, tug_arrival_generator};
use crate::serial::SerialConnection;
use crate::stop::{stop_channel, StopSender};

/// Inter-entity channel capacity. Generous relative to the (low) message rates.
const CHANNEL_CAPACITY: usize = 64;

/// Everything the orchestrator needs to build the sim: physical-model
/// parameters, instrument/actor configuration, the sim<->digital boundary
/// endpoints, and the (caller-created) RFID serial transports.
pub struct Config {
    // Physical model (sim.md §1.1).
    pub tank_capacity_l: f64,
    /// The overfill interlock level as a percentage of `tank_capacity_l`
    /// (sim.md §1.3).
    pub overfill_interlock_pct: f64,
    pub k_l_per_mm: f64,
    /// Tank inflow rate while filling. The tanker pump is the tank's only
    /// inflow source, so this same value is used as the road tanker driver's
    /// pump rate, from which it derives the pump-run duration.
    pub inflow_rate_l_per_min: f64,
    pub outflow_rate_l_per_min: f64,
    pub initial_volume_l: f64,
    pub initial_temperature_c: f64,
    // Instruments.
    pub flow_meter_k_factor: f64,
    pub probe_noise_enabled: bool,
    pub probe_noise_seed: u64,
    // Dispenser.
    /// The cumulative totaliser's reading at the start, in pulses
    /// (sim-impl.md, "Start state").
    pub initial_cumulative_pulses: u32,
    // Dock operator.
    /// The number of the first BDN of the run (sim-impl.md, "Start state").
    pub first_bdn_number: u32,
    // Tug arrivals.
    /// The roster, one tug arrival task per vessel (sim.md §6).
    pub vessels: &'static [tug_arrival_generator::Vessel],
    /// Bounds of each tug's requested volume in litres (sim.md §6).
    pub refuel_volume_min_l: f64,
    pub refuel_volume_max_l: f64,
    pub tug_seed: u64,
    // Road tanker driver.
    pub driver_id: String,
    pub tanker_registration: String,
    /// The number of the first delivery ticket of the run (sim-impl.md,
    /// "Start state").
    pub first_ticket_number: u32,
    /// The tanker meter's reading at the start, in litres (sim-impl.md,
    /// "Start state").
    pub initial_meter_totaliser_l: f64,
    // Fuel supplier.
    pub poll_interval: Duration,
    pub reorder_threshold_l: f64,
    pub delivery_volume_l: f64,
    pub lead_time: Duration,
    /// How the fuel supplier starts, set by the start state (sim-impl.md,
    /// "Start state").
    pub supplier_start: fuel_supplier::SupplierStart,
    // Sim<->digital boundary endpoints.
    pub atg_console_listen_addr: SocketAddr,
    pub dispenser_listen_addr: SocketAddr,
    pub dock_operator_local_addr: SocketAddr,
    pub hmi_addr: SocketAddr,
    pub fuel_supplier_local_addr: SocketAddr,
    pub atg_relay_addr: SocketAddr,
    pub systems_administrator_egress_addr: SocketAddr,
    pub systems_administrator_managed_hosts: Vec<String>,
    pub admin_first_check_after: Duration,
    pub admin_check_interval: Duration,
    pub admin_step_deadline: Duration,
    // RFID serial transports (write ends). The digital world reads the
    // corresponding handles; in tests these are loopbacks (decision 3).
    pub vessel_rfid_serial: SerialConnection,
    pub operator_rfid_serial: SerialConnection,
    // Reconciliation user.
    pub reconciliation_user: ReconciliationUserConfig,
}

impl Config {
    /// The parameters a checkpoint is written under and compared against
    /// ([`Parameters`]). The start state's totalisers, first numbers, fuel
    /// supplier start, first check and first audit, the width floor and the
    /// seeds are not among them.
    pub fn parameters(&self) -> Parameters {
        let recon = &self.reconciliation_user;
        Parameters {
            tank_capacity_l: self.tank_capacity_l,
            k_l_per_mm: self.k_l_per_mm,
            inflow_rate_l_per_min: self.inflow_rate_l_per_min,
            outflow_rate_l_per_min: self.outflow_rate_l_per_min,
            initial_volume_l: self.initial_volume_l,
            initial_temperature_c: self.initial_temperature_c,
            flow_meter_k_factor: self.flow_meter_k_factor,
            probe_noise_enabled: self.probe_noise_enabled,
            overfill_interlock_pct: self.overfill_interlock_pct,
            vessels: self
                .vessels
                .iter()
                .map(|vessel| VesselParameters {
                    vessel_id: vessel.vessel_id.0,
                    name: vessel.name.to_string(),
                    rfid_card: vessel.rfid_card.0,
                    mean_time_away: vessel.mean_time_away,
                })
                .collect(),
            refuel_volume_min_l: self.refuel_volume_min_l,
            refuel_volume_max_l: self.refuel_volume_max_l,
            driver_id: self.driver_id.clone(),
            tanker_registration: self.tanker_registration.clone(),
            poll_interval: self.poll_interval,
            reorder_threshold_l: self.reorder_threshold_l,
            delivery_volume_l: self.delivery_volume_l,
            lead_time: self.lead_time,
            audit_period: recon.audit_period,
            settling_delay: recon.settling_delay,
            initial_window: recon.initial_window,
            boundary_margin: recon.boundary_margin,
            clipboard_deadline: recon.clipboard_deadline,
            desktop_settle: recon.desktop_settle,
            terminal_settle: recon.terminal_settle,
            key_interval: recon.key_interval,
            connect_deadline: recon.connect_deadline,
            admin_check_interval: self.admin_check_interval,
            admin_step_deadline: self.admin_step_deadline,
        }
    }
}

/// The reconciliation user's configuration (sim-impl.md, "The audit loop"):
/// its boundary endpoints, its audit cadence and the timings of its session.
pub struct ReconciliationUserConfig {
    /// The loopback address the session is dialled from, unbound.
    pub local_addr: SocketAddr,
    /// HAProxy's egress port to `recon-01:3389`.
    pub recon_addr: SocketAddr,
    pub audit_period: reconciliation_user::AuditPeriod,
    /// From the start to the first audit, set by the start state for a period
    /// in hours; `None` for a calendar period.
    pub first_audit_after: Option<Duration>,
    pub settling_delay: Duration,
    pub initial_window: Duration,
    pub width_floor: Duration,
    pub boundary_margin: Duration,
    pub clipboard_deadline: Duration,
    /// After connecting, before the session's first `Ctrl+Alt+T`.
    pub desktop_settle: Duration,
    pub terminal_settle: Duration,
    pub key_interval: Duration,
    pub connect_deadline: Duration,
}

/// The spawned sim, supervised as a set. Holds the orphan alarm-clear sender so
/// the ATG console's alarm-clear channel stays open (no producer exists in the
/// sim yet).
pub struct SimHandles {
    set: JoinSet<Result<(), JoinError>>,
    stop: StopSender,
    reports: Option<Reports>,
    state_requests: StateRequests,
    clock: SimClock,
    parameters: Parameters,
    _alarm_clear_tx: mpsc::Sender<()>,
}

/// The sim at rest: the checkpoint to write, and the fuel tank's rows that
/// confirm the sim is at rest.
pub struct AtRest {
    pub checkpoint: Checkpoint,
    pub rows: FuelTankRows,
}

/// The orchestrator's ends of the reports each entity makes at a stop, and of
/// the signals it routes between them (sim-impl.md, "Coming to rest").
struct Reports {
    tug_arrivals: oneshot::Receiver<TugArrivalState>,
    tug_engineer: oneshot::Receiver<()>,
    road_tanker_driver: oneshot::Receiver<RoadTankerDriverState>,
    fuel_supplier: oneshot::Receiver<FuelSupplierState>,
    dock_operator: oneshot::Receiver<DockOperatorState>,
    reconciliation_user: oneshot::Receiver<ReconciliationUserState>,
    systems_administrator: oneshot::Receiver<SystemsAdministratorState>,
    /// To the tug arrival generator, once the tug engineer has reported.
    engineer_at_rest: oneshot::Sender<()>,
    /// To the fuel supplier, once the road tanker driver has reported.
    driver_at_rest: oneshot::Sender<()>,
    /// To the dock operator, once the tug engineer and the driver have reported.
    peers_at_rest: oneshot::Sender<()>,
    /// To the reconciliation user, once the dock operator has reported.
    dock_operator_at_rest: oneshot::Sender<()>,
}

/// The orchestrator's ends of the passive entities' state request channels,
/// held for the life of the sim like every other channel end.
#[derive(Clone)]
struct StateRequests {
    fuel_tank: mpsc::Sender<oneshot::Sender<FuelTankReport>>,
    atg_probe: mpsc::Sender<oneshot::Sender<AtgProbeState>>,
    atg_console: mpsc::Sender<oneshot::Sender<AtgConsoleState>>,
    dispenser: mpsc::Sender<oneshot::Sender<DispenserState>>,
}

impl SimHandles {
    /// Await the first entity to stop. Since every entity runs for the whole
    /// sim, any stop is an error; returns a description of the cause (a panic
    /// message names the failing entity). Pending while all entities run.
    pub async fn await_first_failure(&mut self) -> String {
        match self.set.join_next().await {
            Some(Ok(Ok(()))) => "a sim entity exited unexpectedly".to_string(),
            Some(Ok(Err(error))) if error.is_panic() => panic_message(error.into_panic()),
            Some(Ok(Err(_))) => "a sim entity was cancelled".to_string(),
            Some(Err(_)) => "the sim supervisor wrapper failed".to_string(),
            None => std::future::pending().await,
        }
    }

    /// Ask the sim to stop, as `SIGTERM` does.
    pub fn stop(&self) {
        self.stop.stop();
    }

    /// After [`stop`](Self::stop), await every entity's report and build the
    /// checkpoint. Supervision continues meanwhile: an entity that finishes
    /// is fatal, and its cause is returned as the error.
    ///
    /// # Panics
    ///
    /// If called a second time.
    pub async fn come_to_rest(&mut self) -> Result<AtRest, String> {
        let reports = self
            .reports
            .take()
            .expect("orchestrator: the sim has already come to rest");
        let collecting = collect(
            reports,
            self.state_requests.clone(),
            self.clock.clone(),
            self.parameters.clone(),
        );
        let collected = tokio::select! {
            biased;
            cause = self.await_first_failure() => return Err(cause),
            collected = collecting => collected,
        };
        match collected {
            Some(at_rest) => Ok(at_rest),
            // A report that never came means its entity has gone; the
            // supervisor names it.
            None => Err(self.await_first_failure().await),
        }
    }
}

/// Await every report in the order the work flows (sim-impl.md, "Coming to
/// rest"), routing each signal an entity waits on, then request the passive
/// entities' state. `None` if an entity has gone.
async fn collect(
    reports: Reports,
    state_requests: StateRequests,
    clock: SimClock,
    parameters: Parameters,
) -> Option<AtRest> {
    let Reports {
        tug_arrivals,
        tug_engineer,
        road_tanker_driver,
        fuel_supplier,
        dock_operator,
        reconciliation_user,
        systems_administrator,
        engineer_at_rest,
        driver_at_rest,
        peers_at_rest,
        dock_operator_at_rest,
    } = reports;

    let actors = async {
        let ((), road_tanker_driver) = tokio::try_join!(
            async {
                tug_engineer.await?;
                // A failed send means the generator has gone; its own report
                // fails.
                let _ = engineer_at_rest.send(());
                Ok::<(), oneshot::error::RecvError>(())
            },
            async {
                let driver = road_tanker_driver.await?;
                // A failed send means the supplier has gone; its own report fails.
                let _ = driver_at_rest.send(());
                Ok(driver)
            }
        )
        .ok()?;
        peers_at_rest.send(()).ok()?;
        let dock_operator = dock_operator.await.ok()?;
        dock_operator_at_rest.send(()).ok()?;
        let reconciliation_user = reconciliation_user.await.ok()?;
        Some((road_tanker_driver, dock_operator, reconciliation_user))
    };
    // The generator reports once the tug engineer has; the supplier once the
    // driver has. The systems administrator waits on no peer: it reports once
    // a session in progress has ended.
    let (actors, tug_arrivals, fuel_supplier, systems_administrator) =
        tokio::join!(actors, tug_arrivals, fuel_supplier, systems_administrator);
    let (road_tanker_driver, dock_operator, reconciliation_user) = actors?;
    let tug_arrivals = tug_arrivals.ok()?;
    let fuel_supplier = fuel_supplier.ok()?;
    let systems_administrator = systems_administrator.ok()?;

    let tank = request(&state_requests.fuel_tank).await?;
    let checkpoint = Checkpoint {
        format_version: checkpoint::FORMAT_VERSION,
        written_at: clock.now_datetime(),
        parameters,
        fuel_tank: tank.state,
        atg_probe: request(&state_requests.atg_probe).await?,
        atg_console: request(&state_requests.atg_console).await?,
        dispenser: request(&state_requests.dispenser).await?,
        tug_arrivals,
        dock_operator,
        road_tanker_driver,
        fuel_supplier,
        reconciliation_user,
        systems_administrator,
    };
    Some(AtRest {
        checkpoint,
        rows: tank.rows,
    })
}

/// Request a passive entity's state on its request channel.
async fn request<S>(requests: &mpsc::Sender<oneshot::Sender<S>>) -> Option<S> {
    let (reply_tx, reply_rx) = oneshot::channel();
    requests.send(reply_tx).await.ok()?;
    reply_rx.await.ok()
}

/// Pre-create every channel and spawn every sim entity, returning the
/// supervised set. Each entity starts from its state in `checkpoint` on a warm
/// start, and from the configuration on a cold start (`None`). Must be called
/// within a Tokio runtime (the appliances bind their listeners and all
/// entities spawn tasks).
pub fn build(clock: SimClock, config: Config, checkpoint: Option<Checkpoint>) -> SimHandles {
    let parameters = config.parameters();
    let start = match checkpoint {
        Some(checkpoint) => Start::warm(checkpoint),
        None => Start::cold(&clock, &config),
    };

    // Channels (both ends owned by entities, except `alarm_clear`, which has no
    // producer in the current sim).
    let (sounding_tx, sounding_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (probe_request_tx, probe_request_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (bund_wet_tx, bund_wet_rx) = watch::channel(start.fuel_tank.bund_wet);
    let (interlock_plug_tx, interlock_plug_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (bund_reading_tx, bund_reading_rx) = watch::channel(BundReading {
        wet: start.fuel_tank.bund_wet,
    });
    let (outflow_rate_tx, outflow_rate_rx) = watch::channel::<OutflowRate>(None);
    let (pulse_tx, pulse_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (dispenser_ctrl_tx, dispenser_ctrl_rx) = watch::channel(DispenserControlState::default());
    let (esd_tx, esd_rx) = watch::channel(false);
    let (tanker_pump_tx, tanker_pump_rx) = watch::channel(false);
    // One slot per roster tug: each tug has at most one arrival outstanding, so
    // a tug's arrival never waits on the channel and one tug's waiting does not
    // delay another's arrival (sim.md §6). At least one slot, so that an empty
    // roster reaches the generator's own refusal at spawn.
    let (tug_arrival_tx, tug_arrival_rx) = mpsc::channel(config.vessels.len().max(1));
    let (dock_job_tx, dock_job_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (vessel_card_tx, vessel_card_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (operator_card_tx, operator_card_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (bdn_sign_tx, bdn_sign_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (bdn_signed_tx, bdn_signed_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (ticket_tx, ticket_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (recon_tx, recon_rx) = mpsc::unbounded_channel();
    let (fill_job_tx, fill_job_rx) = mpsc::channel(CHANNEL_CAPACITY);
    let (alarm_clear_tx, alarm_clear_rx) = mpsc::channel(CHANNEL_CAPACITY);

    // Stopping: the stop request, each entity's report, the signals routed
    // between them, and the passive entities' state requests.
    let (stop_tx, stop) = stop_channel();
    let (tug_arrivals_report, tug_arrivals_rx) = oneshot::channel();
    let (tug_engineer_report, tug_engineer_rx) = oneshot::channel();
    let (road_tanker_driver_report, road_tanker_driver_rx) = oneshot::channel();
    let (fuel_supplier_report, fuel_supplier_rx) = oneshot::channel();
    let (dock_operator_report, dock_operator_rx) = oneshot::channel();
    let (reconciliation_user_report, reconciliation_user_rx) = oneshot::channel();
    let (systems_administrator_report, systems_administrator_rx) = oneshot::channel();
    let (engineer_at_rest_tx, engineer_at_rest_rx) = oneshot::channel();
    let (driver_at_rest_tx, driver_at_rest_rx) = oneshot::channel();
    let (peers_at_rest_tx, peers_at_rest_rx) = oneshot::channel();
    let (dock_operator_at_rest_tx, dock_operator_at_rest_rx) = oneshot::channel();
    let (fuel_tank_state_tx, fuel_tank_state_rx) = mpsc::channel(1);
    let (atg_probe_state_tx, atg_probe_state_rx) = mpsc::channel(1);
    let (atg_console_state_tx, atg_console_state_rx) = mpsc::channel(1);
    let (dispenser_state_tx, dispenser_state_rx) = mpsc::channel(1);

    let Config {
        tank_capacity_l,
        overfill_interlock_pct,
        k_l_per_mm,
        inflow_rate_l_per_min,
        outflow_rate_l_per_min,
        initial_volume_l: _,
        initial_temperature_c: _,
        flow_meter_k_factor,
        probe_noise_enabled,
        // The start state's values are read by `Start::cold` above.
        probe_noise_seed: _,
        initial_cumulative_pulses: _,
        first_bdn_number: _,
        vessels,
        refuel_volume_min_l,
        refuel_volume_max_l,
        tug_seed: _,
        driver_id,
        tanker_registration,
        first_ticket_number: _,
        initial_meter_totaliser_l: _,
        poll_interval,
        reorder_threshold_l,
        delivery_volume_l,
        lead_time,
        supplier_start: _,
        atg_console_listen_addr,
        dispenser_listen_addr,
        dock_operator_local_addr,
        hmi_addr,
        fuel_supplier_local_addr,
        atg_relay_addr,
        systems_administrator_egress_addr,
        systems_administrator_managed_hosts,
        admin_first_check_after: _,
        admin_check_interval,
        admin_step_deadline,
        vessel_rfid_serial,
        operator_rfid_serial,
        reconciliation_user,
    } = config;

    // Readiness handles for the RFID readers, taken before the connections are
    // moved into their reader tasks. The dock operator and tug engineer wait on
    // these so a card is presented only once its consuming host (HMI/FMS) is
    // subscribed — the sim compresses startup, so this re-imposes the real
    // ordering where the digital world is attached before operations begin.
    let operator_rfid_ready = operator_rfid_serial.readiness();
    let vessel_rfid_ready = vessel_rfid_serial.readiness();

    let mut handles: Vec<JoinHandle<()>> = vec![
        // Physical model.
        fuel_tank::spawn(
            clock.clone(),
            tank_capacity_l,
            overfill_interlock_pct,
            k_l_per_mm,
            inflow_rate_l_per_min,
            outflow_rate_l_per_min,
            start.fuel_tank,
            fuel_tank_state_rx,
            sounding_rx,
            bund_wet_tx,
            interlock_plug_rx,
            outflow_rate_tx,
            dispenser_ctrl_rx,
            esd_rx,
            tanker_pump_rx,
        ),
        // Instruments.
        atg_probe::spawn(
            probe_request_rx,
            sounding_tx,
            probe_noise_enabled,
            start.atg_probe,
            atg_probe_state_rx,
        ),
        bund_leak_sensor::spawn(bund_wet_rx, bund_reading_tx),
        flow_meter::spawn(
            clock.clone(),
            flow_meter_k_factor,
            outflow_rate_rx,
            pulse_tx,
        ),
    ];
    // The tug arrival generator contributes one task per tug, and one that
    // collects their states at a stop.
    handles.extend(tug_arrival_generator::spawn(
        clock.clone(),
        vessels,
        refuel_volume_min_l,
        refuel_volume_max_l,
        start.tug_arrivals,
        stop.clone(),
        engineer_at_rest_rx,
        tug_arrivals_report,
        tug_arrival_tx,
    ));
    handles.extend([
        // Appliances.
        atg_console::spawn(
            clock.clone(),
            k_l_per_mm,
            tank_capacity_l,
            start.atg_console,
            atg_console_state_rx,
            probe_request_tx,
            bund_reading_rx,
            alarm_clear_rx,
            atg_console_listen_addr,
        ),
        dispenser::spawn(
            clock.clone(),
            start.dispenser,
            dispenser_state_rx,
            pulse_rx,
            dispenser_ctrl_tx,
            dispenser_listen_addr,
        ),
        vessel_rfid_reader::spawn(vessel_card_rx, vessel_rfid_serial),
        operator_rfid_reader::spawn(operator_card_rx, operator_rfid_serial),
        // Actors.
        dock_operator::spawn(
            clock.clone(),
            start.dock_operator,
            peers_at_rest_rx,
            dock_operator_report,
            dock_job_rx,
            esd_tx,
            operator_card_tx,
            operator_rfid_ready,
            ticket_rx,
            bdn_sign_tx,
            bdn_signed_rx,
            recon_tx,
            dock_operator_local_addr,
            hmi_addr,
        ),
        tug_engineer::spawn(
            stop.clone(),
            tug_engineer_report,
            tug_arrival_rx,
            dock_job_tx.clone(),
            vessel_card_tx,
            vessel_rfid_ready,
            bdn_sign_rx,
            bdn_signed_tx,
        ),
        road_tanker_driver::spawn(
            clock.clone(),
            start.road_tanker_driver,
            stop.clone(),
            road_tanker_driver_report,
            // The tanker pump is the tank's only inflow source, so its pump
            // rate is the configured inflow rate (see `Config`).
            inflow_rate_l_per_min,
            driver_id,
            tanker_registration,
            fill_job_rx,
            dock_job_tx,
            tanker_pump_tx,
            interlock_plug_tx,
            ticket_tx,
        ),
        fuel_supplier::spawn(
            clock.clone(),
            start.fuel_supplier,
            stop.clone(),
            driver_at_rest_rx,
            fuel_supplier_report,
            fuel_supplier_local_addr,
            atg_relay_addr,
            poll_interval,
            reorder_threshold_l,
            delivery_volume_l,
            lead_time,
            fill_job_tx,
        ),
        reconciliation_user::spawn(
            clock.clone(),
            start.reconciliation_user,
            stop.clone(),
            dock_operator_at_rest_rx,
            reconciliation_user_report,
            recon_rx,
            reconciliation_user.local_addr,
            reconciliation_user.recon_addr,
            reconciliation_user.audit_period,
            reconciliation_user.first_audit_after,
            reconciliation_user.settling_delay,
            reconciliation_user.initial_window,
            reconciliation_user.width_floor,
            reconciliation_user.boundary_margin,
            reconciliation_user.clipboard_deadline,
            reconciliation_user.desktop_settle,
            reconciliation_user.terminal_settle,
            reconciliation_user.key_interval,
            reconciliation_user.connect_deadline,
        ),
        systems_administrator::spawn(
            clock.clone(),
            start.systems_administrator,
            stop,
            systems_administrator_report,
            systems_administrator_egress_addr,
            systems_administrator_managed_hosts,
            admin_check_interval,
            admin_step_deadline,
        ),
    ]);

    // Enrol each entity's handle in a supervised set. A `JoinHandle` is itself a
    // future resolving to the task's outcome, so spawning it yields a set whose
    // `join_next` surfaces the first entity to stop (and its panic payload).
    let mut set = JoinSet::new();
    for handle in handles {
        set.spawn(handle);
    }

    SimHandles {
        set,
        stop: stop_tx,
        reports: Some(Reports {
            tug_arrivals: tug_arrivals_rx,
            tug_engineer: tug_engineer_rx,
            road_tanker_driver: road_tanker_driver_rx,
            fuel_supplier: fuel_supplier_rx,
            dock_operator: dock_operator_rx,
            reconciliation_user: reconciliation_user_rx,
            systems_administrator: systems_administrator_rx,
            engineer_at_rest: engineer_at_rest_tx,
            driver_at_rest: driver_at_rest_tx,
            peers_at_rest: peers_at_rest_tx,
            dock_operator_at_rest: dock_operator_at_rest_tx,
        }),
        state_requests: StateRequests {
            fuel_tank: fuel_tank_state_tx,
            atg_probe: atg_probe_state_tx,
            atg_console: atg_console_state_tx,
            dispenser: dispenser_state_tx,
        },
        clock,
        parameters,
        _alarm_clear_tx: alarm_clear_tx,
    }
}

/// Each entity's starting state (sim.md §9.3).
struct Start {
    fuel_tank: FuelTankState,
    atg_probe: AtgProbeState,
    atg_console: AtgConsoleState,
    dispenser: DispenserState,
    tug_arrivals: TugArrivalState,
    dock_operator: DockOperatorState,
    road_tanker_driver: RoadTankerDriverState,
    fuel_supplier: FuelSupplierState,
    /// `None` on a cold start: the first window begins at the spawn.
    reconciliation_user: Option<ReconciliationUserState>,
    systems_administrator: SystemsAdministratorState,
}

impl Start {
    /// The states saved in a checkpoint.
    fn warm(checkpoint: Checkpoint) -> Self {
        Self {
            fuel_tank: checkpoint.fuel_tank,
            atg_probe: checkpoint.atg_probe,
            atg_console: checkpoint.atg_console,
            dispenser: checkpoint.dispenser,
            tug_arrivals: checkpoint.tug_arrivals,
            dock_operator: checkpoint.dock_operator,
            road_tanker_driver: checkpoint.road_tanker_driver,
            fuel_supplier: checkpoint.fuel_supplier,
            reconciliation_user: Some(checkpoint.reconciliation_user),
            systems_administrator: checkpoint.systems_administrator,
        }
    }

    /// The configured initial values (sim.md §1.2), with the start state's
    /// readings, numbers and times (sim-impl.md, "Start state"). The
    /// reconciliation user's first audit is placed at its spawn, from
    /// `ReconciliationUserConfig::first_audit_after`.
    fn cold(clock: &SimClock, config: &Config) -> Self {
        let now = clock.now_datetime();
        Self {
            fuel_tank: FuelTankState {
                volume_l: config.initial_volume_l,
                temperature_c: config.initial_temperature_c,
                bund_wet: false,
            },
            atg_probe: AtgProbeState::new(config.probe_noise_seed),
            atg_console: AtgConsoleState::default(),
            dispenser: DispenserState {
                cumulative_pulses: config.initial_cumulative_pulses,
                transaction_pulses: 0,
            },
            tug_arrivals: TugArrivalState::new(config.tug_seed, config.vessels),
            dock_operator: DockOperatorState {
                bdn_seq: config.first_bdn_number,
            },
            road_tanker_driver: RoadTankerDriverState {
                meter_totaliser_l: config.initial_meter_totaliser_l,
                ticket_seq: config.first_ticket_number,
            },
            fuel_supplier: FuelSupplierState::cold(
                now,
                config.poll_interval,
                config.supplier_start,
            ),
            reconciliation_user: None,
            systems_administrator: SystemsAdministratorState {
                next_check_at: now + config.admin_first_check_after,
                previous_check_at: None,
            },
        }
    }
}

/// Build and supervise the sim, from `checkpoint` if one was accepted. The
/// first entity to stop is fatal: log it and exit non-zero (the process exit
/// aborts the remaining tasks). On `SIGTERM`, bring the sim to rest, write the
/// checkpoint to `run_dir`, and exit with status 0. Never returns.
pub async fn run(clock: SimClock, config: Config, checkpoint: Option<Checkpoint>, run_dir: &Path) {
    let mut terminate =
        signal(SignalKind::terminate()).expect("orchestrator: cannot install the SIGTERM handler");
    let mut sim = build(clock, config, checkpoint);
    tokio::select! {
        cause = sim.await_first_failure() => fatal(&cause),
        _ = terminate.recv() => {}
    }

    sim.stop();
    let at_rest = match sim.come_to_rest().await {
        Ok(at_rest) => at_rest,
        Err(cause) => fatal(&cause),
    };
    if let Err(error) = checkpoint::write_at_rest(run_dir, &at_rest.rows, &at_rest.checkpoint) {
        fatal(&format!("cannot write the checkpoint: {error}"));
    }
    tracing::info!("sim at rest; checkpoint written");
    std::process::exit(0);
}

/// Log the single fatal event and exit non-zero.
fn fatal(cause: &str) -> ! {
    tracing::error!(%cause, "sim shutting down");
    std::process::exit(1);
}

/// Best-effort extraction of a panic's message.
fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        (*message).to_string()
    } else if let Some(message) = payload.downcast_ref::<String>() {
        message.clone()
    } else {
        "a sim entity panicked (non-string payload)".to_string()
    }
}
