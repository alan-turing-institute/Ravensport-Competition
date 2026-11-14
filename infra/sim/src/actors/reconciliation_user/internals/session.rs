//! The session with the workstation (sim-impl.md, "Session"): the scancode
//! table, the frame inspector that offers clipboard chunks to the guard, the
//! TLS configuration that verifies the workstation, and the procedure the
//! audit follows inside the session, written against the [`Workstation`] the
//! IronRDP session implements.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use ironrdp_cliprdr::pdu::ClipboardFormatId;
use ironrdp_cliprdr::{Client, CliprdrClient, CliprdrSvcMessages};
use ironrdp_connector::{
    self as connector, ClientConnector, ConnectorErrorExt, Credentials, ServerName,
};
use ironrdp_input::{Database, Operation, Scancode};
use ironrdp_pdu::gcc::{ConnectionType, KeyboardType};
use ironrdp_pdu::mcs::SendDataIndication;
use ironrdp_pdu::rdp::capability_sets::{MajorPlatformType, RailSupportLevel};
use ironrdp_pdu::rdp::client_info::{PerformanceFlags, TimezoneInfo};
use ironrdp_pdu::x224::X224;
use ironrdp_pdu::{Action, PduError};
use ironrdp_session::image::DecodedImage;
use ironrdp_session::{ActiveStage, ActiveStageBuilder, ActiveStageOutput};
use ironrdp_tokio::{FramedWrite as _, NetworkClient, TokioFramed};
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::CertificateDer;
use rustls::{ClientConfig, RootCertStore};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpSocket, TcpStream};
use tokio_rustls::client::TlsStream;
use tokio_rustls::TlsConnector;

use super::audit::Connector;
use super::clipboard::{
    channel_bound, ChunkHeader, Clipboard, ClipboardChannel, ClipboardRead, ReassemblyGuard,
    CLIPBOARD_CEILING_BYTES,
};
use super::{AuditFailure, AuditWindow, ClipboardFailure, TerminalFailure};
use crate::clock::SimClock;

// --- The scancode table ----------------------------------------------------------

pub(super) const SHIFT: u8 = 0x2A;
pub(super) const CTRL: u8 = 0x1D;
pub(super) const ALT: u8 = 0x38;
pub(super) const ENTER: u8 = 0x1C;
const T: u8 = 0x14;

/// One key event, as PS/2 Set 1 scancodes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum KeyEvent {
    Press(u8),
    Release(u8),
}

/// The key for `c` under the `gb` layout `recon.md` §5.2 pins, and whether
/// `Shift` is held. Scancodes name physical keys, so the table is written
/// against that layout: `0x56`, the key beside the left `Shift`, carries `\`
/// and `|`, and `0x2B` carries `#` and `~`.
pub(super) fn key_for(c: char) -> Option<(bool, u8)> {
    #[rustfmt::skip]
    const UNSHIFTED: &[(char, u8)] = &[
        ('1', 0x02), ('2', 0x03), ('3', 0x04), ('4', 0x05), ('5', 0x06),
        ('6', 0x07), ('7', 0x08), ('8', 0x09), ('9', 0x0A), ('0', 0x0B),
        ('-', 0x0C), ('=', 0x0D),
        ('q', 0x10), ('w', 0x11), ('e', 0x12), ('r', 0x13), ('t', 0x14),
        ('y', 0x15), ('u', 0x16), ('i', 0x17), ('o', 0x18), ('p', 0x19),
        ('[', 0x1A), (']', 0x1B),
        ('a', 0x1E), ('s', 0x1F), ('d', 0x20), ('f', 0x21), ('g', 0x22),
        ('h', 0x23), ('j', 0x24), ('k', 0x25), ('l', 0x26), (';', 0x27),
        ('\'', 0x28), ('`', 0x29), ('#', 0x2B), ('\\', 0x56),
        ('z', 0x2C), ('x', 0x2D), ('c', 0x2E), ('v', 0x2F), ('b', 0x30),
        ('n', 0x31), ('m', 0x32), (',', 0x33), ('.', 0x34), ('/', 0x35),
        (' ', 0x39),
    ];
    #[rustfmt::skip]
    const SHIFTED: &[(char, u8)] = &[
        ('!', 0x02), ('"', 0x03), ('$', 0x05), ('%', 0x06), ('^', 0x07),
        ('&', 0x08), ('*', 0x09), ('(', 0x0A), (')', 0x0B), ('_', 0x0C),
        ('+', 0x0D), ('{', 0x1A), ('}', 0x1B), (':', 0x27), ('@', 0x28),
        ('~', 0x2B), ('|', 0x56), ('<', 0x33), ('>', 0x34), ('?', 0x35),
    ];
    if c.is_ascii_uppercase() {
        return key_for(c.to_ascii_lowercase()).map(|(_, code)| (true, code));
    }
    if let Some(&(_, code)) = UNSHIFTED.iter().find(|(key, _)| *key == c) {
        return Some((false, code));
    }
    SHIFTED
        .iter()
        .find(|(key, _)| *key == c)
        .map(|&(_, code)| (true, code))
}

