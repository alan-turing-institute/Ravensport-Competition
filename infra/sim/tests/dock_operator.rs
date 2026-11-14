//! Integration tests for `sim::actors::dock_operator`.
//!
//! Exercises the actor through its public `spawn()` interface only, holding the
//! channel ends neighbouring entities own (see sim-tests.md). A hand-rolled mock
//! HMI HTTP server stands in for `hmi-01`: it captures the inbound request
//! sequence and returns scripted (or default) responses.
//!
//! The actor polls the HMI on a `clock.sleep` cadence, so the tests run under
//! `SimClock::isolated(TimeModel::Synthetic, …)` + `block_on`. Synthetic time
//! advances only when the runtime is quiescent, so each test reaches its
//! assertion by **awaiting an operator-produced channel signal** (which parks
//! the runtime and lets the poll sleeps auto-advance), then inspects the mock —
//! never by busy-polling.

mod mock_hmi;

use std::net::SocketAddr;

use time::macros::datetime;
use time::PrimitiveDateTime;
use tokio::sync::{mpsc, oneshot, watch};

use sim::actors::dock_operator::{
    self, DispenseHandoff, DockJob, DockOperatorState, JobAdmission, BDN,
};
use sim::actors::reconciliation_user::ReconciliationRecord;
use sim::actors::road_tanker_driver::DeliveryTicket;
use sim::actors::tug_engineer::VesselId;
use sim::clock::{SimClock, TimeModel};
use sim::physical::tug_arrival_generator::DispenseJob;
use sim::serial::ReaderReadiness;
use sim::shared::RfidCardNumber;

use mock_hmi::MockHmi;

const SIM_START: PrimitiveDateTime = datetime!(2025-01-01 0:00);

fn test_dispense_job() -> DispenseJob {
    DispenseJob {
        arrived_at: SIM_START,
        vessel_id: VesselId(1999997),
        vessel_name: "Tug Alpha",
        volume_l: 500.0,
        rfid_card: RfidCardNumber(99999),
    }
}

/// The jobs of two further tugs, for the tests of the order in which waiting
/// jobs are taken. Their volume is the first job's, which the mock totaliser's
/// default reports as dispensed.
fn tug_bravo_job() -> DispenseJob {
    DispenseJob {
        arrived_at: SIM_START,
        vessel_id: VesselId(1999985),
        vessel_name: "Tug Bravo",
        volume_l: 500.0,
        rfid_card: RfidCardNumber(99998),
    }
}

fn tug_charlie_job() -> DispenseJob {
    DispenseJob {
        arrived_at: SIM_START,
        vessel_id: VesselId(1999973),
        vessel_name: "Tug Charlie",
        volume_l: 500.0,
        rfid_card: RfidCardNumber(99997),
    }
}

/// Delivery volume forwarded on a `DockJob::Fill`.
const FILL_VOLUME_L: f64 = 10_000.0;

/// The arriving tanker's registration, forwarded on a `DockJob::Fill` and
/// printed on its ticket.
const TANKER_REGISTRATION: &str = "AB12 CDE";

fn test_delivery_ticket() -> DeliveryTicket {
    DeliveryTicket {
        ticket_number: "TICKET-77".to_string(),
        delivery_commenced_at: datetime!(2025-01-31 23:58:30),
        grade: "MGO".to_string(),
        gross_volume_l: 10_000.0,
        temperature_c: TICKET_TEMPERATURE_C,
        temp_corrected_volume_l: 9_950.0,
        density_kg_per_l: 0.84,
        start_totaliser_l: 1000.0,
        end_totaliser_l: 11_000.0,
        tanker_registration: TANKER_REGISTRATION.to_string(),
        driver_id: "driver-1".to_string(),
        signed_by_driver: true,
        signed_by_operator: false,
    }
}

/// Counterpart channel ends held by the test, plus the mock HMI.
struct Harness {
    job_tx: mpsc::Sender<DockJob>,
    operator_card_rx: mpsc::Receiver<RfidCardNumber>,
    ticket_tx: mpsc::Sender<DeliveryTicket>,
    bdn_sign_rx: mpsc::Receiver<DispenseHandoff>,
    bdn_signed_tx: mpsc::Sender<BDN>,
    recon_rx: mpsc::UnboundedReceiver<ReconciliationRecord>,
    mock: MockHmi,
    /// Fired to signal that the tug engineer and the road tanker driver have
    /// reported; `None` once fired.
    peers_at_rest: Option<oneshot::Sender<()>>,
    report_rx: oneshot::Receiver<DockOperatorState>,
    // Retained so the watch channel stays open for the actor's lifetime.
    _esd_rx: watch::Receiver<bool>,
}

fn spawn_operator(clock: SimClock) -> Harness {
    spawn_operator_numbering_from(clock, 1)
}

/// A dock operator whose first BDN takes `first_bdn_number` (sim-impl.md,
/// "Start state").
fn spawn_operator_numbering_from(clock: SimClock, first_bdn_number: u32) -> Harness {
    let mock = MockHmi::start();
    let local_addr: SocketAddr = ([127, 0, 0, 1], 0).into();

    let (job_tx, job_rx) = mpsc::channel(16);
    let (esd_tx, _esd_rx) = watch::channel(false);
    let (operator_card_tx, operator_card_rx) = mpsc::channel(16);
    let (ticket_tx, ticket_rx) = mpsc::channel(16);
    let (bdn_sign_tx, bdn_sign_rx) = mpsc::channel(16);
    let (bdn_signed_tx, bdn_signed_rx) = mpsc::channel(16);
    let (recon_tx, recon_rx) = mpsc::unbounded_channel();
    let (peers_at_rest, peers_at_rest_rx) = oneshot::channel();
    let (report_tx, report_rx) = oneshot::channel();

    dock_operator::spawn(
        clock,
        DockOperatorState {
            bdn_seq: first_bdn_number,
        },
        peers_at_rest_rx,
        report_tx,
        job_rx,
        esd_tx,
        operator_card_tx,
        ReaderReadiness::always_ready(),
        ticket_rx,
        bdn_sign_tx,
        bdn_signed_rx,
        recon_tx,
        local_addr,
        mock.addr,
    );

    Harness {
        job_tx,
        operator_card_rx,
        ticket_tx,
        bdn_sign_rx,
        bdn_signed_tx,
        recon_rx,
        mock,
        peers_at_rest: Some(peers_at_rest),
        report_rx,
        _esd_rx,
    }
}

/// Send a dispense job, returning the proceed receiver the operator fires with
/// the FMS's admission.
async fn send_dispense(harness: &Harness) -> oneshot::Receiver<JobAdmission> {
    send_dispense_job(harness, test_dispense_job()).await
}

/// Send `job` as a dispense job, returning the proceed receiver the operator
/// fires with the FMS's admission.
async fn send_dispense_job(harness: &Harness, job: DispenseJob) -> oneshot::Receiver<JobAdmission> {
    let (proceed_tx, proceed_rx) = oneshot::channel();
    harness
        .job_tx
        .send(DockJob::Dispense(job, proceed_tx))
        .await
        .unwrap();
    proceed_rx
}

