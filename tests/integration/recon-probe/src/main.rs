//! Scripted Remote Desktop Protocol probe for the copy-fidelity measurement.
//!
//! Connects to `recon-01` over the reconciliation user's egress path, runs the
//! actions given on the command line, and reports what the clipboard carried
//! back. It decides nothing: every coordinate, key and timing is in the script.
//!
//! ```text
//! RECON_PASS=… recon-probe \
//!     "ready 20000" "wait 3000" \
//!     "click 567,999" "wait 3000" "clear" \
//!     "type xclip -selection clipboard /tmp/probe-fixture.txt" \
//!     "key enter" "wait 1000" "read 8000"
//! ```
//!
//! `clear` goes before the action that copies, never after: the remote
//! announces a copy within milliseconds, and a later `clear` discards the
//! announcement the `read` is waiting for.
//!
//! See README.md for how it is built and where it is run.

mod clipboard;
mod keys;
mod script;
mod session;

use std::time::{Duration, Instant};

use anyhow::{bail, Context as _, Result};
use sha2::{Digest, Sha256};

use crate::clipboard::{CF_TEXT, CF_UNICODETEXT};
use crate::script::{parse_script, Action};
use crate::session::RdpSession;

/// The reconciliation user's egress port on the sim host (`sim-network.md` §5).
const DEFAULT_ADDR: &str = "127.0.0.1:20003";
const DEFAULT_USER: &str = "recon-user";
const DEFAULT_SIZE: (u16, u16) = (1280, 1024);

#[tokio::main]
async fn main() -> Result<()> {
    init_logging()?;
    // rustls needs a process-wide crypto provider before any connection.
    let _ = tokio_rustls::rustls::crypto::ring::default_provider().install_default();

    let args: Vec<String> = std::env::args().skip(1).collect();
    let args = if args.is_empty() {
        // Connect, let the desktop draw, and photograph it. Enough to learn
        // whether a session starts and where the desktop put things.
        ["ready 20000", "wait 3000", "shot recon-probe.png"]
            .iter()
            .map(|a| (*a).to_owned())
            .collect()
    } else {
        args
    };
    let actions = parse_script(&args)?;

    let addr = env_or("RECON_ADDR", DEFAULT_ADDR);
    let user = env_or("RECON_USER", DEFAULT_USER);
    let pass = std::env::var("RECON_PASS").context(
        "RECON_PASS is not set. It is the reconciliation user's workstation password \
         (world.yaml identities.reconciliation_user.workstation_password); pass it in the \
         environment so it does not reach a shell history or a process listing",
    )?;
    let (width, height) = size_from_env()?;

    let mut session = RdpSession::connect(&addr, &user, &pass, width, height).await?;
    println!("connected to {addr} as {user}, desktop {width}x{height}");

    // Any failure after the connection is photographed before it is reported.
    // What the desktop was showing at the moment of the failure is usually the
    // whole diagnosis — a greeter rather than a session, a dialogue in the way,
    // an editor that never opened — and it is lost once the session closes.
    let outcome = run_script(&mut session, &actions).await;
    if outcome.is_err() {
        const FAILURE_SHOT: &str = "/tmp/recon-probe-failure.png";
        match session.save_screenshot(FAILURE_SHOT) {
            Ok(()) => eprintln!("failure screenshot saved to {FAILURE_SHOT}"),
            Err(e) => eprintln!("no failure screenshot: {e}"),
        }
    }
    outcome
}

async fn run_script(session: &mut RdpSession, actions: &[Action]) -> Result<()> {
    let mut reads = 0usize;
    let mut empty_reads = 0usize;

    for (i, action) in actions.iter().enumerate() {
        let step = i + 1;
        match action {
            Action::Ready { ms } => {
                session.wait_until_ready(Duration::from_millis(*ms)).await?;
                println!("[{step}] clipboard channel ready");
            }
            Action::Wait { ms } => {
                session.settle(Duration::from_millis(*ms)).await?;
                println!("[{step}] waited {ms} ms");
            }
            Action::Click { x, y } => {
                session.click(*x, *y).await?;
                println!("[{step}] clicked {x},{y}");
            }
            Action::Key { codes } => {
                session.press(codes).await?;
                println!("[{step}] pressed {}", hex_codes(codes));
            }
            Action::Type { chords } => {
                session.type_chords(chords).await?;
                println!("[{step}] typed {} characters", chords.len());
            }
            Action::Shot { path } => {
                session.save_screenshot(path)?;
                println!("[{step}] screenshot saved to {path}");
            }
            Action::Clear => {
                session.clear_clipboard();
                println!("[{step}] cleared the clipboard latch");
            }
            Action::Read { ms } => {
                reads += 1;
                // From the start of the read, which follows the command that
                // copies, to the text: the query, the copy and the transfer.
                let started = Instant::now();
                match session.read_clipboard(Duration::from_millis(*ms)).await? {
                    Some((format, text)) => {
                        println!(
                            "[{step}] read completed in {} ms",
                            started.elapsed().as_millis()
                        );
                        report(step, format, session.advertised_formats(), &text);
                    }
                    None => {
                        empty_reads += 1;
                        println!(
                            "[{step}] nothing arrived within {ms} ms. \
                             Announced formats: {}",
                            describe_formats(session.advertised_formats().as_deref())
                        );
                    }
                }
            }
        }
    }

    if reads > 0 && empty_reads == reads {
        bail!("every read returned nothing: no clipboard content reached this client");
    }
    Ok(())
}

