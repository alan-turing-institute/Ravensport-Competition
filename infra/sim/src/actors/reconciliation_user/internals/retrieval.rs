//! The retrieval (sim-impl.md, "Retrieval" and "Splitting the window"): the
//! command for a span, the reading of a response into its records or a
//! failure, the split of the retrieval's bounds into spans, and the procedure
//! that drives them through the [`Workstation`] and assembles the window.

use std::time::Duration;

use time::format_description::well_known::Rfc3339;
use time::PrimitiveDateTime;

use super::session::Workstation;
use super::{AuditFailure, ClipboardFailure, Event, RetrievalFailure};

/// The event store's query endpoint, through Grafana's datasource proxy on the
/// replica (`recon.md` §6.1, `hist.md` §7).
const QUERY_URL: &str =
    "https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs/select/logsql/query";

/// Opens the line the command ends every response with, before `curl`'s exit
/// status.
const COMPLETION: &str = "retrieval-end ";

/// One sub-retrieval: `start` included, `end` excluded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct Span {
    pub(super) start: PrimitiveDateTime,
    pub(super) end: PrimitiveDateTime,
}

/// The command typed to retrieve a span, carrying the compiled credentials
/// (sim-impl.md, "Retrieval"). `curl` scrubs its own `-u` argument, so a
/// `bash -c` wrapper deliberately retains the complete command in its argument
/// vector for the request's lifetime. The span is the whole expression: no
/// filter, and no `limit`.
pub(super) fn retrieval_command(span: &Span) -> String {
    let username = crate::constants::RECON_USERNAME;
    let password = crate::constants::RECON_PASSWORD;
    let (start, end) = (bound(span.start), bound(span.end));
    format!(
        "bash -c '{{ curl -sS --fail-with-body -u {username}:{password} -G {QUERY_URL} \
         --data-urlencode \"query=_time:[{start}, {end})\" 2>&1; \
         echo \"{COMPLETION}$?\"; }} | xclip -selection clipboard'"
    )
}

/// RFC 3339 in UTC with a `Z` designator, with a fractional second only where
/// the instant has one.
fn bound(instant: PrimitiveDateTime) -> String {
    instant
        .assume_utc()
        .format(&Rfc3339)
        .expect("an instant within the sim's run formats as RFC 3339")
}

/// A response as the clipboard returns it: its records, or the failure it
/// reports. The last line must be the completion line; under status zero every
/// line before it is a record.
pub(super) fn read_response(text: &str) -> Result<Vec<Event>, RetrievalFailure> {
    let text = text.strip_suffix('\n').unwrap_or(text);
    let (body, last) = match text.rsplit_once('\n') {
        Some((body, last)) => (Some(body), last),
        None => (None, text),
    };
    let status: u32 = last
        .strip_prefix(COMPLETION)
        .and_then(|status| status.parse().ok())
        .ok_or(RetrievalFailure::Incomplete)?;
    let lines = body.into_iter().flat_map(|body| body.split('\n'));
    match status {
        0 => lines.map(Event::parse).collect(),
        status => Err(classify(status, lines)),
    }
}

/// A failed retrieval, by `curl`'s exit status and, for an HTTP error, the
/// code in its diagnostic line (sim-impl.md, "Reading a response").
fn classify<'a>(status: u32, lines: impl Iterator<Item = &'a str>) -> RetrievalFailure {
    match status {
        6 | 7 | 28 | 52 | 55 | 56 => RetrievalFailure::Unreachable,
        22 => match http_code(lines) {
            Some(401 | 403) => RetrievalFailure::AuthorisationRefused,
            Some(500..=599) => RetrievalFailure::Unreachable,
            _ => RetrievalFailure::MalformedResponse,
        },
        35 | 51 | 58 | 60 | 77 | 83 | 90 | 91 => RetrievalFailure::CertificateVerification,
        18 => RetrievalFailure::Incomplete,
        _ => RetrievalFailure::MalformedResponse,
    }
}