/// Await the next BDN the operator offers for signing, co-sign it and send it
/// back, as the tug engineer does.
async fn cosign_next_bdn(harness: &mut Harness) {
    let mut bdn = to_sign(
        harness
            .bdn_sign_rx
            .recv()
            .await
            .expect("the operator closed the BDN-signing channel"),
    );
    bdn.signed_by_engineer = true;
    harness.bdn_signed_tx.send(bdn).await.unwrap();
}

/// Send a fill job, returning the proceed receiver the operator fires.
async fn send_fill(harness: &Harness) -> oneshot::Receiver<()> {
    let (proceed_tx, proceed_rx) = oneshot::channel();
    harness
        .job_tx
        .send(DockJob::Fill(
            FILL_VOLUME_L,
            TANKER_REGISTRATION.to_string(),
            proceed_tx,
        ))
        .await
        .unwrap();
    proceed_rx
}

/// The BDN in a handoff the test expects to be a request to co-sign.
fn to_sign(handoff: DispenseHandoff) -> BDN {
    match handoff {
        DispenseHandoff::Sign(bdn) => bdn,
        DispenseHandoff::Refused => panic!("expected a BDN to co-sign, got a refusal"),
    }
}

/// Index of the first request matching `method` and `path` in a snapshot.
fn index_of(requests: &[mock_hmi::Recorded], method: &str, path: &str) -> Option<usize> {
    requests
        .iter()
        .position(|r| r.method == method && r.path == path)
}

fn count_of(requests: &[mock_hmi::Recorded], method: &str, path: &str) -> usize {
    requests
        .iter()
        .filter(|r| r.method == method && r.path == path)
        .count()
}

/// Parse the JSON body of the first request matching `method` and `path`.
fn body_of(requests: &[mock_hmi::Recorded], method: &str, path: &str) -> serde_json::Value {
    let recorded = requests
        .iter()
        .find(|r| r.method == method && r.path == path)
        .unwrap_or_else(|| panic!("no {method} {path} request was recorded"));
    serde_json::from_str(&recorded.body).expect("request body is valid JSON")
}

// ---------------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------------

#[test]
fn login_sends_rfid_card_after_password_confirmation() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // Hold the challenge long-poll open so it cannot resolve.
        let _gate = harness.mock.gate("GET", "/api/auth/challenge/chal-1");

        // The card must be presented in response to the challenge — after the
        // password 202 — and before the long-poll resolves (it never does here).
        let card = harness.operator_card_rx.recv().await;
        assert_eq!(card, Some(RfidCardNumber(1_000_002)));

        // The login POST must precede the card.
        let requests = harness.mock.requests();
        assert!(index_of(&requests, "POST", "/api/auth/login").is_some());
    });
}

// ---------------------------------------------------------------------------
// Session re-authentication (session-expiry recovery)
// ---------------------------------------------------------------------------

#[test]
fn reauthenticates_and_retries_after_session_expiry_401() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The FMS session expires mid-dispense: the first totaliser
        // poll is rejected with 401 (exactly the deployed failure). The operator
        // must log in again and retry the request, not treat the 401 as fatal.
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current/dispenser/totaliser",
            401,
            r#"{"error":"unauthorised"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        // The BDN is offered only once the dispense completes, which is reachable
        // only if the operator recovered from the 401 and finished the totaliser
        // poll. Had it panicked instead, the BDN sender drops and recv yields None.
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.expect(
            "operator did not recover from the 401 — it panicked instead of re-authenticating",
        ));

        // Recovery is a second login: the startup login plus one re-authentication.
        let requests = harness.mock.requests();
        assert_eq!(
            count_of(&requests, "POST", "/api/auth/login"),
            2,
            "operator should re-authenticate exactly once after a 401"
        );
    });
}

// ---------------------------------------------------------------------------
// Dispense lifecycle
// ---------------------------------------------------------------------------

#[test]
fn dispense_job_fires_proceed_after_hmi_confirms_active() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        let proceed_rx = send_dispense(&harness).await;
        // Fires once POST /api/jobs/refuel returns 201.
        assert_eq!(proceed_rx.await.unwrap(), JobAdmission::Active);
    });
}

#[test]
fn pump_start_deferred_until_rfid_validated() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // First job-status poll returns pending; the default (rfid_validated)
        // answers the second.
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"pending_vessel_rfid"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        // The BDN is offered only after start + a complete dispense, so its
        // arrival is a sync point past the pump start.
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());

        let requests = harness.mock.requests();
        let start = index_of(&requests, "POST", "/api/jobs/job-1/dispenser/start").unwrap();
        let polls_before_start = requests[..start]
            .iter()
            .filter(|r| r.method == "GET" && r.path == "/api/jobs/current")
            .count();
        assert!(
            polls_before_start >= 2,
            "pump started after only {polls_before_start} status poll(s); must wait for rfid_validated"
        );
    });
}

#[test]
fn bdn_deferred_until_dispense_complete() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // First totaliser poll reports dispensing; the default (complete)
        // answers the second.
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current/dispenser/totaliser",
            200,
            r#"{"job_id":"job-1","dispensed_litres":250.0,"preset_litres":500.0,"status":"dispensing"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());

        let requests = harness.mock.requests();
        let totaliser_polls =
            count_of(&requests, "GET", "/api/jobs/current/dispenser/totaliser");
        assert!(
            totaliser_polls >= 2,
            "BDN offered after only {totaliser_polls} totaliser poll(s); must wait for complete"
        );
    });
}

#[test]
fn dispense_job_sends_bdn_for_signing_on_completion() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;

        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        assert_eq!(bdn.vessel_id, VesselId(1999997));
        assert_eq!(bdn.vessel_name, "Tug Alpha");
        assert_eq!(bdn.volume_l, 500.0);
        assert!(bdn.signed_by_operator);
        assert!(!bdn.signed_by_engineer);
    });
}

#[test]
fn the_first_bdn_is_numbered_1_by_default() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;

        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        assert_eq!(bdn.bdn_number, "BDN-000001");
    });
}

#[test]
fn bdns_are_numbered_from_the_start_state() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator_numbering_from(clock, 37);
        let _proceed = send_dispense(&harness).await;

        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        assert_eq!(bdn.bdn_number, "BDN-000037");
    });
}

#[test]
fn bdn_number_entered_via_hmi_after_cosigning() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;

        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        let number = bdn.bdn_number.clone();
        let mut cosigned = bdn;
        cosigned.signed_by_engineer = true;
        harness.bdn_signed_tx.send(cosigned).await.unwrap();

        // The reconciliation post follows the PATCH, so it is a sync point past it.
        let _record = harness.recon_rx.recv().await.unwrap();

        let requests = harness.mock.requests();
        let patch = requests
            .iter()
            .find(|r| r.method == "PATCH" && r.path == "/api/jobs/job-1")
            .expect("PATCH /api/jobs/job-1 not received");
        let body: serde_json::Value = serde_json::from_str(&patch.body).unwrap();
        assert_eq!(body["bdn_number"], serde_json::Value::String(number));
    });
}

