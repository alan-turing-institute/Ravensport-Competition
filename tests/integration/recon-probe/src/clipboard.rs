//! CLIPRDR (clipboard virtual channel) capture backend.
//!
//! IronRDP hands clipboard events to a `CliprdrBackend`. This one is inert:
//! every callback records a fact into shared [`ClipboardState`] and never
//! touches the socket. The session's pump loop reads that state and decides when
//! to send the client-initiated actions (`initiate_copy` / `initiate_paste`).
//!
//! The transfer is a pull, not a push. When the selection changes on the host,
//! `chansrv` sends only a Format List saying which formats exist; the text
//! arrives afterwards, and only because the client asked for it.
//!
//! `announcements` counts Format Lists rather than latching a flag, because the
//! copy here is caused by keystrokes sent *before* the read begins: a latch
//! cleared on entry to the read would discard the announcement that the copy had
//! already produced, and then wait for one that never comes.

use std::sync::{Arc, Mutex};

use ironrdp_cliprdr::backend::CliprdrBackend;
use ironrdp_cliprdr::pdu::{
    ClipboardFormat, ClipboardGeneralCapabilityFlags, FileContentsRequest, FileContentsResponse,
    FormatDataRequest, FormatDataResponse, LockDataId,
};
use ironrdp_core::impl_as_any;
use tracing::info;

/// Windows clipboard format ids we care about (MS-RDPECLIP standard formats).
pub const CF_TEXT: u32 = 1;
pub const CF_UNICODETEXT: u32 = 13;

/// Facts the backend records for the pump loop to act on. Shared via `Arc<Mutex<_>>`.
#[derive(Debug, Default)]
pub struct ClipboardState {
    /// The clipboard channel finished initialising and can carry copy and paste.
    pub ready: bool,
    /// The server asked us to advertise our clipboard formats (init handshake).
    pub request_format_list: bool,
    /// Format Lists seen since the last [`ClipboardState::clear`].
    pub announcements: u64,
    /// Formats the remote advertised in the most recent Format List.
    pub available: Option<Vec<u32>>,
    /// Format id asked for via `initiate_paste`; used to decode the response.
    pub requested_format: Option<u32>,
    /// Decoded clipboard text once the remote sends it back.
    pub text: Option<String>,
    /// A format-data error reported by the remote.
    pub error: Option<String>,
}

impl ClipboardState {
    /// Forget everything about copies seen so far, keeping the channel state.
    /// Called before the keystrokes that cause a copy, so that what is read
    /// afterwards is known to be the result of them.
    pub fn clear(&mut self) {
        self.announcements = 0;
        self.available = None;
        self.requested_format = None;
        self.text = None;
        self.error = None;
    }
}

/// Clipboard backend that only records into [`ClipboardState`].
#[derive(Debug)]
pub struct ClipboardCapture {
    state: Arc<Mutex<ClipboardState>>,
}

impl ClipboardCapture {
    pub fn new(state: Arc<Mutex<ClipboardState>>) -> Self {
        Self { state }
    }
}

impl_as_any!(ClipboardCapture);

impl CliprdrBackend for ClipboardCapture {
    fn temporary_directory(&self) -> &str {
        ".cliprdr"
    }

    fn client_capabilities(&self) -> ClipboardGeneralCapabilityFlags {
        ClipboardGeneralCapabilityFlags::empty()
    }

    fn on_ready(&mut self) {
        self.state.lock().unwrap().ready = true;
    }

    fn on_request_format_list(&mut self) {
        self.state.lock().unwrap().request_format_list = true;
    }

    fn on_process_negotiated_capabilities(
        &mut self,
        _capabilities: ClipboardGeneralCapabilityFlags,
    ) {
    }

    fn on_remote_copy(&mut self, available_formats: &[ClipboardFormat]) {
        let ids: Vec<u32> = available_formats.iter().map(|f| f.id().value()).collect();
        info!(?ids, "remote clipboard copy — formats advertised");
        let mut state = self.state.lock().unwrap();
        state.announcements += 1;
        state.available = Some(ids);
    }

    fn on_format_data_request(&mut self, _request: FormatDataRequest) {}

    fn on_format_data_response(&mut self, response: FormatDataResponse<'_>) {
        let mut state = self.state.lock().unwrap();
        if response.is_error() {
            state.error = Some("server returned a format-data error".to_owned());
            return;
        }
        let format = state.requested_format.unwrap_or(CF_UNICODETEXT);
        let text = decode_clipboard(response.data(), format);
        info!(
            bytes = response.data().len(),
            chars = text.chars().count(),
            "received clipboard data"
        );
        state.text = Some(text);
    }

    fn on_file_contents_request(&mut self, _request: FileContentsRequest) {}
    fn on_file_contents_response(&mut self, _response: FileContentsResponse<'_>) {}
    fn on_lock(&mut self, _data_id: LockDataId) {}
    fn on_unlock(&mut self, _data_id: LockDataId) {}
}

/// Decode raw clipboard bytes into a string for the given format id.
///
/// `CF_UNICODETEXT` is UTF-16 little-endian with a trailing NUL; everything else
/// is treated as (UTF-8) bytes. Trailing NULs are trimmed in both cases. Line
/// endings are left exactly as they arrived: whether the host's newlines survive
/// the round trip is part of what is being measured.
pub fn decode_clipboard(bytes: &[u8], format_id: u32) -> String {
    if format_id == CF_UNICODETEXT {
        // `as_chunks` rather than `chunks_exact`, so the pair arrives as a
        // `[u8; 2]` and `from_le_bytes` takes it directly. A trailing odd byte
        // goes to the remainder, which is discarded — the same as before, since
        // `chunks_exact` also dropped it.
        let units: Vec<u16> = bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|c| u16::from_le_bytes(*c))
            .take_while(|&u| u != 0)
            .collect();
        String::from_utf16_lossy(&units)
    } else {
        String::from_utf8_lossy(bytes)
            .trim_end_matches('\0')
            .to_owned()
    }
}

