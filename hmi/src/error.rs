use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

pub type Result<T> = std::result::Result<T, HmiError>;

#[derive(Debug, thiserror::Error)]
pub enum HmiError {
    /// The FMS could not be reached (connection refused, timeout, reset). The
    /// HMI is a proxy, so a failure to reach its upstream is reported as 502 Bad
    /// Gateway, not 500 — the fault is upstream, not in the HMI itself.
    #[error("upstream request failed: {0}")]
    Upstream(#[from] reqwest::Error),
    /// An FMS error response, forwarded verbatim: the FMS status code and its
    /// JSON body. The HMI no longer re-maps individual upstream statuses onto its
    /// own, so a meaningful rejection (for example a 422 cavitation refusal) is
    /// passed through unchanged rather than collapsed into a generic 500.
    #[error("upstream returned status {status}: {body}")]
    UpstreamForwarded { status: StatusCode, body: Value },
    /// The FMS reply could not be parsed into the expected shape. Reported as 502
    /// because the upstream response, not the HMI, is at fault.
    #[error("invalid response from upstream")]
    InvalidResponse,
    /// No bearer token on a request that requires one. Generated in the HMI
    /// before any upstream call, so there is no upstream status to forward.
    #[error("unauthorised")]
    Unauthorised,
    /// The presented operator card could not be read during authentication.
    #[error("rfid mismatch")]
    RfidMismatch,
    #[allow(dead_code)]
    #[error("forbidden")]
    Forbidden,
    /// The login challenge expired or was already consumed before the RFID tap.
    #[error("challenge expired")]
    ChallengeExpired,
    /// A job identifier in the request path that is not a UUID. The FMS
    /// identifies every job by a UUID (fms.md §7.4), so the request is refused
    /// before anything reaches the FMS.
    #[error("invalid job id")]
    InvalidJobId,
}

impl IntoResponse for HmiError {
    fn into_response(self) -> Response {
        let (status, body) = match &self {
            HmiError::UpstreamForwarded { status, body } => (*status, body.clone()),
            HmiError::Upstream(_) => (
                StatusCode::BAD_GATEWAY,
                json!({ "error": "upstream_unreachable" }),
            ),
            HmiError::InvalidResponse => (
                StatusCode::BAD_GATEWAY,
                json!({ "error": "invalid_upstream_response" }),
            ),
            HmiError::Unauthorised => (
                StatusCode::UNAUTHORIZED,
                json!({ "error": "invalid_credentials" }),
            ),
            HmiError::RfidMismatch => (
                StatusCode::UNAUTHORIZED,
                json!({ "error": "rfid_mismatch" }),
            ),
            HmiError::Forbidden => (StatusCode::FORBIDDEN, json!({ "error": "forbidden" })),
            HmiError::ChallengeExpired => (
                StatusCode::REQUEST_TIMEOUT,
                json!({ "error": "challenge_expired" }),
            ),
            HmiError::InvalidJobId => (
                StatusCode::BAD_REQUEST,
                json!({ "error": "invalid_job_id" }),
            ),
        };

        // Every error response is logged with its reason before it leaves the
        // HMI: server-side faults (5xx) at ERROR, client rejections (4xx) at
        // WARN. Without this a forwarded rejection was invisible in the HMI's
        // journal, so a crash caused by it could not be traced from HMI logs.
        if status.is_server_error() {
            tracing::error!(status = %status, error = %self, "HMI returning server error");
        } else {
            tracing::warn!(status = %status, error = %self, "HMI returning client error");
        }

        (status, axum::Json(body)).into_response()
    }
}
