//! Private implementation of the serial transport. Not visible to other
//! modules.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;
use tokio::sync::{broadcast, watch};

use super::ReaderReadiness;

/// Capacity of the per-reader broadcast ring that buffers card-number frames for
/// connected clients. Card presentations are infrequent and each frame is a few
/// bytes, so the ring never fills in practice; were a client to fall that far
/// behind it would skip whole frames rather than split one, preserving framing.
const FRAME_RING_CAPACITY: usize = 64;

/// The transport behind a `SerialConnection`. The in-process loopback captures
/// written bytes for in-process wiring and tests; the TCP backend is the
/// boundary transport for the RFID readers (sim-network.md), streaming each
/// written frame to every connected client.
pub(super) enum Backend {
    Loopback(Arc<Mutex<Vec<u8>>>),
    Tcp {
        tx: broadcast::Sender<Vec<u8>>,
        /// Number of digital clients currently connected. A sim actor waits for
        /// this to reach one before presenting a card (see [`ReaderReadiness`]),
        /// so a one-shot card is never broadcast to an unattached reader.
        subscribers: watch::Receiver<usize>,
    },
}

/// Decrements the connected-client count when a per-client writer task ends,
/// so the readiness count tracks live connections without a separate teardown
/// path.
struct SubscriberGuard(watch::Sender<usize>);

impl Drop for SubscriberGuard {
    fn drop(&mut self) {
        self.0.send_modify(|n| *n = n.saturating_sub(1));
    }
}

impl Backend {
    pub(super) fn loopback(buffer: Arc<Mutex<Vec<u8>>>) -> Self {
        Backend::Loopback(buffer)
    }

    /// Bind a TCP listener at `addr` and spawn an accept loop that streams every
    /// written frame to each connected client. The listener is bound
    /// synchronously, so the port is reachable as soon as this returns; the
    /// accept loop is spawned, so this must be called within a Tokio runtime
    /// (mirroring the appliance listeners in `atg_console`/`dispenser`).
    pub(super) fn tcp(addr: SocketAddr) -> Self {
        // All fallible setup is synchronous, so a failure is fatal in the
        // caller (a misconfigured boundary at startup), not swallowed inside a
        // detached task.
        let std_listener =
            std::net::TcpListener::bind(addr).expect("serial tcp: bind TCP listener");
        std_listener
            .set_nonblocking(true)
            .expect("serial tcp: set_nonblocking");
        let listener =
            TcpListener::from_std(std_listener).expect("serial tcp: adopt std listener into Tokio");

        let (tx, _rx) = broadcast::channel::<Vec<u8>>(FRAME_RING_CAPACITY);
        // Tracks currently-connected clients. `subscriber_tx` lives for the
        // accept loop's lifetime, keeping the watch channel open; each writer
        // task holds a `SubscriberGuard` clone that decrements on exit.
        let (subscriber_tx, subscriber_rx) = watch::channel(0usize);
        let accept_tx = tx.clone();
        tokio::spawn(async move {
            loop {
                match listener.accept().await {
                    Ok((stream, _addr)) => {
                        // Each client subscribes from the moment it connects;
                        // frames written before it connected are already gone
                        // from the wire, exactly as on a real serial line.
                        let mut frames = accept_tx.subscribe();
                        subscriber_tx.send_modify(|n| *n += 1);
                        let subscriber_guard = SubscriberGuard(subscriber_tx.clone());
                        tokio::spawn(async move {
                            // Held for the connection's lifetime: dropping it on
                            // task exit decrements the connected-client count.
                            let _subscriber_guard = subscriber_guard;
                            // The reader only ever drives the wire; the client's
                            // read half is unused.
                            let (_read_half, mut write_half) = stream.into_split();
                            loop {
                                match frames.recv().await {
                                    Ok(frame) => {
                                        if write_half.write_all(&frame).await.is_err() {
                                            // The host closed its end. The decoder
                                            // keeps driving the wire, so drop this
                                            // client and carry on.
                                            return;
                                        }
                                    }
                                    // The ring overran a client that fell behind:
                                    // skip the missed frames rather than split one.
                                    Err(broadcast::error::RecvError::Lagged(_)) => continue,
                                    // No senders remain — the sim is shutting down.
                                    Err(broadcast::error::RecvError::Closed) => return,
                                }
                            }
                        });
                    }
                    // A transient accept error is ignored — the reader keeps
                    // serving, as a real sealed appliance would.
                    Err(_) => continue,
                }
            }
        });

        Backend::Tcp {
            tx,
            subscribers: subscriber_rx,
        }
    }

    /// A readiness handle for this backend. The loopback backend is always ready
    /// (its handle captures every write); the TCP backend is ready once at least
    /// one digital client is connected. Callable before the connection is moved
    /// into the reader task.
    pub(super) fn readiness(&self) -> ReaderReadiness {
        match self {
            Backend::Loopback(_) => ReaderReadiness::always_ready(),
            Backend::Tcp { subscribers, .. } => ReaderReadiness::subscribers(subscribers.clone()),
        }
    }

    pub(super) async fn write_all(&mut self, bytes: &[u8]) -> std::io::Result<()> {
        match self {
            Backend::Loopback(buffer) => {
                buffer
                    .lock()
                    .expect("serial loopback: buffer mutex poisoned")
                    .extend_from_slice(bytes);
                Ok(())
            }
            Backend::Tcp { tx, .. } => {
                // A real Wiegand decoder drives the wire whether or not a host is
                // listening: with no client connected the frame is lost on the
                // wire, which is not a transport failure. `send` errs only when
                // there are no subscribers, which is exactly that no-listener
                // case, so the error is dropped rather than surfaced.
                let _ = tx.send(bytes.to_vec());
                Ok(())
            }
        }
    }
}
