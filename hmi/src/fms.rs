use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::error::{HmiError, Result};

pub struct FmsClient {
    client: Client,
    base_url: String,
}

// ---------------------------------------------------------------------------
// FMS response types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct LoginResponse {
    pub session_token: String,
}

#[derive(Deserialize)]
pub struct JobResponse {
    pub job_id: Uuid,
    #[serde(rename = "type")]
    pub job_type: String,
    pub status: String,
    pub rfid_validated: bool,
    /// The dispense start, held as the FMS's text so it passes through
    /// unchanged (hmi.md); `None` before the start and for a fill.
    pub opened_at: Option<String>,
}

// ---------------------------------------------------------------------------
// FMS request types
// ---------------------------------------------------------------------------

/// The fill-close request body (`fms.md` §7): the delivery ticket as the
/// operator transcribed it, in the FMS's field names. Every value is the road
/// tanker's own, printed on its ticket; the HMI forwards them unchanged.
#[derive(Debug, Serialize)]
pub struct FillClose<'a> {
    pub ticket_number: &'a str,
    /// The start time printed on the ticket; the FMS records it as the
    /// transfer's `opened_at`.
    pub delivery_commenced_at: DateTime<Utc>,
    pub delivered_volume_l: f64,
    pub temperature_c: f64,
    pub density_kg_per_l: f64,
    pub temp_corrected_volume_l: f64,
    pub supplier: &'a str,
    pub tanker_registration: &'a str,
}

// ---------------------------------------------------------------------------
// HMI-facing types (translated from FMS)
// ---------------------------------------------------------------------------

#[derive(Serialize)]
pub struct HmiJobStatus {
    pub job_id: String,
    #[serde(rename = "type")]
    pub job_type: String,
    pub status: String,
    pub opened_at: Option<String>,
}

/// Turn a non-success FMS response into a forwarded error carrying the upstream
/// status code and body verbatim. Draining the body here means the caller only
/// reaches this helper on the error path, so the success path is untouched.
async fn forwarded(resp: reqwest::Response) -> HmiError {
    let status = axum::http::StatusCode::from_u16(resp.status().as_u16())
        .unwrap_or(axum::http::StatusCode::BAD_GATEWAY);
    let body = resp
        .json::<Value>()
        .await
        .unwrap_or_else(|_| json!({ "error": "upstream error" }));
    HmiError::UpstreamForwarded { status, body }
}