/// The HTTP code in `curl: (22) The requested URL returned error: <code>`, on
/// any line and wherever it starts in it. Standard error and standard output
/// share the pipe in the order `curl` writes them: the diagnostic before the
/// error body against the deployed Grafana, but a body without a final line
/// feed written first would run into it.
fn http_code<'a>(mut lines: impl Iterator<Item = &'a str>) -> Option<u16> {
    lines
        .find_map(|line| line.find("curl: (22)").map(|at| &line[at..]))
        .and_then(|diagnostic| diagnostic.rsplit(' ').next())
        .and_then(|code| code.parse().ok())
}

/// A span at the floor or narrower reached its deadline: fatal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) struct SpanAtFloor(pub(super) Span);

/// The split of the retrieval's bounds into consecutive spans (sim-impl.md,
/// "Splitting the window"): each from the previous one's end, at a width
/// halved on a deadline, no narrower than the floor, and held once it
/// succeeds. The floor's expected records fill a sixteenth of the clipboard's
/// ceiling, so no legitimate span at the floor approaches it, and one that
/// still reaches its deadline is fatal.
#[derive(Debug)]
pub(super) struct Split {
    /// The start of the span not yet retrieved.
    cursor: PrimitiveDateTime,
    end: PrimitiveDateTime,
    width: Duration,
    floor: Duration,
}

impl Split {
    pub(super) fn new(
        (start, end): (PrimitiveDateTime, PrimitiveDateTime),
        initial_width: Duration,
        floor: Duration,
    ) -> Split {
        Split {
            cursor: start,
            end,
            width: initial_width.min((end - start).unsigned_abs()),
            floor,
        }
    }

    /// The span to retrieve next, or none once the bounds are covered.
    pub(super) fn next_span(&self) -> Option<Span> {
        (self.cursor < self.end).then(|| Span {
            start: self.cursor,
            end: (self.cursor + self.width).min(self.end),
        })
    }

    /// The current span was retrieved.
    pub(super) fn succeeded(&mut self) {
        if let Some(span) = self.next_span() {
            self.cursor = span.end;
        }
    }

    /// The current span's read reached its deadline: halve it and retry from
    /// its start, or end at the floor.
    pub(super) fn reached_deadline(&mut self) -> Result<(), SpanAtFloor> {
        let Some(span) = self.next_span() else {
            return Ok(());
        };
        let length = (span.end - span.start).unsigned_abs();
        if length <= self.floor {
            return Err(SpanAtFloor(span));
        }
        self.width = (length / 2).max(self.floor);
        Ok(())
    }
}

/// The spans' records as one window, in `_time` order; a record with no
/// readable `_time` follows every one with, in the order received.
pub(super) fn assemble(spans: Vec<Vec<Event>>) -> Vec<Event> {
    let mut records: Vec<Event> = spans.into_iter().flatten().collect();
    records.sort_by_key(|record| {
        let time = record.instant("_time");
        (time.is_err(), time.ok())
    });
    records
}

/// A window's records, and the reads it took.
#[derive(Debug, PartialEq)]
pub(super) struct Retrieved {
    pub(super) records: Vec<Event>,
    pub(super) retrievals: usize,
}

/// Why a retrieval stopped short of its records, at the span not yet retrieved.
#[derive(Debug, PartialEq)]
pub(super) enum RetrievalStop {
    /// A failure, for its site's budget.
    Failed(AuditFailure),
    /// A span of one day or less reached its deadline.
    AtFloor(Span),
}

/// A retrieval's progress: the split and the spans retrieved, kept across the
/// sessions that drive it, so that a retry or a restarted session continues
/// from the span that failed.
#[derive(Debug)]
pub(super) struct Retrieval {
    split: Split,
    spans: Vec<Vec<Event>>,
    retrievals: usize,
}

impl Retrieval {
    pub(super) fn new(
        bounds: (PrimitiveDateTime, PrimitiveDateTime),
        initial_width: Duration,
        floor: Duration,
    ) -> Retrieval {
        Retrieval {
            split: Split::new(bounds, initial_width, floor),
            spans: Vec::new(),
            retrievals: 0,
        }
    }

    /// The start of the span not yet retrieved; it moves only when a span
    /// succeeds.
    pub(super) fn position(&self) -> PrimitiveDateTime {
        self.split.cursor
    }

