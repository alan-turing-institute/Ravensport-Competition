//! Private implementation of the dock operator actor. Not visible to other
//! modules.
//!
//! A single `async fn`: establish one authenticated HMI session at startup,
//! then loop over `DockJob`s one at a time, a waiting fill before a waiting
//! dispense, branching on type. Each `.await` is a real HTTP request to the HMI
//! API (digital.md §2.3) or a wait on an inter-actor channel. The sim speaks
//! plain HTTP to the HMI; TLS is terminated at the HAProxy layer
//! (sim-networking.md).
//!
//! Error handling follows sim-impl.md: transient HTTP failures are retried with
//! backoff (and a single WARN on recovery); a closed inter-actor channel, a
//! 4xx/contract response, or a malformed body is fatal — the actor panics with
//! context and the supervisor takes the sim down. Nothing is silently dropped.
//! The exceptions are the answers with a defined recovery: an expired session's
//! 401 (log in again), a lost response's 409 (reconcile against the live job),
//! and the FMS's refusal of a dispense, a 422 carrying `cavitation_threshold`
//! or a 503 carrying `atg_read_failed` that outlasts the retry schedule, which
//! sends the tug away unserved (sim-impl.md, "Refusals").
//!
//! Values not carried on a job are module-private constants (operator identity,
//! the fuel's properties, the BDN's port and supplier, the fuel supplier, the
//! tug-engineer id, and the poll interval),
//! extensible later.

use std::collections::VecDeque;
use std::net::SocketAddr;
use std::time::Duration;

use reqwest::{Client, RequestBuilder, Response};
use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, oneshot, watch};

use super::{DispenseHandoff, DockJob, DockOperatorState, JobAdmission, BDN};
use crate::actors::reconciliation_user::ReconciliationRecord;
use crate::actors::road_tanker_driver::DeliveryTicket;
use crate::clock::SimClock;
use crate::physical::tug_arrival_generator::DispenseJob;
use crate::serial::ReaderReadiness;
use crate::shared::RfidCardNumber;
use crate::stop::{after_peer, rest};

const ENTITY: &str = "dock_operator";

// --- Module-private constants (see sim-impl.md dock operator notes) ---------
// Projected from world.yaml (identities.operators) via sim-constants.toml,
// compiled in by build.rs. The username and password are presented at the HMI
// login (the sim is the authenticating client, so it holds the plaintext); the
// card is presented at the operator-RFID reader.
const OPERATOR_USERNAME: &str = crate::constants::OPERATOR_USERNAME;
const OPERATOR_PASSWORD: &str = crate::constants::OPERATOR_PASSWORD;
const OPERATOR_CARD: RfidCardNumber = RfidCardNumber(crate::constants::OPERATOR_CARD);
const ENGINEER_ID: &str = "tug-engineer";

// The single grade's properties, from world.yaml's `process` section. The grade
// and density are also held by the FMS (`FMS_DENSITY_KG_PER_L`); sulphur and
// flashpoint are written only onto the BDN (sim.md §8.2).
const FUEL_GRADE: &str = crate::constants::FUEL_GRADE;
const DENSITY_KG_PER_L: f64 = crate::constants::DENSITY_KG_PER_L;
const SULPHUR_PCT_MM: f64 = crate::constants::SULPHUR_PCT_MM;
const FLASHPOINT_C: f64 = crate::constants::FLASHPOINT_C;

// The BDN's port and supplier (Appendix V items 2 and 4). The supplier is the
// port itself, the Ravensport Harbour Authority, and its representative is the
// dock operator; the fuel supplier below supplies the port and does not appear
// on the BDN (sim.md §8.2). The address and telephone number are fictional:
// `RV` is not a United Kingdom postcode area, and 01632 960xxx is Ofcom's drama
// range.
const PORT: &str = "Ravensport";
const SUPPLIER_NAME: &str = "Ravensport Harbour Authority";
const SUPPLIER_ADDRESS: &str = "Fuel Dock, East Quay, Ravensport RV1 4QD";
const SUPPLIER_TELEPHONE: &str = "01632 960482";

/// The company whose road tanker delivers to the port, transcribed with each
/// delivery ticket.
const FUEL_SUPPLIER: &str = "Ravensport Fuels";
/// How often the dock operator checks a job's status and the totaliser on the
/// HMI: `intervals.dock_operator_poll_s` in `world.yaml`, 30 s in operation,
/// which `render.py` scales in a compressed scenario (sim-impl.md, "Dock
/// operator").
const POLL_INTERVAL: Duration = crate::constants::DOCK_OPERATOR_POLL_INTERVAL;

/// Retry backoffs (seconds) for transient digital-world failures, slept on the
/// sim clock. Exhausting them is fatal (sim-impl.md retry policy).
const RETRY_BACKOFFS_SECS: [u64; 3] = [1, 2, 4];

// The codes of the FMS's refusals the operator acts on (fms.md §7). The
// operator decides on the code, never on the description, which the FMS may
// reword. A 503 without a code is not the FMS's: a proxy on the path, such as
// HAProxy with no backend to reach, answers 503 with its own page.

/// A cavitation check failed: the current or projected closing volume is at
/// or below the threshold (a `422`).
const CAVITATION_REFUSAL_CODE: &str = "cavitation_threshold";

/// A cavitation check could not be evaluated, because the on-demand ATG read
/// failed (a `503`).
const UNEVALUATED_REFUSAL_CODE: &str = "atg_read_failed";

/// The FMS holds no ATG reading yet (a `503` from the tank level).
const NO_READING_CODE: &str = "no_atg_reading";

/// What a `503` that outlasts the retry schedule means for a request
/// (sim-impl.md, "Retry policy").
#[derive(Clone, Copy, PartialEq, Eq)]
enum Persistent503 {
    /// A fault, fatal like any other exhausted retry.
    Fatal,
    /// An answer for the caller to judge by its code: the refusal of a request
    /// carrying the cavitation check, or the FMS's report that it holds no
    /// reading for the ullage check.
    Returned,
}

/// The FMS's refusal of a dispense create or start (sim-impl.md, "Refusals"),
/// with the FMS's description for the log.
#[derive(Debug)]
enum Refusal {
    /// A `422` carrying the code `cavitation_threshold`.
    Cavitation(String),
    /// A `503` carrying the code `atg_read_failed`, which outlasted the retry
    /// schedule: the FMS could not evaluate its check.
    Unevaluated(String),
}