impl FmsClient {
    /// `ca_cert_pem`, when given, is the sole trusted root for this client — the
    /// built-in public root store is not consulted, matching a single internal CA
    /// as the trust anchor for every OT peer (digital.md §8 decision 7). Tests
    /// against a plain-HTTP mock FMS pass `None`.
    pub fn new(base_url: String, ca_cert_pem: Option<&[u8]>) -> anyhow::Result<Self> {
        let mut builder = Client::builder();
        if let Some(pem) = ca_cert_pem {
            let ca_cert = reqwest::Certificate::from_pem(pem)?;
            builder = builder
                .tls_built_in_root_certs(false)
                .add_root_certificate(ca_cert);
        }
        let client = builder.build()?;
        Ok(Self { client, base_url })
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    /// `POST /api/v1/auth/login` — returns session token.
    pub async fn login(
        &self,
        username: &str,
        password: &str,
        rfid_card_number: u32,
    ) -> Result<LoginResponse> {
        let resp = self
            .client
            .post(self.url("/api/v1/auth/login"))
            .json(&json!({
                "username": username,
                "password": password,
                "rfid_card_number": rfid_card_number,
            }))
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            Ok(resp.json().await.map_err(HmiError::Upstream)?)
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `DELETE /api/v1/auth/logout`
    pub async fn logout(&self, token: &str) -> Result<()> {
        self.client
            .delete(self.url("/api/v1/auth/logout"))
            .bearer_auth(token)
            .send()
            .await
            .map_err(HmiError::Upstream)?;
        Ok(())
    }

    /// `POST /api/v1/jobs` — create a dispense job.
    pub async fn create_dispense_job(
        &self,
        token: &str,
        vessel_id: &str,
        product: &str,
    ) -> Result<Uuid> {
        let resp = self
            .client
            .post(self.url("/api/v1/jobs"))
            .bearer_auth(token)
            .json(&json!({
                "type": "dispense",
                "counterparty": vessel_id,
                "grade": product,
            }))
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            let body: Value = resp.json().await.map_err(HmiError::Upstream)?;
            parse_job_id(&body)
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `POST /api/v1/jobs` — create a fill job.
    pub async fn create_fill_job(
        &self,
        token: &str,
        tanker_registration: &str,
        product: &str,
    ) -> Result<Uuid> {
        let resp = self
            .client
            .post(self.url("/api/v1/jobs"))
            .bearer_auth(token)
            .json(&json!({
                "type": "fill",
                "counterparty": tanker_registration,
                "grade": product,
            }))
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            let body: Value = resp.json().await.map_err(HmiError::Upstream)?;
            parse_job_id(&body)
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `GET /api/v1/jobs/current` — the live job, with translated status.
    pub async fn current_status(&self, token: &str) -> Result<HmiJobStatus> {
        let resp = self
            .client
            .get(self.url("/api/v1/jobs/current"))
            .bearer_auth(token)
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            let job: JobResponse = resp.json().await.map_err(HmiError::Upstream)?;
            let hmi_status = translate_status(&job.status, job.rfid_validated);
            let hmi_type = if job.job_type == "dispense" {
                "refuel"
            } else {
                "tank-fill"
            };
            Ok(HmiJobStatus {
                job_id: job.job_id.to_string(),
                job_type: hmi_type.to_string(),
                status: hmi_status,
                opened_at: job.opened_at,
            })
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `POST /api/v1/jobs/{job_id}/start` — start dispense.
    pub async fn start_dispense(
        &self,
        token: &str,
        job_id: &str,
        preset_volume_litres: f64,
    ) -> Result<()> {
        let resp = self
            .client
            .post(self.url(&format!("/api/v1/jobs/{job_id}/start")))
            .bearer_auth(token)
            .json(&json!({ "preset_volume": preset_volume_litres }))
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            Ok(())
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `DELETE /api/v1/jobs/{job_id}` — cancel an active job, freeing the single
    /// job slot (fms.md §7.4). The path is built from the UUID, so it names only
    /// that job.
    pub async fn cancel_job(&self, token: &str, job_id: Uuid) -> Result<()> {
        let resp = self
            .client
            .delete(self.url(&format!("/api/v1/jobs/{job_id}")))
            .bearer_auth(token)
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            Ok(())
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `POST /api/v1/jobs/{job_id}/bdn` — link a BDN to the job's dispense.
    /// The FMS resolves the dispense transaction from the ledger.
    pub async fn link_bdn_by_job(&self, token: &str, job_id: &str, bdn_number: &str) -> Result<()> {
        let resp = self
            .client
            .post(self.url(&format!("/api/v1/jobs/{job_id}/bdn")))
            .bearer_auth(token)
            .json(&json!({ "bdn_number": bdn_number }))
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            Ok(())
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `POST /api/v1/jobs/{job_id}/close` — close fill job with delivery ticket.
    pub async fn close_fill(
        &self,
        token: &str,
        job_id: &str,
        ticket: &FillClose<'_>,
    ) -> Result<Uuid> {
        let resp = self
            .client
            .post(self.url(&format!("/api/v1/jobs/{job_id}/close")))
            .bearer_auth(token)
            .json(ticket)
            .send()
            .await
            .map_err(HmiError::Upstream)?;

        if resp.status().is_success() {
            let body: Value = resp.json().await.map_err(HmiError::Upstream)?;
            body["transaction_id"]
                .as_str()
                .unwrap_or_default()
                .parse()
                .map_err(|_| HmiError::InvalidResponse)
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `GET /api/v1/tanks/{tank_id}/level`
    pub async fn get_tank_level(&self, token: &str, tank_id: &str) -> Result<Value> {
        let resp = self
            .client
            .get(self.url(&format!("/api/v1/tanks/{tank_id}/level")))
            .bearer_auth(token)
            .send()
            .await
            .map_err(HmiError::Upstream)?;
        if resp.status().is_success() {
            Ok(resp.json().await.map_err(HmiError::Upstream)?)
        } else {
            Err(forwarded(resp).await)
        }
    }

    /// `GET /api/v1/jobs/current/totaliser`
    pub async fn get_totaliser(&self, token: &str) -> Result<Value> {
        let resp = self
            .client
            .get(self.url("/api/v1/jobs/current/totaliser"))
            .bearer_auth(token)
            .send()
            .await
            .map_err(HmiError::Upstream)?;
        if resp.status().is_success() {
            Ok(resp.json().await.map_err(HmiError::Upstream)?)
        } else {
            Err(forwarded(resp).await)
        }
    }
}

/// Extract and parse the `job_id` field from an FMS job-creation body.
fn parse_job_id(body: &Value) -> Result<Uuid> {
    body["job_id"]
        .as_str()
        .unwrap_or_default()
        .parse()
        .map_err(|_| HmiError::InvalidResponse)
}

/// Translate FMS job status + rfid_validated flag to the HMI status string
/// the dock operator expects (hmi.md §8.2).
fn translate_status(fms_status: &str, rfid_validated: bool) -> String {
    match fms_status {
        "active" if !rfid_validated => "pending_vessel_rfid",
        "active" => "rfid_validated",
        "running" => "dispensing",
        "awaiting_bdn" => "awaiting_bdn",
        "closed" => "complete",
        other => other,
    }
    .to_string()
}

pub fn expires_at_from_now(secs: i64) -> DateTime<Utc> {
    Utc::now() + chrono::Duration::seconds(secs)
}