#[test]
fn dispense_job_posts_reconciliation_record_after_bdn_returned() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;

        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        let mut cosigned = bdn;
        cosigned.signed_by_engineer = true;
        harness.bdn_signed_tx.send(cosigned.clone()).await.unwrap();

        let record = harness.recon_rx.recv().await.unwrap();
        assert_eq!(record, ReconciliationRecord::Bdn(cosigned));
    });
}

#[test]
fn authenticated_requests_carry_bearer_token_login_does_not() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;
        // Reaching the BDN-signing step means login and several authenticated
        // requests have completed.
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());

        let requests = harness.mock.requests();
        let login = requests
            .iter()
            .find(|r| r.method == "POST" && r.path == "/api/auth/login")
            .unwrap();
        assert_eq!(login.authorization, None, "login is pre-authentication");

        let refuel = requests
            .iter()
            .find(|r| r.method == "POST" && r.path == "/api/jobs/refuel")
            .unwrap();
        assert_eq!(
            refuel.authorization.as_deref(),
            Some("Bearer token-1"),
            "authenticated requests must carry the session token"
        );
    });
}

// ---------------------------------------------------------------------------
// BDN fields (sim.md §8.2–§8.4)
// ---------------------------------------------------------------------------

mod bdn_fields {
    use super::*;
    use sim::constants::{
        DENSITY_KG_PER_L, FLASHPOINT_C, FUEL_GRADE, OPERATOR_USERNAME, SULPHUR_PCT_MM,
    };

    /// Two minutes before midnight on the last day of January.
    const BEFORE_MIDNIGHT: PrimitiveDateTime = datetime!(2025-01-31 23:58);

    /// The FMS's dispense start, as the mock HMI reports it: two minutes before
    /// midnight, with nanoseconds the sim clock cannot produce.
    const FMS_OPENED_AT: &str = "2025-01-31T23:58:00.123456789Z";

    /// The current job once the dispense is complete, carrying the FMS's start.
    fn completed_job(opened_at: &str) -> String {
        format!(r#"{{"job_id":"job-1","status":"awaiting_bdn","opened_at":{opened_at}}}"#)
    }

    /// The vessel-tag gate poll: validated, not yet started.
    fn gate_poll(harness: &Harness) {
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"rfid_validated","opened_at":null}"#,
        );
    }

    /// The BDN is created at completion, so an operator reading the clock there
    /// would write the completion time into a field named for commencement, and
    /// one reading its own clock at the start would precede the FMS's record of
    /// it. The commencement is the FMS's start, read from the HMI (sim.md §8.3,
    /// narrative.md §2.2).
    #[test]
    fn the_bdn_records_commencement_not_creation() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, BEFORE_MIDNIGHT);
        runtime.block_on(async {
            let mut harness = spawn_operator(clock.clone());
            gate_poll(&harness);
            // Enough polls report the dispense still running to hold completion
            // until after midnight, at the compiled poll interval, which a
            // compressed scenario shortens; the default `complete` answers the
            // next.
            let to_midnight = (datetime!(2025-02-01 0:00) - BEFORE_MIDNIGHT).unsigned_abs();
            let polls = to_midnight.div_duration_f64(sim::constants::DOCK_OPERATOR_POLL_INTERVAL)
                as usize
                + 1;
            for _ in 0..polls {
                harness.mock.enqueue(
                    "GET",
                    "/api/jobs/current/dispenser/totaliser",
                    200,
                    r#"{"job_id":"job-1","dispensed_litres":250.0,"preset_litres":500.0,"status":"dispensing"}"#,
                );
            }
            harness.mock.enqueue(
                "GET",
                "/api/jobs/current",
                200,
                &completed_job(&format!("{FMS_OPENED_AT:?}")),
            );

            let _proceed = send_dispense(&harness).await;
            let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
            let completed = clock.now_datetime();

            assert_eq!(
                completed.date(),
                datetime!(2025-02-01 0:00).date(),
                "the dispense must complete after midnight for this test to discriminate"
            );
            assert_eq!(
                bdn.delivery_commenced_at,
                datetime!(2025-01-31 23:58:00.123456789),
                "delivery_commenced_at holds {}, not the FMS's dispense start",
                bdn.delivery_commenced_at
            );
        });
    }

    /// The start response was lost, so the retry met the running job: the
    /// commencement is still the FMS's, read from the job rather than from the
    /// start response (sim.md §8.3).
    #[test]
    fn the_bdn_commencement_is_the_fms_start_after_a_retried_start() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, BEFORE_MIDNIGHT);
        runtime.block_on(async {
            let mut harness = spawn_operator(clock);
            gate_poll(&harness);
            harness.mock.enqueue(
                "POST",
                "/api/jobs/job-1/dispenser/start",
                409,
                r#"{"error":"job is not in active state"}"#,
            );
            // The reconcile poll after the 409 finds the committed start.
            harness.mock.enqueue(
                "GET",
                "/api/jobs/current",
                200,
                &format!(
                    r#"{{"job_id":"job-1","status":"dispensing","opened_at":{FMS_OPENED_AT:?}}}"#
                ),
            );
            harness.mock.enqueue(
                "GET",
                "/api/jobs/current",
                200,
                &completed_job(&format!("{FMS_OPENED_AT:?}")),
            );

            let _proceed = send_dispense(&harness).await;
            let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
            assert_eq!(
                bdn.delivery_commenced_at,
                datetime!(2025-01-31 23:58:00.123456789)
            );
        });
    }

    /// A completed dispense whose job reports no start breaks the FMS's contract
    /// (fms.md §7.4): the note cannot be dated, so no BDN is offered.
    #[test]
    fn a_completed_dispense_without_an_fms_start_is_fatal() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let mut harness = spawn_operator(clock);
            gate_poll(&harness);
            harness
                .mock
                .enqueue("GET", "/api/jobs/current", 200, &completed_job("null"));

            let _proceed = send_dispense(&harness).await;
            // The operator panics: its BDN sender drops and recv yields None.
            assert!(
                harness.bdn_sign_rx.recv().await.is_none(),
                "a dispense with no recorded start must not produce a BDN"
            );
        });
    }

    /// Every MARPOL Annex VI Appendix V item is on the note (sim.md §8.2). The
    /// fuel's properties and the operator's identity are compared with the
    /// compiled world constants rather than literals: a change of fuel in
    /// world.yaml must not break this test, and a literal in the actor must.
    #[test]
    fn the_bdn_carries_the_appendix_v_fields() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let mut harness = spawn_operator(clock);
            let _proceed = send_dispense(&harness).await;
            let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());

            // Items 2 and 4: the port, and the port as supplier.
            assert_eq!(bdn.port, "Ravensport");
            assert_eq!(bdn.supplier_name, "Ravensport Harbour Authority");
            assert_eq!(
                bdn.supplier_address,
                "Fuel Dock, East Quay, Ravensport RV1 4QD"
            );
            assert_eq!(bdn.supplier_telephone, "01632 960482");

            // Items 5, 7, 8 and 9: the single grade's properties.
            assert_eq!(bdn.grade, FUEL_GRADE);
            assert_eq!(bdn.density_kg_per_m3, DENSITY_KG_PER_L * 1000.0);
            assert_eq!(bdn.sulphur_pct_mm, SULPHUR_PCT_MM);
            assert_eq!(bdn.flashpoint_c, FLASHPOINT_C);

            // Item 6: the mass, from the volume corrected to 15 °C. The mock
            // reports 15 °C, at which the correction is identity (sim.md §1.3).
            assert_eq!(bdn.temp_corrected_volume_l, bdn.volume_l);
            assert_eq!(
                bdn.quantity_t,
                bdn.temp_corrected_volume_l * DENSITY_KG_PER_L / 1000.0
            );

            // Item 10: the regulation 14.1 declaration, signed by the operator
            // as the supplier's representative.
            assert!(bdn.sulphur_limit_declared);
            assert!(bdn.signed_by_operator);

            // The audit compares this with the `operator` on the job's log
            // events, which the FMS takes from the logged-in username.
            assert_eq!(bdn.operator_id, OPERATOR_USERNAME);
        });
    }

    /// The port's one temperature sensor reaches the operator only through the
    /// HMI (sim.md §8.4.1). 17.5 °C is deliberately not the modelled 15 °C, so
    /// a constant in the actor cannot pass. The corrected volume is not
    /// asserted: no thermal model is defined away from 15 °C (sim.md §1.3).
    #[test]
    fn the_bdn_temperature_is_read_from_the_hmi() {
        let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
        runtime.block_on(async {
            let mut harness = spawn_operator(clock);
            harness.mock.enqueue(
                "GET",
                "/api/tanks/01/level",
                200,
                r#"{"tank_id":"01","level_mm":5000.0,"volume_litres":35000.0,"ullage_litres":15000.0,"temperature_c":17.5,"timestamp":"2025-01-01T00:00:00Z"}"#,
            );

            let _proceed = send_dispense(&harness).await;
            let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());

            assert_eq!(bdn.temperature_c, 17.5);

            let requests = harness.mock.requests();
            let complete = requests
                .iter()
                .rposition(|r| {
                    r.method == "GET" && r.path == "/api/jobs/current/dispenser/totaliser"
                })
                .expect("no totaliser poll was recorded");
            let level = index_of(&requests, "GET", "/api/tanks/01/level")
                .expect("the temperature was not read from the HMI");
            assert!(
                level > complete,
                "the temperature must be read after the dispense is complete"
            );
        });
    }
}