impl Refusal {
    /// The refusal's code, for the log.
    fn reason(&self) -> &'static str {
        match self {
            Refusal::Cavitation(_) => CAVITATION_REFUSAL_CODE,
            Refusal::Unevaluated(_) => UNEVALUATED_REFUSAL_CODE,
        }
    }

    /// The FMS's description, for the log.
    fn description(&self) -> &str {
        match self {
            Refusal::Cavitation(description) | Refusal::Unevaluated(description) => description,
        }
    }
}

/// The outcome of a job create.
enum Created {
    /// The FMS created the job, with its id.
    Job(String),
    /// The FMS refused the create, so no job exists.
    Refused(Refusal),
}

// --- HMI API request / response bodies (digital.md §2.3) --------------------
// Responses deserialise only the fields the operator uses; serde ignores the
// rest.

#[derive(Serialize)]
struct LoginRequest<'a> {
    username: &'a str,
    password: &'a str,
}

#[derive(Deserialize)]
struct LoginResponse {
    challenge_id: String,
}

#[derive(Deserialize)]
struct ChallengeResponse {
    token: String,
}

#[derive(Serialize)]
struct RefuelRequest<'a> {
    vessel_id: String,
    product: &'a str,
    requested_volume_litres: f64,
}

#[derive(Serialize)]
struct TankFillRequest<'a> {
    tanker_registration: &'a str,
    product: &'a str,
    delivery_volume_litres: f64,
}

#[derive(Deserialize)]
struct JobCreatedResponse {
    job_id: String,
}

#[derive(Deserialize)]
struct JobStatusResponse {
    job_id: String,
    status: String,
}

/// `GET /api/jobs/current` as read after a completed dispense: the instant the
/// FMS started it, which the operator writes on the BDN (sim.md §8.3).
#[derive(Deserialize)]
struct DispenseStartResponse {
    job_id: String,
    opened_at: Option<String>,
}

/// `GET /api/jobs/current` as read during create reconciliation: the identity
/// and type of the live job, used to decide whether an ambiguous create
/// actually committed.
#[derive(Deserialize)]
struct CurrentJob {
    job_id: String,
    #[serde(rename = "type")]
    job_type: String,
    #[allow(dead_code)]
    status: String,
}

#[derive(Serialize)]
struct PresetStartRequest {
    preset_volume_litres: f64,
}

#[derive(Deserialize)]
struct TotaliserResponse {
    job_id: String,
    dispensed_litres: f64,
    status: String,
}

/// `GET /api/tanks/01/level` as read for the BDN: the port's one temperature
/// sensor, the ATG probe, reaches the operator only through the HMI (sim.md
/// §8.4.1).
#[derive(Deserialize)]
struct TankLevelResponse {
    temperature_c: f64,
}

#[derive(Serialize)]
struct BdnPatchRequest<'a> {
    bdn_number: &'a str,
}

/// The delivery ticket as the operator transcribes it into the HMI (hmi.md
/// §7.4): every field is the road tanker's own, copied from the paper.
#[derive(Serialize)]
struct DeliveryTicketRequest<'a> {
    job_id: &'a str,
    ticket_number: &'a str,
    /// RFC 3339, UTC, `Z` designator (sim.md §2.1); the FMS records it as the
    /// fill's `opened_at`.
    delivery_commenced_at: String,
    delivered_volume_litres: f64,
    temperature_c: f64,
    density_kg_per_l: f64,
    temp_corrected_volume_litres: f64,
    supplier: &'a str,
    tanker_registration: &'a str,
}

/// The body of an FMS error forwarded by the HMI (hmi.md §7): the description
/// and, on a refusal the caller acts on, its code (fms.md §7).
#[derive(Deserialize)]
struct ErrorBody {
    #[serde(default)]
    error: String,
    code: Option<String>,
}

/// The code and description of an error body, or neither where the body is not
/// the FMS's, such as a proxy's own error page.
fn coded_error(body: &str) -> (Option<String>, String) {
    match serde_json::from_str::<ErrorBody>(body) {
        Ok(error) => (error.code, error.error),
        Err(_) => (None, String::new()),
    }
}

/// Read a response's body as text, guarded so simulated time does not advance
/// mid-round-trip (as in [`json_ok`]).
async fn body_text(clock: &SimClock, response: Response) -> String {
    let _io = clock.io_guard();
    response.text().await.unwrap_or_default()
}

/// A sim date-time as written on a form: RFC 3339 with a `Z` designator. Every
/// sim date-time is UTC (sim.md §2.1), so the offset is asserted, not converted.
fn utc_rfc3339(at: time::PrimitiveDateTime) -> String {
    at.assume_utc()
        .format(&time::format_description::well_known::Rfc3339)
        .expect("a UTC date-time always formats as RFC 3339")
}

/// The jobs waiting at the dock, which the operator holds in two queues of its
/// own, each in order of arrival (sim-impl.md, dock operator, "Order of jobs").
#[derive(Default)]
struct WaitingJobs {
    fills: VecDeque<DockJob>,
    dispenses: VecDeque<DockJob>,
}

impl WaitingJobs {
    /// The next job to take. Moves every job already waiting on `job_rx` into
    /// the queues, then takes a waiting fill if there is one, otherwise the
    /// oldest waiting dispense: a road tanker waiting to deliver is handled
    /// before any waiting tug, and tugs in the order in which they arrived.
    /// With both queues empty it awaits `job_rx`, then chooses among every job
    /// waiting by then. A closed channel with no job waiting is fatal.
    async fn next(&mut self, job_rx: &mut mpsc::Receiver<DockJob>) -> DockJob {
        loop {
            while let Ok(job) = job_rx.try_recv() {
                self.hold(job);
            }
            if let Some(job) = self
                .fills
                .pop_front()
                .or_else(|| self.dispenses.pop_front())
            {
                return job;
            }
            let job = job_rx
                .recv()
                .await
                .expect("dock_operator: job channel closed");
            self.hold(job);
        }
    }

