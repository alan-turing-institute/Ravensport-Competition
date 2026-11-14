//! Private implementation of the dispenser. Not visible to other modules.
//!
//! A custom Modbus TCP server (slave) per dispenser-modbus-tcp-spec.md: a 7-byte
//! MBAP header + PDU, function `0x03` (Read Holding Registers) and `0x06` (Write
//! Single Register), the flat register map `0x0001`–`0x0007`, and exception
//! responses `0x01`/`0x02`/`0x03`. The transaction and unit identifiers are
//! echoed; the protocol identifier is `0x0000`. A flow-meter pulse stream drives
//! the totalisers and the preset auto-stop.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinSet;

use super::{DispenserControlState, DispenserState};
use crate::clock::SimClock;
use crate::physical::flow_meter::Pulse;
use crate::supervision::{propagate_connection_panic, supervise};

// Register map (spec §8): PDU data addresses, 0-based.
const PRESET_REGISTER: u16 = 0x0001;
const ENABLE_REGISTER: u16 = 0x0002;
const CUMULATIVE_HI_REGISTER: u16 = 0x0003;
const CUMULATIVE_LO_REGISTER: u16 = 0x0004;
const TRANSACTION_HI_REGISTER: u16 = 0x0005;
const TRANSACTION_LO_REGISTER: u16 = 0x0006;
const STATUS_REGISTER: u16 = 0x0007;

// Function codes (spec §4) and framing constants.
const READ_HOLDING_REGISTERS: u8 = 0x03;
const WRITE_SINGLE_REGISTER: u8 = 0x06;
const EXCEPTION_FLAG: u8 = 0x80;
const PROTOCOL_ID: u16 = 0x0000;
const MAX_READ_QUANTITY: u16 = 125;

/// Modbus exception codes the dispenser raises (spec §5).
///
/// The shared `Illegal` prefix is the canonical Modbus exception naming, kept
/// verbatim so the variants match the standard and the reference device.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(clippy::enum_variant_names)]
enum Exception {
    IllegalFunction = 0x01,
    IllegalDataAddress = 0x02,
    IllegalDataValue = 0x03,
}

/// The dispenser's mutable state, shared between the Modbus server connections
/// and the pulse accumulator behind a `Mutex`.
struct State {
    /// Preset volume in litres × 10 (spec §7 "unsigned div10").
    preset_x10: u16,
    /// Pump enabled — solenoid open and pump energised move in lockstep.
    enabled: bool,
    /// Lifetime pulse count; never cleared (spec §9).
    cumulative_pulses: u32,
    /// Pulses since the last preset write; resets on a new preset (spec §9).
    transaction_pulses: u32,
}

impl State {
    /// A dispenser whose totalisers read `start`'s: on a cold start the start
    /// state's cumulative reading (sim-impl.md, "Start state"), on a warm start
    /// the checkpoint's.
    fn new(start: DispenserState) -> Self {
        Self {
            preset_x10: 0,
            enabled: false,
            cumulative_pulses: start.cumulative_pulses,
            transaction_pulses: start.transaction_pulses,
        }
    }

    fn control(&self) -> DispenserControlState {
        DispenserControlState {
            solenoid_open: self.enabled,
            pump_energised: self.enabled,
        }
    }

    /// Status bitfield (spec §7): only bit 0 (Running) is meaningful.
    fn status(&self) -> u16 {
        u16::from(self.enabled)
    }

    /// The 16-bit value at a mapped address, or `None` if unmapped.
    fn register(&self, address: u16) -> Option<u16> {
        match address {
            PRESET_REGISTER => Some(self.preset_x10),
            ENABLE_REGISTER => Some(u16::from(self.enabled)),
            CUMULATIVE_HI_REGISTER => Some((self.cumulative_pulses >> 16) as u16),
            CUMULATIVE_LO_REGISTER => Some((self.cumulative_pulses & 0xFFFF) as u16),
            TRANSACTION_HI_REGISTER => Some((self.transaction_pulses >> 16) as u16),
            TRANSACTION_LO_REGISTER => Some((self.transaction_pulses & 0xFFFF) as u16),
            STATUS_REGISTER => Some(self.status()),
            _ => None,
        }
    }