    /// Retrieve the spans remaining through the workstation. Each span's read
    /// is latched by clearing the clipboard before its command; a read reaching
    /// its deadline halves the span, and any other failure stops the retrieval
    /// at that span.
    pub(super) async fn continue_in(
        &mut self,
        workstation: &mut impl Workstation,
    ) -> Result<(), RetrievalStop> {
        while let Some(span) = self.split.next_span() {
            workstation.clear_latch().await;
            workstation
                .type_line(&retrieval_command(&span))
                .await
                .map_err(RetrievalStop::Failed)?;
            self.retrievals += 1;
            match workstation.read_clipboard().await {
                Ok(text) => {
                    let records = read_response(&text).map_err(|failure| {
                        RetrievalStop::Failed(AuditFailure::Retrieval(failure))
                    })?;
                    self.spans.push(records);
                    self.split.succeeded();
                }
                Err(AuditFailure::Clipboard(ClipboardFailure::DeadlineReached)) => self
                    .split
                    .reached_deadline()
                    .map_err(|SpanAtFloor(span)| RetrievalStop::AtFloor(span))?,
                Err(failure) => return Err(RetrievalStop::Failed(failure)),
            }
        }
        Ok(())
    }

    /// The window's records in `_time` order, and every read issued.
    pub(super) fn finish(self) -> Retrieved {
        Retrieved {
            records: assemble(self.spans),
            retrievals: self.retrievals,
        }
    }
}

/// Retrieve the bounds through the workstation, span by span, in one session.
#[cfg(test)]
pub(super) async fn retrieve(
    workstation: &mut impl Workstation,
    bounds: (PrimitiveDateTime, PrimitiveDateTime),
    initial_width: Duration,
    floor: Duration,
) -> Result<Retrieved, RetrievalStop> {
    let mut retrieval = Retrieval::new(bounds, initial_width, floor);
    retrieval.continue_in(workstation).await?;
    Ok(retrieval.finish())
}

#[cfg(test)]
mod tests {
    use time::macros::datetime;

    use super::*;
    use crate::actors::reconciliation_user::internals::session::keys_for_line;
    use crate::actors::reconciliation_user::internals::workstation_double::{Double, Recorded};
    use crate::actors::reconciliation_user::internals::ClipboardFailure;
    use crate::constants::{RECON_PASSWORD, RECON_USERNAME};

    const DAY: Duration = Duration::from_secs(86_400);
    const HOUR: Duration = Duration::from_secs(3_600);

    fn span(start: PrimitiveDateTime, end: PrimitiveDateTime) -> Span {
        Span { start, end }
    }

    fn the_week() -> Span {
        span(datetime!(2024-12-31 18:00), datetime!(2025-01-07 0:00))
    }

    // Records as the store returns them, every value as text.
    const JOB_OPENED: &str = r#"{"_time":"2025-01-02T10:00:00Z","_msg":"dispense job opened","event_type":"job_opened","job_id":"job-1","type":"dispense","session_id":"session-1"}"#;
    const TRANSACTION_OPEN: &str = r#"{"_time":"2025-01-02T10:05:00Z","_msg":"dispense transaction opened","event_type":"transaction_open","job_id":"job-1","type":"dispense","session_id":"session-1"}"#;
    const LOGIN: &str = r#"{"_time":"2025-01-02T09:55:00Z","_msg":"login","msg_id":"LOGIN","app_name":"fms","hostname":"fms-01","fms@99999.username":"jrenard","fms@99999.session_id":"session-1"}"#;

    fn response(lines: &[&str]) -> String {
        lines.iter().map(|line| format!("{line}\n")).collect()
    }

    fn event(line: &str) -> Event {
        Event::parse(line).unwrap()
    }