// ---------------------------------------------------------------------------
// Fill lifecycle
// ---------------------------------------------------------------------------

#[test]
fn level_checked_before_fill_job_created() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        // Proceed fires after the tank-fill POST, so both requests are recorded.
        proceed_rx.await.unwrap();

        let requests = harness.mock.requests();
        let level = index_of(&requests, "GET", "/api/tanks/01/level").expect("level not checked");
        let fill = index_of(&requests, "POST", "/api/jobs/tank-fill").expect("fill not created");
        assert!(
            level < fill,
            "level must be checked before the fill job is created"
        );
    });
}

#[test]
fn fill_job_fires_proceed_after_hmi_confirms_active() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        assert!(proceed_rx.await.is_ok());
    });
}

#[test]
fn fill_job_posts_reconciliation_record_after_ticket_received() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        proceed_rx.await.unwrap();

        harness
            .ticket_tx
            .send(test_delivery_ticket())
            .await
            .unwrap();
        let record = harness.recon_rx.recv().await.unwrap();
        match record {
            ReconciliationRecord::DeliveryTicket(ticket) => {
                assert_eq!(ticket.ticket_number, "TICKET-77");
                assert!(
                    ticket.signed_by_operator,
                    "operator must counter-sign the ticket"
                );
            }
            other => panic!("expected a delivery-ticket record, got {other:?}"),
        }
    });
}

// ---------------------------------------------------------------------------
// Sequencing
// ---------------------------------------------------------------------------

#[test]
fn jobs_processed_sequentially() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // First job needs two poll cycles each for RFID and totaliser.
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"pending_vessel_rfid"}"#,
        );
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current/dispenser/totaliser",
            200,
            r#"{"job_id":"job-1","dispensed_litres":250.0,"preset_litres":500.0,"status":"dispensing"}"#,
        );

        let proceed1 = send_dispense(&harness).await;
        let mut proceed2 = send_dispense(&harness).await;

        proceed1.await.unwrap();

        // First job runs to the BDN-signing step.
        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        // The second job must not have started: its proceed is still pending.
        assert!(
            matches!(
                proceed2.try_recv(),
                Err(oneshot::error::TryRecvError::Empty)
            ),
            "second job started before the first completed"
        );

        // Co-sign so the first job finishes (PATCH + reconciliation post).
        let mut cosigned = bdn;
        cosigned.signed_by_engineer = true;
        harness.bdn_signed_tx.send(cosigned).await.unwrap();

        // Only now does the second job's proceed fire.
        assert!(proceed2.await.is_ok());
        let requests = harness.mock.requests();
        assert!(
            index_of(&requests, "PATCH", "/api/jobs/job-1").is_some(),
            "first job's BDN number must have been entered before the second job started"
        );
    });
}

/// The requests in a snapshot that create a job, `POST /api/jobs/refuel` and
/// `POST /api/jobs/tank-fill`, in order, each with its index.
fn job_creates(requests: &[mock_hmi::Recorded]) -> Vec<(usize, &str)> {
    requests
        .iter()
        .enumerate()
        .filter(|(_, r)| {
            r.method == "POST" && (r.path == "/api/jobs/refuel" || r.path == "/api/jobs/tank-fill")
        })
        .map(|(i, r)| (i, r.path.as_str()))
        .collect()
}

