//! Private implementation of the systems administrator actor.

use std::fmt;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use russh::client;
use russh::keys::ssh_key::PublicKey;
use russh::keys::PublicKeyOrCertificate;
use russh::{Channel, ChannelMsg, Disconnect};
use time::PrimitiveDateTime;
use tokio::sync::oneshot;

use super::SystemsAdministratorState;
use crate::clock::{IoGuard, SimClock};
use crate::stop::{rest, sleep_until, StopRequest};

const ENTITY: &str = "systems_administrator";
const ADMIN_HOST: &str = "admin-01";
const TERMINAL_TYPE: &str = "xterm";
const TERMINAL_COLUMNS: u32 = 120;
const TERMINAL_ROWS: u32 = 40;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PromptMatch {
    Primary,
    Fallback,
}

#[derive(Debug)]
struct SessionError(String);

impl SessionError {
    fn context(context: &str, error: impl fmt::Display) -> Self {
        Self(format!("{context}: {error}"))
    }
}

impl fmt::Display for SessionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

#[async_trait::async_trait]
trait AdministrationSession: Send {
    async fn type_command(
        &mut self,
        command: &str,
        primary_prompt: &str,
        fallback_prompt: Option<&str>,
        deadline: Duration,
    ) -> Result<PromptMatch, SessionError>;

    async fn recover_to_admin(
        &mut self,
        managed_host: &str,
        deadline: Duration,
    ) -> Result<(), SessionError>;

    async fn disconnect(&mut self, deadline: Duration);
}

#[async_trait::async_trait]
trait SessionConnector: Send + Sync + 'static {
    type Session: AdministrationSession;

    async fn connect(
        &self,
        clock: &SimClock,
        deadline: Duration,
    ) -> Result<Self::Session, SessionError>;
}

struct PinnedHostKey {
    expected: PublicKey,
}

impl client::Handler for PinnedHostKey {
    type Error = russh::Error;

    async fn check_server_key(
        &mut self,
        server_key: &PublicKeyOrCertificate,
    ) -> Result<bool, Self::Error> {
        // The deployment pins a bare Ed25519 host key, not a certificate.
        Ok(server_key.certificate().is_none() && server_key.public_key() == self.expected)
    }
}

struct RusshConnector {
    egress_addr: SocketAddr,
    username: &'static str,
    password: &'static str,
    expected_host_key: PublicKey,
}

impl RusshConnector {
    fn new(egress_addr: SocketAddr) -> Self {
        let expected_host_key = PublicKey::from_openssh(crate::constants::ADMIN_HOST_KEY)
            .expect("systems_administrator: generated admin-01 host key is invalid");
        Self {
            egress_addr,
            username: crate::constants::SYSTEMS_ADMINISTRATOR_USERNAME,
            password: crate::constants::SYSTEMS_ADMINISTRATOR_PASSWORD,
            expected_host_key,
        }
    }
}

#[async_trait::async_trait]
impl SessionConnector for RusshConnector {
    type Session = RusshSession;

    async fn connect(
        &self,
        clock: &SimClock,
        deadline: Duration,
    ) -> Result<Self::Session, SessionError> {
        // Prevent synthetic time from advancing while a real SSH exchange is
        // in flight. The guard remains with the session until it disconnects.
        let io_guard = clock.io_guard();
        let config = Arc::new(client::Config {
            inactivity_timeout: Some(deadline),
            ..Default::default()
        });
        let handler = PinnedHostKey {
            expected: self.expected_host_key.clone(),
        };
        let mut handle = client::connect(config, self.egress_addr, handler)
            .await
            .map_err(|error| SessionError::context("SSH connection failed", error))?;

        // Russh completes host-key verification during connect, before this
        // call can put the local workstation password on the wire.
        let authentication = handle
            .authenticate_password(self.username, self.password)
            .await
            .map_err(|error| SessionError::context("password authentication failed", error))?;
        if !authentication.success() {
            return Err(SessionError(
                "workstation credential was rejected".to_string(),
            ));
        }

        let channel = handle
            .channel_open_session()
            .await
            .map_err(|error| SessionError::context("session channel failed", error))?;
        channel
            .request_pty(
                true,
                TERMINAL_TYPE,
                TERMINAL_COLUMNS,
                TERMINAL_ROWS,
                0,
                0,
                &[],
            )
            .await
            .map_err(|error| SessionError::context("pseudo-terminal request failed", error))?;
        channel
            .request_shell(true)
            .await
            .map_err(|error| SessionError::context("shell request failed", error))?;

        let mut session = RusshSession {
            handle,
            channel,
            _io_guard: io_guard,
        };
        session.wait_for_prompt(ADMIN_HOST, None, deadline).await?;
        Ok(session)
    }
}