/// The key events that type `line` and then `Enter`, or the first character
/// the table lacks: no part of a line is typed unless all of it can be.
pub(super) fn keys_for_line(line: &str) -> Result<Vec<KeyEvent>, char> {
    let mut events = Vec::new();
    for c in line.chars() {
        let (shift, code) = key_for(c).ok_or(c)?;
        if shift {
            events.push(KeyEvent::Press(SHIFT));
        }
        events.extend([KeyEvent::Press(code), KeyEvent::Release(code)]);
        if shift {
            events.push(KeyEvent::Release(SHIFT));
        }
    }
    events.extend([KeyEvent::Press(ENTER), KeyEvent::Release(ENTER)]);
    Ok(events)
}

/// `Ctrl+Alt+T`, the binding that opens a terminal (`recon.md` §5.4).
pub(super) fn open_terminal_keys() -> [KeyEvent; 6] {
    [
        KeyEvent::Press(CTRL),
        KeyEvent::Press(ALT),
        KeyEvent::Press(T),
        KeyEvent::Release(T),
        KeyEvent::Release(ALT),
        KeyEvent::Release(CTRL),
    ]
}

// --- TLS ------------------------------------------------------------------------

/// The name the workstation's certificate is verified against. The address
/// dialled is the proxy's loopback port and names nothing.
pub(super) const WORKSTATION_NAME: &str = "recon-01";

/// A TLS configuration trusting the deployment's root alone, with the `ring`
/// provider named here rather than taken from a process-wide default.
pub(super) fn tls_config(root_pem: &[u8]) -> Result<Arc<ClientConfig>, String> {
    let mut roots = RootCertStore::empty();
    for certificate in CertificateDer::pem_slice_iter(root_pem) {
        let certificate = certificate.map_err(|e| format!("the root is not PEM: {e}"))?;
        roots
            .add(certificate)
            .map_err(|e| format!("the root is not a usable certificate: {e}"))?;
    }
    if roots.is_empty() {
        return Err("no root certificate given".to_string());
    }
    let config =
        ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .map_err(|e| format!("no protocol version the provider supports: {e}"))?
            .with_root_certificates(roots)
            .with_no_client_auth();
    Ok(Arc::new(config))
}

/// Upgrade `stream` to TLS, verifying the certificate presented against the
/// configuration's root and [`WORKSTATION_NAME`]. A certificate that does not
/// verify is `CertificateVerification`, and nothing has been sent inside the
/// session.
pub(super) async fn upgrade<S>(
    stream: S,
    config: Arc<ClientConfig>,
) -> Result<TlsStream<S>, AuditFailure>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let name = rustls::pki_types::ServerName::try_from(WORKSTATION_NAME)
        .expect("the workstation's name is a valid DNS name");
    TlsConnector::from(config)
        .connect(name, stream)
        .await
        .map_err(|e| {
            let refused = e
                .get_ref()
                .and_then(|inner| inner.downcast_ref::<rustls::Error>())
                .is_some_and(|tls| matches!(tls, rustls::Error::InvalidCertificate(_)));
            AuditFailure::Terminal(if refused {
                TerminalFailure::CertificateVerification
            } else {
                TerminalFailure::ConnectionFailed
            })
        })
}

// --- The frame inspector ---------------------------------------------------------

/// The channel header flag marking a message's first chunk.
const CHANNEL_FLAG_FIRST: u32 = 0x1;
/// The channel header flag marking a message's last chunk.
const CHANNEL_FLAG_LAST: u32 = 0x2;

/// The header of the chunk `frame` carries, if it is channel data on the
/// clipboard channel: the total length of the message, whether this chunk
/// begins or ends it, and how many bytes of it the chunk carries.
pub(super) fn clipboard_chunk(frame: &[u8], clipboard_channel: u16) -> Option<ChunkHeader> {
    let X224(indication) = ironrdp_core::decode::<X224<SendDataIndication<'_>>>(frame).ok()?;
    if indication.channel_id != clipboard_channel {
        return None;
    }
    let (header, payload) = indication.user_data.split_at_checked(8)?;
    let declared = u32::from_le_bytes(header[0..4].try_into().ok()?);
    let flags = u32::from_le_bytes(header[4..8].try_into().ok()?);
    Some(ChunkHeader {
        declared,
        first: flags & CHANNEL_FLAG_FIRST != 0,
        last: flags & CHANNEL_FLAG_LAST != 0,
        payload: payload.len(),
    })
}

// --- The procedure -----------------------------------------------------------------

/// The session as the audit drives it. The IronRDP session implements it, and
/// so does the double the tests drive.
#[allow(async_fn_in_trait)]
pub(super) trait Workstation {
    /// Wait for the desktop to start, before the session's first terminal:
    /// the binding that opens a terminal is not handled until it has.
    async fn await_desktop(&mut self) -> Result<(), AuditFailure>;
    /// Open a terminal and wait for it to settle.
    async fn open_terminal(&mut self) -> Result<(), AuditFailure>;
    /// Clear the clipboard latch, before typing the command whose copy is
    /// read next.
    async fn clear_latch(&mut self);
    async fn type_line(&mut self, line: &str) -> Result<(), AuditFailure>;
    /// Read the selection the command typed since the clear placed there.
    async fn read_clipboard(&mut self) -> Result<String, AuditFailure>;
    async fn disconnect(&mut self);
}

/// The file in the account's home directory the summary entries are appended
/// to (sim-impl.md, "Reporting").
const SUMMARY_FILE: &str = "~/reconciliation-audits.txt";

