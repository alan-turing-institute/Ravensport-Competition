//! Private implementation of the fuel supplier actor. Not visible to other
//! modules.
//!
//! The supplier is a Veeder-Root TLS *client* (the counterpart to the ATG
//! console server in `appliances::atg_console`): every `poll_interval` it
//! connects to the relay, sends `i201`/`i101`, and parses the computer-format
//! responses (tls-protocol.md §6) for the tank volume and alarm state. It polls
//! at every interval, whether or not an order is pending (sim.md §5.3). An
//! order is pending from the poll that places it until the road tanker driver
//! signals that the delivery has ended, so at most one is pending at a time.
//! Each action is held as its due time, so that a stop can report it
//! (sim-impl.md, "Stopping and warm start").
//!
//! Error handling (sim-impl.md): transient connection failures are retried with
//! backoff, then fatal; a malformed response from the (trusted) ATG console is
//! fatal; a closed channel to (or a dropped completion signal from) the road
//! tanker driver is fatal.

use std::io;
use std::net::SocketAddr;
use std::time::Duration;

use time::format_description::well_known::Rfc3339;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpSocket, TcpStream};
use tokio::sync::{mpsc, oneshot};

use time::PrimitiveDateTime;

use super::{FillJob, FuelSupplierState, SupplierOrder, LOG_TARGET};
use crate::clock::SimClock;
use crate::stop::{after_peer, rest, sleep_until, StopRequest};

const ENTITY: &str = "fuel_supplier";

const SOH: u8 = 0x01;
const ETX: u8 = 0x03;

/// In-Tank Inventory Report for all tanks (tls-protocol.md §6.2).
const I201_REQUEST: &[u8] = b"\x01i20100";
/// System Status Report (tls-protocol.md §6.1).
const I101_REQUEST: &[u8] = b"\x01i10100";

/// The i101 body when no alarm is active.
const NO_ALARM_BODY: &[u8] = b"000000";

/// Retry backoffs (seconds) for transient connection failures, slept on the sim
/// clock. Exhausting them is fatal (sim-impl.md retry policy).
const RETRY_BACKOFFS_SECS: [u64; 3] = [1, 2, 4];

