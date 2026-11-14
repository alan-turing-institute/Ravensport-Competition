//! The clipboard read (sim-impl.md, "Reading the clipboard"): the backend that
//! records what the workstation's clipboard channel announces and sends, the
//! guard that bounds a message before IronRDP reassembles it, and the read that
//! takes the backend's record to the decoded text or a typed failure.
//!
//! Nothing here acts on the channel directly: the read sends its request and
//! waits through a [`ClipboardChannel`], which the session implements, and the
//! session loop offers each chunk header to the guard.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use ironrdp_cliprdr::backend::CliprdrBackend;
use ironrdp_cliprdr::pdu::{
    ClipboardFormat, ClipboardFormatId, ClipboardFormatName, ClipboardGeneralCapabilityFlags,
    FileContentsRequest, FileContentsResponse, FormatDataRequest, FormatDataResponse, LockDataId,
};
use ironrdp_core::impl_as_any;

use super::{AuditFailure, ClipboardFailure, RetrievalFailure};
use crate::clock::SimClock;

/// The largest selection the workstation's clipboard bridge delivers, in UTF-8
/// bytes: measured, and recorded in `recon.md` §11.2, with which it must be
/// changed.
pub(super) const CLIPBOARD_CEILING_BYTES: usize = 1_048_575;

/// Unicode text, the one format a read requests.
pub(super) const CF_UNICODETEXT: u32 = 13;

/// The clipboard message header that precedes the format data.
const CLIPRDR_HEADER_BYTES: usize = 8;

/// The largest clipboard message a selection of `ceiling` UTF-8 bytes produces:
/// each byte becomes at most two bytes of UTF-16, the most for ASCII, plus the
/// terminating null unit and the message header.
pub(super) fn channel_bound(ceiling: usize) -> usize {
    CLIPRDR_HEADER_BYTES + 2 * ceiling + 2
}

/// The header of one chunk of the clipboard channel's data: the total length
/// of the message it belongs to, whether it begins or ends that message, and
/// how many bytes of it this chunk carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct ChunkHeader {
    pub(super) declared: u32,
    pub(super) first: bool,
    pub(super) last: bool,
    pub(super) payload: usize,
}

/// Bounds each clipboard message by its chunk headers, before IronRDP
/// reassembles it into a buffer of no limit.
#[derive(Debug)]
pub(super) struct ReassemblyGuard {
    bound: usize,
    /// The declared length of the message in progress.
    declared: usize,
    /// The bytes of it accepted so far.
    accumulated: usize,
}

impl ReassemblyGuard {
    pub(super) fn new(bound: usize) -> ReassemblyGuard {
        ReassemblyGuard {
            bound,
            declared: 0,
            accumulated: 0,
        }
    }

    /// Accept a chunk, or refuse it: a message declaring more than the bound,
    /// before any of it is accumulated; one whose chunks carry more than it
    /// declared, as soon as they do.
    pub(super) fn accept(&mut self, chunk: ChunkHeader) -> Result<(), ClipboardFailure> {
        if chunk.first {
            let declared = chunk.declared as usize;
            if declared > self.bound {
                return Err(ClipboardFailure::DeclaredLengthOverCeiling { declared });
            }
            self.declared = declared;
            self.accumulated = 0;
        }
        let received = self.accumulated + chunk.payload;
        if received > self.declared {
            return Err(ClipboardFailure::ExceedsDeclaredLength {
                declared: self.declared,
                received,
            });
        }
        self.accumulated = received;
        Ok(())
    }

    #[cfg(test)]
    pub(super) fn accumulated(&self) -> usize {
        self.accumulated
    }
}

/// Decode Unicode text as the channel carries it: UTF-16, little-endian, up to
/// the terminating null unit, line endings as they arrived.
pub(super) fn decode_unicode_text(bytes: &[u8]) -> Result<String, AuditFailure> {
    let malformed = AuditFailure::Retrieval(RetrievalFailure::MalformedResponse);
    let (pairs, remainder) = bytes.as_chunks::<2>();
    if !remainder.is_empty() {
        return Err(malformed);
    }
    let units: Vec<u16> = pairs
        .iter()
        .map(|pair| u16::from_le_bytes(*pair))
        .take_while(|&unit| unit != 0)
        .collect();
    String::from_utf16(&units).map_err(|_| malformed)
}

