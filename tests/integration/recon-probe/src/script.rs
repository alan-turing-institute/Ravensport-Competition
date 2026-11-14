//! The probe's script: one action per command-line argument.
//!
//! Parsing is separated from execution so that a session sequence — where to
//! click, what to type, how long to wait — is checked before a connection is
//! made, and so the whole vocabulary is testable without one. A mistyped
//! coordinate should be an argument error, not a screenshot of the wrong thing
//! twenty seconds later.

use anyhow::{bail, Context as _, Result};

use crate::keys::{parse_chord, type_chords};

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    /// Wait for the clipboard channel to finish initialising.
    Ready { ms: u64 },
    /// Pump frames for a while, drawing but doing nothing.
    Wait { ms: u64 },
    /// Left-click at a desktop coordinate.
    Click { x: u16, y: u16 },
    /// Press one chord, e.g. `enter` or `ctrl+shift+c`.
    Key { codes: Vec<u8> },
    /// Type a string, one chord per character.
    Type { chords: Vec<Vec<u8>> },
    /// Save the framebuffer to a PNG.
    Shot { path: String },
    /// Forget copies seen so far, so the next read is known to be fresh.
    Clear,
    /// Wait for a copy on the host, ask for it, and report what arrived.
    Read { ms: u64 },
}

/// Parse one argument, such as `click 20,10` or `type ggVG"+y`.
pub fn parse_action(arg: &str) -> Result<Action> {
    let trimmed = arg.trim_start();
    let (verb, rest) = match trimmed.split_once(char::is_whitespace) {
        Some((v, r)) => (v, r.trim_start()),
        None => (trimmed, ""),
    };

    let action = match verb {
        "ready" => Action::Ready {
            ms: parse_ms(rest, 20_000)?,
        },
        "wait" => Action::Wait {
            ms: parse_ms(rest, 1_000)?,
        },
        "read" => Action::Read {
            ms: parse_ms(rest, 5_000)?,
        },
        "clear" => {
            if !rest.is_empty() {
                bail!("`clear` takes no argument (got `{rest}`)");
            }
            Action::Clear
        }
        "click" => {
            let (x, y) = rest
                .split_once([',', ' '])
                .with_context(|| format!("`click` needs `x,y` (got `{rest}`)"))?;
            Action::Click {
                x: x.trim()
                    .parse()
                    .with_context(|| format!("x coordinate `{x}`"))?,
                y: y.trim()
                    .parse()
                    .with_context(|| format!("y coordinate `{y}`"))?,
            }
        }
        "key" => {
            if rest.is_empty() {
                bail!("`key` needs a chord, e.g. `key enter` or `key ctrl+shift+c`");
            }
            Action::Key {
                codes: parse_chord(rest.trim())?,
            }
        }
        // Deliberately not trimmed: a trailing space may be part of what is typed.
        "type" => {
            if rest.is_empty() {
                bail!("`type` needs text");
            }
            Action::Type {
                chords: type_chords(rest)?,
            }
        }
        "shot" => {
            if rest.is_empty() {
                bail!("`shot` needs a path");
            }
            Action::Shot {
                path: rest.trim().to_owned(),
            }
        }
        other => bail!(
            "unknown action `{other}`; expected one of: \
             ready, wait, click, key, type, shot, clear, read"
        ),
    };
    Ok(action)
}

/// Parse every argument, or explain which one was wrong.
pub fn parse_script(args: &[String]) -> Result<Vec<Action>> {
    args.iter()
        .enumerate()
        .map(|(i, arg)| parse_action(arg).with_context(|| format!("argument {} (`{arg}`)", i + 1)))
        .collect()
}

fn parse_ms(rest: &str, default: u64) -> Result<u64> {
    if rest.is_empty() {
        return Ok(default);
    }
    rest.trim()
        .parse()
        .with_context(|| format!("`{rest}` is not a number of milliseconds"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::keys::{CTRL, SHIFT};

    #[test]
    fn timings_default_when_omitted() {
        assert_eq!(parse_action("ready").unwrap(), Action::Ready { ms: 20_000 });
        assert_eq!(parse_action("wait").unwrap(), Action::Wait { ms: 1_000 });
        assert_eq!(parse_action("read").unwrap(), Action::Read { ms: 5_000 });
        assert_eq!(
            parse_action("wait 2500").unwrap(),
            Action::Wait { ms: 2_500 }
        );
    }

    #[test]
    fn click_takes_a_comma_or_a_space() {
        assert_eq!(
            parse_action("click 20,10").unwrap(),
            Action::Click { x: 20, y: 10 }
        );
        assert_eq!(
            parse_action("click 640 512").unwrap(),
            Action::Click { x: 640, y: 512 }
        );
    }

    #[test]
    fn key_parses_a_chord() {
        assert_eq!(
            parse_action("key enter").unwrap(),
            Action::Key { codes: vec![0x1C] }
        );
        assert_eq!(
            parse_action("key ctrl+shift+c").unwrap(),
            Action::Key {
                codes: vec![CTRL, SHIFT, 0x2E]
            }
        );
    }

    #[test]
    fn type_keeps_internal_spaces() {
        let cmd = "xclip -selection clipboard /tmp/probe-fixture.txt";
        let a = parse_action(&format!("type {cmd}")).unwrap();
        match a {
            Action::Type { chords } => assert_eq!(chords.len(), cmd.len()),
            other => panic!("expected Type, got {other:?}"),
        }
    }

    #[test]
    fn an_untypeable_character_fails_before_a_session_is_opened() {
        // The point of parsing first: this must not surface as a wrong character
        // typed into a live session.
        // `£` is on the United Kingdom layout, so an absent character has to
        // come from elsewhere: AltGr is not modelled, so what it reaches is
        // unmapped.
        let err = parse_action("type €10").unwrap_err().to_string();
        assert!(
            err.contains("not on the pinned United Kingdom layout"),
            "{err}"
        );
    }

    #[test]
    fn unknown_actions_and_bad_arguments_are_named() {
        assert!(parse_action("frobnicate 3")
            .unwrap_err()
            .to_string()
            .contains("unknown action"));
        assert!(parse_action("click 20")
            .unwrap_err()
            .to_string()
            .contains("needs `x,y`"));
        assert!(parse_action("wait soon")
            .unwrap_err()
            .to_string()
            .contains("milliseconds"));
        assert!(parse_action("shot")
            .unwrap_err()
            .to_string()
            .contains("needs a path"));
        assert!(parse_action("clear now")
            .unwrap_err()
            .to_string()
            .contains("no argument"));
    }

    #[test]
    fn a_script_reports_which_argument_was_wrong() {
        let args = ["ready".to_owned(), "click nowhere".to_owned()];
        let err = format!("{:#}", parse_script(&args).unwrap_err());
        assert!(err.contains("argument 2"), "{err}");
    }

    #[test]
    fn a_whole_copy_sequence_parses() {
        let args: Vec<String> = [
            "ready 20000",
            "wait 3000",
            "shot /tmp/desktop.png",
            "click 567,999",
            "wait 3000",
            "clear",
            "type xclip -selection clipboard /tmp/probe-fixture.txt",
            "key enter",
            "wait 1000",
            "read 8000",
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        assert_eq!(parse_script(&args).unwrap().len(), args.len());
    }
}