/// Pick the best text format from those the remote advertised: prefer Unicode
/// text, then ANSI text, else the first offered (else Unicode as a default).
pub fn choose_format(available: &[u32]) -> u32 {
    if available.contains(&CF_UNICODETEXT) {
        CF_UNICODETEXT
    } else if available.contains(&CF_TEXT) {
        CF_TEXT
    } else {
        available.first().copied().unwrap_or(CF_UNICODETEXT)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ironrdp_cliprdr::pdu::ClipboardFormatId;

    fn utf16le(s: &str) -> Vec<u8> {
        let mut v: Vec<u8> = s.encode_utf16().flat_map(|u| u.to_le_bytes()).collect();
        v.extend_from_slice(&[0, 0]); // trailing NUL, as a real source sends
        v
    }

    #[test]
    fn decode_unicode_text() {
        assert_eq!(
            decode_clipboard(&utf16le("Lorem ipsum"), CF_UNICODETEXT),
            "Lorem ipsum"
        );
    }

    #[test]
    fn decode_unicode_stops_at_nul() {
        let bytes = [0x4c, 0x00, 0x6f, 0x00, 0x00, 0x00, 0x21, 0x00];
        assert_eq!(decode_clipboard(&bytes, CF_UNICODETEXT), "Lo");
    }

    #[test]
    fn decode_preserves_line_endings_as_received() {
        // Whether the host's LF arrives as CRLF is a measurement, so decoding
        // must not normalise it away.
        assert_eq!(
            decode_clipboard(&utf16le("a\r\nb\n"), CF_UNICODETEXT),
            "a\r\nb\n"
        );
        assert_eq!(decode_clipboard(b"a\r\nb\n\0", CF_TEXT), "a\r\nb\n");
    }

    #[test]
    fn decode_ansi_text_trims_nul() {
        assert_eq!(decode_clipboard(b"hello\0", CF_TEXT), "hello");
    }

    #[test]
    fn decode_empty_is_empty() {
        assert_eq!(decode_clipboard(&[], CF_UNICODETEXT), "");
        assert_eq!(decode_clipboard(&[], CF_TEXT), "");
    }

    #[test]
    fn choose_format_prefers_unicode_then_text_then_first() {
        assert_eq!(choose_format(&[CF_TEXT, CF_UNICODETEXT, 7]), CF_UNICODETEXT);
        assert_eq!(choose_format(&[7, CF_TEXT]), CF_TEXT);
        assert_eq!(choose_format(&[99, 42]), 99);
        assert_eq!(choose_format(&[]), CF_UNICODETEXT);
    }

    fn state_and_backend() -> (Arc<Mutex<ClipboardState>>, ClipboardCapture) {
        let state = Arc::new(Mutex::new(ClipboardState::default()));
        let backend = ClipboardCapture::new(Arc::clone(&state));
        (state, backend)
    }

    #[test]
    fn backend_records_ready_and_format_list_request() {
        let (state, mut backend) = state_and_backend();
        backend.on_ready();
        backend.on_request_format_list();
        let s = state.lock().unwrap();
        assert!(s.ready);
        assert!(s.request_format_list);
    }

    #[test]
    fn backend_counts_each_announcement() {
        let (state, mut backend) = state_and_backend();
        let formats = [
            ClipboardFormat::new(ClipboardFormatId::new(CF_UNICODETEXT)),
            ClipboardFormat::new(ClipboardFormatId::new(CF_TEXT)),
        ];
        backend.on_remote_copy(&formats);
        backend.on_remote_copy(&formats);
        let s = state.lock().unwrap();
        assert_eq!(s.announcements, 2);
        assert_eq!(s.available, Some(vec![CF_UNICODETEXT, CF_TEXT]));
    }

    #[test]
    fn an_announcement_before_the_read_survives_it() {
        // The copy is caused by keystrokes sent before the read starts, so the
        // announcement it produces must still be there when the read looks.
        let (state, mut backend) = state_and_backend();
        state.lock().unwrap().clear();
        backend.on_remote_copy(&[ClipboardFormat::new(ClipboardFormatId::new(CF_UNICODETEXT))]);
        assert_eq!(state.lock().unwrap().announcements, 1);
    }

    #[test]
    fn clear_forgets_copies_but_keeps_the_channel_ready() {
        let (state, mut backend) = state_and_backend();
        backend.on_ready();
        backend.on_remote_copy(&[ClipboardFormat::new(ClipboardFormatId::new(CF_TEXT))]);
        state.lock().unwrap().clear();
        let s = state.lock().unwrap();
        assert!(s.ready, "clearing must not tear down the channel state");
        assert_eq!(s.announcements, 0);
        assert!(s.available.is_none());
        assert!(s.text.is_none());
    }

    #[test]
    fn backend_decodes_format_data_response() {
        let (state, mut backend) = state_and_backend();
        state.lock().unwrap().requested_format = Some(CF_UNICODETEXT);
        backend.on_format_data_response(FormatDataResponse::new_data(utf16le("Lorem ipsum dolor")));
        assert_eq!(
            state.lock().unwrap().text.as_deref(),
            Some("Lorem ipsum dolor")
        );
    }

    #[test]
    fn backend_records_format_data_error() {
        let (state, mut backend) = state_and_backend();
        backend.on_format_data_response(FormatDataResponse::new_error());
        assert!(state.lock().unwrap().error.is_some());
        assert!(state.lock().unwrap().text.is_none());
    }
}