/// The value echoed to confirm the session: unique to the window and to the
/// attempt within it.
pub(super) fn latch_value(window: &AuditWindow, attempt: u32) -> String {
    let start = window.start;
    format!(
        "latch-{:04}{:02}{:02}{:02}{:02}{:02}-{attempt}",
        start.year(),
        u8::from(start.month()),
        start.day(),
        start.hour(),
        start.minute(),
        start.second(),
    )
}

/// The command placing `text` on the clipboard.
fn latch_command(value: &str) -> String {
    format!("echo {value} | xclip -selection clipboard")
}

fn append_command(entry: &str) -> String {
    format!("echo {entry} >> {SUMMARY_FILE}")
}

fn confirm_append_command() -> String {
    format!("tail -n 1 {SUMMARY_FILE} | xclip -selection clipboard")
}

/// What a command echoed reads back as: its text, without the line feed
/// `echo` and `tail` end it with.
fn echoed(read: &str) -> &str {
    read.strip_suffix('\n').unwrap_or(read)
}

/// Open a terminal and confirm, through the clipboard, that it is a shell
/// executing commands in this session (sim-impl.md, "Confirming the session").
/// Nothing further is typed if it is not.
pub(super) async fn open_and_confirm(
    workstation: &mut impl Workstation,
    value: &str,
) -> Result<(), AuditFailure> {
    workstation.open_terminal().await?;
    workstation.clear_latch().await;
    workstation.type_line(&latch_command(value)).await?;
    match workstation.read_clipboard().await {
        Ok(read) if echoed(&read) == value => Ok(()),
        Ok(_) => Err(AuditFailure::Terminal(TerminalFailure::LatchMismatch)),
        Err(failure) if failure.ends_the_session() => Err(failure),
        Err(_) => Err(AuditFailure::Terminal(TerminalFailure::LatchAbsent)),
    }
}

/// The summary entry was not confirmed on the workstation: reported at WARN
/// with the audit's own result, and not an audit failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct SummaryNotWritten;

/// Append the summary entry and confirm it through the clipboard.
pub(super) async fn record_summary(
    workstation: &mut impl Workstation,
    entry: &str,
) -> Result<(), SummaryNotWritten> {
    workstation
        .type_line(&append_command(entry))
        .await
        .map_err(|_| SummaryNotWritten)?;
    workstation.clear_latch().await;
    workstation
        .type_line(&confirm_append_command())
        .await
        .map_err(|_| SummaryNotWritten)?;
    match workstation.read_clipboard().await {
        Ok(read) if echoed(&read) == entry => Ok(()),
        _ => Err(SummaryNotWritten),
    }
}

/// Close the terminal, then disconnect.
pub(super) async fn close(workstation: &mut impl Workstation) {
    // A terminal that does not close is one the next audit does not reuse.
    let _ = workstation.type_line("exit").await;
    workstation.disconnect().await;
}

// --- The IronRDP session ---------------------------------------------------------------

/// The configured durations of a session.
#[derive(Debug, Clone, Copy)]
pub(super) struct SessionSettings {
    /// Between keys: a terminal drops characters typed faster than it reads.
    pub(super) key_interval: Duration,
    /// After connecting, before the session's first `Ctrl+Alt+T`: the binding
    /// is not handled until the desktop has started.
    pub(super) desktop_settle: Duration,
    /// After `Ctrl+Alt+T`, before typing.
    pub(super) terminal_settle: Duration,
    /// For one clipboard read.
    pub(super) clipboard_deadline: Duration,
    /// For the whole of connecting: the connection, the TLS handshake, the
    /// logon and the clipboard channel's setup.
    pub(super) connect_deadline: Duration,
}

/// Connects to the workstation with IronRDP: the connector the audit loop runs
/// with outside the tests. The workstation credential is the compiled world
/// constant, as the dock operator's are.
pub(super) struct RdpConnector {
    pub(super) clock: SimClock,
    pub(super) local_addr: SocketAddr,
    pub(super) recon_addr: SocketAddr,
    pub(super) tls: Arc<ClientConfig>,
    pub(super) settings: SessionSettings,
}

impl Connector for RdpConnector {
    type Workstation = RdpWorkstation;

    async fn connect(&mut self) -> Result<RdpWorkstation, AuditFailure> {
        RdpWorkstation::connect(
            self.clock.clone(),
            self.local_addr,
            self.recon_addr,
            crate::constants::RECON_USERNAME,
            crate::constants::RECON_WORKSTATION_PASSWORD,
            Arc::clone(&self.tls),
            channel_bound(CLIPBOARD_CEILING_BYTES),
            self.settings,
        )
        .await
    }
}

/// The desktop size the session asks for.
const DESKTOP_WIDTH: u16 = 1280;
const DESKTOP_HEIGHT: u16 = 1024;

/// Credential Security Support Provider is not used (`recon.md` §4), so the
/// connector never calls on a network client; this one refuses.
struct NoNetworkClient;

impl NetworkClient for NoNetworkClient {
    async fn send(
        &mut self,
        _request: &connector::sspi::generator::NetworkRequest,
    ) -> connector::ConnectorResult<Vec<u8>> {
        Err(connector::ConnectorError::general(
            "no network client: Credential Security Support Provider is not used",
        ))
    }
}

type Session = TokioFramed<TlsStream<TcpStream>>;

/// The remote desktop session with the workstation.
pub(super) struct RdpWorkstation {
    framed: Session,
    active_stage: ActiveStage,
    image: DecodedImage,
    input: Database,
    clipboard: Clipboard,
    clipboard_channel: u16,
    guard: ReassemblyGuard,
    read: Option<ClipboardRead>,
    clock: SimClock,
    settings: SessionSettings,
}

