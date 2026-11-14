//! Serial transport abstraction connecting an RFID reader to its host.
//!
//! The public interface is defined directly in this file per the module
//! convention in sim-impl.md; the private implementation lives in
//! `internals` and is not visible to other modules.
//!
//! Owns: `SerialConnection`, `LoopbackHandle`.

mod internals;

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use tokio::sync::watch;

/// Transport-agnostic serial link. A device writes bytes with [`write_all`];
/// the underlying transport — an in-process loopback for in-process wiring and
/// tests, or a TCP listener at the sim<->digital boundary — is opaque to the
/// writer.
///
/// [`write_all`]: SerialConnection::write_all
pub struct SerialConnection {
    backend: internals::Backend,
}

/// A read view over a loopback [`SerialConnection`], exposing the bytes written
/// to it. Used for in-process wiring and tests.
pub struct LoopbackHandle {
    buffer: Arc<Mutex<Vec<u8>>>,
}

/// Observes whether a digital consumer (HMI or FMS) is currently subscribed to a
/// reader's [`SerialConnection`].
///
/// A real port's HMI and FMS are attached to their readers before plant
/// operations begin; the sim compresses startup to nothing, so its actors would
/// otherwise present a card the instant they spawn — possibly before the digital
/// consumer has connected, losing the one-shot card on the wire. Actors hold a
/// readiness handle and [`wait_ready`] before presenting a card, re-imposing the
/// real ordering (sim-impl.md).
///
/// [`wait_ready`]: ReaderReadiness::wait_ready
#[derive(Clone)]
pub struct ReaderReadiness {
    kind: ReadinessKind,
}

#[derive(Clone)]
enum ReadinessKind {
    /// Loopback backend: the in-process handle captures every write, so there is
    /// no subscription to wait for — always ready.
    AlwaysReady,
    /// TCP backend: ready once at least one client is connected. Holds the
    /// transport's connected-client counter.
    Subscribers(watch::Receiver<usize>),
}

impl ReaderReadiness {
    /// A handle that is always ready. This is the loopback backend's readiness,
    /// and is also the right handle for a consumer that drives the card channel
    /// directly with no reader connection (in-process wiring and actor tests).
    pub fn always_ready() -> Self {
        Self {
            kind: ReadinessKind::AlwaysReady,
        }
    }

    /// A handle backed by the TCP transport's connected-client counter.
    fn subscribers(rx: watch::Receiver<usize>) -> Self {
        Self {
            kind: ReadinessKind::Subscribers(rx),
        }
    }

    /// Resolve once at least one digital consumer is subscribed to the reader
    /// (immediately for the loopback backend).
    pub async fn wait_ready(&self) {
        match &self.kind {
            ReadinessKind::AlwaysReady => {}
            ReadinessKind::Subscribers(rx) => {
                let mut rx = rx.clone();
                // Returns immediately if a client is already connected; otherwise
                // waits for the accept loop to increment. An error means the
                // sender was dropped (the sim is shutting down) — nothing to wait
                // for, so return.
                let _ = rx.wait_for(|&n| n >= 1).await;
            }
        }
    }
}

impl SerialConnection {
    /// Create an in-process loopback connection together with a handle that
    /// captures every byte written to it.
    pub fn loopback() -> (SerialConnection, LoopbackHandle) {
        let buffer = Arc::new(Mutex::new(Vec::new()));
        let connection = SerialConnection {
            backend: internals::Backend::loopback(Arc::clone(&buffer)),
        };
        (connection, LoopbackHandle { buffer })
    }

    /// Create a TCP-backed connection: a listener on `addr` that streams every
    /// byte written to it to each connected client. This is the boundary
    /// transport for the RFID readers (sim-network.md) — a digital client
    /// connects to the reader's address and reads card-number frames, identical
    /// on the wire to the loopback backend. The listener is bound synchronously,
    /// so clients may connect as soon as this returns; the accept loop is
    /// spawned, so it must be called within a Tokio runtime.
    pub fn tcp(addr: SocketAddr) -> SerialConnection {
        SerialConnection {
            backend: internals::Backend::tcp(addr),
        }
    }

    /// A [`ReaderReadiness`] handle for this connection, observing whether a
    /// digital consumer is subscribed. Call before the connection is moved into
    /// the reader task (e.g. in the orchestrator, before spawning the reader).
    pub fn readiness(&self) -> ReaderReadiness {
        self.backend.readiness()
    }

    /// Write all of `bytes` to the transport.
    pub async fn write_all(&mut self, bytes: &[u8]) -> std::io::Result<()> {
        self.backend.write_all(bytes).await
    }
}

impl LoopbackHandle {
    /// A snapshot of every byte written to the connection so far.
    pub fn bytes(&self) -> Vec<u8> {
        self.buffer
            .lock()
            .expect("serial loopback: buffer mutex poisoned")
            .clone()
    }
}
