//! PS/2 Set 1 scancodes for the United Kingdom layout `recon.md` §5.2 pins.
//!
//! The client sends scancodes, not characters; which character a scancode
//! produces is decided by the layout running on `recon-01`, which pins `gb` in
//! `.xsession`. This table is therefore written *against* that layout, and an
//! unmapped character is an error rather than a substitution — a character
//! typed silently wrong is the failure §5.2 exists to prevent.
//!
//! **Scancodes are positional.** They name a physical key, not a character, so
//! the difference between this table and a United States one is which character
//! each position produces rather than which codes are sent. Six positions
//! differ, and one exists only here:
//!
//! | Scancode | United Kingdom | United States |
//! |---|---|---|
//! | `0x03` shifted | `"` | `@` |
//! | `0x04` shifted | `£` | `#` |
//! | `0x28` shifted | `@` | `"` |
//! | `0x29` shifted | `¬` | `~` |
//! | `0x2B` | `#` / `~` | `\` / `|` |
//! | `0x56` | `\` / `|` | *absent* |
//!
//! `0x56` is the extra key beside the left Shift, which a United States
//! keyboard does not have. It carries the pipe, which `recon-tests.md` §1 makes
//! load-bearing: LogsQL separates pipeline stages with it, so a table without
//! `0x56` cannot compose the audit's query at all.
//!
//! Pure data and pure functions, so the table is tested without a session.

use anyhow::{bail, Result};

pub const CTRL: u8 = 0x1D;
pub const SHIFT: u8 = 0x2A;
pub const ALT: u8 = 0x38;

/// Scancode for `c` under the United Kingdom layout, and whether Shift is held.
pub fn char_key(c: char) -> Option<(bool, u8)> {
    // Unshifted keys, in scancode order.
    #[rustfmt::skip]
    const UNSHIFTED: &[(char, u8)] = &[
        ('1', 0x02), ('2', 0x03), ('3', 0x04), ('4', 0x05), ('5', 0x06),
        ('6', 0x07), ('7', 0x08), ('8', 0x09), ('9', 0x0A), ('0', 0x0B),
        ('-', 0x0C), ('=', 0x0D),
        ('q', 0x10), ('w', 0x11), ('e', 0x12), ('r', 0x13), ('t', 0x14),
        ('y', 0x15), ('u', 0x16), ('i', 0x17), ('o', 0x18), ('p', 0x19),
        ('[', 0x1A), (']', 0x1B),
        ('a', 0x1E), ('s', 0x1F), ('d', 0x20), ('f', 0x21), ('g', 0x22),
        ('h', 0x23), ('j', 0x24), ('k', 0x25), ('l', 0x26), (';', 0x27),
        ('\'', 0x28), ('`', 0x29), ('#', 0x2B),
        ('\\', 0x56),
        ('z', 0x2C), ('x', 0x2D), ('c', 0x2E), ('v', 0x2F), ('b', 0x30),
        ('n', 0x31), ('m', 0x32), (',', 0x33), ('.', 0x34), ('/', 0x35),
        (' ', 0x39),
    ];
    // Shifted keys: the character above, on the same physical key.
    #[rustfmt::skip]
    const SHIFTED: &[(char, u8)] = &[
        ('!', 0x02), ('"', 0x03), ('£', 0x04), ('$', 0x05), ('%', 0x06),
        ('^', 0x07), ('&', 0x08), ('*', 0x09), ('(', 0x0A), (')', 0x0B),
        ('_', 0x0C), ('+', 0x0D),
        ('{', 0x1A), ('}', 0x1B), (':', 0x27), ('@', 0x28), ('¬', 0x29),
        ('~', 0x2B), ('|', 0x56),
        ('<', 0x33), ('>', 0x34), ('?', 0x35),
    ];

    if let Some(&(_, code)) = UNSHIFTED.iter().find(|&&(k, _)| k == c) {
        return Some((false, code));
    }
    if let Some(&(_, code)) = SHIFTED.iter().find(|&&(k, _)| k == c) {
        return Some((true, code));
    }
    if c.is_ascii_uppercase() {
        let lower = c.to_ascii_lowercase();
        return UNSHIFTED
            .iter()
            .find(|&&(k, _)| k == lower)
            .map(|&(_, code)| (true, code));
    }
    None
}