    fn at(time: &str) -> String {
        format!(r#"{{"_time":"{time}","event_type":"job_opened","job_id":"job-{time}"}}"#)
    }

    // --- The command -------------------------------------------------------------

    #[test]
    fn the_retrieval_command_is_the_exact_line_typed() {
        let expected = format!(
            "bash -c '{{ curl -sS --fail-with-body -u {RECON_USERNAME}:{RECON_PASSWORD} -G \
             https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs/select/logsql/query \
             --data-urlencode \"query=_time:[2024-12-31T18:00:00Z, 2025-01-07T00:00:00Z)\" 2>&1; \
             echo \"retrieval-end $?\"; }} | xclip -selection clipboard'"
        );
        assert_eq!(retrieval_command(&the_week()), expected);
    }

    #[test]
    fn the_retrieval_command_is_typable() {
        assert_eq!(
            keys_for_line(&retrieval_command(&the_week())).map(|_| ()),
            Ok(())
        );
    }

    #[test]
    fn a_span_bound_with_a_fractional_second_carries_it() {
        let command = retrieval_command(&span(
            datetime!(2025-01-03 9:30:15.25),
            datetime!(2025-01-04 0:00),
        ));
        assert!(
            command.contains("\"query=_time:[2025-01-03T09:30:15.25Z, 2025-01-04T00:00:00Z)\""),
            "{command}"
        );
    }

    // --- Reading a response --------------------------------------------------------

    #[test]
    fn a_response_ending_in_status_zero_is_its_records() {
        let text = response(&[JOB_OPENED, TRANSACTION_OPEN, LOGIN, "retrieval-end 0"]);
        assert_eq!(
            read_response(&text),
            Ok(vec![
                event(JOB_OPENED),
                event(TRANSACTION_OPEN),
                event(LOGIN)
            ])
        );
    }

    #[test]
    fn a_response_of_the_completion_line_alone_is_a_span_with_no_records() {
        assert_eq!(read_response(&response(&["retrieval-end 0"])), Ok(vec![]));
    }

    #[test]
    fn a_response_without_its_completion_line_is_incomplete() {
        assert_eq!(
            read_response(&response(&[JOB_OPENED, TRANSACTION_OPEN])),
            Err(RetrievalFailure::Incomplete)
        );
        assert_eq!(read_response(""), Err(RetrievalFailure::Incomplete));
    }

    #[test]
    fn a_record_line_that_is_not_a_json_object_is_a_malformed_response() {
        let text = response(&[JOB_OPENED, r#"{"_time":"2025-01-02T1"#, "retrieval-end 0"]);
        assert_eq!(
            read_response(&text),
            Err(RetrievalFailure::MalformedResponse)
        );
    }

    #[test]
    fn a_failed_retrieval_is_classified_by_its_status() {
        let http = |code: u16| format!("curl: (22) The requested URL returned error: {code}");
        let cases: [(Vec<String>, RetrievalFailure); 12] = [
            (
                vec!["curl: (6) Could not resolve host: hist-dmz-01".into(), "retrieval-end 6".into()],
                RetrievalFailure::Unreachable,
            ),
            (
                vec![
                    "curl: (7) Failed to connect to hist-dmz-01 port 443 after 3 ms: Couldn't connect to server".into(),
                    "retrieval-end 7".into(),
                ],
                RetrievalFailure::Unreachable,
            ),
            (
                vec!["curl: (28) Connection timed out after 300000 milliseconds".into(), "retrieval-end 28".into()],
                RetrievalFailure::Unreachable,
            ),
            (
                // The error body is itself a JSON object.
                vec![
                    r#"{"message":"invalid username or password"}"#.into(),
                    http(401),
                    "retrieval-end 22".into(),
                ],
                RetrievalFailure::AuthorisationRefused,
            ),
            (
                vec![r#"{"message":"Permission denied"}"#.into(), http(403), "retrieval-end 22".into()],
                RetrievalFailure::AuthorisationRefused,
            ),
            (
                // An error body without a final line feed: the diagnostic joins
                // it on one line.
                vec![
                    format!(r#"{{"message":"invalid username or password"}}{}"#, http(401)),
                    "retrieval-end 22".into(),
                ],
                RetrievalFailure::AuthorisationRefused,
            ),
            (
                // As the deployed Grafana's refusal arrives: the diagnostic,
                // then the body.
                vec![
                    http(401),
                    r#"{"extra":null,"message":"Invalid username or password","messageId":"password-auth.failed","statusCode":401,"traceID":""}"#.into(),
                    "retrieval-end 22".into(),
                ],
                RetrievalFailure::AuthorisationRefused,
            ),
            (
                vec!["Bad Gateway".into(), http(502), "retrieval-end 22".into()],
                RetrievalFailure::Unreachable,
            ),
            (
                vec![
                    "curl: (60) SSL certificate problem: unable to get local issuer certificate".into(),
                    "retrieval-end 60".into(),
                ],
                RetrievalFailure::CertificateVerification,
            ),
            (
                vec![
                    JOB_OPENED.into(),
                    "curl: (18) transfer closed with outstanding read data remaining".into(),
                    "retrieval-end 18".into(),
                ],
                RetrievalFailure::Incomplete,
            ),
            (
                vec!["cannot parse query".into(), http(400), "retrieval-end 22".into()],
                RetrievalFailure::MalformedResponse,
            ),
            (
                vec!["curl: (3) URL rejected: Malformed input to a URL function".into(), "retrieval-end 3".into()],
                RetrievalFailure::MalformedResponse,
            ),
        ];
        for (lines, failure) in cases {
            let lines: Vec<&str> = lines.iter().map(String::as_str).collect();
            assert_eq!(read_response(&response(&lines)), Err(failure), "{lines:?}");
        }
    }

    // --- The split ---------------------------------------------------------------

    /// Each span the split gives, treating every read as a success.
    fn spans(mut split: Split) -> Vec<Span> {
        let mut spans = Vec::new();
        while let Some(span) = split.next_span() {
            spans.push(span);
            split.succeeded();
        }
        spans
    }

    #[test]
    fn the_first_span_is_the_initial_width_or_the_whole_retrieval() {
        let bounds = (datetime!(2024-12-31 18:00), datetime!(2025-01-08 0:00));
        assert_eq!(
            Split::new(bounds, 7 * DAY, DAY).next_span(),
            Some(span(
                datetime!(2024-12-31 18:00),
                datetime!(2025-01-07 18:00)
            ))
        );
        assert_eq!(
            Split::new(bounds, 14 * DAY, DAY).next_span(),
            Some(span(bounds.0, bounds.1))
        );
    }

    #[test]
    fn the_spans_are_consecutive_and_cover_the_bounds() {
        let bounds = (datetime!(2024-12-31 18:00), datetime!(2025-01-08 0:00));
        assert_eq!(
            spans(Split::new(bounds, 2 * DAY, DAY)),
            [
                span(datetime!(2024-12-31 18:00), datetime!(2025-01-02 18:00)),
                span(datetime!(2025-01-02 18:00), datetime!(2025-01-04 18:00)),
                span(datetime!(2025-01-04 18:00), datetime!(2025-01-06 18:00)),
                span(datetime!(2025-01-06 18:00), datetime!(2025-01-08 0:00)),
            ]
        );
    }

    #[test]
    fn a_span_reaching_its_deadline_is_halved_and_retried_from_its_start() {
        let bounds = (datetime!(2024-12-31 18:00), datetime!(2025-01-08 0:00));
        let mut split = Split::new(bounds, 7 * DAY, DAY);
        assert_eq!(split.reached_deadline(), Ok(()));
        assert_eq!(
            split.next_span(),
            Some(span(
                datetime!(2024-12-31 18:00),
                datetime!(2025-01-04 6:00)
            ))
        );
    }

    #[test]
    fn halving_stops_at_one_day() {
        let bounds = (datetime!(2025-01-01 0:00), datetime!(2025-01-02 12:00));
        let mut split = Split::new(bounds, 36 * HOUR, DAY);
        assert_eq!(split.reached_deadline(), Ok(()));
        assert_eq!(
            split.next_span(),
            Some(span(datetime!(2025-01-01 0:00), datetime!(2025-01-02 0:00)))
        );
    }

    #[test]
    fn a_span_of_one_day_or_less_reaching_its_deadline_ends_the_split() {
        let day = span(datetime!(2025-01-01 0:00), datetime!(2025-01-02 0:00));
        let mut split = Split::new((day.start, day.end), DAY, DAY);
        assert_eq!(split.reached_deadline(), Err(SpanAtFloor(day)));

        let bounds = (datetime!(2025-01-01 0:00), datetime!(2025-01-03 12:00));
        let mut split = Split::new(bounds, DAY, DAY);
        split.succeeded();
        split.succeeded();
        let remainder = span(datetime!(2025-01-03 0:00), datetime!(2025-01-03 12:00));
        assert_eq!(split.next_span(), Some(remainder));
        assert_eq!(split.reached_deadline(), Err(SpanAtFloor(remainder)));
    }

    #[test]
    fn a_width_that_succeeds_is_held() {
        let bounds = (datetime!(2024-12-31 18:00), datetime!(2025-01-08 0:00));
        let mut split = Split::new(bounds, 4 * DAY, DAY);
        split.reached_deadline().unwrap();
        assert_eq!(
            spans(split),
            [
                span(datetime!(2024-12-31 18:00), datetime!(2025-01-02 18:00)),
                span(datetime!(2025-01-02 18:00), datetime!(2025-01-04 18:00)),
                span(datetime!(2025-01-04 18:00), datetime!(2025-01-06 18:00)),
                span(datetime!(2025-01-06 18:00), datetime!(2025-01-08 0:00)),
            ]
        );
    }

    // --- Assembly ----------------------------------------------------------------

    #[test]
    fn the_assembled_window_is_in_time_order() {
        let untimed = r#"{"event_type":"bdn_linked","job_id":"job-untimed"}"#;
        let first = vec![
            event(&at("2025-01-02T10:00:00Z")),
            event(untimed),
            event(&at("2025-01-01T10:00:00Z")),
        ];
        let second = vec![
            event(&at("2025-01-01T12:00:00.5Z")),
            event(&at("2025-01-03T10:00:00Z")),
        ];
        assert_eq!(
            assemble(vec![first, second]),
            [
                event(&at("2025-01-01T10:00:00Z")),
                event(&at("2025-01-01T12:00:00.5Z")),
                event(&at("2025-01-02T10:00:00Z")),
                event(&at("2025-01-03T10:00:00Z")),
                event(untimed),
            ]
        );
    }

    // --- The procedure -------------------------------------------------------------

    #[tokio::test]
    async fn each_sub_retrieval_clears_the_latch_before_its_command() {
        let bounds = (datetime!(2025-01-01 0:00), datetime!(2025-01-04 0:00));
        let empty = || Ok(response(&["retrieval-end 0"]));
        let mut workstation = Double::answering([empty(), empty(), empty()]);
        retrieve(&mut workstation, bounds, DAY, DAY).await.unwrap();

        let commands = [
            span(datetime!(2025-01-01 0:00), datetime!(2025-01-02 0:00)),
            span(datetime!(2025-01-02 0:00), datetime!(2025-01-03 0:00)),
            span(datetime!(2025-01-03 0:00), datetime!(2025-01-04 0:00)),
        ]
        .map(|span| retrieval_command(&span));
        let expected: Vec<Recorded> = commands
            .into_iter()
            .flat_map(|command| {
                [
                    Recorded::ClearedLatch,
                    Recorded::Typed(command),
                    Recorded::Read,
                ]
            })
            .collect();
        assert_eq!(workstation.recorded, expected);
    }

    #[tokio::test]
    async fn a_failed_sub_retrieval_ends_the_retrieval_with_its_failure() {
        let bounds = (datetime!(2025-01-01 0:00), datetime!(2025-01-04 0:00));
        let mut workstation = Double::answering([
            Ok(response(&[JOB_OPENED, "retrieval-end 0"])),
            Ok(response(&[
                r#"{"message":"invalid username or password"}"#,
                "curl: (22) The requested URL returned error: 401",
                "retrieval-end 22",
            ])),
        ]);
        assert_eq!(
            retrieve(&mut workstation, bounds, DAY, DAY).await,
            Err(RetrievalStop::Failed(AuditFailure::Retrieval(
                RetrievalFailure::AuthorisationRefused
            )))
        );
        assert_eq!(workstation.typed().len(), 2, "no third command");
    }

    #[tokio::test]
    async fn every_read_counts_as_a_retrieval() {
        let bounds = (datetime!(2025-01-01 0:00), datetime!(2025-01-03 0:00));
        let mut workstation = Double::answering([
            Err(AuditFailure::Clipboard(ClipboardFailure::DeadlineReached)),
            Ok(response(&[JOB_OPENED, "retrieval-end 0"])),
            Ok(response(&[LOGIN, "retrieval-end 0"])),
        ]);
        assert_eq!(
            retrieve(&mut workstation, bounds, 2 * DAY, DAY).await,
            Ok(Retrieved {
                records: vec![event(LOGIN), event(JOB_OPENED)],
                retrievals: 3,
            })
        );
    }
}