/// Connecting failed, at any stage up to the clipboard channel's setup.
fn connection_failed() -> AuditFailure {
    AuditFailure::Terminal(TerminalFailure::ConnectionFailed)
}

/// The connection was lost once the session was established.
fn connection_lost() -> AuditFailure {
    AuditFailure::Terminal(TerminalFailure::ConnectionLost)
}

impl RdpWorkstation {
    /// Connect from `local_addr` to `recon_addr`, verify the workstation's
    /// certificate, log on with the workstation credential, and wait for the
    /// clipboard channel to be set up, all within `connect_deadline`. A stage
    /// outside the deadline would wait on a workstation that stalls until the
    /// proxy's inactivity timeout.
    #[allow(clippy::too_many_arguments)]
    pub(super) async fn connect(
        clock: SimClock,
        local_addr: SocketAddr,
        recon_addr: SocketAddr,
        username: &str,
        password: &str,
        tls: Arc<ClientConfig>,
        guard_bound: usize,
        settings: SessionSettings,
    ) -> Result<RdpWorkstation, AuditFailure> {
        let deadline = clock.clone();
        tokio::select! {
            () = deadline.sleep(settings.connect_deadline) => Err(connection_failed()),
            connected = Self::establish(
                clock,
                local_addr,
                recon_addr,
                username,
                password,
                tls,
                guard_bound,
                settings,
            ) => connected,
        }
    }

    /// The stages of [`RdpWorkstation::connect`], without its deadline.
    #[allow(clippy::too_many_arguments)]
    async fn establish(
        clock: SimClock,
        local_addr: SocketAddr,
        recon_addr: SocketAddr,
        username: &str,
        password: &str,
        tls: Arc<ClientConfig>,
        guard_bound: usize,
        settings: SessionSettings,
    ) -> Result<RdpWorkstation, AuditFailure> {
        let socket = TcpSocket::new_v4().map_err(|_| connection_failed())?;
        socket.bind(local_addr).map_err(|_| connection_failed())?;
        let tcp = socket
            .connect(recon_addr)
            .await
            .map_err(|_| connection_failed())?;
        let client_addr = tcp.local_addr().map_err(|_| connection_failed())?;

        let clipboard = Clipboard::default();
        let mut framed = TokioFramed::new(tcp);
        let mut connector = ClientConnector::new(rdp_config(username, password), client_addr)
            .with_static_channel(CliprdrClient::new(Box::new(clipboard.backend())));

        let should_upgrade = ironrdp_tokio::connect_begin(&mut framed, &mut connector)
            .await
            .map_err(|_| connection_failed())?;
        let (tcp, leftover) = framed.into_inner();
        // The certificate is verified before any credential is sent.
        let tls_stream = upgrade(tcp, tls).await?;
        let upgraded = ironrdp_tokio::mark_as_upgraded(should_upgrade, &mut connector);
        let mut framed = TokioFramed::new_with_leftover(tls_stream, leftover);
        let connection = ironrdp_tokio::connect_finalize(
            upgraded,
            connector,
            &mut framed,
            &mut NoNetworkClient,
            ServerName::new(WORKSTATION_NAME),
            // Used only by Credential Security Support Provider.
            Vec::new(),
            None,
        )
        .await
        .and_then(|outcome| outcome.into_connected())
        .map_err(|_| connection_failed())?;

        let clipboard_channel = connection
            .static_channels
            .get_channel_id_by_type::<CliprdrClient>()
            .ok_or_else(connection_failed)?;
        let image = DecodedImage::new(
            ironrdp_graphics::image_processing::PixelFormat::RgbA32,
            connection.desktop_size.width,
            connection.desktop_size.height,
        );
        let active_stage = ActiveStageBuilder {
            static_channels: connection.static_channels,
            user_channel_id: connection.user_channel_id,
            io_channel_id: connection.io_channel_id,
            message_channel_id: connection.message_channel_id,
            share_id: connection.share_id,
            compression_type: connection.compression_type,
            enable_server_pointer: connection.enable_server_pointer,
            pointer_software_rendering: connection.pointer_software_rendering,
        }
        .build();
        let mut workstation = RdpWorkstation {
            framed,
            active_stage,
            image,
            input: Database::new(),
            clipboard,
            clipboard_channel,
            guard: ReassemblyGuard::new(guard_bound),
            read: None,
            clock,
            settings,
        };
        // Connecting lasts until the clipboard channel is set up, so a loss
        // before then is a failure to connect.
        workstation
            .await_clipboard_channel()
            .await
            .map_err(|failure| match failure {
                AuditFailure::Terminal(TerminalFailure::ConnectionLost) => connection_failed(),
                failure => failure,
            })?;
        Ok(workstation)
    }

    /// Pump frames until the clipboard channel is set up, answering the
    /// workstation's request for this client's formats with an empty list: the
    /// client never holds a selection. Bounded by the deadline on connecting.
    async fn await_clipboard_channel(&mut self) -> Result<(), AuditFailure> {
        let mut answered = false;
        loop {
            let (ready, requested) = self.clipboard.channel_state();
            if requested && !answered {
                self.send_cliprdr(|cliprdr| cliprdr.initiate_copy(&[]))
                    .await?;
                answered = true;
            }
            if ready {
                return Ok(());
            }
            self.pump_frame().await?;
        }
    }

