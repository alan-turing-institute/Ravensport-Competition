//! Async Remote Desktop Protocol session over IronRDP — pure mechanism.
//!
//! Each method does one thing and reports honestly: it never retries, validates
//! or guesses. The probe's script decides what to do; this decides nothing.
//!
//! Connects over Transport Layer Security with the enhanced security layer,
//! which is what `recon.md` §4 pins `security_layer=tls` for, and registers the
//! clipboard virtual channel at connect — a channel not asked for at negotiation
//! cannot be added later.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context as _, Result};
use ironrdp::connector::{self, ClientConnector, Credentials};
use ironrdp::input::{Database, MouseButton, MousePosition, Operation, Scancode};
use ironrdp::pdu::gcc::KeyboardType;
use ironrdp::pdu::rdp::capability_sets::MajorPlatformType;
use ironrdp::session::image::DecodedImage;
use ironrdp::session::{ActiveStage, ActiveStageOutput};
use ironrdp_cliprdr::pdu::ClipboardFormatId;
use ironrdp_cliprdr::{Client, CliprdrClient, CliprdrSvcMessages};
use ironrdp_pdu::rdp::client_info::{PerformanceFlags, TimezoneInfo};
use ironrdp_pdu::PduError;
use ironrdp_tokio::reqwest::ReqwestNetworkClient;
use ironrdp_tokio::{FramedWrite as _, TokioFramed};
use tokio::net::TcpStream;
use tracing::{debug, info};

use crate::clipboard::{choose_format, ClipboardCapture, ClipboardState};
use crate::keys::SHIFT;

/// Read timeout for a single frame poll — small, so the loop stays responsive.
const POLL: Duration = Duration::from_millis(400);
/// Gap between keystrokes. A desktop application needs longer than the wire
/// does; typing faster than this drops characters in a terminal.
const KEY_INTERVAL: Duration = Duration::from_millis(30);

type UpgradedFramed = TokioFramed<ironrdp_tls::TlsStream<TcpStream>>;

pub struct RdpSession {
    framed: UpgradedFramed,
    active_stage: ActiveStage,
    image: DecodedImage,
    input: Database,
    clip: Arc<Mutex<ClipboardState>>,
}

impl RdpSession {
    /// TCP, protocol negotiation, Transport Layer Security upgrade, finalize.
    /// `addr` is `host:port`.
    pub async fn connect(
        addr: &str,
        username: &str,
        password: &str,
        width: u16,
        height: u16,
    ) -> Result<Self> {
        let (host, port) = split_host_port(addr)?;
        info!(host, port, username, "connecting");

        let clip = Arc::new(Mutex::new(ClipboardState::default()));
        let backend = ClipboardCapture::new(Arc::clone(&clip));

        let tcp = TcpStream::connect((host, port))
            .await
            .context("TCP connect")?;
        let client_addr = tcp.local_addr().context("local addr")?;

        let mut framed = TokioFramed::new(tcp);
        let mut connector =
            ClientConnector::new(rdp_config(username, password, width, height), client_addr)
                .with_static_channel(CliprdrClient::new(Box::new(backend)));

        let should_upgrade = ironrdp_tokio::connect_begin(&mut framed, &mut connector)
            .await
            .map_err(|e| anyhow!("connect_begin: {e}"))?;

        debug!("Transport Layer Security upgrade");
        let (initial_stream, leftover) = framed.into_inner();
        let (tls_stream, tls_cert) = ironrdp_tls::upgrade(initial_stream, host)
            .await
            .context("TLS upgrade")?;
        let upgraded = ironrdp_tokio::mark_as_upgraded(should_upgrade, &mut connector);
        let mut framed = TokioFramed::new_with_leftover(tls_stream, leftover);

        let server_public_key = ironrdp_tls::extract_tls_server_public_key(&tls_cert)
            .context("extract TLS server public key")?
            .to_owned();

        let connection_result = ironrdp_tokio::connect_finalize(
            upgraded,
            connector,
            &mut framed,
            &mut ReqwestNetworkClient::new(),
            host.to_owned().into(),
            server_public_key,
            None,
        )
        .await
        .map_err(|e| anyhow!("connect_finalize: {e}"))?;

        info!(
            width = connection_result.desktop_size.width,
            height = connection_result.desktop_size.height,
            "connected"
        );

        let image = DecodedImage::new(
            ironrdp_graphics::image_processing::PixelFormat::RgbA32,
            connection_result.desktop_size.width,
            connection_result.desktop_size.height,
        );
        let active_stage = ActiveStage::new(connection_result);

        Ok(Self {
            framed,
            active_stage,
            image,
            input: Database::new(),
            clip,
        })
    }

