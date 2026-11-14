use axum::{
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

use crate::state::JobValidationError;

/// A refusal a caller is expected to act on programmatically (fms.md §7). The
/// code identifies the refusal, so the description may be reworded freely, and
/// each code is sent with exactly one status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefusalCode {
    /// The current or projected closing volume is at or below the cavitation
    /// threshold (fms.md §5.1).
    CavitationThreshold,
    /// A cavitation check could not be evaluated because the on-demand ATG read
    /// failed (fms.md §5.1).
    AtgReadFailed,
    /// No ATG reading is available yet (fms.md §7.3).
    NoAtgReading,
}

impl RefusalCode {
    /// The code as the response body carries it.
    pub fn as_str(self) -> &'static str {
        match self {
            RefusalCode::CavitationThreshold => "cavitation_threshold",
            RefusalCode::AtgReadFailed => "atg_read_failed",
            RefusalCode::NoAtgReading => "no_atg_reading",
        }
    }

    /// The status the code is sent with (fms.md §7).
    pub fn status(self) -> StatusCode {
        match self {
            RefusalCode::CavitationThreshold => StatusCode::UNPROCESSABLE_ENTITY,
            RefusalCode::AtgReadFailed | RefusalCode::NoAtgReading => {
                StatusCode::SERVICE_UNAVAILABLE
            }
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("not found")]
    NotFound,

    #[error("method not allowed")]
    MethodNotAllowed,

    #[error("unauthorised")]
    Unauthorised,

    #[error("forbidden")]
    Forbidden,

    /// A refusal carrying a code (fms.md §7); its status follows from the code.
    #[error("{1}")]
    Refused(RefusalCode, String),

    #[error("{0}")]
    Conflict(String),

    #[error("{0}")]
    UnprocessableEntity(String),

    #[error(transparent)]
    Database(#[from] sqlx::Error),

    #[error(transparent)]
    Internal(#[from] anyhow::Error),
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match &self {
            AppError::NotFound => (StatusCode::NOT_FOUND, self.to_string()),
            AppError::MethodNotAllowed => (StatusCode::METHOD_NOT_ALLOWED, self.to_string()),
            AppError::Unauthorised => (StatusCode::UNAUTHORIZED, self.to_string()),
            AppError::Forbidden => (StatusCode::FORBIDDEN, self.to_string()),
            AppError::Refused(code, m) => (code.status(), m.clone()),
            AppError::Conflict(m) => (StatusCode::CONFLICT, m.clone()),
            AppError::UnprocessableEntity(m) => (StatusCode::UNPROCESSABLE_ENTITY, m.clone()),
            AppError::Database(e) => {
                tracing::error!("database error: {e}");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal error".to_owned(),
                )
            }
            AppError::Internal(e) => {
                tracing::error!("internal error: {e}");
                (
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal error".to_owned(),
                )
            }
        };
        // Log the reason for every rejection. 5xx variants log their (redacted)
        // detail above; here we record the 4xx rejections at WARN with the
        // client-visible message. A rejection such as the 422 cavitation refusal
        // was previously invisible in the FMS journal, so a downstream failure it
        // triggered could not be traced back to its cause. A refusal carrying a
        // code is recorded with its status, code and description whatever its
        // status, a 503 included (fms.md §7).
        if let AppError::Refused(code, _) = &self {
            tracing::warn!(status = %status, code = code.as_str(), "request refused: {message}");
            let body = json!({ "error": message, "code": code.as_str() });
            return (status, Json(body)).into_response();
        }
        if status.is_client_error() {
            tracing::warn!(status = %status, "request rejected: {message}");
        }
        (status, Json(json!({ "error": message }))).into_response()
    }
}

impl From<JobValidationError> for AppError {
    fn from(e: JobValidationError) -> Self {
        AppError::UnprocessableEntity(e.to_string())
    }
}

pub type Result<T> = std::result::Result<T, AppError>;

#[cfg(test)]
mod tests {
    use super::*;
    use axum::response::IntoResponse;
    use http_body_util::BodyExt;

    async fn decode(err: AppError) -> (u16, serde_json::Value) {
        let response = err.into_response();
        let status = response.status().as_u16();
        let bytes = response.into_body().collect().await.unwrap().to_bytes();
        let json: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        (status, json)
    }

    #[tokio::test]
    async fn not_found_is_404() {
        let (status, body) = decode(AppError::NotFound).await;
        assert_eq!(status, 404);
        assert_eq!(body["error"], "not found");
    }

    #[tokio::test]
    async fn unauthorised_is_401() {
        let (status, body) = decode(AppError::Unauthorised).await;
        assert_eq!(status, 401);
        assert_eq!(body["error"], "unauthorised");
    }

    #[tokio::test]
    async fn forbidden_is_403() {
        let (status, body) = decode(AppError::Forbidden).await;
        assert_eq!(status, 403);
        assert_eq!(body["error"], "forbidden");
    }

    #[tokio::test]
    async fn conflict_is_409_with_message() {
        let (status, body) = decode(AppError::Conflict("already running".to_owned())).await;
        assert_eq!(status, 409);
        assert_eq!(body["error"], "already running");
    }

    #[tokio::test]
    async fn unprocessable_is_422_with_message() {
        let (status, body) =
            decode(AppError::UnprocessableEntity("invalid input".to_owned())).await;
        assert_eq!(status, 422);
        assert_eq!(body["error"], "invalid input");
    }

    /// Each code goes out with its own status, the code and the description
    /// (fms.md §7).
    #[tokio::test]
    async fn refusals_carry_their_code_with_its_status() {
        for (code, status, text) in [
            (
                RefusalCode::CavitationThreshold,
                422,
                "cavitation_threshold",
            ),
            (RefusalCode::AtgReadFailed, 503, "atg_read_failed"),
            (RefusalCode::NoAtgReading, 503, "no_atg_reading"),
        ] {
            let (got, body) = decode(AppError::Refused(code, "a description".to_owned())).await;
            assert_eq!(got, status, "{text}");
            assert_eq!(body["code"], text);
            assert_eq!(body["error"], "a description");
        }
    }

    /// An error without a code keeps the one-field body.
    #[tokio::test]
    async fn a_rejection_without_a_code_has_no_code_field() {
        let (_, body) = decode(AppError::UnprocessableEntity("invalid input".to_owned())).await;
        assert!(body.get("code").is_none(), "unexpected code in {body}");
    }

    #[tokio::test]
    async fn database_error_is_500_with_generic_body() {
        let db_err = sqlx::Error::RowNotFound;
        let (status, body) = decode(AppError::Database(db_err)).await;
        assert_eq!(status, 500);
        assert_eq!(body["error"], "internal error");
    }

    #[tokio::test]
    async fn internal_error_is_500_with_generic_body() {
        let (status, body) = decode(AppError::Internal(anyhow::anyhow!("secret detail"))).await;
        assert_eq!(status, 500);
        assert_eq!(body["error"], "internal error");
        // Verify the internal detail is not leaked.
        assert!(!body["error"].as_str().unwrap().contains("secret"));
    }
}