struct RusshSession {
    handle: client::Handle<PinnedHostKey>,
    channel: Channel<client::Msg>,
    _io_guard: IoGuard,
}

impl RusshSession {
    async fn send(&mut self, bytes: &[u8]) -> Result<(), SessionError> {
        self.channel
            .data(bytes)
            .await
            .map_err(|error| SessionError::context("shell write failed", error))
    }

    async fn wait_for_prompt(
        &mut self,
        primary: &str,
        fallback: Option<&str>,
        deadline: Duration,
    ) -> Result<(PromptMatch, String), SessionError> {
        let receive = async {
            let mut output = Vec::new();
            loop {
                let message = self.channel.wait().await.ok_or_else(|| {
                    SessionError("SSH shell closed before its prompt".to_string())
                })?;
                match message {
                    ChannelMsg::Data { data } | ChannelMsg::ExtendedData { data, .. } => {
                        output.extend_from_slice(&data);
                        if prompt_is_for(&output, primary) {
                            return Ok((
                                PromptMatch::Primary,
                                String::from_utf8_lossy(&output).into_owned(),
                            ));
                        }
                        if fallback.is_some_and(|host| prompt_is_for(&output, host)) {
                            return Ok((
                                PromptMatch::Fallback,
                                String::from_utf8_lossy(&output).into_owned(),
                            ));
                        }
                    }
                    ChannelMsg::Close | ChannelMsg::Eof => {
                        return Err(SessionError(
                            "SSH shell closed before its prompt".to_string(),
                        ));
                    }
                    _ => {}
                }
            }
        };

        tokio::time::timeout(deadline, receive)
            .await
            .map_err(|_| SessionError(format!("prompt for {primary} timed out")))?
    }
}

#[async_trait::async_trait]
impl AdministrationSession for RusshSession {
    async fn type_command(
        &mut self,
        command: &str,
        primary_prompt: &str,
        fallback_prompt: Option<&str>,
        deadline: Duration,
    ) -> Result<PromptMatch, SessionError> {
        self.send(format!("{command}\n").as_bytes()).await?;
        let (matched, output) = self
            .wait_for_prompt(primary_prompt, fallback_prompt, deadline)
            .await?;
        tracing::debug!(
            host = primary_prompt,
            %command,
            output = output.trim_end(),
            "systems-administrator command completed"
        );
        Ok(matched)
    }

    async fn recover_to_admin(
        &mut self,
        managed_host: &str,
        deadline: Duration,
    ) -> Result<(), SessionError> {
        self.send(b"\x03").await?;
        let (matched, _) = self
            .wait_for_prompt(ADMIN_HOST, Some(managed_host), deadline)
            .await?;
        if matched == PromptMatch::Fallback {
            self.type_command("exit", ADMIN_HOST, None, deadline)
                .await?;
        }
        Ok(())
    }

    async fn disconnect(&mut self, deadline: Duration) {
        // `exit` closes the interactive shell. The protocol disconnect below
        // is still sent if the shell does not close within the step deadline.
        if self.send(b"exit\n").await.is_ok() {
            let _ = tokio::time::timeout(deadline, async {
                while let Some(message) = self.channel.wait().await {
                    if matches!(message, ChannelMsg::Close | ChannelMsg::Eof) {
                        break;
                    }
                }
            })
            .await;
        }
        let _ = self
            .handle
            .disconnect(Disconnect::ByApplication, "", "en")
            .await;
    }
}

fn prompt_is_for(output: &[u8], host: &str) -> bool {
    let line = output
        .rsplit(|byte| *byte == b'\n')
        .next()
        .unwrap_or_default();
    let has_prompt_suffix = line.ends_with(b"$ ") || line.ends_with(b"# ");
    has_prompt_suffix
        && line
            .windows(host.len())
            .any(|window| window == host.as_bytes())
}

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    start: SystemsAdministratorState,
    stop: StopRequest,
    report: oneshot::Sender<SystemsAdministratorState>,
    egress_addr: SocketAddr,
    managed_hosts: Vec<String>,
    check_interval: Duration,
    step_deadline: Duration,
) -> tokio::task::JoinHandle<()> {
    spawn_with_connector(
        clock,
        start,
        stop,
        report,
        managed_hosts,
        check_interval,
        step_deadline,
        RusshConnector::new(egress_addr),
    )
}