    /// Read `quantity` consecutive holding registers from `start`. The whole
    /// range must be mapped (spec §5).
    fn read(&self, start: u16, quantity: u16) -> Result<Vec<u16>, Exception> {
        if !(1..=MAX_READ_QUANTITY).contains(&quantity) {
            return Err(Exception::IllegalDataValue);
        }
        let mut values = Vec::with_capacity(quantity as usize);
        for offset in 0..quantity {
            let address = start
                .checked_add(offset)
                .ok_or(Exception::IllegalDataAddress)?;
            values.push(
                self.register(address)
                    .ok_or(Exception::IllegalDataAddress)?,
            );
        }
        Ok(values)
    }

    /// Write a single register. Only the preset and enable registers are
    /// writable; everything else is Illegal Data Address (spec §5). Returns
    /// whether the control state (solenoid/pump) changed.
    fn write(&mut self, address: u16, value: u16) -> Result<bool, Exception> {
        match address {
            PRESET_REGISTER => {
                self.preset_x10 = value;
                self.transaction_pulses = 0;
                Ok(false)
            }
            ENABLE_REGISTER => {
                let enable = value != 0;
                let changed = enable != self.enabled;
                self.enabled = enable;
                Ok(changed)
            }
            _ => Err(Exception::IllegalDataAddress),
        }
    }

    /// Account for one received pulse. Increments both totalisers and, while the
    /// pump is enabled, auto-stops on reaching the preset target. Returns whether
    /// the control state changed (i.e. an auto-stop fired).
    fn on_pulse(&mut self, k_factor: f64) -> bool {
        self.cumulative_pulses = self.cumulative_pulses.saturating_add(1);
        self.transaction_pulses = self.transaction_pulses.saturating_add(1);
        if self.enabled {
            let target = auto_stop_target(self.preset_x10, k_factor);
            if target > 0 && self.transaction_pulses >= target {
                self.enabled = false;
                return true;
            }
        }
        false
    }
}

/// Auto-stop pulse target = preset litres × pulses-per-litre (spec §9).
fn auto_stop_target(preset_x10: u16, k_factor: f64) -> u32 {
    let litres = f64::from(preset_x10) / 10.0;
    (litres * k_factor).round() as u32
}

/// Build an exception response PDU: `(function | 0x80)` then the code.
fn exception_pdu(function: u8, exception: Exception) -> Vec<u8> {
    vec![function | EXCEPTION_FLAG, exception as u8]
}

/// Dispatch a request PDU, returning the response PDU and whether the control
/// state changed (so the caller can publish it).
fn apply_request(pdu: &[u8], state: &mut State) -> (Vec<u8>, bool) {
    match pdu.first().copied() {
        Some(READ_HOLDING_REGISTERS) => (read_response(pdu, state), false),
        Some(WRITE_SINGLE_REGISTER) => write_response(pdu, state),
        Some(function) => (exception_pdu(function, Exception::IllegalFunction), false),
        None => (exception_pdu(0, Exception::IllegalFunction), false),
    }
}

/// Read Holding Registers (`0x03`): `start(2) quantity(2)`.
fn read_response(pdu: &[u8], state: &State) -> Vec<u8> {
    if pdu.len() != 5 {
        return exception_pdu(READ_HOLDING_REGISTERS, Exception::IllegalDataValue);
    }
    let start = u16::from_be_bytes([pdu[1], pdu[2]]);
    let quantity = u16::from_be_bytes([pdu[3], pdu[4]]);
    match state.read(start, quantity) {
        Ok(values) => {
            let mut response = Vec::with_capacity(2 + values.len() * 2);
            response.push(READ_HOLDING_REGISTERS);
            response.push((values.len() * 2) as u8);
            for value in values {
                response.extend_from_slice(&value.to_be_bytes());
            }
            response
        }
        Err(exception) => exception_pdu(READ_HOLDING_REGISTERS, exception),
    }
}

/// Write Single Register (`0x06`): `address(2) value(2)`; the response echoes
/// the request on success.
fn write_response(pdu: &[u8], state: &mut State) -> (Vec<u8>, bool) {
    if pdu.len() != 5 {
        return (
            exception_pdu(WRITE_SINGLE_REGISTER, Exception::IllegalDataValue),
            false,
        );
    }
    let address = u16::from_be_bytes([pdu[1], pdu[2]]);
    let value = u16::from_be_bytes([pdu[3], pdu[4]]);
    match state.write(address, value) {
        Ok(control_changed) => (pdu.to_vec(), control_changed),
        Err(exception) => (exception_pdu(WRITE_SINGLE_REGISTER, exception), false),
    }
}