    fn hold(&mut self, job: DockJob) {
        match job {
            DockJob::Fill(..) => self.fills.push_back(job),
            DockJob::Dispense(..) => self.dispenses.push_back(job),
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    start: DockOperatorState,
    mut peers_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<DockOperatorState>,
    mut job_rx: mpsc::Receiver<DockJob>,
    esd_tx: watch::Sender<bool>,
    operator_card_tx: mpsc::Sender<RfidCardNumber>,
    operator_rfid_ready: ReaderReadiness,
    mut ticket_rx: mpsc::Receiver<DeliveryTicket>,
    bdn_sign_tx: mpsc::Sender<DispenseHandoff>,
    mut bdn_signed_rx: mpsc::Receiver<BDN>,
    recon_tx: mpsc::UnboundedSender<ReconciliationRecord>,
    local_addr: SocketAddr,
    hmi_addr: SocketAddr,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        // Held for the duration of the sim: the operator owns the ESD contact.
        // No scripted ESD press exists yet, so the contact stays released.
        let _esd_tx = esd_tx;

        let client = Client::builder()
            .local_address(local_addr.ip())
            .build()
            .expect("dock_operator: failed to build HTTP client");
        let base = format!("http://{hmi_addr}");

        let mut session = Session::establish(
            &clock,
            &client,
            &base,
            &operator_card_tx,
            &operator_rfid_ready,
        )
        .await;

        // The start state's first BDN number on a cold start, or the
        // checkpoint's next number on a warm start (Start::cold, Start::warm).
        let DockOperatorState { mut bdn_seq } = start;
        // Identity of the last job the operator handled. Passed into each create
        // so a create whose response is lost can be reconciled against the live
        // job: a current job distinct from this one is the operator's own
        // committed create (see `Session::create_job`).
        let mut last_job_id: Option<String> = None;
        let mut waiting = WaitingJobs::default();
        loop {
            // The next job by the dock's order of service: a road tanker
            // waiting to deliver before any waiting tug, and tugs in the order
            // in which they arrived (narrative §2.2). A job taken up by the tug
            // engineer or the road tanker driver is work in progress, served
            // even after a stop; once both have reported, no further job can
            // arrive, and none is waiting.
            let job = tokio::select! {
                biased;
                job = waiting.next(&mut job_rx) => job,
                _ = after_peer(ENTITY, "tug engineer and road tanker driver", &mut peers_at_rest) => {
                    break;
                }
            };
            match job {
                DockJob::Dispense(dispense, proceed) => {
                    let handled = run_dispense(
                        &mut session,
                        dispense,
                        proceed,
                        &bdn_sign_tx,
                        &mut bdn_signed_rx,
                        &recon_tx,
                        &mut bdn_seq,
                        last_job_id.as_deref(),
                    )
                    .await;
                    // A refused create leaves no job, so the previous job stays
                    // the one a lost create is reconciled against.
                    if handled.is_some() {
                        last_job_id = handled;
                    }
                }
                DockJob::Fill(delivery_volume_l, tanker_registration, proceed) => {
                    last_job_id = Some(
                        run_fill(
                            &mut session,
                            delivery_volume_l,
                            &tanker_registration,
                            proceed,
                            &mut ticket_rx,
                            &recon_tx,
                            last_job_id.as_deref(),
                        )
                        .await,
                    );
                }
            }
        }

        rest(ENTITY, report, DockOperatorState { bdn_seq }).await
    })
}

/// Is a transport error transient (worth retrying)?
fn is_transient(error: &reqwest::Error) -> bool {
    error.is_connect() || error.is_timeout() || error.is_request()
}

/// Send a request built fresh on each attempt, retrying transient failures
/// (connection errors, timeouts, HTTP 5xx) with backoff on the sim clock. A
/// request that recovers after one or more retries logs a single WARN. A
/// non-transient transport error, or exhausted retries, is fatal, except a
/// `503` that ends the schedule where `persistent_503` returns it to the
/// caller. The returned response may still be a 4xx — the caller decides
/// whether that is expected.
async fn send_with_retry(
    clock: &SimClock,
    label: &str,
    persistent_503: Persistent503,
    build: impl Fn() -> RequestBuilder,
) -> Response {
    let mut attempt = 0usize;
    loop {
        // Guard the round-trip's send (connect/write/read-headers); simulated
        // time must not advance while it is in flight. The guard is released
        // before any retry backoff, so the backoff sleep still advances time.
        let sent = {
            let _io = clock.io_guard();
            build().send().await
        };
        match sent {
            Ok(response) if response.status().is_server_error() => {
                if attempt >= RETRY_BACKOFFS_SECS.len() {
                    if persistent_503 == Persistent503::Returned
                        && response.status() == reqwest::StatusCode::SERVICE_UNAVAILABLE
                    {
                        return response;
                    }
                    panic!(
                        "dock_operator: {label} returned HTTP {} after {attempt} retries",
                        response.status()
                    );
                }
                clock
                    .sleep(Duration::from_secs(RETRY_BACKOFFS_SECS[attempt]))
                    .await;
                attempt += 1;
            }
            Ok(response) => {
                if attempt > 0 {
                    tracing::warn!(request = label, attempts = attempt + 1, "request recovered");
                }
                return response;
            }
            Err(error) if is_transient(&error) => {
                if attempt >= RETRY_BACKOFFS_SECS.len() {
                    panic!("dock_operator: {label} failed after {attempt} retries: {error}");
                }
                clock
                    .sleep(Duration::from_secs(RETRY_BACKOFFS_SECS[attempt]))
                    .await;
                attempt += 1;
            }
            Err(error) => panic!("dock_operator: {label} request failed: {error}"),
        }
    }
}

/// Run a successful request through `error_for_status` (a 4xx here is an
/// unexpected contract breach → fatal) and deserialize its JSON body. The body
/// read is guarded so simulated time does not advance mid-round-trip; the gap
/// from the [`send_with_retry`] guard is synchronous (no await), so the clock
/// cannot move between the two.
async fn json_ok<T: serde::de::DeserializeOwned>(
    clock: &SimClock,
    response: Response,
    label: &str,
) -> T {
    let _io = clock.io_guard();
    let status = response.status();
    if !status.is_success() {
        // Include the response body: it carries the reason (for example the FMS's
        // "tank level ... at or below the cavitation threshold" 422, forwarded
        // verbatim by the HMI), which the status code alone omits. Without it a
        // crash left only an opaque status in sim.log.
        let body = response.text().await.unwrap_or_default();
        panic!("dock_operator: {label} unexpected HTTP {status}: {body}");
    }
    response
        .json()
        .await
        .unwrap_or_else(|e| panic!("dock_operator: {label} malformed response body: {e}"))
}

/// Like [`json_ok`] but for endpoints whose body the operator does not read.
async fn expect_ok(response: Response, label: &str) {
    response
        .error_for_status()
        .unwrap_or_else(|e| panic!("dock_operator: {label} unexpected HTTP status: {e}"));
}