/// What arrived, in enough detail to compare it with the file on the host
/// without reading a kilobyte of lorem ipsum by eye.
fn report(step: usize, format: u32, advertised: Option<Vec<u32>>, text: &str) {
    let normalised = text.replace("\r\n", "\n");
    let crlf = text.matches("\r\n").count();
    let bare_lf = normalised.matches('\n').count() - crlf;

    println!("[{step}] ── clipboard ─────────────────────────────────────────");
    println!("      format:     {} ({})", format, format_name(format));
    println!(
        "      advertised: {}",
        describe_formats(advertised.as_deref())
    );
    println!(
        "      size:       {} bytes, {} characters, {} lines",
        text.len(),
        text.chars().count(),
        text.lines().count()
    );
    println!("      newlines:   {crlf} CRLF, {bare_lf} bare LF");
    println!(
        "      sha256:     {}  (as received)",
        sha256(text.as_bytes())
    );
    if crlf > 0 {
        println!(
            "      sha256:     {}  (CRLF converted to LF)",
            sha256(normalised.as_bytes())
        );
    }
    println!("      ── text ──────────────────────────────────────────────");
    println!("{text}");
    println!("      ── end of clipboard text ─────────────────────────────");
}

/// Names for the standard clipboard formats xrdp advertises for a text copy.
fn format_name(id: u32) -> &'static str {
    match id {
        CF_UNICODETEXT => "CF_UNICODETEXT",
        CF_TEXT => "CF_TEXT",
        7 => "CF_OEMTEXT",
        16 => "CF_LOCALE",
        _ => "not a standard text format",
    }
}

fn describe_formats(ids: Option<&[u32]>) -> String {
    match ids {
        None => "none — the host announced no copy".to_owned(),
        Some(ids) => ids
            .iter()
            .map(|id| format!("{id} ({})", format_name(*id)))
            .collect::<Vec<_>>()
            .join(", "),
    }
}

fn sha256(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn hex_codes(codes: &[u8]) -> String {
    codes
        .iter()
        .map(|c| format!("0x{c:02X}"))
        .collect::<Vec<_>>()
        .join("+")
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_owned())
}

/// `RECON_SIZE=1280x1024`. The geometry is the client's to choose (`recon.md`
/// §5.1), and the desktop layout — hence every coordinate in a script — depends
/// on it, so it is stated rather than defaulted silently.
fn size_from_env() -> Result<(u16, u16)> {
    let Ok(raw) = std::env::var("RECON_SIZE") else {
        return Ok(DEFAULT_SIZE);
    };
    let (w, h) = raw
        .split_once('x')
        .context("RECON_SIZE must look like 1280x1024")?;
    Ok((
        w.trim().parse().context("RECON_SIZE width")?,
        h.trim().parse().context("RECON_SIZE height")?,
    ))
}

/// Install a `tracing` subscriber (INFO by default; override via `IRONRDP_LOG`).
fn init_logging() -> Result<()> {
    use tracing::metadata::LevelFilter;
    use tracing_subscriber::prelude::*;
    use tracing_subscriber::EnvFilter;

    let filter = EnvFilter::builder()
        .with_default_directive(LevelFilter::INFO.into())
        .with_env_var("IRONRDP_LOG")
        .from_env_lossy();
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::fmt::layer()
                .compact()
                .with_writer(std::io::stderr),
        )
        .with(filter)
        .try_init()
        .context("set tracing subscriber")?;
    Ok(())
}