/// Frame a response PDU into a complete MBAP message, echoing the transaction
/// and unit identifiers.
fn encode_response(transaction_id: [u8; 2], unit_id: u8, pdu: &[u8]) -> Vec<u8> {
    let length = (pdu.len() + 1) as u16; // unit identifier + PDU
    let mut message = Vec::with_capacity(7 + pdu.len());
    message.extend_from_slice(&transaction_id);
    message.extend_from_slice(&PROTOCOL_ID.to_be_bytes());
    message.extend_from_slice(&length.to_be_bytes());
    message.push(unit_id);
    message.extend_from_slice(pdu);
    message
}

pub(super) fn spawn(
    clock: SimClock,
    start: DispenserState,
    state_rx: mpsc::Receiver<oneshot::Sender<DispenserState>>,
    pulse_rx: mpsc::Receiver<Pulse>,
    ctrl_tx: watch::Sender<DispenserControlState>,
    listen_addr: SocketAddr,
) -> tokio::task::JoinHandle<()> {
    // Bind synchronously so callers can connect as soon as `spawn` returns.
    let std_listener =
        std::net::TcpListener::bind(listen_addr).expect("dispenser: bind TCP listener");
    std_listener
        .set_nonblocking(true)
        .expect("dispenser: set_nonblocking");
    let listener =
        TcpListener::from_std(std_listener).expect("dispenser: adopt std listener into Tokio");

    let state = Arc::new(Mutex::new(State::new(start)));

    // Three long-lived sibling tasks — the pulse accumulator, the Modbus
    // server and the state server — supervised so a panic in any brings the
    // sim down.
    tokio::spawn(async move {
        let mut tasks: JoinSet<()> = JoinSet::new();
        tasks.spawn(pulse_accumulator(
            pulse_rx,
            Arc::clone(&state),
            ctrl_tx.clone(),
        ));
        tasks.spawn(state_server(state_rx, Arc::clone(&state)));
        tasks.spawn(server(clock, listener, state, ctrl_tx));
        supervise(tasks, "dispenser").await;
    })
}

/// Drive the totalisers and the preset auto-stop from the flow-meter pulses.
async fn pulse_accumulator(
    mut pulse_rx: mpsc::Receiver<Pulse>,
    state: Arc<Mutex<State>>,
    ctrl_tx: watch::Sender<DispenserControlState>,
) {
    loop {
        let pulse = pulse_rx
            .recv()
            .await
            .expect("dispenser: flow meter pulse channel closed");
        let control_change = {
            let mut state = state.lock().expect("dispenser: state mutex poisoned");
            if state.on_pulse(pulse.k_factor) {
                Some(state.control())
            } else {
                None
            }
        };
        if let Some(control) = control_change {
            ctrl_tx
                .send(control)
                .expect("dispenser: fuel tank control channel closed");
        }
    }
}

/// Answer the orchestrator's state requests with the totalisers.
async fn state_server(
    mut state_rx: mpsc::Receiver<oneshot::Sender<DispenserState>>,
    state: Arc<Mutex<State>>,
) {
    loop {
        let reply = state_rx
            .recv()
            .await
            .expect("dispenser: orchestrator state channel closed");
        let totalisers = {
            let state = state.lock().expect("dispenser: state mutex poisoned");
            DispenserState {
                cumulative_pulses: state.cumulative_pulses,
                transaction_pulses: state.transaction_pulses,
            }
        };
        reply
            .send(totalisers)
            .expect("dispenser: orchestrator dropped the state reply");
    }
}

/// Modbus TCP server: one supervised task per connection, all sharing `state`.
async fn server(
    clock: SimClock,
    listener: TcpListener,
    state: Arc<Mutex<State>>,
    ctrl_tx: watch::Sender<DispenserControlState>,
) {
    let mut connections: JoinSet<()> = JoinSet::new();
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                if let Ok((stream, _addr)) = accepted {
                    connections.spawn(handle_connection(
                        stream,
                        clock.clone(),
                        Arc::clone(&state),
                        ctrl_tx.clone(),
                    ));
                }
                // A transient accept error is ignored — a real appliance keeps
                // serving.
            }
            Some(joined) = connections.join_next(), if !connections.is_empty() => {
                propagate_connection_panic(joined);
            }
        }
    }
}