#[allow(clippy::too_many_arguments)]
pub(super) fn spawn(
    clock: SimClock,
    start: FuelSupplierState,
    mut stop: StopRequest,
    mut driver_at_rest: oneshot::Receiver<()>,
    report: oneshot::Sender<FuelSupplierState>,
    local_addr: SocketAddr,
    atg_relay_addr: SocketAddr,
    poll_interval: Duration,
    reorder_threshold_l: f64,
    delivery_volume_l: f64,
    lead_time: Duration,
    tanker_arrival_tx: mpsc::Sender<FillJob>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        // The poll schedule and the order start from `start`: on a cold start
        // as the start state gives them, the first poll when it gives it, or
        // one interval after the start with its order pending (Start::cold;
        // sim-impl.md, "Start state"); on a warm start as the checkpoint saved
        // them. Polls fall due one `poll_interval` apart, which an order does
        // not move.
        let FuelSupplierState {
            mut next_poll_at,
            order,
        } = start;
        let mut order = match order {
            SupplierOrder::None => Order::None,
            SupplierOrder::TankerDueAt(arrives_at) => {
                // The order pending at the start is logged as an ordering
                // poll's is, with its tanker's scheduled arrival; no poll read
                // a volume for it.
                tracing::info!(
                    target: LOG_TARGET,
                    tanker_arrival = %rfc3339(arrives_at),
                    "order placed"
                );
                Order::LeadTime { arrives_at }
            }
        };

        loop {
            match next_event(&clock, next_poll_at, &mut order, &mut stop).await {
                Event::Stop => {
                    // No further poll or dispatch. A delivery in progress is
                    // waited for; the driver's report before it ends means the
                    // driver has not taken it up, and the tanker waits from its
                    // arrival.
                    let order = match order {
                        Order::None => SupplierOrder::None,
                        Order::LeadTime { arrives_at } => SupplierOrder::TankerDueAt(arrives_at),
                        Order::Delivering {
                            mut completed_rx,
                            sent_at,
                        } => tokio::select! {
                            biased;
                            ended = &mut completed_rx => {
                                ended.expect(
                                    "fuel_supplier: road tanker driver dropped the delivery-completion signal",
                                );
                                SupplierOrder::None
                            }
                            _ = after_peer(ENTITY, "road tanker driver", &mut driver_at_rest) => {
                                SupplierOrder::TankerDueAt(sent_at)
                            }
                        },
                    };
                    rest(
                        ENTITY,
                        report,
                        FuelSupplierState {
                            next_poll_at,
                            order,
                        },
                    )
                    .await
                }
                Event::PollDue => {
                    // Each poll falls due one interval after the previous one
                    // fell due, not after it ended, so retry backoffs do not
                    // move the schedule either.
                    next_poll_at += poll_interval;
                    let (volume_l, alarm_active) =
                        poll_atg(&clock, local_addr, atg_relay_addr).await;
                    // A poll while an order is pending places no order.
                    let orders = matches!(order, Order::None)
                        && volume_l <= reorder_threshold_l
                        && !alarm_active;
                    if orders {
                        order = Order::LeadTime {
                            arrives_at: clock.now_datetime() + lead_time,
                        };
                    }
                    tracing::info!(
                        target: LOG_TARGET,
                        volume_l,
                        alarm_active,
                        order_placed = orders,
                        "poll"
                    );
                    if orders {
                        // The order, with the volume the poll read and the
                        // tanker's scheduled arrival, one lead time on.
                        tracing::info!(
                            target: LOG_TARGET,
                            volume_l,
                            tanker_arrival = %rfc3339(clock.now_datetime() + lead_time),
                            "order placed"
                        );
                    }
                }
                Event::TankerArrives => {
                    let sent_at = clock.now_datetime();
                    let (completed, completed_rx) = oneshot::channel();
                    tanker_arrival_tx
                        .send(FillJob {
                            delivery_volume_l,
                            completed,
                        })
                        .await
                        .expect("fuel_supplier: road tanker driver arrival channel closed");
                    tracing::info!(target: LOG_TARGET, delivery_volume_l, "tanker arrival");
                    order = Order::Delivering {
                        completed_rx,
                        sent_at,
                    };
                }
                Event::DeliveryEnded => {
                    tracing::info!(target: LOG_TARGET, "delivery ended");
                    order = Order::None;
                }
            }
        }
    })
}

/// The supplier's order, from the poll that places it to the end of its
/// delivery.
enum Order {
    /// No order pending: a below-threshold poll with no critical alarm places
    /// one.
    None,
    /// Placed; the tanker arrives, and the `FillJob` is sent, at `arrives_at`.
    LeadTime { arrives_at: PrimitiveDateTime },
    /// The `FillJob` sent at `sent_at`; pending until the road tanker driver
    /// fires its `completed` signal, which it does on every outcome of the
    /// delivery.
    Delivering {
        completed_rx: oneshot::Receiver<()>,
        sent_at: PrimitiveDateTime,
    },
}

/// What the supplier acts on next.
enum Event {
    Stop,
    PollDue,
    TankerArrives,
    DeliveryEnded,
}

/// Wait for the next event: a stop, the next poll, and, depending on the
/// order, the end of its lead time or the driver's signal that its delivery
/// has ended.
///
/// A stop is taken first. Where the order's event and the poll are both ready
/// when the supplier is woken, the order's event is taken first; the poll
/// follows at the same simulated instant, so a poll at the instant a delivery
/// ends may place the next order. A dropped `completed` sender is fatal: the
/// order could then never end.
async fn next_event(
    clock: &SimClock,
    next_poll_at: PrimitiveDateTime,
    order: &mut Order,
    stop: &mut StopRequest,
) -> Event {
    match order {
        Order::None => tokio::select! {
            biased;
            () = stop.requested() => Event::Stop,
            () = sleep_until(clock, next_poll_at) => Event::PollDue,
        },
        Order::LeadTime { arrives_at } => {
            let arrives_at = *arrives_at;
            tokio::select! {
                biased;
                () = stop.requested() => Event::Stop,
                () = sleep_until(clock, arrives_at) => Event::TankerArrives,
                () = sleep_until(clock, next_poll_at) => Event::PollDue,
            }
        }
        Order::Delivering { completed_rx, .. } => {
            tokio::select! {
                biased;
                () = stop.requested() => Event::Stop,
                ended = completed_rx => {
                    ended.expect(
                        "fuel_supplier: road tanker driver dropped the delivery-completion signal",
                    );
                    Event::DeliveryEnded
                }
                () = sleep_until(clock, next_poll_at) => Event::PollDue,
            }
        }
    }
}