    /// Pump frames until the clipboard channel is ready, completing its
    /// initialisation handshake (answering the server's format-list request)
    /// along the way. Until this returns, a copy on the host reaches nothing.
    pub async fn wait_until_ready(&mut self, timeout: Duration) -> Result<()> {
        let deadline = Instant::now() + timeout;
        let mut advertised = false;
        loop {
            let (ready, need_advertise) = {
                let s = self.clip.lock().unwrap();
                (s.ready, s.request_format_list && !advertised)
            };
            if need_advertise {
                // An empty list: this client offers the host nothing, and only
                // ever reads. Paste into the session is out of scope.
                self.send_cliprdr(|c| c.initiate_copy(&[])).await?;
                advertised = true;
                debug!("advertised an empty client format list");
            }
            if ready {
                return Ok(());
            }
            if Instant::now() >= deadline {
                // Whether the server's clipboard side spoke at all separates a
                // channel that is absent from one whose handshake did not
                // finish: no format-list request means nothing on the far side
                // is bridging a clipboard, which is what a session with no
                // `xrdp-chansrv` — including one still on the greeter — looks
                // like from here.
                let s = self.clip.lock().unwrap();
                bail!(
                    "clipboard channel not ready within {timeout:?} \
                     (format-list request from the server: {}; copies announced: {})",
                    s.request_format_list,
                    s.announcements
                );
            }
            self.pump_once(POLL).await?;
        }
    }

    /// Move the pointer to `(x, y)` and left-click, focusing whatever is there.
    pub async fn click(&mut self, x: u16, y: u16) -> Result<()> {
        self.send_input([Operation::MouseMove(MousePosition { x, y })])
            .await?;
        self.send_input([Operation::MouseButtonPressed(MouseButton::Left)])
            .await?;
        self.send_input([Operation::MouseButtonReleased(MouseButton::Left)])
            .await?;
        Ok(())
    }

    /// Press `codes` in order and release them in reverse — a chord.
    pub async fn press(&mut self, codes: &[u8]) -> Result<()> {
        let scancodes: Vec<Scancode> = codes.iter().map(|&c| Scancode::from((false, c))).collect();
        let mut ops = Vec::with_capacity(scancodes.len() * 2);
        ops.extend(scancodes.iter().map(|sc| Operation::KeyPressed(*sc)));
        ops.extend(scancodes.iter().rev().map(|sc| Operation::KeyReleased(*sc)));
        self.send_input(ops).await
    }

    /// Type `chords` — one per character — with a gap and a frame pump between
    /// them, so the application on the far side keeps up.
    pub async fn type_chords(&mut self, chords: &[Vec<u8>]) -> Result<()> {
        for chord in chords {
            self.press(chord).await?;
            self.settle(KEY_INTERVAL).await?;
        }
        // Shift held across a chord boundary would alter the next character; the
        // input database releases it, but let the host apply it before moving on.
        debug!(
            keys = chords.len(),
            shift_used = chords.iter().any(|c| c.contains(&SHIFT)),
            "typed"
        );
        Ok(())
    }

    /// Forget copies seen so far, so that what is read next is known to be the
    /// result of the keystrokes sent after this call.
    pub fn clear_clipboard(&mut self) {
        self.clip.lock().unwrap().clear();
    }

