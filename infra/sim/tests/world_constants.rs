//! Do the generated world constants hold the values their names claim?
//!
//! `build.rs` reads `sim-constants.toml` line by line and does not distinguish
//! TOML sections: a lookup returns the first line beginning with the key,
//! wherever it appears (`deployment-config.md` §4.1). Were the reconciliation
//! user's credentials emitted under the operator's key names, the build would
//! compile the operator's values into the reconciliation user's constants. The
//! build would succeed, the agreement test in `deploy/tests/unit/` would pass —
//! the TOML file itself is correct — and the first symptom would be a failed
//! Grafana or workstation login on a deployed host.
//!
//! These tests read the compiled constants, which is the only place that
//! failure is visible before deployment.

use std::time::Duration;

use sim::constants::{
    OPERATOR_PASSWORD, OPERATOR_USERNAME, RECON_PASSWORD, RECON_USERNAME,
    RECON_WORKSTATION_PASSWORD, VESSELS,
};

/// The committed crate input `build.rs` compiles in.
const SIM_CONSTANTS_TOML: &str = include_str!("../sim-constants.toml");

/// The right-hand side of the `key = value` line for `key`, read independently
/// of `build.rs`.
fn toml_value(key: &str) -> &'static str {
    SIM_CONSTANTS_TOML
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once('=')?;
            (name.trim() == key).then(|| value.trim())
        })
        .unwrap_or_else(|| panic!("sim-constants.toml has no {key}"))
}

/// The roster is compiled from numbered keys (`deployment-config.md` §4.1). A
/// lookup that returned another tug's line, or a count read wrongly, would
/// compile a roster whose cards and IMO numbers no longer match the FMS card
/// table's.
#[test]
fn compiled_roster_matches_the_numbered_vessel_keys() {
    let count: usize = toml_value("vessel_count").parse().unwrap();
    assert_eq!(
        VESSELS.len(),
        count,
        "VESSELS holds a different number of tugs"
    );
    for (i, vessel) in VESSELS.iter().enumerate() {
        let n = i + 1;
        let field = |name: &str| toml_value(&format!("vessel_{n}_{name}"));
        assert_eq!(
            vessel.vessel_id.0.to_string(),
            field("imo_number"),
            "tug {n}"
        );
        assert_eq!(format!("{:?}", vessel.name), field("name"), "tug {n}");
        assert_eq!(
            vessel.rfid_card.0.to_string(),
            field("rfid_card"),
            "tug {n}"
        );
        // A decimal number of seconds, which a compressed scenario's division
        // need not leave whole, compiled to the nanosecond.
        let seconds: f64 = field("mean_time_away_s").parse().unwrap();
        assert_eq!(
            vessel.mean_time_away,
            Duration::from_nanos((seconds * 1e9).round() as u64),
            "tug {n}"
        );
    }
}

#[test]
fn recon_username_is_not_the_operators() {
    assert_ne!(RECON_USERNAME, OPERATOR_USERNAME);
}

#[test]
fn recon_passwords_are_not_the_operators() {
    assert_ne!(RECON_PASSWORD, OPERATOR_PASSWORD);
    assert_ne!(RECON_WORKSTATION_PASSWORD, OPERATOR_PASSWORD);
}

/// The two credentials authenticate against separate user stores — Grafana on
/// `hist-dmz-01` and the directory on `corp-it` (`recon.md` §7.1) — and
/// `world_schema.py` refuses them equal. If they compile equal, one lookup
/// returned the other's line.
#[test]
fn recon_grafana_and_workstation_passwords_differ() {
    assert_ne!(RECON_PASSWORD, RECON_WORKSTATION_PASSWORD);
}