/// The refusal a failed response to a dispense create or start carries
/// (sim-impl.md, "Refusals"), decided on its code: a `422` carrying
/// `cavitation_threshold`, or a `503` carrying `atg_read_failed`, which reaches
/// here only once it has outlasted the retry schedule. Any other failure is
/// fatal, its body preserved verbatim, a `503` without that code included: it
/// is a fault on the path, such as a proxy with no backend, not the FMS's
/// answer.
async fn refusal(clock: &SimClock, response: Response, label: &str) -> Refusal {
    let status = response.status();
    let body = body_text(clock, response).await;
    let (code, description) = coded_error(&body);
    match (status, code.as_deref()) {
        (reqwest::StatusCode::UNPROCESSABLE_ENTITY, Some(CAVITATION_REFUSAL_CODE)) => {
            Refusal::Cavitation(description)
        }
        (reqwest::StatusCode::SERVICE_UNAVAILABLE, Some(UNEVALUATED_REFUSAL_CODE)) => {
            Refusal::Unevaluated(description)
        }
        _ => panic!("dock_operator: {label} unexpected HTTP {status}: {body}"),
    }
}

/// Two-phase login: password, then the RFID card presented at the HMI reader in
/// response to the challenge. Returns the session token.
async fn login(
    clock: &SimClock,
    client: &Client,
    base: &str,
    operator_card_tx: &mpsc::Sender<RfidCardNumber>,
    operator_rfid_ready: &ReaderReadiness,
) -> String {
    // Begin the login only once the HMI is subscribed to the operator reader.
    // The card is presented mid-login (between the POST and the challenge poll),
    // so were the HMI not yet attached the card would be lost on the wire and the
    // challenge would expire. Waiting here also avoids opening a challenge whose
    // 30-second TTL could lapse while waiting.
    operator_rfid_ready.wait_ready().await;

    let login_url = format!("{base}/api/auth/login");
    let request = LoginRequest {
        username: OPERATOR_USERNAME,
        password: OPERATOR_PASSWORD,
    };
    let response = send_with_retry(clock, "POST /api/auth/login", Persistent503::Fatal, || {
        client.post(&login_url).json(&request)
    })
    .await;
    let login: LoginResponse = json_ok(clock, response, "POST /api/auth/login").await;

    // Present the operator card at the HMI reader, then resolve the challenge.
    operator_card_tx
        .send(OPERATOR_CARD)
        .await
        .expect("dock_operator: operator RFID reader channel closed");

    let challenge_url = format!("{base}/api/auth/challenge/{}", login.challenge_id);
    let response = send_with_retry(
        clock,
        "GET /api/auth/challenge",
        Persistent503::Fatal,
        || client.get(&challenge_url),
    )
    .await;
    let challenge: ChallengeResponse = json_ok(clock, response, "GET /api/auth/challenge").await;
    challenge.token
}

/// The operator's authenticated HMI session: the bearer token plus everything
/// needed to obtain a fresh one. The FMS session expires a fixed time after
/// login, so a long-running operator's token eventually expires; rather than
/// treat the resulting 401 as fatal, the operator does what a person would —
/// logs in again and retries the request once (sim-impl.md).
struct Session<'a> {
    clock: &'a SimClock,
    client: &'a Client,
    base: &'a str,
    operator_card_tx: &'a mpsc::Sender<RfidCardNumber>,
    operator_rfid_ready: &'a ReaderReadiness,
    token: String,
}