    /// Wait for the host to announce a copy, then ask for it as text.
    ///
    /// Returns the format id and the decoded text, or `None` if nothing was
    /// announced or nothing arrived within `timeout`. An announcement that
    /// arrived before this call — the usual case, since the copy is caused by
    /// keystrokes sent first — counts.
    pub async fn read_clipboard(&mut self, timeout: Duration) -> Result<Option<(u32, String)>> {
        let deadline = Instant::now() + timeout;

        if !self.pump_until(deadline, |s| s.announcements > 0).await? {
            return Ok(None);
        }

        let format = {
            let s = self.clip.lock().unwrap();
            choose_format(s.available.as_deref().unwrap_or(&[]))
        };
        self.clip.lock().unwrap().requested_format = Some(format);
        self.send_cliprdr(|c| c.initiate_paste(ClipboardFormatId::new(format)))
            .await?;
        info!(format, "requested clipboard data (initiate_paste)");

        if !self
            .pump_until(deadline, |s| s.text.is_some() || s.error.is_some())
            .await?
        {
            return Ok(None);
        }
        let s = self.clip.lock().unwrap();
        if let Some(err) = &s.error {
            bail!("{err}");
        }
        Ok(s.text.clone().map(|t| (format, t)))
    }

    /// The formats the host advertised in the most recent Format List.
    pub fn advertised_formats(&self) -> Option<Vec<u32>> {
        self.clip.lock().unwrap().available.clone()
    }

    /// Save the current framebuffer to a PNG — evidence on success, diagnosis on
    /// failure, and the only way to learn where the desktop put a menu.
    pub fn save_screenshot(&self, path: &str) -> Result<()> {
        let img: image::ImageBuffer<image::Rgba<u8>, _> = image::ImageBuffer::from_raw(
            u32::from(self.image.width()),
            u32::from(self.image.height()),
            self.image.data(),
        )
        .context("invalid image buffer")?;
        img.save(path).with_context(|| format!("save {path}"))?;
        Ok(())
    }

    /// Pump frames for `dur` without waiting on any condition — used to let the
    /// desktop finish drawing, or an application finish starting.
    pub async fn settle(&mut self, dur: Duration) -> Result<()> {
        self.pump_until(Instant::now() + dur, |_| false).await?;
        Ok(())
    }

    // ---- internal mechanics ------------------------------------------------

    /// Pump frames until `cond` holds (→ `true`) or the deadline passes (→ `false`).
    async fn pump_until(
        &mut self,
        deadline: Instant,
        cond: impl Fn(&ClipboardState) -> bool,
    ) -> Result<bool> {
        loop {
            if cond(&self.clip.lock().unwrap()) {
                return Ok(true);
            }
            let now = Instant::now();
            if now >= deadline {
                return Ok(false);
            }
            let budget = POLL.min(deadline - now);
            self.pump_once(budget).await?;
        }
    }

    /// Read at most one server frame (bounded by `read_timeout`) and process it.
    /// A server-initiated terminate or a real IO error is surfaced as `Err`; an
    /// idle timeout is a no-op.
    async fn pump_once(&mut self, read_timeout: Duration) -> Result<()> {
        let read = tokio::time::timeout(read_timeout, self.framed.read_pdu()).await;
        let (action, payload) = match read {
            Err(_elapsed) => return Ok(()), // idle — nothing to read
            Ok(Ok(frame)) => frame,
            Ok(Err(e)) => return Err(anyhow!("read frame: {e}")),
        };
        let outputs = self
            .active_stage
            .process(&mut self.image, action, &payload)
            .map_err(|e| anyhow!("process frame: {e}"))?;
        for out in outputs {
            match out {
                ActiveStageOutput::ResponseFrame(frame) => self
                    .framed
                    .write_all(&frame)
                    .await
                    .map_err(|e| anyhow!("write response: {e}"))?,
                ActiveStageOutput::Terminate(reason) => {
                    bail!("server terminated session: {reason:?}")
                }
                _ => {}
            }
        }
        Ok(())
    }

    /// Encode a client-initiated clipboard action and write it to the channel.
    async fn send_cliprdr<F>(&mut self, action: F) -> Result<()>
    where
        F: FnOnce(&mut CliprdrClient) -> Result<CliprdrSvcMessages<Client>, PduError>,
    {
        let messages = {
            let cliprdr = self
                .active_stage
                .get_svc_processor_mut::<CliprdrClient>()
                .context("CLIPRDR channel not present")?;
            action(cliprdr).map_err(|e| anyhow!("cliprdr action: {e}"))?
        };
        let frame = self
            .active_stage
            .process_svc_processor_messages(messages)
            .map_err(|e| anyhow!("encode cliprdr messages: {e}"))?;
        self.framed
            .write_all(&frame)
            .await
            .map_err(|e| anyhow!("write cliprdr frame: {e}"))?;
        Ok(())
    }