    /// Read and process one frame. A chunk on the clipboard channel is offered
    /// to the guard before IronRDP sees the frame; a refused chunk ends the
    /// session, since a message partly withheld cannot be resumed.
    ///
    /// A timer can cut this call short: the pause after each keystroke, the
    /// wait for a terminal to open, or a clipboard read's deadline. What that
    /// costs depends on what the call was doing at the time:
    ///
    /// - Cut short while reading: nothing is lost. IronRDP keeps what has
    ///   arrived for the next call.
    /// - Cut short while sending replies: a reply can be lost, but only if the
    ///   workstation has stopped accepting data. Until then, each reply is
    ///   queued whole before it is sent.
    ///
    /// In that case the session is already failing: the next read, or the
    /// clipboard deadline, reports it, and the session is restarted.
    async fn pump_frame(&mut self) -> Result<(), AuditFailure> {
        let (action, payload) = self
            .framed
            .read_pdu()
            .await
            .map_err(|_| connection_lost())?;
        if action == Action::X224 {
            if let Some(chunk) = clipboard_chunk(&payload, self.clipboard_channel) {
                self.guard.accept(chunk).map_err(AuditFailure::Clipboard)?;
            }
        }
        let received_at = self.framed.last_read_at();
        let outputs = self
            .active_stage
            .process(&mut self.image, action, &payload, received_at)
            .map_err(|_| connection_lost())?;
        self.write_outputs(outputs).await
    }

    /// Pump frames for `duration` on the sim clock.
    async fn pump_for(&mut self, duration: Duration) -> Result<(), AuditFailure> {
        let clock = self.clock.clone();
        let expiry = clock.sleep(duration);
        tokio::pin!(expiry);
        loop {
            tokio::select! {
                () = &mut expiry => return Ok(()),
                pumped = self.pump_frame() => pumped?,
            }
        }
    }

    async fn write_outputs(&mut self, outputs: Vec<ActiveStageOutput>) -> Result<(), AuditFailure> {
        for output in outputs {
            match output {
                ActiveStageOutput::ResponseFrame(frame) => self
                    .framed
                    .write_all(&frame)
                    .await
                    .map_err(|_| connection_lost())?,
                ActiveStageOutput::Terminate(_) => return Err(connection_lost()),
                _ => {}
            }
        }
        Ok(())
    }

    async fn send_cliprdr(
        &mut self,
        action: impl FnOnce(&mut CliprdrClient) -> Result<CliprdrSvcMessages<Client>, PduError>,
    ) -> Result<(), AuditFailure> {
        let messages = {
            let cliprdr = self
                .active_stage
                .get_svc_processor_mut::<CliprdrClient>()
                .ok_or_else(connection_lost)?;
            action(cliprdr).map_err(|_| connection_lost())?
        };
        let frame = self
            .active_stage
            .process_svc_processor_messages(messages)
            .map_err(|_| connection_lost())?;
        self.framed
            .write_all(&frame)
            .await
            .map_err(|_| connection_lost())
    }

    async fn send_key(&mut self, event: KeyEvent) -> Result<(), AuditFailure> {
        let operation = match event {
            KeyEvent::Press(code) => Operation::KeyPressed(Scancode::from((false, code))),
            KeyEvent::Release(code) => Operation::KeyReleased(Scancode::from((false, code))),
        };
        let events = self.input.apply([operation]);
        if events.is_empty() {
            return Ok(());
        }
        let outputs = self
            .active_stage
            .process_fastpath_input(&mut self.image, &events)
            .map_err(|_| connection_lost())?;
        self.write_outputs(outputs).await
    }
}

impl Workstation for RdpWorkstation {
    async fn await_desktop(&mut self) -> Result<(), AuditFailure> {
        self.pump_for(self.settings.desktop_settle).await
    }

    async fn open_terminal(&mut self) -> Result<(), AuditFailure> {
        for event in open_terminal_keys() {
            self.send_key(event).await?;
        }
        self.pump_for(self.settings.terminal_settle).await
    }

    async fn clear_latch(&mut self) {
        self.read = Some(self.clipboard.clear());
    }

    async fn type_line(&mut self, line: &str) -> Result<(), AuditFailure> {
        let events = keys_for_line(line)
            .map_err(|c| AuditFailure::Terminal(TerminalFailure::UntypableCharacter(c)))?;
        for event in events {
            self.send_key(event).await?;
            if matches!(event, KeyEvent::Release(code) if code != SHIFT) {
                self.pump_for(self.settings.key_interval).await?;
            }
        }
        Ok(())
    }

    async fn read_clipboard(&mut self) -> Result<String, AuditFailure> {
        // A read is begun only by a clear; one without is content from before.
        let read = self
            .read
            .take()
            .ok_or(AuditFailure::Clipboard(ClipboardFailure::StaleSelection))?;
        let clock = self.clock.clone();
        let deadline = self.settings.clipboard_deadline;
        read.complete(&clock, deadline, self).await
    }

    async fn disconnect(&mut self) {
        if let Ok(outputs) = self.active_stage.graceful_shutdown() {
            let _ = self.write_outputs(outputs).await;
        }
    }
}