impl<'a> Session<'a> {
    /// Establish the initial session via the two-phase login.
    async fn establish(
        clock: &'a SimClock,
        client: &'a Client,
        base: &'a str,
        operator_card_tx: &'a mpsc::Sender<RfidCardNumber>,
        operator_rfid_ready: &'a ReaderReadiness,
    ) -> Session<'a> {
        let token = login(clock, client, base, operator_card_tx, operator_rfid_ready).await;
        Session {
            clock,
            client,
            base,
            operator_card_tx,
            operator_rfid_ready,
            token,
        }
    }

    /// Send an authenticated request, retrying it once with a fresh token if the
    /// session has expired. `build` receives the current bearer token, so the
    /// retry carries the re-authenticated one. A 401 on the retry is left to the
    /// caller's response handling — a genuine auth failure stays fatal, so this
    /// cannot loop.
    async fn send(&mut self, label: &str, build: impl Fn(&str) -> RequestBuilder) -> Response {
        self.send_with(label, Persistent503::Fatal, build).await
    }

    /// [`Session::send`], with `persistent_503` saying what a `503` that
    /// outlasts the retry schedule means for this request.
    async fn send_with(
        &mut self,
        label: &str,
        persistent_503: Persistent503,
        build: impl Fn(&str) -> RequestBuilder,
    ) -> Response {
        let response =
            send_with_retry(self.clock, label, persistent_503, || build(&self.token)).await;
        if response.status() != reqwest::StatusCode::UNAUTHORIZED {
            return response;
        }
        self.reauthenticate(label).await;
        send_with_retry(self.clock, label, persistent_503, || build(&self.token)).await
    }

    /// Obtain a fresh session token via the two-phase login, replacing the
    /// expired one. `request` names the call that met the 401, for the WARN.
    async fn reauthenticate(&mut self, request: &str) {
        tracing::warn!(request, "HMI session expired; re-authenticating");
        self.token = login(
            self.clock,
            self.client,
            self.base,
            self.operator_card_tx,
            self.operator_rfid_ready,
        )
        .await;
    }

    /// Create the single current job, tolerant of a create whose response is
    /// lost in transit. `POST /api/jobs` is not idempotent and the FMS holds one
    /// job slot, so blindly retrying a create that already committed collides
    /// with that slot (HTTP 409 "a job is already active") — historically fatal.
    /// Instead, any ambiguous outcome — a transient transport error, a 5xx, or a
    /// 409 — triggers reconciliation against the live job: if a job of
    /// `expected_type` distinct from `prior_job_id` is now current, the create
    /// committed and its id is adopted; otherwise the create did not commit and
    /// is retried.
    ///
    /// A `503` carrying the code `atg_read_failed` is excluded from the
    /// reconciliation: the FMS could not evaluate its creation check and so
    /// created no job (fms.md §7.4), and the create is re-POSTed under the same
    /// backoff without a reconciliation poll. Such a `503` that ends the
    /// schedule, or a `422` carrying the code `cavitation_threshold`, is the
    /// FMS's refusal (sim-impl.md, "Refusals"), returned for the caller to act
    /// on. A `503` without the code is a fault on the path, reconciled like any
    /// other 5xx. Any other rejection on its merits (a 4xx other than 401 and
    /// 409, a 422 without the code included) stays fatal, its body preserved
    /// verbatim.
    async fn create_job(
        &mut self,
        label: &str,
        expected_type: &str,
        prior_job_id: Option<&str>,
        build: impl Fn(&str) -> RequestBuilder,
    ) -> Created {
        let mut attempt = 0usize;
        // At most one re-authentication per create: a fresh token still met with
        // a 401 is a genuine auth failure, not an expiry, and must stay fatal
        // rather than loop (matching `Session::send`).
        let mut reauthed = false;
        loop {
            // One create attempt, guarded so simulated time does not advance
            // while the round-trip is in flight (as in `send_with_retry`).
            let sent = {
                let _io = self.clock.io_guard();
                build(&self.token).send().await
            };
            let ambiguous = match sent {
                Ok(resp) if resp.status().is_success() => {
                    let created: JobCreatedResponse = json_ok(self.clock, resp, label).await;
                    return Created::Job(created.job_id);
                }
                Ok(resp) if resp.status() == reqwest::StatusCode::UNAUTHORIZED => {
                    if reauthed {
                        // A freshly issued token was still rejected: fatal, body
                        // preserved verbatim by json_ok (which never returns).
                        json_ok::<JobCreatedResponse>(self.clock, resp, label).await;
                        unreachable!("json_ok panics on a non-success response")
                    }
                    // The token expired before the FMS processed the create, so
                    // nothing committed: re-authenticate and re-POST afresh.
                    self.reauthenticate(label).await;
                    reauthed = true;
                    attempt = 0;
                    continue;
                }
                Ok(resp) if resp.status() == reqwest::StatusCode::SERVICE_UNAVAILABLE => {
                    let body = body_text(self.clock, resp).await;
                    let (code, description) = coded_error(&body);
                    if code.as_deref() == Some(UNEVALUATED_REFUSAL_CODE) {
                        // The FMS could not evaluate its check, so no job was
                        // created and the outcome is not ambiguous.
                        if attempt >= RETRY_BACKOFFS_SECS.len() {
                            return Created::Refused(Refusal::Unevaluated(description));
                        }
                        false
                    } else {
                        // Not the FMS's answer: a fault on the path, ambiguous
                        // like any other 5xx.
                        true
                    }
                }
                Ok(resp)
                    if resp.status() == reqwest::StatusCode::CONFLICT
                        || resp.status().is_server_error() =>
                {
                    true
                }
                Ok(resp) if resp.status() == reqwest::StatusCode::UNPROCESSABLE_ENTITY => {
                    // The cavitation refusal, or else a fatal rejection with the
                    // body preserved verbatim by `refusal`, which never returns
                    // then.
                    return Created::Refused(refusal(self.clock, resp, label).await);
                }
                Ok(resp) => {
                    // A genuine rejection (400, 403, ...): fatal, with the body
                    // preserved verbatim by json_ok, which never returns.
                    json_ok::<JobCreatedResponse>(self.clock, resp, label).await;
                    unreachable!("json_ok panics on a non-success response")
                }
                Err(error) if is_transient(&error) => true,
                Err(error) => panic!("dock_operator: {label} request failed: {error}"),
            };

            if ambiguous {
                if let Some(job_id) = self
                    .reconcile_created_job(expected_type, prior_job_id)
                    .await
                {
                    tracing::warn!(
                        request = label,
                        job_id = %job_id,
                        "create response was lost; adopted the already-committed job"
                    );
                    return Created::Job(job_id);
                }
            }
            if attempt >= RETRY_BACKOFFS_SECS.len() {
                panic!("dock_operator: {label} failed after {attempt} retries");
            }
            self.clock
                .sleep(Duration::from_secs(RETRY_BACKOFFS_SECS[attempt]))
                .await;
            attempt += 1;
        }
    }

    /// Read the live job after an ambiguous create to decide whether it
    /// committed. Returns the id to adopt if a job of `expected_type` distinct
    /// from `prior_job_id` is now current (the create committed but its response
    /// was lost), or `None` if the slot is empty or still holds the prior job
    /// (the create did not commit). The read is the idempotent
    /// `GET /api/jobs/current`, so it keeps the ordinary retrying send; a 404 (no
    /// live job) is expected here and, unlike elsewhere, is not fatal.
    async fn reconcile_created_job(
        &mut self,
        expected_type: &str,
        prior_job_id: Option<&str>,
    ) -> Option<String> {
        let clock = self.clock;
        let client = self.client;
        let url = format!("{}/api/jobs/current", self.base);
        let response = self
            .send("GET /api/jobs/current", |tok| {
                client.get(&url).bearer_auth(tok)
            })
            .await;
        match response.status() {
            reqwest::StatusCode::NOT_FOUND => None,
            status if status.is_success() => {
                let job: CurrentJob =
                    json_ok(clock, response, "GET /api/jobs/current (reconcile)").await;
                match prior_job_id {
                    // Still the previous job in the slot: our create did not commit.
                    Some(prev) if job.job_id == prev => None,
                    // A job distinct from the prior one, and only the operator
                    // creates jobs (and does so sequentially): it is ours.
                    _ => {
                        debug_assert_eq!(
                            job.job_type, expected_type,
                            "reconcile: live job type {} does not match the create ({expected_type})",
                            job.job_type
                        );
                        Some(job.job_id)
                    }
                }
            }
            _ => {
                json_ok::<CurrentJob>(clock, response, "GET /api/jobs/current (reconcile)").await;
                unreachable!("json_ok panics on a non-success response")
            }
        }
    }

    /// Send a non-idempotent job-state transition, tolerant of a lost response.
    /// The FMS holds one job slot and each such transition is one-way, so a retry
    /// of a request whose response was lost meets the already-transitioned job and
    /// is rejected with HTTP 409 — historically fatal. Instead, a 409 triggers
    /// reconciliation against the live job: if `job_id` is still current and now
    /// reports one of `committed_states` (statuses reachable only after the
    /// transition took effect), the request committed and the operator proceeds
    /// without repeating it. Any other outcome — a non-409 rejection, a different
    /// or absent live job, or a status showing the transition did not take effect
    /// — is returned as the failed response, boxed, for the caller to judge.
    ///
    /// Applies to the operator's one-way writes whose retry is unsafe: the
    /// dispense start (Active → Running), the BDN link and the fill close (both
    /// Active/Running → Closed). Job creation has its own reconciliation
    /// (`create_job`) because an uncommitted create must be re-POSTed, not adopted.
    async fn try_transition(
        &mut self,
        label: &str,
        job_id: &str,
        committed_states: &[&str],
        persistent_503: Persistent503,
        build: impl Fn(&str) -> RequestBuilder,
    ) -> Result<(), Box<Response>> {
        let response = self.send_with(label, persistent_503, build).await;
        if response.status().is_success() {
            return Ok(());
        }
        if response.status() == reqwest::StatusCode::CONFLICT
            && self.job_reports_state(job_id, committed_states).await
        {
            tracing::warn!(
                request = label,
                job_id,
                "response was lost; adopted the already-committed transition"
            );
            return Ok(());
        }
        Err(Box::new(response))
    }

    /// [`Session::try_transition`] for a transition with no defined refusal,
    /// the BDN link and the fill close: any failure that is not a lost response
    /// is fatal, its body preserved verbatim by `json_ok`.
    async fn commit_transition(
        &mut self,
        label: &str,
        job_id: &str,
        committed_states: &[&str],
        build: impl Fn(&str) -> RequestBuilder,
    ) {
        let outcome = self
            .try_transition(label, job_id, committed_states, Persistent503::Fatal, build)
            .await;
        if let Err(response) = outcome {
            // json_ok never returns on a non-success response.
            json_ok::<JobStatusResponse>(self.clock, *response, label).await;
            unreachable!("json_ok panics on a non-success response")
        }
    }

    /// Read the live job after a 409 to decide whether a non-idempotent
    /// transition committed. Returns `true` only if `job_id` is still the current
    /// job and reports a status in `committed_states`. The read is the idempotent
    /// `GET /api/jobs/current`, so it keeps the ordinary retrying send; a
    /// non-success response (including a 404, no live job) yields `false`.
    async fn job_reports_state(&mut self, job_id: &str, committed_states: &[&str]) -> bool {
        let clock = self.clock;
        let client = self.client;
        let url = format!("{}/api/jobs/current", self.base);
        let response = self
            .send("GET /api/jobs/current", |tok| {
                client.get(&url).bearer_auth(tok)
            })
            .await;
        if !response.status().is_success() {
            return false;
        }
        let job: JobStatusResponse = json_ok(
            clock,
            response,
            "GET /api/jobs/current (transition reconcile)",
        )
        .await;
        job.job_id == job_id && committed_states.contains(&job.status.as_str())
    }

    /// Transition the dispense job to Running via `POST …/dispenser/start`. On a
    /// lost response the retry meets the already-Running job (409 "job is not in
    /// active state"); `dispensing`, or `awaiting_bdn` where the preset volume was
    /// reached within the lost-response window, confirm the start committed.
    ///
    /// The start carries the cavitation check, so the FMS may refuse it as it may
    /// refuse the create, identified the same way (sim-impl.md, "Refusals"). A
    /// refusal leaves the job `active` and is returned for the caller to act on;
    /// any other failure stays fatal.
    async fn start_dispense(
        &mut self,
        job_id: &str,
        preset_volume_litres: f64,
    ) -> Result<(), Refusal> {
        const LABEL: &str = "POST .../dispenser/start";
        let start_url = format!("{}/api/jobs/{job_id}/dispenser/start", self.base);
        let start_request = PresetStartRequest {
            preset_volume_litres,
        };
        let client = self.client;
        let outcome = self
            .try_transition(
                LABEL,
                job_id,
                &["dispensing", "awaiting_bdn"],
                Persistent503::Returned,
                |tok| {
                    client
                        .post(&start_url)
                        .bearer_auth(tok)
                        .json(&start_request)
                },
            )
            .await;
        match outcome {
            Ok(()) => Ok(()),
            Err(response) => Err(refusal(self.clock, *response, LABEL).await),
        }
    }

    /// Cancel a job whose start the FMS refused, freeing the single job slot that
    /// an active job holds against every later one, the restocking fill included
    /// (sim-impl.md, "Refusals"). Retried on the standard schedule; a cancel whose
    /// response was lost is safe to send again, since the FMS answers a cancel of
    /// a job it has already cancelled with `204` (fms.md §7.4). A `404` means the
    /// job is no longer the current one, which is the state the cancel seeks. Any
    /// other outcome is fatal, its body preserved verbatim, a `5xx` that outlasts
    /// the schedule included: a failed cancel leaves the refused job holding the
    /// slot, and the next create would fail on it under a message that does not
    /// name the cause. One WARN records the outcome.
    async fn cancel_job(&mut self, job_id: &str) {
        const LABEL: &str = "DELETE /api/jobs/{id}";
        let url = format!("{}/api/jobs/{job_id}", self.base);
        let client = self.client;
        let response = self
            .send(LABEL, |tok| client.delete(&url).bearer_auth(tok))
            .await;
        let status = response.status();
        if status.is_success() {
            tracing::warn!(request = LABEL, job_id, "cancelled the refused job");
        } else if status == reqwest::StatusCode::NOT_FOUND {
            tracing::warn!(
                request = LABEL,
                job_id,
                "the refused job is no longer the current job; nothing to cancel"
            );
        } else {
            let body = body_text(self.clock, response).await;
            panic!("dock_operator: {LABEL} for job {job_id} unexpected HTTP {status}: {body}");
        }
    }
}