    /// Turn input operations into a fast-path frame and send it.
    async fn send_input(&mut self, ops: impl IntoIterator<Item = Operation>) -> Result<()> {
        let events = self.input.apply(ops);
        if events.is_empty() {
            return Ok(());
        }
        let outputs = self
            .active_stage
            .process_fastpath_input(&mut self.image, &events)
            .map_err(|e| anyhow!("process input: {e}"))?;
        for out in outputs {
            if let ActiveStageOutput::ResponseFrame(frame) = out {
                self.framed
                    .write_all(&frame)
                    .await
                    .map_err(|e| anyhow!("write input frame: {e}"))?;
            }
        }
        Ok(())
    }
}

fn split_host_port(addr: &str) -> Result<(&str, u16)> {
    let (host, port) = addr.rsplit_once(':').context("address must be host:port")?;
    Ok((host, port.parse().context("invalid port")?))
}

/// Connection settings for xrdp: plain Transport Layer Security rather than
/// Credential Security Support Provider, and autologon so the supplied
/// credentials skip the greeter.
fn rdp_config(username: &str, password: &str, width: u16, height: u16) -> connector::Config {
    connector::Config {
        credentials: Credentials::UsernamePassword {
            username: username.to_owned(),
            password: password.to_owned(),
        },
        domain: None,
        enable_tls: true,
        enable_credssp: false,
        keyboard_type: KeyboardType::IbmEnhanced,
        keyboard_subtype: 0,
        // United Kingdom (0x0809). The third leg of `recon.md` §5.2's contract:
        // the layout `.xsession` pins with `setxkbmap gb`, the scancode table in
        // `keys.rs`, and the identifier advertised here must all agree. xrdp
        // performs its own client-advertised negotiation and loads a
        // `km-<layout>.ini` on every connect, so a client advertising nothing —
        // 0, which is neither layout's identifier — leaves what a scancode types
        // to whatever the server falls back to.
        keyboard_layout: 0x0809,
        keyboard_functional_keys_count: 12,
        ime_file_name: String::new(),
        dig_product_id: String::new(),
        desktop_size: connector::DesktopSize { width, height },
        bitmap: None,
        client_build: 0,
        client_name: "recon-probe".to_owned(),
        client_dir: "C:\\Windows\\System32\\mstscax.dll".to_owned(),
        // As proven in the de-risking work; xrdp does not act on this field.
        platform: MajorPlatformType::MACINTOSH,
        enable_server_pointer: false,
        request_data: None,
        autologon: true,
        enable_audio_playback: false,
        compression_type: None,
        pointer_software_rendering: true,
        multitransport_flags: None,
        performance_flags: PerformanceFlags::default(),
        desktop_scale_factor: 0,
        hardware_id: None,
        license_cache: None,
        timezone_info: TimezoneInfo::default(),
        alternate_shell: String::new(),
        work_dir: String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_host_port_parses() {
        assert_eq!(
            split_host_port("127.0.0.1:20003").unwrap(),
            ("127.0.0.1", 20003)
        );
        assert_eq!(
            split_host_port("10.30.0.10:3389").unwrap(),
            ("10.30.0.10", 3389)
        );
        assert!(split_host_port("no-port").is_err());
        assert!(split_host_port("host:notanumber").is_err());
    }

    #[test]
    fn the_configuration_pins_the_enhanced_security_layer() {
        // recon.md §4: the host forces security_layer=tls, so a client offering
        // the legacy layer, or asking for CredSSP the host has no provider for,
        // fails at negotiation rather than at login.
        let cfg = rdp_config("recon-user", "secret", 1280, 1024);
        assert!(cfg.enable_tls);
        assert!(!cfg.enable_credssp);
        assert!(cfg.autologon, "the greeter cannot be driven by a script");
    }
}
