use std::net::SocketAddr;

use axum::{
    extract::{ConnectInfo, Extension, State},
    http::StatusCode,
    Json,
};
use chrono::Utc;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use serde_json::json;

use crate::{
    error::{AppError, Result},
    state::{AppState, EventKind, LogEvent, Session},
};

/// The role the attempt was made against, derived the same way the login path
/// itself decides which branch to take (fms.md §4).
fn role_attempted(state: &AppState, username: &str) -> &'static str {
    if username == state.config.admin_username {
        "admin"
    } else {
        "operator"
    }
}

/// Record a rejected attempt. The HTTP response stays a generic 401 (fms.md §4);
/// `reason` is the granularity that exists only in the audit trail, and the
/// syslog event is the sole record — nothing is written to `sessions`.
fn emit_login_failed(state: &AppState, username: &str, source: &SocketAddr, reason: &str) {
    state.emit_log_event(LogEvent::new(
        EventKind::LoginFailed,
        Utc::now(),
        json!({
            "username": username,
            "role_attempted": role_attempted(state, username),
            // The peer address without its ephemeral port: the port identifies
            // one connection, not one client, so it is noise to an audit.
            "source_address": source.ip().to_string(),
            "reason": reason,
        }),
    ));
}

#[derive(Deserialize)]
pub struct LoginRequest {
    username: String,
    password: String,
    rfid_card_number: Option<i32>,
}

#[derive(Serialize)]
pub struct LoginResponse {
    session_token: String,
    role: String,
}

pub async fn login(
    State(state): State<AppState>,
    ConnectInfo(source): ConnectInfo<SocketAddr>,
    Json(body): Json<LoginRequest>,
) -> Result<(StatusCode, Json<LoginResponse>)> {
    let (role, session_id) = if body.username == state.config.admin_username {
        // Local admin: argon2 verify, no RFID required.
        let hash = state.config.admin_password_hash.clone();
        let password = body.password.clone();
        let verified = tokio::task::spawn_blocking(move || -> bool {
            use argon2::{Argon2, PasswordHash, PasswordVerifier};
            let Ok(parsed) = PasswordHash::new(&hash) else {
                return false;
            };
            Argon2::default()
                .verify_password(password.as_bytes(), &parsed)
                .is_ok()
        })
        .await
        .map_err(|e| AppError::Internal(anyhow::anyhow!("{e}")))?;

        if !verified {
            emit_login_failed(&state, &body.username, &source, "invalid_credentials");
            return Err(AppError::Unauthorised);
        }
        ("admin".to_owned(), Uuid::new_v4())
    } else {
        // Operator: LDAP simple-bind + RFID card table lookup.
        // No card number supplied: rejected before any lookup, so neither
        // "invalid_credentials" nor "invalid_rfid" describes it (fms.md §4).
        let Some(rfid) = body.rfid_card_number else {
            emit_login_failed(&state, &body.username, &source, "missing_rfid");
            return Err(AppError::Unauthorised);
        };

        // LDAP bind
        let uri = state.config.ldap_uri.clone();
        let base_dn = state.config.ldap_base_dn.clone();
        let username = body.username.clone();
        let password = body.password.clone();
        // A directory that cannot be reached, and a password the directory
        // rejects, are the same 401 to the client: the response must not let a
        // caller discover which accounts exist (fms.md §4). They are separated
        // here instead, because the transport failures — an untrusted or
        // expired certificate, a name that does not resolve, a dropped route
        // to corp-it — otherwise present only as every operator login being
        // refused, which is indistinguishable from a mistyped password.
        let ldap_ok = tokio::task::spawn_blocking(move || -> bool {
            let bind_dn = format!("uid={username},ou=people,{base_dn}");
            match ldap3::LdapConn::new(&uri) {
                Ok(mut conn) => match conn.simple_bind(&bind_dn, &password) {
                    Ok(result) => result.success().is_ok(),
                    Err(e) => {
                        tracing::warn!(
                            uri = %uri,
                            "directory bind failed before a password could be \
                             verified: {e}"
                        );
                        false
                    }
                },
                Err(e) => {
                    tracing::warn!(
                        uri = %uri,
                        "directory unreachable; no operator login can succeed \
                         until it is restored: {e}"
                    );
                    false
                }
            }
        })
        .await
        .map_err(|e| AppError::Internal(anyhow::anyhow!("{e}")))?;

        if !ldap_ok {
            emit_login_failed(&state, &body.username, &source, "invalid_credentials");
            return Err(AppError::Unauthorised);
        }

        // RFID card table lookup: the card must be active and enrolled to
        // this operator, so a vessel tag or another operator's card is refused.
        let active = crate::db::is_card_active_for(&state.db, rfid, &body.username).await?;
        if !active {
            emit_login_failed(&state, &body.username, &source, "invalid_rfid");
            return Err(AppError::Unauthorised);
        }

        ("operator".to_owned(), Uuid::new_v4())
    };

    // Generate session token and store in memory
    let token = Uuid::new_v4().to_string();
    let session = Session {
        session_id,
        username: body.username.clone(),
        role: role.clone(),
        expires_at: Utc::now() + state.config.session_timeout,
    };
    state.sessions.write().await.insert(token.clone(), session);

    // Persist login event
    sqlx::query!(
        "INSERT INTO sessions (session_id, username, role) VALUES ($1, $2, $3)",
        session_id,
        body.username,
        role,
    )
    .execute(&state.db)
    .await?;

    state.emit_log_event(LogEvent::new(
        EventKind::Login,
        Utc::now(),
        json!({
            "username": body.username,
            "role": role,
            "session_id": session_id,
            "source_address": source.ip().to_string(),
            "result": "success",
        }),
    ));

    Ok((
        StatusCode::OK,
        Json(LoginResponse {
            session_token: token,
            role,
        }),
    ))
}

pub async fn logout(
    State(state): State<AppState>,
    Extension(session): Extension<Session>,
) -> StatusCode {
    // Remove the specific token from the session map.
    let mut sessions = state.sessions.write().await;
    sessions.retain(|_, v| v.session_id != session.session_id);
    StatusCode::NO_CONTENT
}