/// A road tanker waiting to deliver is handled before a waiting tug, although
/// the tug arrived first, and the job in progress is completed first
/// (narrative §2.2; `sim.md` §6).
#[test]
fn waiting_fill_taken_before_waiting_dispense() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // DispenseJobActive.
        send_dispense(&harness).await.await.unwrap();

        // A tug, then a road tanker, arrive while the first dispense is under
        // way.
        let mut dispense_proceed = send_dispense_job(&harness, tug_bravo_job()).await;
        let mut fill_proceed = send_fill(&harness).await;

        // The rest of the first dispense, on the mock's defaults: RFID
        // validation, completion and the BDN exchange.
        cosign_next_bdn(&mut harness).await;

        // The waiting job the operator takes next is the first whose proceed
        // signal fires; the other's cannot fire until that job is complete.
        tokio::select! {
            biased;
            fired = &mut fill_proceed => {
                fired.expect("the operator dropped the fill's proceed signal");
            }
            fired = &mut dispense_proceed => {
                fired.expect("the operator dropped the dispense's proceed signal");
                panic!("the waiting dispense was taken before the waiting fill");
            }
        }

        // Complete the fill; the waiting dispense is taken after it.
        harness
            .ticket_tx
            .send(test_delivery_ticket())
            .await
            .unwrap();
        dispense_proceed
            .await
            .expect("the operator dropped the dispense's proceed signal");

        let requests = harness.mock.requests();
        let creates = job_creates(&requests);
        let paths: Vec<&str> = creates.iter().map(|&(_, path)| path).collect();
        assert_eq!(
            paths,
            [
                "/api/jobs/refuel",
                "/api/jobs/tank-fill",
                "/api/jobs/refuel"
            ],
            "the fill must be created before the waiting dispense"
        );
        let patch = index_of(&requests, "PATCH", "/api/jobs/job-1")
            .expect("the first dispense's BDN number was not entered");
        assert!(
            patch < creates[1].0,
            "the first dispense's BDN number must be entered before any further job is created"
        );
        let second: serde_json::Value = serde_json::from_str(&requests[creates[2].0].body).unwrap();
        assert_eq!(
            second["vessel_id"], "1999985",
            "the last job created must be the waiting tug's"
        );
    });
}

/// Waiting tugs are handled in the order in which they arrived (narrative
/// §2.2; `sim.md` §6).
#[test]
fn waiting_dispenses_taken_in_order_received() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // DispenseJobActive.
        send_dispense(&harness).await.await.unwrap();

        // Two further tugs arrive while the first dispense is under way.
        let bravo_proceed = send_dispense_job(&harness, tug_bravo_job()).await;
        let charlie_proceed = send_dispense_job(&harness, tug_charlie_job()).await;

        // Each dispense ends in its BDN exchange, the next starting after it.
        for _ in 0..3 {
            cosign_next_bdn(&mut harness).await;
        }
        bravo_proceed
            .await
            .expect("the operator dropped Tug Bravo's proceed signal");
        charlie_proceed
            .await
            .expect("the operator dropped Tug Charlie's proceed signal");

        let requests = harness.mock.requests();
        let vessels: Vec<String> = requests
            .iter()
            .filter(|r| r.method == "POST" && r.path == "/api/jobs/refuel")
            .map(|r| {
                let body: serde_json::Value = serde_json::from_str(&r.body).unwrap();
                body["vessel_id"].as_str().unwrap().to_string()
            })
            .collect();
        assert_eq!(
            vessels,
            ["1999997", "1999985", "1999973"],
            "waiting tugs must be handled in the order in which they arrived"
        );
    });
}

// ---------------------------------------------------------------------------
// Request bodies
// ---------------------------------------------------------------------------

#[test]
fn login_request_carries_credentials() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The card is presented after the login POST, so its arrival confirms
        // the login request was sent.
        harness.operator_card_rx.recv().await.unwrap();

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/auth/login");
        // Credentials are projected from world.yaml (via sim-constants.toml),
        // so assert against the generated constants rather than a literal.
        assert_eq!(body["username"], sim::constants::OPERATOR_USERNAME);
        assert_eq!(body["password"], sim::constants::OPERATOR_PASSWORD);
    });
}

#[test]
fn refuel_request_carries_job_fields() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        let proceed_rx = send_dispense(&harness).await;
        proceed_rx.await.unwrap(); // fires after the refuel POST

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/jobs/refuel");
        assert_eq!(body["vessel_id"], "1999997");
        assert_eq!(body["product"], "MGO");
        assert_eq!(body["requested_volume_litres"], 500.0);
    });
}

#[test]
fn dispenser_start_request_carries_preset_volume() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;
        // The BDN offer is a sync point past the pump start.
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/jobs/job-1/dispenser/start");
        assert_eq!(body["preset_volume_litres"], 500.0);
    });
}

#[test]
fn tank_fill_request_carries_delivery_fields() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        proceed_rx.await.unwrap(); // fires after the tank-fill POST

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/jobs/tank-fill");
        assert_eq!(body["product"], "MGO");
        assert_eq!(body["delivery_volume_litres"], 10_000.0);
    });
}

/// The job's counterparty is the tanker delivering, whose registration is
/// printed on its ticket (narrative.md §2.1); a registration of the operator's
/// own would make every clean fill disagree with its paper.
#[test]
fn the_fill_job_names_the_arriving_tanker() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        proceed_rx.await.unwrap(); // fires after the tank-fill POST

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/jobs/tank-fill");
        assert_eq!(body["tanker_registration"], TANKER_REGISTRATION);
    });
}

#[test]
fn delivery_ticket_transcription_carries_ticket_fields() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        proceed_rx.await.unwrap();

        harness
            .ticket_tx
            .send(test_delivery_ticket())
            .await
            .unwrap();
        // The reconciliation post is a sync point past the transcription POST.
        harness.recon_rx.recv().await.unwrap();

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/delivery-tickets");
        assert_eq!(body["ticket_number"], "TICKET-77");
        assert_eq!(body["delivered_volume_litres"], 10_000.0);
        assert_eq!(body["supplier"], "Ravensport Fuels");
        assert_eq!(body["tanker_registration"], TANKER_REGISTRATION);
    });
}

/// The tanker's own temperature on `test_delivery_ticket()`. Deliberately not
/// 15 °C: the shore tank is held at 15 °C, so only a different value shows the
/// operator transcribes the ticket's figure rather than the shore reading the
/// HMI displays (sim.md §8.4.1).
const TICKET_TEMPERATURE_C: f64 = 12.0;

/// The dock operator transcribes every ticket field the HMI takes (hmi.md
/// §7.4) — exactly that set — including the tanker's own measurements and its
/// start time. The start is written as RFC 3339 in UTC with `Z` (sim.md §2.1);
/// the FMS records it as the fill's `opened_at`, so a missing or local time
/// would put the record in the wrong audit window.
#[test]
fn delivery_ticket_transcription_carries_the_full_ticket() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let proceed_rx = send_fill(&harness).await;
        proceed_rx.await.unwrap();

        harness
            .ticket_tx
            .send(test_delivery_ticket())
            .await
            .unwrap();
        // The reconciliation post is a sync point past the transcription POST.
        harness.recon_rx.recv().await.unwrap();

        let requests = harness.mock.requests();
        let body = body_of(&requests, "POST", "/api/delivery-tickets");

        let mut keys: Vec<&str> = body
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        let mut expected = vec![
            "job_id",
            "ticket_number",
            "delivery_commenced_at",
            "delivered_volume_litres",
            "temperature_c",
            "density_kg_per_l",
            "temp_corrected_volume_litres",
            "supplier",
            "tanker_registration",
        ];
        expected.sort_unstable();
        assert_eq!(keys, expected);

        // The fixture's start is 23:58:30 on 31 January — before midnight at a
        // month end, the case the transcribed start exists for (sim.md §8.3).
        assert_eq!(body["delivery_commenced_at"], "2025-01-31T23:58:30Z");
        assert_eq!(body["temperature_c"], TICKET_TEMPERATURE_C);
        assert_eq!(body["density_kg_per_l"], 0.84);
        assert_eq!(body["temp_corrected_volume_litres"], 9_950.0);
    });
}