/// Scancode for a key that is named rather than typed (`enter`, `esc`, `f3`, …).
#[rustfmt::skip]
pub fn named_key(name: &str) -> Option<u8> {
    let code = match name {
        "esc" | "escape" => 0x01,
        "backspace"      => 0x0E,
        "tab"            => 0x0F,
        "enter" | "return" => 0x1C,
        "space"          => 0x39,
        "ctrl" | "control" => CTRL,
        "shift"          => SHIFT,
        "alt"            => ALT,
        "f1" => 0x3B, "f2" => 0x3C, "f3"  => 0x3D, "f4"  => 0x3E,
        "f5" => 0x3F, "f6" => 0x40, "f7"  => 0x41, "f8"  => 0x42,
        "f9" => 0x43, "f10" => 0x44, "f11" => 0x57, "f12" => 0x58,
        _ => return None,
    };
    Some(code)
}

/// Parse a chord such as `ctrl+a`, `ctrl+shift+r` or `enter` into the scancodes
/// to press, in order; they are released in the reverse order.
///
/// A single character is accepted as the base key, so `ctrl+c` and `"` are both
/// valid, and a character needing Shift contributes it as a modifier.
pub fn parse_chord(spec: &str) -> Result<Vec<u8>> {
    // `+` is both the separator and a key, so `+` and `ctrl++` are split by hand
    // rather than by `split('+')`, which would yield empty parts for them.
    let parts: Vec<&str> = if spec == "+" {
        vec!["+"]
    } else if let Some(mods) = spec.strip_suffix("++") {
        let mut v: Vec<&str> = mods.split('+').collect();
        v.push("+");
        v
    } else {
        spec.split('+').collect()
    };
    if parts.iter().any(|p| p.is_empty()) {
        bail!("`{spec}` has an empty key; write the plus key as `+`, e.g. `ctrl++`");
    }

    let (base, modifiers) = parts
        .split_last()
        .expect("split never yields an empty vector");
    let mut codes = Vec::with_capacity(parts.len());
    for m in modifiers {
        match named_key(&m.to_ascii_lowercase()) {
            Some(code) if code == CTRL || code == SHIFT || code == ALT => codes.push(code),
            _ => bail!("`{m}` is not a modifier (ctrl, shift, alt) in chord `{spec}`"),
        }
    }

    if let Some(code) = named_key(&base.to_ascii_lowercase()) {
        codes.push(code);
        return Ok(codes);
    }
    let mut chars = base.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) => {
            let (shift, code) = char_key(c).ok_or_else(|| {
                anyhow::anyhow!("`{c}` is not on the pinned United Kingdom layout")
            })?;
            if shift && !codes.contains(&SHIFT) {
                codes.push(SHIFT);
            }
            codes.push(code);
            Ok(codes)
        }
        _ => bail!("`{base}` is neither a named key nor a single character (chord `{spec}`)"),
    }
}