/// Poll the ATG for `(volume_litres, alarm_active)`, retrying transient
/// connection failures with backoff. A malformed response is fatal (the ATG
/// console is a trusted internal appliance).
async fn poll_atg(clock: &SimClock, local_addr: SocketAddr, relay_addr: SocketAddr) -> (f64, bool) {
    let mut attempt = 0usize;
    loop {
        // Guard the whole connect/request/response exchange; simulated time must
        // not advance while it is in flight. Released before any retry backoff,
        // so the backoff sleep still advances time.
        let polled = {
            let _io = clock.io_guard();
            try_poll(local_addr, relay_addr).await
        };
        match polled {
            Ok(result) => {
                if attempt > 0 {
                    tracing::warn!(attempts = attempt + 1, "ATG poll recovered");
                }
                // Record every inventory reading the supplier receives, whatever
                // its source. Under a false-feed attack this is the fabricated or
                // replayed value the supplier was deceived into accepting, so it
                // is the observation point for the attack-chain test suite
                // (attack-chains-tests.md §5) and shows on the deployment monitor.
                // The report's own console timestamp is logged too: a replayed
                // reply repeats a captured reading's frozen timestamp, so a stale
                // replay is distinguishable from a live feed whose timestamp
                // advances, independent of how far the tank has drained.
                let (volume_l, alarm_active, report_ts) = result;
                tracing::info!(
                    volume_l,
                    alarm_active,
                    report_ts = %report_ts,
                    "supplier received ATG reading"
                );
                return (volume_l, alarm_active);
            }
            Err(error) => {
                if attempt >= RETRY_BACKOFFS_SECS.len() {
                    panic!("fuel_supplier: ATG poll failed after {attempt} retries: {error}");
                }
                clock
                    .sleep(Duration::from_secs(RETRY_BACKOFFS_SECS[attempt]))
                    .await;
                attempt += 1;
            }
        }
    }
}

/// One poll attempt: connect, request inventory and status, parse both. Returns
/// the volume, the alarm state, and the inventory report's own `YYMMDDHHmm`
/// console timestamp — the last carried so a replayed reply, which repeats a
/// captured reading's frozen timestamp, is distinguishable from a live feed whose
/// timestamp advances (attack-chains-tests.md §5).
async fn try_poll(
    local_addr: SocketAddr,
    relay_addr: SocketAddr,
) -> io::Result<(f64, bool, String)> {
    let mut stream = connect(local_addr, relay_addr).await?;

    stream.write_all(I201_REQUEST).await?;
    let i201 = read_response(&mut stream).await?;
    let volume_l = parse_volume(&i201);
    let report_ts = parse_report_timestamp(&i201);

    stream.write_all(I101_REQUEST).await?;
    let i101 = read_response(&mut stream).await?;
    let alarm = alarm_active(&i101);

    Ok((f64::from(volume_l), alarm, report_ts))
}

/// Connect to the relay, source-bound to the supplier's `vnet-ext` address.
async fn connect(local_addr: SocketAddr, relay_addr: SocketAddr) -> io::Result<TcpStream> {
    let socket = if relay_addr.is_ipv4() {
        TcpSocket::new_v4()?
    } else {
        TcpSocket::new_v6()?
    };
    socket.bind(local_addr)?;
    socket.connect(relay_addr).await
}

/// Read one computer-format response, up to and including `ETX`.
async fn read_response(stream: &mut TcpStream) -> io::Result<Vec<u8>> {
    let mut buffer = Vec::new();
    loop {
        let mut byte = [0u8; 1];
        stream.read_exact(&mut byte).await?;
        buffer.push(byte[0]);
        if byte[0] == ETX {
            return Ok(buffer);
        }
    }
}

/// The body of a computer-format response: the bytes between the
/// `SOH echo(6) timestamp(10)` prefix and the `&& checksum(4) ETX` suffix
/// (tls-protocol.md §6).
fn response_body(response: &[u8]) -> &[u8] {
    assert!(
        response.first() == Some(&SOH) && response.last() == Some(&ETX),
        "fuel_supplier: response is not SOH…ETX framed"
    );
    let end = response
        .len()
        .checked_sub(7)
        .expect("fuel_supplier: response too short for the trailing checksum");
    response
        .get(17..end)
        .expect("fuel_supplier: response too short for its header")
}

