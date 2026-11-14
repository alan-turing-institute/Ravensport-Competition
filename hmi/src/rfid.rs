use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::anyhow;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;
use tokio::sync::{mpsc, Mutex};
use tracing::{info, warn};

/// Wait between reconnection attempts once a connection has been accepted and
/// then closed, matching `fms/src/background/rfid.rs`.
///
/// The peer is HAProxy, whose frontend keeps listening after the reader
/// appliance behind it dies. Every connect therefore succeeds and is closed
/// immediately, so the exponential backoff in [`connect`] — which only guards
/// the connect-*failure* path — never engages. Without this wait the loop runs
/// at kernel-handshake speed.
const RECONNECT_DELAY: Duration = Duration::from_secs(2);

/// Ceiling the connect-failure backoff doubles up to, for a reader whose proxy
/// is not listening at all.
const MAX_CONNECT_BACKOFF: Duration = Duration::from_secs(30);

/// How long a connection must hold before its loss counts as a fresh fault
/// rather than another turn of an outage already reported.
///
/// The two cases are orders of magnitude apart: a proxy with a dead backend
/// accepts and closes within microseconds, whereas a working reader holds the
/// connection open for the whole shift.
const HEALTHY_SESSION: Duration = Duration::from_secs(10);

/// Whether losing a connection should be logged.
///
/// Reporting every loss floods the disk; reporting only the first would
/// silence the fault that matters. A reader spends almost all its time idle —
/// cards arrive only when one is presented — so "have we seen traffic since"
/// cannot distinguish recovery from a reader that came back and failed again
/// before anyone tapped a card. The length of the session can: a connection
/// that held is evidence the reader was genuinely there, making its loss news.
///
/// A session that reaches `healthy_after` normally clears `already_reported`
/// through the health timer in [`RfidReader::start`], so the duration test here
/// covers only the race where the connection drops just as the timer becomes
/// ready and the read loop wins: the loss is still of a connection that held,
/// and is still reported.
fn report_disconnect(already_reported: bool, session: Duration, healthy_after: Duration) -> bool {
    !already_reported || session >= healthy_after
}

/// Client for the operator RFID reader.
///
/// hmi.md §5 requires a single persistent connection. A background task owns the
/// connection: it connects (retrying), reads card-number frames continuously,
/// and reconnects on loss — so the connection is established and kept live
/// independently of whether a login is in progress. This matters because the sim
/// presents the operator card only once the HMI is subscribed to the reader
/// (sim.md): the HMI must attach proactively at startup, not lazily on the first
/// challenge, or the two would deadlock. Card numbers read by the background task
/// are delivered to [`read_card`] over an internal channel.
///
/// [`read_card`]: RfidReader::read_card
#[derive(Clone)]
pub struct RfidReader {
    addr: SocketAddr,
    /// Delivers card numbers from the background reader to `read_card`. The
    /// sender is retained so the channel stays open for the reader's lifetime.
    card_tx: mpsc::UnboundedSender<u32>,
    card_rx: Arc<Mutex<mpsc::UnboundedReceiver<u32>>>,
    /// Warnings actually emitted for unparseable input, as opposed to malformed
    /// lines received. The two differ because a run of malformed lines is
    /// reported once, not per line, and the gap between them is the property
    /// worth asserting.
    malformed_warnings: Arc<AtomicUsize>,
    /// Recoveries reported, for the same reason as `malformed_warnings`: the
    /// transition out of an outage is otherwise visible only in the log, and the
    /// reader runs on a spawned task.
    recoveries: Arc<AtomicUsize>,
    /// How long a connection must hold before it counts as recovered. Always
    /// [`HEALTHY_SESSION`] in a shipped build; a test shortens it so it does not
    /// have to wait out the real threshold.
    healthy_after: Duration,
}

impl RfidReader {
    pub fn new(addr: SocketAddr) -> Self {
        Self::with_healthy_after(addr, HEALTHY_SESSION)
    }

    fn with_healthy_after(addr: SocketAddr, healthy_after: Duration) -> Self {
        let (card_tx, card_rx) = mpsc::unbounded_channel();
        Self {
            addr,
            card_tx,
            card_rx: Arc::new(Mutex::new(card_rx)),
            malformed_warnings: Arc::new(AtomicUsize::new(0)),
            recoveries: Arc::new(AtomicUsize::new(0)),
            healthy_after,
        }
    }