/// The session as a clipboard read uses it: the request goes over the
/// clipboard channel, and the workstation's next message arrives in the next
/// frame, which the backend records as IronRDP processes it.
impl ClipboardChannel for RdpWorkstation {
    async fn request(&mut self, format: u32) -> Result<(), AuditFailure> {
        self.send_cliprdr(|cliprdr| cliprdr.initiate_paste(ClipboardFormatId::new(format)))
            .await
    }

    async fn wait(&mut self) -> Result<(), AuditFailure> {
        self.pump_frame().await
    }
}

/// Connection settings: TLS without Credential Security Support Provider, which
/// is what the workstation offers (`recon.md` §4); the United Kingdom layout,
/// `0x0809`, so the scancode table, the layout the session pins and the one
/// advertised agree; and logon with the workstation credential.
fn rdp_config(username: &str, password: &str) -> connector::Config {
    connector::Config {
        credentials: Credentials::UsernamePassword {
            username: username.to_owned(),
            password: password.to_owned(),
        },
        domain: None,
        enable_tls: true,
        enable_credssp: false,
        enable_standard_rdp_security: false,
        keyboard_type: KeyboardType::IBM_ENHANCED,
        keyboard_subtype: 0,
        keyboard_layout: 0x0809,
        keyboard_functional_keys_count: 12,
        connection_type: ConnectionType::NotUsed,
        ime_file_name: String::new(),
        dig_product_id: String::new(),
        desktop_size: connector::DesktopSize {
            width: DESKTOP_WIDTH,
            height: DESKTOP_HEIGHT,
        },
        bitmap: None,
        client_build: 0,
        client_name: "audit-client".to_owned(),
        client_dir: "C:\\Windows\\System32\\mstscax.dll".to_owned(),
        // As proven by the probe; xrdp does not act on this field.
        platform: MajorPlatformType::MACINTOSH,
        monitor_layout: None,
        remote_application_mode: false,
        rail_support_level: RailSupportLevel::empty(),
        enable_server_pointer: false,
        request_data: None,
        autologon: true,
        enable_audio_playback: false,
        enable_audio_capture: false,
        compression_type: None,
        pointer_software_rendering: true,
        multitransport_flags: None,
        support_dyn_vc_gfx_protocol: false,
        performance_flags: PerformanceFlags::default(),
        desktop_scale_factor: 0,
        hardware_id: None,
        license_cache: None,
        timezone_info: TimezoneInfo::default(),
        alternate_shell: String::new(),
        work_dir: String::new(),
    }
}