/// Extract the Volume field (field 1) from an `i201` inventory response. The
/// body is `TT(2) p(1) ssss(4) NN(2)` then seven IEEE-754 floats of eight
/// upper-case hex characters each; field 1 begins at offset 9
/// (tls-protocol.md §6.2).
fn parse_volume(response: &[u8]) -> f32 {
    let body = response_body(response);
    let hex = body
        .get(9..17)
        .expect("fuel_supplier: i201 response too short for the Volume field");
    decode_f32_be(hex)
}

/// The `YYMMDDHHmm` console timestamp from a computer-format response: the ten
/// bytes after the `SOH echo(6)` prefix, before the body (tls-protocol.md §6).
/// The console fills it with its current simulated wall-clock time, so a genuine
/// reply's timestamp advances between polls while a replayed reply repeats a
/// captured one verbatim.
fn parse_report_timestamp(response: &[u8]) -> String {
    let ts = response
        .get(7..17)
        .expect("fuel_supplier: response too short for its timestamp");
    String::from_utf8_lossy(ts).into_owned()
}

/// True when the `i101` system-status response carries any active alarm. The
/// all-normal body is `000000` (tls-protocol.md §6.1).
fn alarm_active(response: &[u8]) -> bool {
    response_body(response) != NO_ALARM_BODY
}

/// Decode eight upper-case hex characters as a big-endian IEEE-754 single.
fn decode_f32_be(hex: &[u8]) -> f32 {
    let text = std::str::from_utf8(hex).expect("fuel_supplier: non-UTF-8 hex in float field");
    let mut bytes = [0u8; 4];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16)
            .expect("fuel_supplier: invalid hex in float field");
    }
    f32::from_be_bytes(bytes)
}

/// An instant of the simulator's calendar, which is in UTC, in RFC 3339.
fn rfc3339(at: time::PrimitiveDateTime) -> String {
    at.assume_utc()
        .format(&Rfc3339)
        .expect("a date in the simulator's calendar formats as RFC 3339")
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Encode a value as the eight-hex-character big-endian float the ATG emits.
    fn float_field(value: f32) -> String {
        value
            .to_be_bytes()
            .iter()
            .map(|byte| format!("{byte:02X}"))
            .collect()
    }

    /// Build an i201 response carrying `volume` in field 1 (the other six
    /// fields are zero), framed exactly as the ATG console emits it.
    fn i201_response(volume: f32) -> Vec<u8> {
        let mut body = String::from("01D000007"); // TT=01, p=D, ssss=0000, NN=07
        body.push_str(&float_field(volume));
        for _ in 0..6 {
            body.push_str(&float_field(0.0));
        }
        frame(b"i20100", &body)
    }

    fn i101_response(body: &str) -> Vec<u8> {
        frame(b"i10100", body)
    }

    /// Frame an echoed code and body as `SOH echo timestamp body && checksum ETX`.
    fn frame(echo: &[u8], body: &str) -> Vec<u8> {
        let mut message = vec![SOH];
        message.extend_from_slice(echo);
        message.extend_from_slice(b"0000000000"); // timestamp
        message.extend_from_slice(body.as_bytes());
        message.extend_from_slice(b"&&");
        let sum = message
            .iter()
            .fold(0u16, |acc, &b| acc.wrapping_add(u16::from(b)));
        let checksum = 0u16.wrapping_sub(sum);
        message.extend_from_slice(format!("{checksum:04X}").as_bytes());
        message.push(ETX);
        message
    }

    #[test]
    fn parses_volume_from_inventory_response() {
        let response = i201_response(35_000.0);
        assert!((parse_volume(&response) - 35_000.0).abs() < 0.5);
    }

    #[test]
    fn parses_a_low_volume() {
        let response = i201_response(8_000.0);
        assert!((parse_volume(&response) - 8_000.0).abs() < 0.5);
    }

    #[test]
    fn no_alarm_body_is_not_active() {
        assert!(!alarm_active(&i101_response("000000")));
    }

    #[test]
    fn bund_leak_alarm_is_active() {
        // The bund liquid-sensor fuel alarm (tls-protocol.md §6.1).
        assert!(alarm_active(&i101_response("030301")));
    }
}