/// The scancodes for typing `text`, one chord per character.
pub fn type_chords(text: &str) -> Result<Vec<Vec<u8>>> {
    text.chars()
        .map(|c| {
            let (shift, code) = char_key(c).ok_or_else(|| {
                anyhow::anyhow!(
                    "`{c}` (U+{:04X}) is not on the pinned United Kingdom layout; \
                     typing it would send some other character",
                    c as u32
                )
            })?;
            Ok(if shift { vec![SHIFT, code] } else { vec![code] })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn letters_take_shift_only_when_capital() {
        assert_eq!(char_key('a'), Some((false, 0x1E)));
        assert_eq!(char_key('A'), Some((true, 0x1E)));
        assert_eq!(char_key('z'), Some((false, 0x2C)));
        assert_eq!(char_key('G'), Some((true, 0x22)));
    }

    #[test]
    fn punctuation_maps_to_the_united_kingdom_layout() {
        // The characters a command line and a file path need, plus the shifted
        // punctuation the layout assertion depends on.
        assert_eq!(char_key('"'), Some((true, 0x03))); // Shift + 2, not Shift + '
        assert_eq!(char_key('+'), Some((true, 0x0D))); // Shift + equals
        assert_eq!(char_key('/'), Some((false, 0x35)));
        assert_eq!(char_key('.'), Some((false, 0x34)));
        assert_eq!(char_key('-'), Some((false, 0x0C)));
        assert_eq!(char_key(':'), Some((true, 0x27)));
        assert_eq!(char_key(' '), Some((false, 0x39)));
    }

    #[test]
    fn the_six_positions_the_layouts_disagree_on() {
        // Each of these sends a scancode a United States table maps to some
        // other character, so a table built for the wrong layout types silently
        // wrong text rather than failing.
        assert_eq!(char_key('"'), Some((true, 0x03))); // US: @
        assert_eq!(char_key('£'), Some((true, 0x04))); // US: # — and absent there
        assert_eq!(char_key('@'), Some((true, 0x28))); // US: "
        assert_eq!(char_key('¬'), Some((true, 0x29))); // US: ~ — and absent there
        assert_eq!(char_key('#'), Some((false, 0x2B))); // US: \\
        assert_eq!(char_key('~'), Some((true, 0x2B))); // US: |
    }

    #[test]
    fn the_pipe_is_on_the_key_the_united_states_layout_lacks() {
        // `recon-tests.md` §1: LogsQL separates pipeline stages with `|`, so a
        // table without 0x56 cannot compose the audit's query. Under `us` the
        // pipe is Shift + 0x2B, which `gb` gives to `~`.
        assert_eq!(char_key('|'), Some((true, 0x56)));
        assert_eq!(char_key('\\'), Some((false, 0x56)));
    }

    #[test]
    fn the_layout_assertion_sequence_encodes_as_recon_tests_specifies() {
        // `recon-tests.md` §3's `keyboard_layout_is_the_pinned_one` sends
        // Shift+0x56, 0x2B, Shift+0x04 and expects `|`, `#`, `£`. This is the
        // client half of that contract; the session half needs a running host.
        assert_eq!(
            type_chords("|#£").unwrap(),
            vec![vec![SHIFT, 0x56], vec![0x2B], vec![SHIFT, 0x04]]
        );
    }

    #[test]
    fn an_unmapped_character_is_an_error_not_a_substitution() {
        // `£` is on this layout and no longer an example of an absent one.
        // AltGr is not modelled, so the characters it reaches are unmapped.
        assert_eq!(char_key('é'), None);
        assert_eq!(char_key('€'), None);
        let err = type_chords("€10").unwrap_err().to_string();
        assert!(
            err.contains("not on the pinned United Kingdom layout"),
            "{err}"
        );
    }

    #[test]
    fn shifted_punctuation_encodes_as_a_modifier_and_a_base_key() {
        // `@` and `#` are the pair that separates the two layouts most sharply:
        // under `gb` the first takes Shift and the second does not, and under
        // `us` it is the other way round.
        let chords = type_chords("a@#Z").unwrap();
        assert_eq!(
            chords,
            vec![
                vec![0x1E],        // a
                vec![SHIFT, 0x28], // @  — Shift + apostrophe
                vec![0x2B],        // #  — its own key, unshifted
                vec![SHIFT, 0x2C], // Z
            ]
        );
    }

    #[test]
    fn an_audit_query_carrying_a_pipe_is_typeable() {
        // The shape `recon.md` §6.1 retrieves with: an address, single quotes
        // protecting the shell, and a LogsQL pipeline stage.
        assert!(type_chords(
            "curl -sS 'https://hist-dmz-01/api/v1/query?q=_time:30d | count()' 2>&1 | xclip"
        )
        .is_ok());
    }

    #[test]
    fn the_copy_command_is_typeable() {
        assert!(type_chords("xclip -selection clipboard /tmp/probe-fixture.txt").is_ok());
    }

    #[test]
    fn chords_press_modifiers_before_the_base_key() {
        assert_eq!(parse_chord("ctrl+a").unwrap(), vec![CTRL, 0x1E]);
        assert_eq!(
            parse_chord("ctrl+shift+r").unwrap(),
            vec![CTRL, SHIFT, 0x13]
        );
        assert_eq!(parse_chord("enter").unwrap(), vec![0x1C]);
        assert_eq!(
            parse_chord("ctrl+shift+c").unwrap(),
            vec![CTRL, SHIFT, 0x2E]
        );
    }

    #[test]
    fn a_shifted_character_contributes_shift_once() {
        // Shift + 2 under `gb`; 0x28 is the apostrophe key, which carries `@`.
        assert_eq!(parse_chord("\"").unwrap(), vec![SHIFT, 0x03]);
        assert_eq!(parse_chord("shift+\"").unwrap(), vec![SHIFT, 0x03]);
        assert_eq!(parse_chord("@").unwrap(), vec![SHIFT, 0x28]);
        assert_eq!(parse_chord("|").unwrap(), vec![SHIFT, 0x56]);
        assert_eq!(parse_chord("+").unwrap(), vec![SHIFT, 0x0D]);
        assert_eq!(parse_chord("ctrl++").unwrap(), vec![CTRL, SHIFT, 0x0D]);
    }

    #[test]
    fn a_bad_chord_names_what_was_wrong() {
        assert!(parse_chord("meta+a")
            .unwrap_err()
            .to_string()
            .contains("not a modifier"));
        assert!(parse_chord("nosuchkey")
            .unwrap_err()
            .to_string()
            .contains("neither a named key"));
        assert!(parse_chord("ctrl+")
            .unwrap_err()
            .to_string()
            .contains("empty key"));
    }
}