/// The session, tested without a workstation (sim-tests.md, "Session").
#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use ironrdp_core::encode_vec;
    use ironrdp_pdu::mcs::SendDataIndication;
    use ironrdp_pdu::x224::X224;
    use rcgen::{BasicConstraints, CertificateParams, IsCa, KeyPair};
    use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
    use time::macros::datetime;
    use tokio::net::{TcpListener, TcpStream};
    use tokio_rustls::TlsAcceptor;

    use super::*;
    use crate::actors::reconciliation_user::internals::workstation_double::{Double, Recorded};
    use crate::actors::reconciliation_user::internals::{
        AuditFailure, AuditWindow, TerminalFailure,
    };

    fn january_2025() -> AuditWindow {
        AuditWindow {
            start: datetime!(2025-01-01 0:00),
            end: datetime!(2025-02-01 0:00),
        }
    }

    // --- The scancode table ------------------------------------------------------

    #[test]
    fn the_scancode_table_covers_every_printable_ascii_character() {
        let missing: Vec<char> = (' '..='~').filter(|&c| key_for(c).is_none()).collect();
        assert!(missing.is_empty(), "no key for {missing:?}");
    }

    #[test]
    fn the_table_follows_the_gb_layout() {
        assert_eq!(key_for('\\'), Some((false, 0x56)));
        assert_eq!(key_for('|'), Some((true, 0x56)));
        assert_eq!(key_for('#'), Some((false, 0x2B)));
        assert_eq!(key_for('~'), Some((true, 0x2B)));
        assert_eq!(key_for('"'), Some((true, 0x03)));
        assert_eq!(key_for('@'), Some((true, 0x28)));
    }

    #[test]
    fn a_line_with_a_character_the_table_lacks_is_refused_before_any_key() {
        assert_eq!(keys_for_line("echo\tdone"), Err('\t'));
    }

    #[test]
    fn a_line_is_typed_as_presses_and_releases_ending_with_enter() {
        use KeyEvent::{Press, Release};
        assert_eq!(
            keys_for_line("a|B"),
            Ok(vec![
                Press(0x1E),
                Release(0x1E),
                Press(SHIFT),
                Press(0x56),
                Release(0x56),
                Release(SHIFT),
                Press(SHIFT),
                Press(0x30),
                Release(0x30),
                Release(SHIFT),
                Press(ENTER),
                Release(ENTER),
            ])
        );
    }

    #[test]
    fn the_terminal_is_opened_with_ctrl_alt_t() {
        use KeyEvent::{Press, Release};
        assert_eq!(
            open_terminal_keys(),
            [
                Press(CTRL),
                Press(ALT),
                Press(0x14),
                Release(0x14),
                Release(ALT),
                Release(CTRL),
            ]
        );
    }

    // --- TLS -------------------------------------------------------------------------

    /// A root of the test's own, and certificates it issues.
    struct TestRoot {
        certificate: rcgen::Certificate,
        key: KeyPair,
    }

    impl TestRoot {
        fn new() -> TestRoot {
            let key = KeyPair::generate().unwrap();
            let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
            params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
            let certificate = params.self_signed(&key).unwrap();
            TestRoot { certificate, key }
        }

        fn pem(&self) -> String {
            self.certificate.pem()
        }

        /// A leaf naming `name`, issued by this root.
        fn issue(&self, name: &str) -> (CertificateDer<'static>, KeyPair) {
            let key = KeyPair::generate().unwrap();
            let leaf = CertificateParams::new(vec![name.to_string()])
                .unwrap()
                .signed_by(&key, &self.certificate, &self.key)
                .unwrap();
            (leaf.der().clone(), key)
        }
    }

    /// A self-signed certificate naming `name`.
    fn self_signed(name: &str) -> (CertificateDer<'static>, KeyPair) {
        let key = KeyPair::generate().unwrap();
        let certificate = CertificateParams::new(vec![name.to_string()])
            .unwrap()
            .self_signed(&key)
            .unwrap();
        (certificate.der().clone(), key)
    }

    /// Serve one TLS handshake on the loopback interface with `certificate`,
    /// and upgrade a connection to it as the actor does, verifying against
    /// `root_pem`.
    async fn handshake(
        certificate: CertificateDer<'static>,
        key: KeyPair,
        root_pem: &str,
    ) -> Result<(), AuditFailure> {
        let provider = Arc::new(rustls::crypto::ring::default_provider());
        let server = rustls::ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(
                vec![certificate],
                PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key.serialize_der())),
            )
            .unwrap();
        let acceptor = TlsAcceptor::from(Arc::new(server));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            // The client may refuse the certificate; the server's side of that
            // is of no interest.
            let _ = acceptor.accept(stream).await;
        });

        let stream = TcpStream::connect(address).await.unwrap();
        let config = tls_config(root_pem.as_bytes()).expect("the root is a certificate");
        upgrade(stream, config).await.map(|_| ())
    }

    fn certificate_refused() -> Result<(), AuditFailure> {
        Err(AuditFailure::Terminal(
            TerminalFailure::CertificateVerification,
        ))
    }

    #[tokio::test]
    async fn a_certificate_from_the_root_naming_recon_01_is_accepted_at_a_loopback_address() {
        let root = TestRoot::new();
        let (certificate, key) = root.issue("recon-01");
        assert_eq!(handshake(certificate, key, &root.pem()).await, Ok(()));
    }

    #[tokio::test]
    async fn a_certificate_naming_another_host_is_refused() {
        let root = TestRoot::new();
        let (certificate, key) = root.issue("recon-02");
        assert_eq!(
            handshake(certificate, key, &root.pem()).await,
            certificate_refused()
        );
    }

    #[tokio::test]
    async fn a_certificate_not_issued_by_the_root_is_refused() {
        let root = TestRoot::new();
        let (certificate, key) = self_signed("recon-01");
        assert_eq!(
            handshake(certificate, key, &root.pem()).await,
            certificate_refused()
        );
    }

    // --- Connecting ------------------------------------------------------------------

    #[test]
    fn a_workstation_that_never_answers_ends_at_the_connect_deadline() {
        let (clock, runtime) = SimClock::isolated(
            crate::clock::TimeModel::Synthetic,
            datetime!(2025-01-15 12:00),
        );
        runtime.block_on(async {
            // Its backlog completes the connection; nothing is read or written.
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let settings = SessionSettings {
                key_interval: Duration::from_millis(30),
                desktop_settle: Duration::from_secs(10),
                terminal_settle: Duration::from_secs(3),
                clipboard_deadline: Duration::from_secs(10),
                connect_deadline: Duration::from_secs(30),
            };
            let started = clock.now_datetime();
            let connecting = RdpWorkstation::connect(
                clock.clone(),
                "127.0.0.1:0".parse().unwrap(),
                listener.local_addr().unwrap(),
                "recon-user",
                "a-password",
                tls_config(TestRoot::new().pem().as_bytes()).unwrap(),
                channel_bound(CLIPBOARD_CEILING_BYTES),
                settings,
            );
            // Bounds the test itself: a stage of connecting outside the
            // deadline waits on the silent listener indefinitely.
            let failure = tokio::select! {
                connected = connecting => connected.err(),
                () = clock.sleep(2 * settings.connect_deadline) => None,
            };
            assert_eq!(
                failure,
                Some(AuditFailure::Terminal(TerminalFailure::ConnectionFailed)),
                "connecting did not end at its deadline"
            );
            assert_eq!(
                (clock.now_datetime() - started).unsigned_abs(),
                settings.connect_deadline
            );
        });
    }

    // --- The frame inspector -------------------------------------------------------------

    /// The clipboard channel's id in these tests.
    const CLIPBOARD_CHANNEL: u16 = 1004;

    /// A frame carrying channel data on `channel`: the channel header (total
    /// length, flags) and `payload` bytes of the message.
    fn channel_frame(channel: u16, declared: u32, flags: u32, payload: usize) -> Vec<u8> {
        let mut user_data = Vec::new();
        user_data.extend_from_slice(&declared.to_le_bytes());
        user_data.extend_from_slice(&flags.to_le_bytes());
        user_data.extend(std::iter::repeat_n(0x41, payload));
        encode_vec(&X224(SendDataIndication {
            initiator_id: 1002,
            channel_id: channel,
            user_data: user_data.into(),
        }))
        .unwrap()
    }

    #[test]
    fn a_clipboard_channel_chunk_is_offered_to_the_guard() {
        const FIRST: u32 = 0x1;
        const LAST: u32 = 0x2;
        assert_eq!(
            clipboard_chunk(
                &channel_frame(CLIPBOARD_CHANNEL, 100, FIRST, 40),
                CLIPBOARD_CHANNEL
            ),
            Some(ChunkHeader {
                declared: 100,
                first: true,
                last: false,
                payload: 40,
            })
        );
        assert_eq!(
            clipboard_chunk(
                &channel_frame(CLIPBOARD_CHANNEL, 100, LAST, 60),
                CLIPBOARD_CHANNEL
            ),
            Some(ChunkHeader {
                declared: 100,
                first: false,
                last: true,
                payload: 60,
            })
        );
        assert_eq!(
            clipboard_chunk(
                &channel_frame(1005, 100, FIRST | LAST, 100),
                CLIPBOARD_CHANNEL
            ),
            None
        );
        // A fast-path screen update: not channel data.
        assert_eq!(
            clipboard_chunk(&[0x00, 0x06, 0x01, 0x02, 0x03, 0x04], CLIPBOARD_CHANNEL),
            None
        );
    }

    // --- The procedure -------------------------------------------------------------------

    #[test]
    fn the_latch_value_differs_between_audits_and_attempts() {
        let january = january_2025();
        let february = AuditWindow {
            start: datetime!(2025-02-01 0:00),
            end: datetime!(2025-03-01 0:00),
        };
        assert_eq!(latch_value(&january, 1), "latch-20250101000000-1");
        assert_ne!(latch_value(&january, 2), latch_value(&january, 1));
        assert_ne!(latch_value(&february, 1), latch_value(&january, 1));
    }

    #[tokio::test]
    async fn the_session_is_confirmed_by_reading_back_the_latch_value() {
        let value = latch_value(&january_2025(), 1);
        let command = "echo latch-20250101000000-1 | xclip -selection clipboard";

        // `echo` ends the value with a line feed.
        let mut workstation = Double::answering([Ok(format!("{value}\n"))]);
        assert_eq!(open_and_confirm(&mut workstation, &value).await, Ok(()));
        assert_eq!(
            workstation.recorded,
            [
                Recorded::OpenedTerminal,
                Recorded::ClearedLatch,
                Recorded::Typed(command.to_string()),
                Recorded::Read,
            ]
        );

        let mut workstation = Double::answering([Ok("latch-20241201000000-1\n".to_string())]);
        assert_eq!(
            open_and_confirm(&mut workstation, &value).await,
            Err(AuditFailure::Terminal(TerminalFailure::LatchMismatch))
        );
        assert_eq!(workstation.recorded.last(), Some(&Recorded::Read));

        let mut workstation = Double::answering([Err(AuditFailure::Clipboard(
            ClipboardFailure::DeadlineReached,
        ))]);
        assert_eq!(
            open_and_confirm(&mut workstation, &value).await,
            Err(AuditFailure::Terminal(TerminalFailure::LatchAbsent))
        );
        assert_eq!(
            workstation.typed(),
            [command],
            "nothing typed after the failure"
        );
    }

    #[tokio::test]
    async fn a_session_ended_during_the_latch_read_is_not_an_absent_latch() {
        let value = latch_value(&january_2025(), 1);
        for ended in [
            AuditFailure::Terminal(TerminalFailure::ConnectionLost),
            AuditFailure::Clipboard(ClipboardFailure::DeclaredLengthOverCeiling {
                declared: 9_999_999,
            }),
            AuditFailure::Clipboard(ClipboardFailure::ExceedsDeclaredLength {
                declared: 16,
                received: 32,
            }),
        ] {
            let mut workstation = Double::answering([Err(ended.clone())]);
            assert_eq!(open_and_confirm(&mut workstation, &value).await, Err(ended));
        }
    }

    #[tokio::test]
    async fn the_summary_entry_is_appended_and_confirmed() {
        let entry = "2025-01-01T00:00:00Z 2025-02-01T00:00:00Z agreed";
        let append = format!("echo {entry} >> ~/reconciliation-audits.txt");
        let confirm = "tail -n 1 ~/reconciliation-audits.txt | xclip -selection clipboard";

        let mut workstation = Double::answering([Ok(format!("{entry}\n"))]);
        assert_eq!(record_summary(&mut workstation, entry).await, Ok(()));
        assert_eq!(
            workstation.recorded,
            [
                Recorded::Typed(append),
                Recorded::ClearedLatch,
                Recorded::Typed(confirm.to_string()),
                Recorded::Read,
            ]
        );

        let mut workstation = Double::answering([Ok("an earlier entry\n".to_string())]);
        assert_eq!(
            record_summary(&mut workstation, entry).await,
            Err(SummaryNotWritten)
        );
    }

    #[tokio::test]
    async fn the_session_ends_with_exit_then_disconnect() {
        let entry = "2025-01-01T00:00:00Z 2025-02-01T00:00:00Z agreed";
        let mut workstation = Double::answering([Ok(format!("{entry}\n"))]);
        record_summary(&mut workstation, entry).await.unwrap();
        close(&mut workstation).await;
        assert_eq!(workstation.typed().last(), Some(&"exit"));
        assert_eq!(workstation.recorded.last(), Some(&Recorded::Disconnected));
        assert_eq!(
            workstation.recorded[workstation.recorded.len() - 2],
            Recorded::Typed("exit".to_string())
        );
    }
}