// ---------------------------------------------------------------------------
// Transient-failure retry
// ---------------------------------------------------------------------------

#[test]
fn transient_server_error_is_retried_then_recovers() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        // The first refuel attempt fails with HTTP 500. A 500 from the FMS create
        // rolls back the job slot, so the operator's reconciling GET finds no live
        // job (404) and re-POSTs; the retry hits the default 201 and the job
        // proceeds. The backoff sleeps on the sim clock, which auto-advances under
        // Synthetic time.
        harness.mock.enqueue("POST", "/api/jobs/refuel", 500, "{}");
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            404,
            r#"{"error":"no active job"}"#,
        );

        let proceed_rx = send_dispense(&harness).await;
        assert!(
            proceed_rx.await.is_ok(),
            "operator must recover from a transient 500 and create the job"
        );

        // Exactly two refuel attempts: the failed one and the recovery.
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "POST", "/api/jobs/refuel"), 2);
    });
}

#[test]
fn ambiguous_create_adopts_already_committed_job() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        // Reproduces the incident: the first create committed in the FMS but its
        // response was lost in transit, so the retried POST collides with the
        // single-job slot and returns 409 "a job is already active". Rather than
        // treat that 409 as fatal, the operator reconciles against
        // GET /api/jobs/current — which reports the live job it just created (the
        // mock default) — and adopts it without re-POSTing.
        harness.mock.enqueue(
            "POST",
            "/api/jobs/refuel",
            409,
            r#"{"error":"a job is already active"}"#,
        );

        let proceed_rx = send_dispense(&harness).await;
        assert!(
            proceed_rx.await.is_ok(),
            "operator must adopt the committed job after a 409 instead of crashing"
        );

        // Exactly one refuel POST: the 409 is reconciled, not retried into another
        // create.
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "POST", "/api/jobs/refuel"), 1);
    });
}

#[test]
fn ambiguous_dispense_start_adopts_running_job() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The lost-response hazard on the non-idempotent Active -> Running
        // transition: the first dispenser/start committed in the FMS but its
        // response was lost, so the retried POST meets the already-Running job and
        // is rejected with 409 "job is not in active state". The operator must
        // reconcile against GET /api/jobs/current and adopt the committed dispense
        // rather than crash. The gate poll answers rfid_validated (the start is
        // permitted); the reconcile poll after the 409 answers dispensing (the
        // committed transition).
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"rfid_validated"}"#,
        );
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"dispensing"}"#,
        );
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            409,
            r#"{"error":"job is not in active state"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        // The BDN is offered only after a start the operator treated as successful
        // and a complete dispense; its arrival as a BDN to co-sign proves the 409
        // was adopted, neither fatal nor taken as a refusal.
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.expect(
            "operator must adopt the already-running dispense after a 409 instead of crashing",
        ));

        // Exactly one start POST: the 409 is reconciled, not retried into another
        // start.
        let requests = harness.mock.requests();
        assert_eq!(
            count_of(&requests, "POST", "/api/jobs/job-1/dispenser/start"),
            1
        );
    });
}

#[test]
fn ambiguous_dispense_start_adopts_completed_dispense() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The dispense completed within the lost-response window, so the reconcile
        // poll reports awaiting_bdn rather than dispensing. That state is reachable
        // only by having passed through the Active -> Running transition, so the
        // start committed and must be adopted.
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"rfid_validated"}"#,
        );
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"awaiting_bdn"}"#,
        );
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            409,
            r#"{"error":"job is not in active state"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        let _bdn = to_sign(harness.bdn_sign_rx.recv().await.expect(
            "operator must adopt a dispense that completed during the lost-response window",
        ));

        let requests = harness.mock.requests();
        assert_eq!(
            count_of(&requests, "POST", "/api/jobs/job-1/dispenser/start"),
            1
        );
    });
}

#[test]
fn dispense_start_409_without_committed_transition_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // A 409 whose reconcile does not show the job past the Active -> Running
        // transition is a genuine anomaly, not a lost response, and must stay
        // fatal. Here both the gate poll and the reconcile poll use the default
        // rfid_validated (the job is still Active): the start did not commit.
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            409,
            r#"{"error":"job is not in active state"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        // The operator panics on the unrecoverable 409: its BDN sender drops and
        // recv yields None. A wrongly adopted 409 would instead drive the dispense
        // to completion and offer a BDN.
        assert!(
            harness.bdn_sign_rx.recv().await.is_none(),
            "operator must treat a 409 with no committed transition as fatal"
        );
    });
}

#[test]
fn ambiguous_bdn_link_adopts_closed_job() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The lost-response hazard on the BDN-linking PATCH, which closes the job:
        // the first PATCH committed in the FMS but its response was lost, so the
        // retried PATCH meets the closed job and is rejected with 409 "a BDN is
        // already linked". The operator must reconcile against GET /api/jobs/current
        // and adopt the committed link rather than crash. The gate poll answers
        // rfid_validated; the dispense-start read after completion answers
        // awaiting_bdn with the start; the reconcile poll after the 409 answers
        // complete (the closed job).
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"rfid_validated"}"#,
        );
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"awaiting_bdn","opened_at":"2025-01-01T00:00:00.5Z"}"#,
        );
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"complete"}"#,
        );
        harness.mock.enqueue(
            "PATCH",
            "/api/jobs/job-1",
            409,
            r#"{"error":"a BDN is already linked"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        let mut cosigned = bdn;
        cosigned.signed_by_engineer = true;
        harness.bdn_signed_tx.send(cosigned).await.unwrap();

        // The reconciliation post follows the PATCH, so its arrival proves the 409
        // was adopted, not fatal.
        let _record =
            harness.recon_rx.recv().await.expect(
                "operator must adopt the already-linked BDN after a 409 instead of crashing",
            );

        // Exactly one PATCH: the 409 is reconciled, not retried into another link.
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "PATCH", "/api/jobs/job-1"), 1);
    });
}

#[test]
fn bdn_link_409_without_close_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // A 409 whose reconcile does not show the job closed is a genuine anomaly,
        // not a lost response, and must stay fatal. Here the reconcile poll uses the
        // default rfid_validated (the job is not complete).
        harness.mock.enqueue(
            "PATCH",
            "/api/jobs/job-1",
            409,
            r#"{"error":"a BDN is already linked"}"#,
        );

        let _proceed = send_dispense(&harness).await;
        let bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
        let mut cosigned = bdn;
        cosigned.signed_by_engineer = true;
        harness.bdn_signed_tx.send(cosigned).await.unwrap();

        // The operator panics on the unrecoverable 409: its reconciliation sender
        // drops and recv yields None. A wrongly adopted 409 would post the record.
        assert!(
            harness.recon_rx.recv().await.is_none(),
            "operator must treat a BDN-link 409 with no committed close as fatal"
        );
    });
}