/// Run one dispense job. Returns the id of the job the FMS created, or `None`
/// if it refused the create, in which case no job exists.
#[allow(clippy::too_many_arguments)]
async fn run_dispense(
    session: &mut Session<'_>,
    job: DispenseJob,
    proceed: oneshot::Sender<JobAdmission>,
    bdn_sign_tx: &mpsc::Sender<DispenseHandoff>,
    bdn_signed_rx: &mut mpsc::Receiver<BDN>,
    recon_tx: &mpsc::UnboundedSender<ReconciliationRecord>,
    bdn_seq: &mut u32,
    prior_job_id: Option<&str>,
) -> Option<String> {
    let client = session.client;
    let base = session.base;

    let refuel_url = format!("{base}/api/jobs/refuel");
    let request = RefuelRequest {
        vessel_id: job.vessel_id.0.to_string(),
        product: FUEL_GRADE,
        requested_volume_litres: job.volume_l,
    };
    let created = session
        .create_job("POST /api/jobs/refuel", "refuel", prior_job_id, |tok| {
            client.post(&refuel_url).bearer_auth(tok).json(&request)
        })
        .await;
    let job_id = match created {
        Created::Job(job_id) => job_id,
        Created::Refused(refusal) => {
            // No job exists: stand the tug engineer down, and the tug departs
            // unserved.
            tracing::warn!(
                request = "POST /api/jobs/refuel",
                vessel_id = job.vessel_id.0,
                reason = refusal.reason(),
                description = refusal.description(),
                "the FMS refused the dispense; no job was created"
            );
            proceed
                .send(JobAdmission::Refused)
                .expect("dock_operator: tug engineer dropped the proceed receiver");
            return None;
        }
    };

    // The job is active in the FMS — release the tug engineer to present the
    // vessel RFID tag.
    proceed
        .send(JobAdmission::Active)
        .expect("dock_operator: tug engineer dropped the proceed receiver");

    // Block on the RFID gate: the pump cannot start until the FMS validates the
    // vessel tag against the job.
    poll_job_status(session, &job_id, "rfid_validated").await;

    if let Err(refusal) = session.start_dispense(&job_id, job.volume_l).await {
        // The refused job stays active and would block every later job, so it
        // is cancelled before the tug engineer is stood down in place of the
        // BDN exchange.
        tracing::warn!(
            request = "POST .../dispenser/start",
            job_id = %job_id,
            vessel_id = job.vessel_id.0,
            reason = refusal.reason(),
            description = refusal.description(),
            "the FMS refused the dispense start; cancelling the job"
        );
        session.cancel_job(&job_id).await;
        bdn_sign_tx
            .send(DispenseHandoff::Refused)
            .await
            .expect("dock_operator: tug engineer BDN-signing channel closed");
        return Some(job_id);
    }

    let totaliser = poll_totaliser_complete(session, &job_id).await;

    // The BDN is written at completion but dated from the start of the
    // transfer, the instant the FMS recorded (sim.md §8.3).
    let commenced_at = read_dispense_start(session, &job_id).await;

    // Read the tank temperature for the BDN, once, now the dispense is complete.
    let level_url = format!("{base}/api/tanks/01/level");
    let response = session
        .send("GET /api/tanks/01/level", |tok| {
            client.get(&level_url).bearer_auth(tok)
        })
        .await;
    let level: TankLevelResponse =
        json_ok(session.clock, response, "GET /api/tanks/01/level").await;

    // Hand-write and sign the BDN; the start/end fields carry the transaction
    // totaliser delta the HMI exposes (absolute meter readings are not surfaced).
    // The fuel is held at 15 °C, where the correction is identity (sim.md
    // §1.3), so the corrected volume is the metered volume. The operator signs
    // as the supplier's representative, making the regulation 14.1 declaration.
    let volume_l = totaliser.dispensed_litres;
    let temp_corrected_volume_l = volume_l;
    let bdn = BDN {
        bdn_number: format!("BDN-{:06}", *bdn_seq),
        delivery_commenced_at: commenced_at,
        vessel_id: job.vessel_id,
        vessel_name: job.vessel_name,
        port: PORT,
        supplier_name: SUPPLIER_NAME,
        supplier_address: SUPPLIER_ADDRESS,
        supplier_telephone: SUPPLIER_TELEPHONE,
        grade: FUEL_GRADE.to_string(),
        volume_l,
        temperature_c: level.temperature_c,
        temp_corrected_volume_l,
        quantity_t: temp_corrected_volume_l * DENSITY_KG_PER_L / 1000.0,
        density_kg_per_m3: DENSITY_KG_PER_L * 1000.0,
        sulphur_pct_mm: SULPHUR_PCT_MM,
        flashpoint_c: FLASHPOINT_C,
        sulphur_limit_declared: true,
        start_totaliser_l: 0.0,
        end_totaliser_l: volume_l,
        // The audit compares this with the `operator` the FMS records on the
        // job's log events, which is the logged-in username.
        operator_id: OPERATOR_USERNAME.to_string(),
        engineer_id: ENGINEER_ID.to_string(),
        signed_by_operator: true,
        signed_by_engineer: false,
    };
    *bdn_seq += 1;

    bdn_sign_tx
        .send(DispenseHandoff::Sign(bdn))
        .await
        .expect("dock_operator: tug engineer BDN-signing channel closed");
    let cosigned = bdn_signed_rx
        .recv()
        .await
        .expect("dock_operator: tug engineer co-signed-BDN channel closed");

    // Enter the BDN number, linking the paper record to the FMS transaction.
    // Linking closes the job, so a retry after a lost response meets the closed
    // job (409 "a BDN is already linked"); a live job reporting `complete`
    // confirms the link committed.
    let patch_url = format!("{base}/api/jobs/{job_id}");
    let patch_request = BdnPatchRequest {
        bdn_number: &cosigned.bdn_number,
    };
    session
        .commit_transition("PATCH /api/jobs/{id}", &job_id, &["complete"], |tok| {
            client
                .patch(&patch_url)
                .bearer_auth(tok)
                .json(&patch_request)
        })
        .await;

    recon_tx
        .send(ReconciliationRecord::Bdn(cosigned))
        .expect("dock_operator: reconciliation user channel closed");

    Some(job_id)
}