pub(super) async fn run_once(
    clock: SimClock,
    egress_addr: SocketAddr,
    managed_hosts: Vec<String>,
    step_deadline: Duration,
) -> bool {
    run_once_with_connector(
        &clock,
        &managed_hosts,
        None,
        step_deadline,
        &RusshConnector::new(egress_addr),
    )
    .await
}

/// The actor's schedule (sim-impl.md, "Systems administrator", "Cadence"):
/// sleep until the due time, kept rather than the remaining duration, raced
/// against the stop request; run one session; and set the next due time from
/// the session's end. The stop request is checked only before a session, so a
/// session in progress runs to its end before the actor reports.
#[allow(clippy::too_many_arguments)]
fn spawn_with_connector<C: SessionConnector>(
    clock: SimClock,
    start: SystemsAdministratorState,
    mut stop: StopRequest,
    report: oneshot::Sender<SystemsAdministratorState>,
    managed_hosts: Vec<String>,
    check_interval: Duration,
    step_deadline: Duration,
    connector: C,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        // The first session is due at `start.next_check_at`: on a cold start the
        // start state's first check after the start (Start::cold), on a warm
        // start the checkpoint's due time.
        let mut state = start;
        loop {
            tokio::select! {
                biased;
                _ = stop.requested() => rest(ENTITY, report, state).await,
                _ = sleep_until(&clock, state.next_check_at) => {}
            }

            let session_started = clock.now_datetime();
            run_once_with_connector(
                &clock,
                &managed_hosts,
                state.previous_check_at,
                step_deadline,
                &connector,
            )
            .await;
            state = SystemsAdministratorState {
                next_check_at: clock.now_datetime() + check_interval,
                previous_check_at: Some(session_started),
            };
        }
    })
}

async fn run_once_with_connector<C: SessionConnector>(
    clock: &SimClock,
    managed_hosts: &[String],
    previous_check: Option<PrimitiveDateTime>,
    step_deadline: Duration,
    connector: &C,
) -> bool {
    match connector.connect(clock, step_deadline).await {
        Ok(mut session) => {
            let succeeded =
                run_session(&mut session, managed_hosts, previous_check, step_deadline).await;
            session.disconnect(step_deadline).await;
            succeeded
        }
        Err(error) => {
            tracing::warn!(%error, "systems-administrator session failed");
            false
        }
    }
}

async fn run_session<S: AdministrationSession>(
    session: &mut S,
    managed_hosts: &[String],
    previous_check: Option<PrimitiveDateTime>,
    step_deadline: Duration,
) -> bool {
    let commands = routine_commands(previous_check);
    let mut succeeded = true;
    for host in managed_hosts {
        let enter = session
            .type_command(
                &format!("ssh {host}"),
                host,
                Some(ADMIN_HOST),
                step_deadline,
            )
            .await;
        match enter {
            Ok(PromptMatch::Primary) => {}
            Ok(PromptMatch::Fallback) => {
                tracing::warn!(%host, "managed host is unreachable; skipping check");
                succeeded = false;
                continue;
            }
            Err(error) => {
                tracing::warn!(%host, %error, "managed-host connection failed");
                succeeded = false;
                if let Err(recovery) = session.recover_to_admin(host, step_deadline).await {
                    tracing::warn!(%host, error = %recovery, "could not recover workstation shell");
                    return false;
                }
                continue;
            }
        }

        let mut step_failed = false;
        for command in &commands {
            if let Err(error) = session
                .type_command(command, host, None, step_deadline)
                .await
            {
                tracing::warn!(%host, %command, %error, "routine check step failed");
                succeeded = false;
                step_failed = true;
                break;
            }
        }

        if step_failed {
            if let Err(error) = session.recover_to_admin(host, step_deadline).await {
                tracing::warn!(%host, %error, "could not recover workstation shell");
                return false;
            }
            continue;
        }

        if let Err(error) = session
            .type_command("exit", ADMIN_HOST, None, step_deadline)
            .await
        {
            tracing::warn!(%host, %error, "managed-host shell did not exit cleanly");
            succeeded = false;
            if session.recover_to_admin(host, step_deadline).await.is_err() {
                return false;
            }
        }
    }
    succeeded
}

fn routine_commands(previous_check: Option<PrimitiveDateTime>) -> [String; 4] {
    let journal = match previous_check {
        Some(previous) => format!("sudo journalctl --priority=err --since '{}'", previous),
        None => "sudo journalctl --priority=err --boot".to_string(),
    };
    [
        "systemctl --failed".to_string(),
        "df -h".to_string(),
        "chronyc tracking".to_string(),
        journal,
    ]
}

#[cfg(test)]
mod tests;