#[test]
fn ambiguous_fill_close_adopts_closed_job() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The same hazard on the fill flow: transcribing the delivery ticket closes
        // the fill job, so a retry after a lost response meets the closed job and is
        // rejected with 409 "fill job is not active". The reconcile poll answers
        // complete (the closed job), confirming the close committed.
        harness.mock.enqueue(
            "GET",
            "/api/jobs/current",
            200,
            r#"{"job_id":"job-1","status":"complete"}"#,
        );
        harness.mock.enqueue(
            "POST",
            "/api/delivery-tickets",
            409,
            r#"{"error":"fill job is not active"}"#,
        );

        let proceed_rx = send_fill(&harness).await;
        proceed_rx.await.unwrap();
        harness
            .ticket_tx
            .send(test_delivery_ticket())
            .await
            .unwrap();

        let _record =
            harness.recon_rx.recv().await.expect(
                "operator must adopt the already-closed fill after a 409 instead of crashing",
            );

        // Exactly one delivery-ticket POST: the 409 is reconciled, not retried.
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "POST", "/api/delivery-tickets"), 1);
    });
}

// ---------------------------------------------------------------------------
// Refusals (sim-impl.md, "Refusals")
// ---------------------------------------------------------------------------

/// Run an admitted dispense to its end: co-sign the BDN and wait for the
/// reconciliation post, which follows the BDN number's entry.
async fn complete_dispense(harness: &mut Harness, proceed: oneshot::Receiver<JobAdmission>) {
    assert_eq!(proceed.await.unwrap(), JobAdmission::Active);
    let mut cosigned = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
    cosigned.signed_by_engineer = true;
    harness.bdn_signed_tx.send(cosigned).await.unwrap();
    let _record = harness.recon_rx.recv().await.unwrap();
}

#[test]
fn refused_create_stands_tug_down_and_operator_continues() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        harness.mock.enqueue(
            "POST",
            "/api/jobs/refuel",
            422,
            r#"{"error":"tank level 4000 L is at or below the cavitation threshold (5000 L)","code":"cavitation_threshold"}"#,
        );

        let refused = send_dispense(&harness).await;
        assert_eq!(refused.await.unwrap(), JobAdmission::Refused);
        assert_eq!(
            count_of(&harness.mock.requests(), "POST", "/api/jobs/job-1/dispenser/start"),
            0,
            "a refused job is never started"
        );

        // The operator loop survives the refusal: the next dispense completes.
        let admitted = send_dispense(&harness).await;
        complete_dispense(&mut harness, admitted).await;
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "POST", "/api/jobs/refuel"), 2);
        assert!(index_of(&requests, "PATCH", "/api/jobs/job-1").is_some());
    });
}

#[test]
fn persistent_atg_unavailable_503_refuses_create_without_panic() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        // Every attempt: the initial POST and one re-POST per backoff.
        for _ in 0..4 {
            harness.mock.enqueue(
                "POST",
                "/api/jobs/refuel",
                503,
                r#"{"error":"cavitation check cannot be evaluated","code":"atg_read_failed"}"#,
            );
        }

        let refused = send_dispense(&harness).await;
        assert_eq!(refused.await.unwrap(), JobAdmission::Refused);
        let requests = harness.mock.requests();
        assert_eq!(
            count_of(&requests, "POST", "/api/jobs/refuel"),
            4,
            "the initial attempt plus one re-POST per backoff"
        );
        assert_eq!(
            count_of(&requests, "GET", "/api/jobs/current"),
            0,
            "a 503 created no job, so it is not reconciled"
        );
    });
}

#[test]
fn refused_start_cancels_job_and_hands_refusal_to_engineer() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            422,
            r#"{"error":"projected closing volume 4500 L is at or below the cavitation threshold (5000 L)","code":"cavitation_threshold"}"#,
        );

        let admitted = send_dispense(&harness).await;
        assert_eq!(admitted.await.unwrap(), JobAdmission::Active);
        assert_eq!(
            harness.bdn_sign_rx.recv().await.unwrap(),
            DispenseHandoff::Refused
        );
        assert_eq!(
            count_of(&harness.mock.requests(), "DELETE", "/api/jobs/job-1"),
            1,
            "the refused job is cancelled, freeing the single job slot"
        );

        // A subsequent dispense completes normally, with no further cancel.
        let next = send_dispense(&harness).await;
        complete_dispense(&mut harness, next).await;
        assert_eq!(
            count_of(&harness.mock.requests(), "DELETE", "/api/jobs/job-1"),
            1
        );
    });
}

/// The FMS's cavitation refusal of a start, which leaves the job active.
const CAVITATION_422: &str = r#"{"error":"projected closing volume 4500 L is at or below the cavitation threshold (5000 L)","code":"cavitation_threshold"}"#;

#[test]
fn a_cancel_of_a_job_no_longer_current_hands_over_the_refusal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            422,
            CAVITATION_422,
        );
        harness
            .mock
            .enqueue("DELETE", "/api/jobs/job-1", 404, r#"{"error":"not found"}"#);

        let admitted = send_dispense(&harness).await;
        assert_eq!(admitted.await.unwrap(), JobAdmission::Active);
        // A job that is no longer the current one is the state the cancel seeks.
        assert_eq!(
            harness.bdn_sign_rx.recv().await.unwrap(),
            DispenseHandoff::Refused
        );
        assert_eq!(
            count_of(&harness.mock.requests(), "DELETE", "/api/jobs/job-1"),
            1
        );
    });
}

#[test]
fn a_cancel_that_fails_through_the_retry_schedule_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            422,
            CAVITATION_422,
        );
        for _ in 0..4 {
            harness
                .mock
                .enqueue("DELETE", "/api/jobs/job-1", 503, PROXY_503);
        }

        let admitted = send_dispense(&harness).await;
        assert_eq!(admitted.await.unwrap(), JobAdmission::Active);
        // A failed cancel leaves the refused job holding the single job slot,
        // so the operator stops here rather than at its next create.
        assert!(
            harness.bdn_sign_rx.recv().await.is_none(),
            "a cancel that fails through the schedule is fatal"
        );
        assert_eq!(
            count_of(&harness.mock.requests(), "DELETE", "/api/jobs/job-1"),
            4,
            "the initial attempt plus one retry per backoff"
        );
    });
}