async fn run_fill(
    session: &mut Session<'_>,
    delivery_volume_l: f64,
    tanker_registration: &str,
    proceed: oneshot::Sender<()>,
    ticket_rx: &mut mpsc::Receiver<DeliveryTicket>,
    recon_tx: &mpsc::UnboundedSender<ReconciliationRecord>,
    prior_job_id: Option<&str>,
) -> String {
    let client = session.client;
    let base = session.base;

    // Check there is sufficient ullage before accepting the delivery. A 503
    // carrying `no_atg_reading` reports only that the FMS has no ATG reading
    // yet (fms.md §7.3), and an unavailable reading must not block a delivery
    // (digital.md §4.3), so the check is skipped with a WARN (sim-impl.md,
    // "Retry policy"). A 503 without that code is a fault on the path, and
    // fatal like any other exhausted retry.
    const LEVEL: &str = "GET /api/tanks/01/level";
    let level_url = format!("{base}/api/tanks/01/level");
    let response = session
        .send_with(LEVEL, Persistent503::Returned, |tok| {
            client.get(&level_url).bearer_auth(tok)
        })
        .await;
    if response.status() == reqwest::StatusCode::SERVICE_UNAVAILABLE {
        let body = body_text(session.clock, response).await;
        let (code, _) = coded_error(&body);
        if code.as_deref() != Some(NO_READING_CODE) {
            panic!(
                "dock_operator: {LEVEL} returned HTTP 503 after {} retries: {body}",
                RETRY_BACKOFFS_SECS.len()
            );
        }
        tracing::warn!(
            request = LEVEL,
            "no ATG reading is available; the ullage check is skipped"
        );
    } else {
        expect_ok(response, LEVEL).await;
    }

    let fill_url = format!("{base}/api/jobs/tank-fill");
    let request = TankFillRequest {
        // The tanker delivering: the job's counterparty is the registration
        // printed on its ticket (narrative.md §2.1).
        tanker_registration,
        product: FUEL_GRADE,
        delivery_volume_litres: delivery_volume_l,
    };
    let created = session
        .create_job(
            "POST /api/jobs/tank-fill",
            "tank-fill",
            prior_job_id,
            |tok| client.post(&fill_url).bearer_auth(tok).json(&request),
        )
        .await;
    let job_id = match created {
        Created::Job(job_id) => job_id,
        // Fills are exempt from the cavitation check and tolerate an
        // unavailable reading (fms.md §5.1), so the FMS has no defined reason to
        // refuse one: a refusal is a contract breach (sim-impl.md, "Refusals").
        Created::Refused(refusal) => panic!(
            "dock_operator: POST /api/jobs/tank-fill refused ({}: {}); a fill has no defined refusal",
            refusal.reason(),
            refusal.description()
        ),
    };

    // The job is active — release the road tanker driver to begin pumping.
    proceed
        .send(())
        .expect("dock_operator: road tanker driver dropped the proceed receiver");

    // Await the driver's signed delivery ticket, counter-sign it, and
    // transcribe its fields into the FMS.
    let mut ticket = ticket_rx
        .recv()
        .await
        .expect("dock_operator: road tanker driver ticket channel closed");
    ticket.signed_by_operator = true;

    let ticket_url = format!("{base}/api/delivery-tickets");
    let ticket_request = DeliveryTicketRequest {
        job_id: &job_id,
        ticket_number: &ticket.ticket_number,
        delivery_commenced_at: utc_rfc3339(ticket.delivery_commenced_at),
        delivered_volume_litres: ticket.gross_volume_l,
        temperature_c: ticket.temperature_c,
        density_kg_per_l: ticket.density_kg_per_l,
        temp_corrected_volume_litres: ticket.temp_corrected_volume_l,
        supplier: FUEL_SUPPLIER,
        tanker_registration: &ticket.tanker_registration,
    };
    // Transcribing the ticket closes the fill job, so a retry after a lost
    // response meets the closed job (409 "fill job is not active"); a live job
    // reporting `complete` confirms the close committed.
    session
        .commit_transition(
            "POST /api/delivery-tickets",
            &job_id,
            &["complete"],
            |tok| {
                client
                    .post(&ticket_url)
                    .bearer_auth(tok)
                    .json(&ticket_request)
            },
        )
        .await;

    recon_tx
        .send(ReconciliationRecord::DeliveryTicket(ticket))
        .expect("dock_operator: reconciliation user channel closed");

    job_id
}