    /// Number of warnings emitted for unparseable input since startup.
    ///
    /// Test-only: the suppression in [`start`] is otherwise observable solely
    /// through the log, and the reader runs on a spawned task, so a scoped
    /// tracing subscriber in the test would not see it. Counting in the reader
    /// keeps the assertion deterministic. The counter itself is maintained in
    /// every build; only the accessor is conditional, so the code under test is
    /// the code that ships.
    ///
    /// [`start`]: RfidReader::start
    #[cfg(test)]
    pub fn unparseable_warnings(&self) -> usize {
        self.malformed_warnings.load(Ordering::Relaxed)
    }

    /// Number of times the reader has reported an outage as recovered.
    #[cfg(test)]
    pub fn recoveries_reported(&self) -> usize {
        self.recoveries.load(Ordering::Relaxed)
    }

    /// Spawn the background reader task. Call once at startup: it establishes the
    /// persistent connection, reads card frames, and reconnects on loss, holding
    /// the connection live so the sim observes the HMI as subscribed. Returns
    /// immediately (the HTTP listener can bind without waiting for the reader).
    pub fn start(&self) {
        let addr = self.addr;
        let card_tx = self.card_tx.clone();
        let malformed_warnings = Arc::clone(&self.malformed_warnings);
        let recoveries = Arc::clone(&self.recoveries);
        let healthy_after = self.healthy_after;
        tokio::spawn(async move {
            // Both flags exist to report a fault once rather than once per
            // attempt. The reader is not respawned after the sim dies
            // (`sim-network.md` §7), so an outage lasts days, and a line per
            // pass is what fills the shared disk even once the
            // reconnect itself is paced. `fms/src/background/syslog.rs` reports
            // its own outages the same way.
            let mut outage_reported = false;
            let mut malformed_run: u64 = 0;
            loop {
                let stream = connect(addr, outage_reported).await;
                let connected_at = std::time::Instant::now();
                let mut lines = BufReader::new(stream).lines();
                let lost: String;
                // Fires once, `healthy_after` into this connection. Recovery has
                // to be observed while the connection is still open: the reader
                // is idle almost all the time, so waiting for a card leaves a
                // genuine recovery unreported indefinitely, and `connect` is
                // quiet for the duration of a reported outage, so nothing else
                // marks the return.
                let held_long_enough = tokio::time::sleep(healthy_after);
                tokio::pin!(held_long_enough);
                loop {
                    let next = tokio::select! {
                        // Disabled once the outage is cleared, so the elapsed
                        // timer is never polled again. `outage_reported` cannot
                        // become true again within this iteration — it is set
                        // only after the loop breaks — so the branch is live at
                        // most once per connection.
                        () = &mut held_long_enough, if outage_reported => {
                            info!(
                                "RFID reader recovered: connection has held for \
                                 {healthy_after:?}"
                            );
                            recoveries.fetch_add(1, Ordering::Relaxed);
                            outage_reported = false;
                            // A run of malformed lines belongs to the connection
                            // it was seen on, so this connection having proved
                            // itself ends it: the next unparseable line is a new
                            // fault and warns. Clearing it here rather
                            // than per connection is what keeps a peer that
                            // emits rubbish and drops from warning every
                            // RECONNECT_DELAY — such a connection never holds
                            // long enough to reach this branch.
                            if malformed_run > 0 {
                                info!(
                                    "RFID reader: {malformed_run} malformed line(s) \
                                     suppressed before the connection recovered"
                                );
                                malformed_run = 0;
                            }
                            continue;
                        }
                        // Cancel-safe: partially read data stays in the `Lines`
                        // buffer, so losing this race cannot drop half a card.
                        line = lines.next_line() => line,
                    };
                    match next {
                        Ok(Some(line)) => match line.trim().parse::<u32>() {
                            Ok(card) => {
                                if malformed_run > 0 {
                                    info!(
                                        "RFID reader: {malformed_run} malformed line(s) \
                                         suppressed before this card"
                                    );
                                    malformed_run = 0;
                                }
                                // A card is definitive evidence the reader is
                                // working, so it clears the outage ahead of the
                                // timer rather than waiting it out.
                                if outage_reported {
                                    info!("RFID reader recovered: card read");
                                    recoveries.fetch_add(1, Ordering::Relaxed);
                                    outage_reported = false;
                                }
                                // The receiver is gone only if the HMI is shutting
                                // down; stop the reader in that case.
                                if card_tx.send(card).is_err() {
                                    return;
                                }
                            }
                            // Only the first of a run is logged. A reader
                            // stuck emitting rubbish holds the connection open, so
                            // the loop never exits and RECONNECT_DELAY never
                            // applies; without suppression it logs at line rate.
                            Err(e) => {
                                if malformed_run == 0 {
                                    warn!(
                                        "RFID reader: unparseable card {line:?}: {e}; \
                                         further malformed lines suppressed until a \
                                         card is read"
                                    );
                                    malformed_warnings.fetch_add(1, Ordering::Relaxed);
                                }
                                malformed_run = malformed_run.saturating_add(1);
                            }
                        },
                        Ok(None) => {
                            lost = "closed by the peer".to_owned();
                            break;
                        }
                        Err(e) => {
                            lost = format!("read error: {e}");
                            break;
                        }
                    }
                }

                if report_disconnect(outage_reported, connected_at.elapsed(), healthy_after) {
                    warn!(
                        "RFID reader connection {lost}; reconnecting every \
                         {RECONNECT_DELAY:?} until it recovers"
                    );
                }
                outage_reported = true;
                // The connection was accepted then dropped — e.g. the reader
                // appliance is down and HAProxy closes an unrouteable connection
                // immediately. Back off before reconnecting so a persistently-down
                // reader cannot drive a tight connect->EOF->connect loop (mirrors
                // fms/src/background/rfid.rs).
                tokio::time::sleep(RECONNECT_DELAY).await;
            }
        });
    }