#[test]
fn an_unexpected_status_on_the_cancel_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        harness.mock.enqueue(
            "POST",
            "/api/jobs/job-1/dispenser/start",
            422,
            CAVITATION_422,
        );
        harness.mock.enqueue(
            "DELETE",
            "/api/jobs/job-1",
            409,
            r#"{"error":"job is not in active state"}"#,
        );

        let admitted = send_dispense(&harness).await;
        assert_eq!(admitted.await.unwrap(), JobAdmission::Active);
        assert!(
            harness.bdn_sign_rx.recv().await.is_none(),
            "an unexpected status on the cancel is fatal"
        );
        assert_eq!(
            count_of(&harness.mock.requests(), "DELETE", "/api/jobs/job-1"),
            1
        );
    });
}

/// HAProxy's own answer when it has no backend to reach: a 503 with its page,
/// which carries no FMS code.
const PROXY_503: &str = "<html><body><h1>503 Service Unavailable</h1>\nNo server is available to handle this request.\n</body></html>";

#[test]
fn persistent_503_without_the_fms_code_on_create_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        for _ in 0..4 {
            harness
                .mock
                .enqueue("POST", "/api/jobs/refuel", 503, PROXY_503);
            // Not the FMS's answer, so each attempt is reconciled, against an
            // empty job slot.
            harness
                .mock
                .enqueue("GET", "/api/jobs/current", 404, r#"{"error":"not found"}"#);
        }

        let proceed = send_dispense(&harness).await;
        // The operator stops: the proceed sender drops without an admission.
        assert!(
            proceed.await.is_err(),
            "a 503 without the FMS's code is a fault, not a refusal"
        );
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "POST", "/api/jobs/refuel"), 4);
        assert_eq!(count_of(&requests, "GET", "/api/jobs/current"), 4);
    });
}

#[test]
fn persistent_503_without_the_fms_code_on_start_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        for _ in 0..4 {
            harness
                .mock
                .enqueue("POST", "/api/jobs/job-1/dispenser/start", 503, PROXY_503);
        }

        let admitted = send_dispense(&harness).await;
        assert_eq!(admitted.await.unwrap(), JobAdmission::Active);
        // The operator stops: no handoff reaches the engineer.
        assert!(
            harness.bdn_sign_rx.recv().await.is_none(),
            "a 503 without the FMS's code is a fault, not a refusal"
        );
        let requests = harness.mock.requests();
        assert_eq!(
            count_of(&requests, "POST", "/api/jobs/job-1/dispenser/start"),
            4
        );
        assert_eq!(count_of(&requests, "DELETE", "/api/jobs/job-1"), 0);
    });
}

#[test]
fn no_reading_503_skips_the_ullage_check_and_the_fill_proceeds() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        for _ in 0..4 {
            harness.mock.enqueue(
                "GET",
                "/api/tanks/01/level",
                503,
                r#"{"error":"no ATG reading is available yet","code":"no_atg_reading"}"#,
            );
        }

        let proceed = send_fill(&harness).await;
        assert!(
            proceed.await.is_ok(),
            "an unavailable reading must not block a delivery"
        );
        let requests = harness.mock.requests();
        assert_eq!(count_of(&requests, "GET", "/api/tanks/01/level"), 4);
        assert!(index_of(&requests, "POST", "/api/jobs/tank-fill").is_some());
    });
}

#[test]
fn ullage_503_without_the_fms_code_is_fatal() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let harness = spawn_operator(clock);
        for _ in 0..4 {
            harness
                .mock
                .enqueue("GET", "/api/tanks/01/level", 503, PROXY_503);
        }

        let proceed = send_fill(&harness).await;
        assert!(
            proceed.await.is_err(),
            "a 503 without the FMS's code is a fault, not a missing reading"
        );
        assert_eq!(
            count_of(&harness.mock.requests(), "POST", "/api/jobs/tank-fill"),
            0
        );
    });
}

#[test]
fn persistent_atg_unavailable_503_refuses_start_and_cancels_job() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        for _ in 0..4 {
            harness.mock.enqueue(
                "POST",
                "/api/jobs/job-1/dispenser/start",
                503,
                r#"{"error":"the on-demand ATG read failed, so the cavitation check cannot be evaluated","code":"atg_read_failed"}"#,
            );
        }

        let admitted = send_dispense(&harness).await;
        assert_eq!(admitted.await.unwrap(), JobAdmission::Active);
        assert_eq!(
            harness.bdn_sign_rx.recv().await.unwrap(),
            DispenseHandoff::Refused
        );
        let requests = harness.mock.requests();
        assert_eq!(
            count_of(&requests, "POST", "/api/jobs/job-1/dispenser/start"),
            4,
            "the initial attempt plus one retry per backoff"
        );
        assert_eq!(count_of(&requests, "DELETE", "/api/jobs/job-1"), 1);
    });
}

// ---------------------------------------------------------------------------
// Stopping and warm start (sim-tests.md, "Stopping and warm start")
// ---------------------------------------------------------------------------

/// Play the tug engineer's part of a dispense already sent: co-sign the BDN
/// the operator offers and return it. Returns the BDN number.
async fn cosign_bdn(harness: &mut Harness) -> String {
    let mut bdn = to_sign(harness.bdn_sign_rx.recv().await.unwrap());
    bdn.signed_by_engineer = true;
    let number = bdn.bdn_number.clone();
    harness.bdn_signed_tx.send(bdn).await.unwrap();
    number
}

#[test]
fn refuel_in_progress_completes_before_report() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        let _proceed = send_dispense(&harness).await;

        // The engineer has co-signed and reported; the operator has not yet
        // entered the BDN number.
        let number = cosign_bdn(&mut harness).await;
        harness.peers_at_rest.take().unwrap().send(()).unwrap();

        let report = (&mut harness.report_rx).await.unwrap();
        let requests = harness.mock.requests();
        let patch = body_of(&requests, "PATCH", "/api/jobs/job-1");
        assert_eq!(
            patch["bdn_number"],
            serde_json::Value::String(number.clone())
        );
        let Ok(ReconciliationRecord::Bdn(posted)) = harness.recon_rx.try_recv() else {
            panic!("the BDN is posted before the report");
        };
        assert_eq!(posted.bdn_number, number);
        assert_eq!(number, "BDN-000001");
        assert_eq!(report, DockOperatorState { bdn_seq: 2 });
    });
}

#[test]
fn dock_operator_serves_dock_job_arriving_after_stop() {
    let (clock, runtime) = SimClock::isolated(TimeModel::Synthetic, SIM_START);
    runtime.block_on(async {
        let mut harness = spawn_operator(clock);
        // The operator is idle when the stop is requested; the engineer has a
        // refuel taken up that it forwards afterwards.
        tokio::task::yield_now().await;
        let _proceed = send_dispense(&harness).await;
        cosign_bdn(&mut harness).await;
        harness.recon_rx.recv().await.unwrap();

        tokio::task::yield_now().await;
        assert!(
            harness.report_rx.try_recv().is_err(),
            "the operator reports only after the engineer has reported"
        );
        harness.peers_at_rest.take().unwrap().send(()).unwrap();
        let report = (&mut harness.report_rx).await.unwrap();
        assert_eq!(report, DockOperatorState { bdn_seq: 2 });
    });
}