/// What the workstation's clipboard channel did, in order, since the latch was
/// last cleared.
#[derive(Debug, Clone)]
enum ChannelEvent {
    /// A copy, announcing the formats the selection is available in.
    Announced(Vec<ClipboardFormat>),
    /// The format data requested.
    Data(Vec<u8>),
    /// The workstation's failure response in place of the data.
    Failed,
}

#[derive(Debug, Default)]
struct Record {
    /// The channel finished its setup and can carry a copy.
    ready: bool,
    /// The workstation asked for this client's formats, during setup.
    format_list_requested: bool,
    /// Incremented by each clear, so a read made before it sees nothing after.
    latch: u64,
    events: Vec<ChannelEvent>,
}

/// The record the backend writes and a read takes, shared between them.
#[derive(Debug, Clone, Default)]
pub(super) struct Clipboard {
    record: Arc<Mutex<Record>>,
}

impl Clipboard {
    /// The IronRDP clipboard backend that writes this record.
    pub(super) fn backend(&self) -> ClipboardBackend {
        ClipboardBackend {
            clipboard: self.clone(),
        }
    }

    /// Clear the latch and begin a read. The command that causes the copy is
    /// typed after this, so its announcement follows the clear.
    pub(super) fn clear(&self) -> ClipboardRead {
        let mut record = self.record.lock().unwrap();
        record.latch += 1;
        record.events.clear();
        ClipboardRead {
            clipboard: self.clone(),
            latch: record.latch,
            next: 0,
            phase: Phase::AwaitingAnnouncement,
        }
    }

    /// Whether the channel has finished its setup, and whether the workstation
    /// has asked for this client's formats.
    pub(super) fn channel_state(&self) -> (bool, bool) {
        let record = self.record.lock().unwrap();
        (record.ready, record.format_list_requested)
    }

    fn push(&self, event: ChannelEvent) {
        self.record.lock().unwrap().events.push(event);
    }
}