/// Serve one persistent Modbus TCP connection until it closes.
async fn handle_connection(
    mut stream: TcpStream,
    clock: SimClock,
    state: Arc<Mutex<State>>,
    ctrl_tx: watch::Sender<DispenserControlState>,
) {
    let mut header = [0u8; 7];
    loop {
        // Idle between requests: await the next MBAP header. No I/O guard — while
        // merely waiting for the next request, simulated time should advance.
        if stream.read_exact(&mut header).await.is_err() {
            return; // connection closed
        }

        // A request has begun. Guard the exchange (read the PDU, apply it, write
        // the response) so simulated time does not advance while the client
        // awaits its reply. The guard is the sim *server's* — the FMS client may
        // be a mocked component, which is not permitted to guard.
        let _io = clock.io_guard();

        // MBAP length counts the unit identifier (already read) plus the PDU.
        let length = usize::from(u16::from_be_bytes([header[4], header[5]]));
        if length < 2 {
            return; // no PDU — malformed framing
        }
        let mut pdu = vec![0u8; length - 1];
        if stream.read_exact(&mut pdu).await.is_err() {
            return;
        }

        let (response_pdu, control) = {
            let mut state = state.lock().expect("dispenser: state mutex poisoned");
            let (response_pdu, changed) = apply_request(&pdu, &mut state);
            let control = if changed { Some(state.control()) } else { None };
            (response_pdu, control)
        };
        // Publish before replying so an observer of the control channel sees the
        // new state no later than the client sees the response.
        if let Some(control) = control {
            ctrl_tx
                .send(control)
                .expect("dispenser: fuel tank control channel closed");
        }

        let transaction_id = [header[0], header[1]];
        let unit_id = header[6];
        let message = encode_response(transaction_id, unit_id, &response_pdu);
        if stream.write_all(&message).await.is_err() {
            return;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auto_stop_target_is_preset_litres_times_k_factor() {
        assert_eq!(auto_stop_target(1000, 10.0), 1000); // 100 L × 10 pulses/L
        assert_eq!(auto_stop_target(5000, 100.0), 50_000); // 500 L × 100 pulses/L
        assert_eq!(auto_stop_target(0, 10.0), 0);
    }

    #[test]
    fn read_single_mapped_register() {
        let mut state = State::new(DispenserState::default());
        state.preset_x10 = 1500;
        assert_eq!(state.read(PRESET_REGISTER, 1), Ok(vec![1500]));
    }

    #[test]
    fn read_whole_mapped_range_in_one_request() {
        let state = State::new(DispenserState::default());
        assert_eq!(state.read(0x0001, 7).map(|v| v.len()), Ok(7));
    }

    #[test]
    fn read_unmapped_base_address_is_illegal_data_address() {
        let state = State::new(DispenserState::default());
        assert_eq!(state.read(0x0000, 1), Err(Exception::IllegalDataAddress));
    }

    #[test]
    fn read_range_spilling_past_map_is_illegal_data_address() {
        let state = State::new(DispenserState::default());
        // 0x0007 is the last mapped register; quantity 2 spills into 0x0008.
        assert_eq!(state.read(0x0007, 2), Err(Exception::IllegalDataAddress));
    }

    #[test]
    fn read_quantity_out_of_range_is_illegal_data_value() {
        let state = State::new(DispenserState::default());
        assert_eq!(state.read(0x0001, 0), Err(Exception::IllegalDataValue));
        assert_eq!(state.read(0x0001, 126), Err(Exception::IllegalDataValue));
    }

    #[test]
    fn totaliser_is_encoded_high_word_first() {
        let mut state = State::new(DispenserState::default());
        state.cumulative_pulses = 70_000; // 0x0001_1170
        assert_eq!(state.register(CUMULATIVE_HI_REGISTER), Some(0x0001));
        assert_eq!(state.register(CUMULATIVE_LO_REGISTER), Some(0x1170));
    }

    #[test]
    fn write_preset_sets_value_and_resets_transaction() {
        let mut state = State::new(DispenserState::default());
        state.transaction_pulses = 200;
        assert_eq!(state.write(PRESET_REGISTER, 1500), Ok(false));
        assert_eq!(state.preset_x10, 1500);
        assert_eq!(state.transaction_pulses, 0);
    }

    #[test]
    fn write_enable_toggles_and_reports_change() {
        let mut state = State::new(DispenserState::default());
        assert_eq!(state.write(ENABLE_REGISTER, 1), Ok(true));
        assert!(state.enabled);
        // Re-writing the same value is not a control change.
        assert_eq!(state.write(ENABLE_REGISTER, 1), Ok(false));
        assert_eq!(state.write(ENABLE_REGISTER, 0), Ok(true));
        assert!(!state.enabled);
    }

    #[test]
    fn write_read_only_or_unmapped_is_illegal_data_address() {
        let mut state = State::new(DispenserState::default());
        assert_eq!(
            state.write(CUMULATIVE_HI_REGISTER, 1),
            Err(Exception::IllegalDataAddress)
        );
        assert_eq!(state.write(0x0000, 1), Err(Exception::IllegalDataAddress));
        assert_eq!(state.write(0x0008, 1), Err(Exception::IllegalDataAddress));
    }

    #[test]
    fn pulses_accumulate_both_totalisers() {
        let mut state = State::new(DispenserState::default());
        state.enabled = true;
        state.preset_x10 = 1000;
        for _ in 0..50 {
            assert!(!state.on_pulse(10.0));
        }
        assert_eq!(state.cumulative_pulses, 50);
        assert_eq!(state.transaction_pulses, 50);
    }

    #[test]
    fn on_pulse_auto_stops_exactly_at_target() {
        let mut state = State::new(DispenserState::default());
        state.enabled = true;
        state.preset_x10 = 1000; // target 1000 pulses
        for _ in 0..999 {
            assert!(!state.on_pulse(10.0));
        }
        assert!(state.on_pulse(10.0)); // the 1000th pulse fires auto-stop
        assert!(!state.enabled);
        assert_eq!(state.transaction_pulses, 1000);
    }

    #[test]
    fn cumulative_survives_transaction_reset() {
        let mut state = State::new(DispenserState::default());
        state.enabled = true;
        state.preset_x10 = 1000;
        for _ in 0..200 {
            state.on_pulse(10.0);
        }
        state
            .write(PRESET_REGISTER, 2000)
            .expect("preset is a writable register");
        assert_eq!(state.transaction_pulses, 0);
        assert_eq!(state.cumulative_pulses, 200);
    }

    #[test]
    fn apply_request_reads_holding_registers() {
        let mut state = State::new(DispenserState::default());
        state.preset_x10 = 1500; // 0x05DC
        let (pdu, changed) = apply_request(&[0x03, 0x00, 0x01, 0x00, 0x01], &mut state);
        assert!(!changed);
        assert_eq!(pdu, vec![0x03, 0x02, 0x05, 0xDC]);
    }

    #[test]
    fn apply_request_echoes_write_single_register() {
        let mut state = State::new(DispenserState::default());
        let request = [0x06, 0x00, 0x02, 0x00, 0x01];
        let (pdu, changed) = apply_request(&request, &mut state);
        assert!(changed);
        assert_eq!(pdu, request.to_vec());
    }

    #[test]
    fn apply_request_rejects_unknown_function() {
        let mut state = State::new(DispenserState::default());
        let (pdu, changed) = apply_request(&[0x05, 0x00, 0x00, 0xFF, 0x00], &mut state);
        assert!(!changed);
        assert_eq!(pdu, vec![0x85, 0x01]); // 0x05 | 0x80, IllegalFunction
    }

    #[test]
    fn exception_pdu_sets_high_bit_and_code() {
        assert_eq!(
            exception_pdu(0x03, Exception::IllegalDataAddress),
            vec![0x83, 0x02]
        );
    }

    #[test]
    fn encode_response_builds_mbap_header() {
        let message = encode_response([0xBE, 0xEF], 0x07, &[0x03, 0x02, 0x00, 0x00]);
        assert_eq!(&message[0..2], &[0xBE, 0xEF]); // transaction id echoed
        assert_eq!(&message[2..4], &[0x00, 0x00]); // protocol id
        assert_eq!(&message[4..6], &[0x00, 0x05]); // length = unit + 4-byte PDU
        assert_eq!(message[6], 0x07); // unit id echoed
        assert_eq!(&message[7..], &[0x03, 0x02, 0x00, 0x00]);
    }
}