    /// Await the next card number presented at the reader. A card read by the
    /// background task before this is called is buffered and returned at once.
    pub async fn read_card(&self) -> anyhow::Result<u32> {
        let mut rx = self.card_rx.lock().await;
        rx.recv()
            .await
            .ok_or_else(|| anyhow!("RFID reader channel closed"))
    }
}

/// Connect to the reader, retrying with exponential backoff until it succeeds.
///
/// `quiet` suppresses the per-attempt lines while an outage is already being
/// reported by the caller. A dead reader whose proxy still accepts produces a
/// successful connect on every pass, so logging each one is a line per
/// `RECONNECT_DELAY` for the whole outage.
async fn connect(addr: SocketAddr, quiet: bool) -> TcpStream {
    let mut backoff = Duration::from_millis(500);
    let mut failure_reported = false;
    loop {
        match TcpStream::connect(addr).await {
            Ok(stream) => {
                if !quiet {
                    info!("RFID reader connected to {addr}");
                }
                return stream;
            }
            Err(e) => {
                if !quiet && !failure_reported {
                    warn!(
                        "RFID reader connect failed: {e}; retrying with backoff \
                         up to {MAX_CONNECT_BACKOFF:?}"
                    );
                    failure_reported = true;
                }
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(MAX_CONNECT_BACKOFF);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    use tokio::io::AsyncWriteExt;
    use tokio::net::TcpListener;
    use tokio::time::timeout;

    /// A card presented at the reader is delivered to `read_card`, including when
    /// it is written before `read_card` is called (buffered by the background
    /// task) — the case that the sim's dock operator triggers: it presents the
    /// card between the login POST and the challenge poll.
    #[tokio::test]
    async fn presented_card_is_delivered() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let reader = RfidReader::new(addr);
        reader.start();

        // The background reader connects; accept it and present a card before any
        // read_card call.
        let (mut stream, _) = listener.accept().await.unwrap();
        stream.write_all(b"1000002\n").await.unwrap();

        let card = timeout(Duration::from_secs(1), reader.read_card())
            .await
            .expect("read_card timed out")
            .unwrap();
        assert_eq!(card, 1_000_002);
    }

    /// The background reader reconnects after the connection drops and still
    /// delivers a subsequent card — so a transient reader/proxy disconnect does
    /// not leave the HMI permanently detached.
    #[tokio::test]
    async fn reader_reconnects_after_connection_drop() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let reader = RfidReader::new(addr);
        reader.start();

        // Accept the first connection, then drop it to force a reconnect.
        let (stream, _) = listener.accept().await.unwrap();
        drop(stream);

        // The reader reconnects after the ~2 s backoff; accept the new connection
        // and present a card. The timeout allows for that backoff.
        let (mut stream, _) = listener.accept().await.unwrap();
        stream.write_all(b"1000002\n").await.unwrap();

        let card = timeout(Duration::from_secs(5), reader.read_card())
            .await
            .expect("read_card timed out after reconnect")
            .unwrap();
        assert_eq!(card, 1_000_002);
    }

    /// The reader waits before reconnecting after the peer accepts and then
    /// closes.
    ///
    /// `reader_reconnects_after_connection_drop` above asserts only that a
    /// reconnect happens, so it passes whether or not the backoff is present.
    /// This asserts the delay itself, which is the property that stops the
    /// storm: without it the loop spins at kernel-handshake speed, which wrote
    /// 42 GB to `/var/log/syslog` and filled the host disk.
    ///
    /// Real time rather than tokio's paused clock. The clock auto-advances
    /// whenever the runtime is idle, which here would skip the very sleep under
    /// test; `log_pusher.rs` records the same constraint for its own reason.
    /// The bound is deliberately below `RECONNECT_DELAY` so a loaded machine
    /// cannot flake it, and far enough above zero that removing the sleep fails.
    #[tokio::test]
    async fn reconnect_after_a_dropped_connection_is_delayed() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let reader = RfidReader::new(addr);
        reader.start();

        // Accept and immediately close, which is what HAProxy does when the
        // reader appliance behind it is down.
        let (stream, _) = listener.accept().await.unwrap();
        drop(stream);
        let dropped_at = std::time::Instant::now();

        // The next connection attempt must not arrive until the backoff elapses.
        let (_stream, _) = timeout(Duration::from_secs(10), listener.accept())
            .await
            .expect("reader never reconnected")
            .unwrap();
        let waited = dropped_at.elapsed();

        assert!(
            waited >= Duration::from_millis(1_500),
            "reconnected after {waited:?}; expected a wait of about \
             {RECONNECT_DELAY:?}. Without it the reader storms the peer and \
             fills the disk"
        );
    }

    /// A connection that survives `HEALTHY_SESSION` clears the outage while it is
    /// still open, without waiting for a card.
    ///
    /// Adds a report that the connection has held for (10 seconds) after
    /// a disconnect
    ///
    /// The reader is idle almost all the time, so tying recovery to a card read
    /// leaves a genuine recovery unlogged until someone presents one — and
    /// `connect` is quiet for the duration of a reported outage, so nothing marks
    /// the return either. The log then shows two losses hours apart with no
    /// indication the reader was working in between, which is the question the
    /// log exists to answer.
    ///
    /// The threshold is injected rather than waited out: the mechanism under
    /// test is that a held connection clears the outage on a timer, not the
    /// particular number of seconds, and `HEALTHY_SESSION` would put ten idle
    /// seconds into the suite. The shipped path is unchanged — `new` supplies
    /// the constant.
    #[tokio::test]
    async fn a_connection_that_holds_clears_the_outage_without_a_card() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let healthy_after = Duration::from_millis(200);
        let reader = RfidReader::with_healthy_after(addr, healthy_after);
        reader.start();

        // Accept and close, which is what HAProxy does with a dead reader behind
        // it. This is the loss that gets reported.
        let (stream, _) = listener.accept().await.unwrap();
        drop(stream);

        // The reader comes back. Hold the connection open and present nothing,
        // which is the idle reader the card signal cannot observe.
        let (_held, _) = timeout(Duration::from_secs(10), listener.accept())
            .await
            .expect("reader never reconnected")
            .unwrap();

        // Wait out the health threshold on the held connection.
        tokio::time::sleep(healthy_after * 4).await;

        assert_eq!(
            reader.recoveries_reported(),
            1,
            "a connection held for {healthy_after:?} did not clear the outage; \
             an idle reader that recovers is never reported as back"
        );
    }

    /// The first loss is always reported, so a fault is never silent.
    #[test]
    fn first_disconnect_is_always_reported() {
        assert!(report_disconnect(false, Duration::ZERO, HEALTHY_SESSION));
        assert!(report_disconnect(
            false,
            HEALTHY_SESSION * 10,
            HEALTHY_SESSION
        ));
    }

    /// A proxy accepting and closing immediately must not report every turn:
    /// that is the storm, just written to the log instead of the socket.
    #[test]
    fn repeated_instant_disconnects_are_reported_once() {
        assert!(!report_disconnect(true, Duration::ZERO, HEALTHY_SESSION));
        assert!(!report_disconnect(
            true,
            Duration::from_millis(500),
            HEALTHY_SESSION
        ));
        assert!(!report_disconnect(
            true,
            HEALTHY_SESSION - Duration::from_millis(1),
            HEALTHY_SESSION
        ));
    }

    /// Losing a connection that held is a fresh fault and must be reported even
    /// though an earlier outage already was.
    ///
    /// The health timer normally clears the flag before the loss, so this covers
    /// the race where the read loop wins that select. Either way the loss of a
    /// connection that held is reported rather than folded into the outage that
    /// preceded it.
    #[test]
    fn losing_a_held_connection_is_reported_again() {
        assert!(report_disconnect(true, HEALTHY_SESSION, HEALTHY_SESSION));
        assert!(report_disconnect(
            true,
            HEALTHY_SESSION * 6,
            HEALTHY_SESSION
        ));
    }

    /// A run of malformed lines does not outlive the connection it was seen
    /// on.
    ///
    /// The run is cleared only by a card, and cards are rare, so a reader that
    /// drops and comes back emitting fresh rubbish stays silent — the same
    /// "wait for a card" assumption this PR rejects for outages, left in place
    /// on the malformed path. Once a connection has held long enough to be real,
    /// the next unparseable line is a new fault and must warn.
    #[tokio::test]
    async fn malformed_run_resets_once_a_connection_has_held() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let healthy_after = Duration::from_millis(200);
        let reader = RfidReader::with_healthy_after(addr, healthy_after);
        reader.start();

        // One malformed line on the first connection, then lose it.
        let (mut first, _) = listener.accept().await.unwrap();
        first.write_all(b"not-a-card\n").await.unwrap();
        tokio::time::sleep(healthy_after).await;
        assert_eq!(
            reader.unparseable_warnings(),
            1,
            "the first malformed line did not warn"
        );
        drop(first);

        // The reader comes back and the connection proves itself.
        let (mut second, _) = timeout(Duration::from_secs(10), listener.accept())
            .await
            .expect("reader never reconnected")
            .unwrap();
        tokio::time::sleep(healthy_after * 2).await;

        // Rubbish on a connection that has held is a fresh fault.
        second.write_all(b"not-a-card\n").await.unwrap();
        tokio::time::sleep(healthy_after).await;
        assert_eq!(
            reader.unparseable_warnings(),
            2,
            "a malformed line after recovery did not warn again; the run \
             outlived the connection it was seen on"
        );
    }

    /// Clearing the run must not become a warning per connection.
    ///
    /// A peer that emits rubbish and drops would otherwise warn every
    /// `RECONNECT_DELAY` for as long as it stayed broken, which is the storm
    /// this PR exists to stop, moved onto the malformed path. The run is cleared
    /// by evidence the connection is real, and a connection that dies before
    /// `HEALTHY_SESSION` never provides it.
    #[tokio::test]
    async fn malformed_run_survives_a_connection_that_did_not_hold() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        // The shipped threshold: neither connection here comes close to it.
        let reader = RfidReader::new(addr);
        reader.start();

        let (mut first, _) = listener.accept().await.unwrap();
        first.write_all(b"not-a-card\n").await.unwrap();
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert_eq!(
            reader.unparseable_warnings(),
            1,
            "the first malformed line did not warn"
        );
        drop(first);

        let (mut second, _) = timeout(Duration::from_secs(10), listener.accept())
            .await
            .expect("reader never reconnected")
            .unwrap();
        second.write_all(b"not-a-card\n").await.unwrap();

        // Long enough for the line to be read, far short of HEALTHY_SESSION.
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert_eq!(
            reader.unparseable_warnings(),
            1,
            "warned again for a connection that never held; a reader stuck \
             emitting rubbish would warn every {RECONNECT_DELAY:?}"
        );
    }

    /// A peer that streams unparseable data must not be logged line by
    /// line.
    ///
    /// The read loop warns once per malformed line with no pacing, so a reader
    /// stuck emitting rubbish floods the log at line rate. This is the same
    /// unbounded-logging shape as the reconnection storm, on a different path:
    /// the reconnection backoff does not cover it, because the connection
    /// stays open and the loop never exits.
    ///
    /// Asserts the count of emitted warnings rather than timing, so it is
    /// deterministic.
    #[tokio::test]
    async fn unparseable_lines_are_logged_once_not_per_line() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let reader = RfidReader::new(addr);
        reader.start();

        let (mut stream, _) = listener.accept().await.unwrap();
        for _ in 0..500 {
            stream.write_all(b"not-a-card\n").await.unwrap();
        }
        // A valid card afterwards proves the reader is still working, and ends
        // the suppressed run.
        stream.write_all(b"1000002\n").await.unwrap();

        let card = timeout(Duration::from_secs(5), reader.read_card())
            .await
            .expect("reader stopped delivering cards after malformed input")
            .unwrap();
        assert_eq!(card, 1_000_002);

        assert_eq!(
            reader.unparseable_warnings(),
            1,
            "logged {} warnings for 500 malformed lines; a stuck reader would \
             fill the disk",
            reader.unparseable_warnings()
        );
    }
}