/// What a read needs next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum ReadStep {
    /// Nothing yet: wait for the channel.
    Wait,
    /// Request the selection in this format.
    Request(u32),
    /// The read is complete.
    Done(Result<String, AuditFailure>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Phase {
    AwaitingAnnouncement,
    AwaitingData,
    Done(Result<String, AuditFailure>),
}

/// One read of the selection, from a cleared latch (sim-impl.md, "Reading the
/// clipboard").
#[derive(Debug)]
pub(super) struct ClipboardRead {
    clipboard: Clipboard,
    /// The clear this read began from.
    latch: u64,
    /// The first event this read has not yet taken.
    next: usize,
    phase: Phase,
}

impl ClipboardRead {
    /// Take the events recorded since the last step, in order.
    pub(super) fn step(&mut self) -> ReadStep {
        let events: Vec<ChannelEvent> = {
            let record = self.clipboard.record.lock().unwrap();
            if record.latch != self.latch {
                Vec::new()
            } else {
                let fresh = record.events[self.next..].to_vec();
                self.next = record.events.len();
                fresh
            }
        };
        let mut request = None;
        for event in events {
            match (&self.phase, event) {
                (Phase::Done(_), _) => break,
                (Phase::AwaitingAnnouncement, ChannelEvent::Announced(formats)) => {
                    if accepts(&formats) {
                        self.phase = Phase::AwaitingData;
                        request = Some(CF_UNICODETEXT);
                    } else {
                        self.phase = Phase::Done(Err(AuditFailure::Clipboard(
                            ClipboardFailure::NonTextFormat,
                        )));
                    }
                }
                // Data before this read's request is content from before it.
                (Phase::AwaitingAnnouncement, _) => {
                    self.phase = Phase::Done(Err(AuditFailure::Clipboard(
                        ClipboardFailure::StaleSelection,
                    )));
                }
                (Phase::AwaitingData, ChannelEvent::Data(bytes)) => {
                    self.phase = Phase::Done(decode_unicode_text(&bytes));
                }
                (Phase::AwaitingData, ChannelEvent::Failed) => {
                    self.phase = Phase::Done(Err(AuditFailure::Clipboard(
                        ClipboardFailure::ResponseFailed,
                    )));
                }
                // A later copy while awaiting the data requested: the response
                // to the request already made is still the one awaited.
                (Phase::AwaitingData, ChannelEvent::Announced(_)) => {}
            }
        }
        match &self.phase {
            Phase::Done(result) => ReadStep::Done(result.clone()),
            _ => request.map_or(ReadStep::Wait, ReadStep::Request),
        }
    }

    /// Run the read to completion through `channel`, within `deadline` on the
    /// sim clock from the start of the read: send each request the read makes,
    /// and otherwise wait for the workstation's next message. A failure of the
    /// channel ends the read with that failure.
    pub(super) async fn complete(
        mut self,
        clock: &SimClock,
        deadline: Duration,
        channel: &mut impl ClipboardChannel,
    ) -> Result<String, AuditFailure> {
        let expiry = clock.sleep(deadline);
        tokio::pin!(expiry);
        loop {
            match self.step() {
                ReadStep::Wait => {}
                ReadStep::Request(format) => {
                    channel.request(format).await?;
                    continue;
                }
                ReadStep::Done(result) => return result,
            }
            tokio::select! {
                () = &mut expiry => {
                    return Err(AuditFailure::Clipboard(ClipboardFailure::DeadlineReached));
                }
                waited = channel.wait() => waited?,
            }
        }
    }
}

/// What a read needs from the session it reads through: to send its request
/// for the selection, and to wait for the workstation's next message, which
/// the backend records. The IronRDP session implements it, and so does the
/// scripted channel the tests drive.
#[allow(async_fn_in_trait)]
pub(super) trait ClipboardChannel {
    /// Request the selection in `format`.
    async fn request(&mut self, format: u32) -> Result<(), AuditFailure>;
    /// Wait for the workstation's next message.
    async fn wait(&mut self) -> Result<(), AuditFailure>;
}

/// Whether a read accepts a selection offered in `formats`: Unicode text must
/// be among them, and no file-descriptor format, whatever else is offered.
fn accepts(formats: &[ClipboardFormat]) -> bool {
    let file_descriptor = |format: &ClipboardFormat| {
        format.id == ClipboardFormatId::CF_HDROP
            || format.name.as_ref() == Some(&ClipboardFormatName::FILE_LIST)
    };
    formats
        .iter()
        .any(|format| format.id == ClipboardFormatId::CF_UNICODETEXT)
        && !formats.iter().any(file_descriptor)
}

/// The IronRDP clipboard backend: records each callback into the shared
/// [`Clipboard`] and never acts on the channel itself.
#[derive(Debug)]
pub(super) struct ClipboardBackend {
    clipboard: Clipboard,
}

impl_as_any!(ClipboardBackend);

impl CliprdrBackend for ClipboardBackend {
    /// Used only for file transfer, which this client does not advertise.
    fn temporary_directory(&self) -> &str {
        ""
    }

    /// No file-transfer capability: the workstation must not be able to offer
    /// a write onto the sim's filesystem (`recon.md` §11.1).
    fn client_capabilities(&self) -> ClipboardGeneralCapabilityFlags {
        ClipboardGeneralCapabilityFlags::empty()
    }

    fn on_ready(&mut self) {
        self.clipboard.record.lock().unwrap().ready = true;
    }

    fn on_request_format_list(&mut self) {
        self.clipboard.record.lock().unwrap().format_list_requested = true;
    }

    fn on_process_negotiated_capabilities(
        &mut self,
        _capabilities: ClipboardGeneralCapabilityFlags,
    ) {
    }

    fn on_remote_copy(&mut self, available_formats: &[ClipboardFormat]) {
        self.clipboard
            .push(ChannelEvent::Announced(available_formats.to_vec()));
    }

    /// The workstation asking for this client's selection: it holds none.
    fn on_format_data_request(&mut self, _request: FormatDataRequest) {}

    fn on_format_data_response(&mut self, response: FormatDataResponse<'_>) {
        if response.is_error() {
            self.clipboard.push(ChannelEvent::Failed);
        } else {
            self.clipboard
                .push(ChannelEvent::Data(response.data().to_vec()));
        }
    }

    fn on_file_contents_request(&mut self, _request: FileContentsRequest) {}

    fn on_file_contents_response(&mut self, _response: FileContentsResponse<'_>) {}

    fn on_lock(&mut self, _data_id: LockDataId) {}

    fn on_unlock(&mut self, _data_id: LockDataId) {}
}

/// The clipboard read, tested without a session (sim-tests.md, "Clipboard"):
/// each test plays the workstation by calling the backend's callbacks, by
/// offering chunk headers to the guard, or through a scripted channel.
#[cfg(test)]
mod tests {
    use std::collections::VecDeque;
    use std::time::Duration;

    use ironrdp_cliprdr::backend::CliprdrBackend;
    use ironrdp_cliprdr::pdu::{
        ClipboardFormat, ClipboardFormatId, ClipboardFormatName, ClipboardGeneralCapabilityFlags,
        FormatDataResponse,
    };
    use time::macros::datetime;

    use super::*;
    use crate::actors::reconciliation_user::internals::TerminalFailure;
    use crate::clock::{SimClock, TimeModel};

    /// A ceiling small enough to state its bound by hand: 8 + 2 × 10 + 2.
    const SMALL_CEILING: usize = 10;

    const DEADLINE: Duration = Duration::from_secs(60);

    /// The formats the workstation's service offers for a text selection,
    /// as measured: Unicode text, locale, text and OEM text.
    fn workstation_formats() -> Vec<ClipboardFormat> {
        [13, 16, 1, 7]
            .into_iter()
            .map(|id| ClipboardFormat::new(ClipboardFormatId::new(id)))
            .collect()
    }

    fn file_list() -> ClipboardFormat {
        ClipboardFormat::new(ClipboardFormatId::new(49_158))
            .with_name(ClipboardFormatName::FILE_LIST)
    }

    /// Text as the channel carries it: UTF-16, little-endian, with a
    /// terminating null unit.
    fn utf16(text: &str) -> Vec<u8> {
        let mut bytes: Vec<u8> = text.encode_utf16().flat_map(u16::to_le_bytes).collect();
        bytes.extend_from_slice(&[0, 0]);
        bytes
    }

    fn first(declared: u32, payload: usize) -> ChunkHeader {
        ChunkHeader {
            declared,
            first: true,
            last: false,
            payload,
        }
    }

    fn following(declared: u32, payload: usize, last: bool) -> ChunkHeader {
        ChunkHeader {
            declared,
            first: false,
            last,
            payload,
        }
    }

    // --- The bound -----------------------------------------------------------

    #[test]
    fn the_channel_bound_follows_from_the_ceiling() {
        assert_eq!(channel_bound(1_048_575), 2_097_160);
        assert_eq!(channel_bound(SMALL_CEILING), 30);
        // The deployed ceiling is the figure recon.md §11.2 records.
        assert_eq!(CLIPBOARD_CEILING_BYTES, 1_048_575);
    }

    #[test]
    fn a_declared_length_over_the_bound_is_refused_before_accumulation() {
        let mut guard = ReassemblyGuard::new(channel_bound(SMALL_CEILING));
        assert_eq!(
            guard.accept(first(31, 4)),
            Err(ClipboardFailure::DeclaredLengthOverCeiling { declared: 31 })
        );
        assert_eq!(guard.accumulated(), 0);

        let mut guard = ReassemblyGuard::new(channel_bound(SMALL_CEILING));
        assert_eq!(guard.accept(first(30, 4)), Ok(()));
    }

    #[test]
    fn chunks_carrying_more_than_their_declaration_are_refused() {
        let mut guard = ReassemblyGuard::new(channel_bound(SMALL_CEILING));
        assert_eq!(guard.accept(first(20, 12)), Ok(()));
        assert_eq!(
            guard.accept(following(20, 9, true)),
            Err(ClipboardFailure::ExceedsDeclaredLength {
                declared: 20,
                received: 21
            })
        );
    }

    // --- The latch -------------------------------------------------------------

    #[test]
    fn only_an_announcement_after_the_latch_is_cleared_is_fresh() {
        let clipboard = Clipboard::default();
        let mut backend = clipboard.backend();
        backend.on_remote_copy(&workstation_formats());

        let mut read = clipboard.clear();
        assert_eq!(
            read.step(),
            ReadStep::Wait,
            "the earlier announcement was cleared"
        );

        backend.on_remote_copy(&workstation_formats());
        assert_eq!(read.step(), ReadStep::Request(CF_UNICODETEXT));
    }

    #[test]
    fn data_without_a_request_in_this_read_is_refused() {
        let clipboard = Clipboard::default();
        let mut backend = clipboard.backend();
        let mut read = clipboard.clear();
        backend.on_format_data_response(FormatDataResponse::new_data(utf16("earlier")));
        backend.on_remote_copy(&workstation_formats());
        assert_eq!(
            read.step(),
            ReadStep::Done(Err(AuditFailure::Clipboard(
                ClipboardFailure::StaleSelection
            )))
        );
    }

    // --- Formats ---------------------------------------------------------------

    #[test]
    fn the_client_advertises_no_file_transfer() {
        let capabilities = Clipboard::default().backend().client_capabilities();
        for file_transfer in [
            ClipboardGeneralCapabilityFlags::STREAM_FILECLIP_ENABLED,
            ClipboardGeneralCapabilityFlags::FILECLIP_NO_FILE_PATHS,
            ClipboardGeneralCapabilityFlags::HUGE_FILE_SUPPORT_ENABLED,
        ] {
            assert!(!capabilities.contains(file_transfer), "{capabilities:?}");
        }
    }

    #[test]
    fn a_read_requests_unicode_text_only_after_a_fresh_announcement() {
        let clipboard = Clipboard::default();
        let mut backend = clipboard.backend();
        let mut read = clipboard.clear();
        assert_eq!(read.step(), ReadStep::Wait);

        backend.on_remote_copy(&workstation_formats());
        assert_eq!(read.step(), ReadStep::Request(CF_UNICODETEXT));
        assert_eq!(
            read.step(),
            ReadStep::Wait,
            "requested once, now awaiting the data"
        );
    }

    #[test]
    fn a_file_descriptor_format_is_refused_even_beside_text() {
        let refused = ReadStep::Done(Err(AuditFailure::Clipboard(
            ClipboardFailure::NonTextFormat,
        )));
        let hdrop = ClipboardFormat::new(ClipboardFormatId::CF_HDROP);
        let unicode = ClipboardFormat::new(ClipboardFormatId::CF_UNICODETEXT);
        let text_only = workstation_formats()
            .into_iter()
            .filter(|format| format.id != ClipboardFormatId::CF_UNICODETEXT)
            .collect::<Vec<_>>();

        for offered in [
            vec![unicode.clone(), file_list()],
            vec![unicode, hdrop],
            text_only,
        ] {
            let clipboard = Clipboard::default();
            let mut backend = clipboard.backend();
            let mut read = clipboard.clear();
            backend.on_remote_copy(&offered);
            assert_eq!(read.step(), refused, "{offered:?}");
        }
    }

    // --- Decoding and failure ----------------------------------------------------

    #[test]
    fn unicode_text_decodes_with_its_line_endings_unchanged() {
        assert_eq!(
            decode_unicode_text(&utf16("a\nb\r\nc")),
            Ok("a\nb\r\nc".to_string())
        );

        let clipboard = Clipboard::default();
        let mut backend = clipboard.backend();
        let mut read = clipboard.clear();
        backend.on_remote_copy(&workstation_formats());
        assert_eq!(read.step(), ReadStep::Request(CF_UNICODETEXT));
        backend.on_format_data_response(FormatDataResponse::new_data(utf16("{\"a\":1}\n")));
        assert_eq!(read.step(), ReadStep::Done(Ok("{\"a\":1}\n".to_string())));
    }

    #[test]
    fn malformed_unicode_text_is_a_malformed_response() {
        let malformed = Err(AuditFailure::Retrieval(RetrievalFailure::MalformedResponse));
        // An odd number of bytes.
        assert_eq!(decode_unicode_text(&[0x61, 0x00, 0x62]), malformed);
        // An unpaired high surrogate, then the terminating null unit.
        assert_eq!(decode_unicode_text(&[0x00, 0xd8, 0x00, 0x00]), malformed);
    }

    #[test]
    fn the_workstations_failure_response_is_a_typed_failure() {
        let clipboard = Clipboard::default();
        let mut backend = clipboard.backend();
        let mut read = clipboard.clear();
        backend.on_remote_copy(&workstation_formats());
        assert_eq!(read.step(), ReadStep::Request(CF_UNICODETEXT));
        backend.on_format_data_response(FormatDataResponse::new_error());
        assert_eq!(
            read.step(),
            ReadStep::Done(Err(AuditFailure::Clipboard(
                ClipboardFailure::ResponseFailed
            )))
        );
    }

    // --- The read's loop ------------------------------------------------------------

    /// What one wait on the scripted channel brings.
    enum Next {
        /// The workstation announces a copy in these formats.
        Announced(Vec<ClipboardFormat>),
        /// The workstation sends the data requested.
        Data(Vec<u8>),
        /// The wait itself fails.
        Fails(AuditFailure),
    }

    /// Plays the workstation for a read. Each wait delivers the next scripted
    /// message to the backend after its delay on the sim clock, as one frame
    /// does in the session; once the script is spent, a wait never ends. Each
    /// request is recorded, and refused if the channel is scripted to refuse.
    struct ScriptedChannel {
        clock: SimClock,
        backend: ClipboardBackend,
        script: VecDeque<(Duration, Next)>,
        requested: Vec<u32>,
        refusing_requests: Option<AuditFailure>,
    }

    impl ScriptedChannel {
        fn new(
            clock: &SimClock,
            clipboard: &Clipboard,
            script: impl IntoIterator<Item = (Duration, Next)>,
        ) -> ScriptedChannel {
            ScriptedChannel {
                clock: clock.clone(),
                backend: clipboard.backend(),
                script: script.into_iter().collect(),
                requested: Vec::new(),
                refusing_requests: None,
            }
        }

        fn refusing_requests(mut self, failure: AuditFailure) -> ScriptedChannel {
            self.refusing_requests = Some(failure);
            self
        }
    }

    impl ClipboardChannel for ScriptedChannel {
        async fn request(&mut self, format: u32) -> Result<(), AuditFailure> {
            self.requested.push(format);
            match &self.refusing_requests {
                Some(failure) => Err(failure.clone()),
                None => Ok(()),
            }
        }

        async fn wait(&mut self) -> Result<(), AuditFailure> {
            let Some((after, next)) = self.script.pop_front() else {
                return std::future::pending().await;
            };
            if !after.is_zero() {
                self.clock.sleep(after).await;
            }
            match next {
                Next::Announced(formats) => self.backend.on_remote_copy(&formats),
                Next::Data(bytes) => self
                    .backend
                    .on_format_data_response(FormatDataResponse::new_data(bytes)),
                Next::Fails(failure) => return Err(failure),
            }
            Ok(())
        }
    }

    fn synthetic() -> (SimClock, tokio::runtime::Runtime) {
        SimClock::isolated(TimeModel::Synthetic, datetime!(2025-02-03 09:00))
    }

    fn announced() -> (Duration, Next) {
        (Duration::ZERO, Next::Announced(workstation_formats()))
    }

    #[test]
    fn a_read_completes_through_its_channel() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let clipboard = Clipboard::default();
            let read = clipboard.clear();
            let mut channel = ScriptedChannel::new(
                &clock,
                &clipboard,
                [
                    announced(),
                    (Duration::ZERO, Next::Data(utf16("{\"a\":1}\n"))),
                ],
            );

            let result = read.complete(&clock, DEADLINE, &mut channel).await;

            assert_eq!(result, Ok("{\"a\":1}\n".to_string()));
            assert_eq!(channel.requested, [CF_UNICODETEXT]);
        });
    }

    #[test]
    fn a_read_that_never_completes_resolves_at_its_deadline() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let clipboard = Clipboard::default();
            let read = clipboard.clear();
            let mut channel = ScriptedChannel::new(&clock, &clipboard, [announced()]);

            let started = clock.now();
            let result = read.complete(&clock, DEADLINE, &mut channel).await;

            assert_eq!(
                result,
                Err(AuditFailure::Clipboard(ClipboardFailure::DeadlineReached))
            );
            assert_eq!(channel.requested, [CF_UNICODETEXT]);
            assert_eq!(clock.now().duration_since(started), DEADLINE);
        });
    }

    #[test]
    fn the_deadline_runs_from_the_start_of_the_read() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let clipboard = Clipboard::default();
            let read = clipboard.clear();
            // Ten further copies, 25 seconds apart: each wait is shorter than
            // the deadline, and all of them together far longer.
            let further = (0..10).map(|_| {
                (
                    Duration::from_secs(25),
                    Next::Announced(workstation_formats()),
                )
            });
            let mut channel = ScriptedChannel::new(
                &clock,
                &clipboard,
                std::iter::once(announced()).chain(further),
            );

            let started = clock.now();
            let result = read.complete(&clock, DEADLINE, &mut channel).await;

            assert_eq!(
                result,
                Err(AuditFailure::Clipboard(ClipboardFailure::DeadlineReached))
            );
            assert_eq!(channel.requested, [CF_UNICODETEXT]);
            assert_eq!(clock.now().duration_since(started), DEADLINE);
        });
    }

    #[test]
    fn a_failure_while_waiting_ends_the_read() {
        for failure in [
            AuditFailure::Terminal(TerminalFailure::ConnectionLost),
            AuditFailure::Clipboard(ClipboardFailure::DeclaredLengthOverCeiling {
                declared: 9_999_999,
            }),
        ] {
            let (clock, runtime) = synthetic();
            runtime.block_on(async {
                let clipboard = Clipboard::default();
                let read = clipboard.clear();
                let mut channel = ScriptedChannel::new(
                    &clock,
                    &clipboard,
                    [announced(), (Duration::ZERO, Next::Fails(failure.clone()))],
                );

                let started = clock.now();
                let result = read.complete(&clock, DEADLINE, &mut channel).await;

                assert_eq!(result, Err(failure));
                assert_eq!(clock.now().duration_since(started), Duration::ZERO);
            });
        }
    }

    #[test]
    fn a_failure_sending_the_request_ends_the_read() {
        let (clock, runtime) = synthetic();
        runtime.block_on(async {
            let lost = AuditFailure::Terminal(TerminalFailure::ConnectionLost);
            let clipboard = Clipboard::default();
            let read = clipboard.clear();
            let mut channel = ScriptedChannel::new(&clock, &clipboard, [announced()])
                .refusing_requests(lost.clone());

            let started = clock.now();
            let result = read.complete(&clock, DEADLINE, &mut channel).await;

            assert_eq!(result, Err(lost));
            assert_eq!(channel.requested, [CF_UNICODETEXT]);
            assert_eq!(clock.now().duration_since(started), Duration::ZERO);
        });
    }
}