/// Poll `GET /api/jobs/current` every `POLL_INTERVAL` until the status matches
/// `target`. The live job is the singleton `current` job (there is only ever
/// one); `expected_job_id` is the id returned at job creation, asserted against
/// the polled job to catch the contract drifting out from under the operator.
async fn poll_job_status(session: &mut Session<'_>, expected_job_id: &str, target: &str) {
    let clock = session.clock;
    let client = session.client;
    let base = session.base;
    let url = format!("{base}/api/jobs/current");
    loop {
        let response = session
            .send("GET /api/jobs/current", |tok| {
                client.get(&url).bearer_auth(tok)
            })
            .await;
        let status: JobStatusResponse = json_ok(clock, response, "GET /api/jobs/current").await;
        assert_eq!(
            status.job_id, expected_job_id,
            "dock_operator: GET /api/jobs/current returned job {} but the operator's live job is {expected_job_id}",
            status.job_id
        );
        if status.status == target {
            return;
        }
        clock.sleep(POLL_INTERVAL).await;
    }
}

/// Read the FMS's dispense start from `GET /api/jobs/current` once the dispense
/// is complete. Reading it from the job rather than the start response dates the
/// note correctly after a retried start. `expected_job_id` is asserted as in
/// [`poll_job_status`]; a missing or unparseable `opened_at` is a malformed
/// response and fatal (fms.md §7.4).
async fn read_dispense_start(
    session: &mut Session<'_>,
    expected_job_id: &str,
) -> time::PrimitiveDateTime {
    let clock = session.clock;
    let client = session.client;
    let url = format!("{}/api/jobs/current", session.base);
    let response = session
        .send("GET /api/jobs/current", |tok| {
            client.get(&url).bearer_auth(tok)
        })
        .await;
    let job: DispenseStartResponse =
        json_ok(clock, response, "GET /api/jobs/current (dispense start)").await;
    assert_eq!(
        job.job_id, expected_job_id,
        "dock_operator: GET /api/jobs/current returned job {} but the operator's live job is {expected_job_id}",
        job.job_id
    );
    let opened_at = job.opened_at.unwrap_or_else(|| {
        panic!("dock_operator: completed dispense {expected_job_id} reports no opened_at")
    });
    let start =
        time::OffsetDateTime::parse(&opened_at, &time::format_description::well_known::Rfc3339)
            .unwrap_or_else(|e| {
                panic!("dock_operator: opened_at {opened_at:?} is not RFC 3339: {e}")
            })
            .to_offset(time::UtcOffset::UTC);
    time::PrimitiveDateTime::new(start.date(), start.time())
}

/// Poll `GET /api/jobs/current/dispenser/totaliser` every `POLL_INTERVAL` until
/// the dispense is complete; returns the final totaliser reading.
/// `expected_job_id` is asserted against the polled job, as in
/// [`poll_job_status`].
async fn poll_totaliser_complete(
    session: &mut Session<'_>,
    expected_job_id: &str,
) -> TotaliserResponse {
    let clock = session.clock;
    let client = session.client;
    let base = session.base;
    let url = format!("{base}/api/jobs/current/dispenser/totaliser");
    loop {
        let response = session
            .send("GET /api/jobs/current/dispenser/totaliser", |tok| {
                client.get(&url).bearer_auth(tok)
            })
            .await;
        let totaliser: TotaliserResponse =
            json_ok(clock, response, "GET /api/jobs/current/dispenser/totaliser").await;
        assert_eq!(
            totaliser.job_id, expected_job_id,
            "dock_operator: totaliser poll returned job {} but the operator's live job is {expected_job_id}",
            totaliser.job_id
        );
        if totaliser.status == "complete" {
            return totaliser;
        }
        clock.sleep(POLL_INTERVAL).await;
    }
}
