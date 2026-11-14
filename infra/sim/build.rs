//! Compiles the sim's shared physical constants into the binary from
//! `sim-constants.toml`, the committed projection of `world.yaml` (rendered by
//! `deploy/scripts/render.py`). This is the single-source wiring: the sim reads
//! no configuration file at runtime; these values are fixed at compile time,
//! sourced from `world.yaml` rather than authored as Rust literals
//! (sim-network.md §7). The committed TOML is kept in step with `world.yaml` by
//! `deploy/tests/unit/test_sim_constants_agreement.py`. The internal certificate
//! authority's root is compiled in the same way, from the committed `ca.pem`
//! that `deploy/scripts/build-ca.sh` writes.
//!
//! The committed TOML holds the values in operation. When `SIM_CONSTANTS` names
//! another file, such as the scaled copy that a compressed render writes to
//! `build/sim/sim-constants.toml`, the build reads that file in its place
//! (deployment-config.md §4.1, sim projection).

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn main() {
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR unset");
    println!("cargo:rerun-if-env-changed=SIM_CONSTANTS");
    let toml_path = match env::var_os("SIM_CONSTANTS") {
        Some(path) => PathBuf::from(path),
        None => Path::new(&manifest_dir).join("sim-constants.toml"),
    };
    println!("cargo:rerun-if-changed={}", toml_path.display());
    println!("cargo:rerun-if-changed=build.rs");

    let text = fs::read_to_string(&toml_path).unwrap_or_else(|e| {
        panic!(
            "build.rs: cannot read {}: {e}. Run `make render` in deploy/ to \
             generate it from world.yaml.",
            toml_path.display()
        )
    });

    // Only world.yaml's projection to the sim is sourced here (deployment-config.md
    // §3): the constants shared with the digital world, the fuel's properties
    // written onto the bunker delivery note, the flow rates, the overfill
    // interlock level, the fuel supplier's ordering, the refuel range, the
    // roster of tugs, the FMS session lifetime, the reconciliation
    // audit's period and settling delay, and the credentials the sim's actors
    // present. The sim-only constants remain literals in main.rs, so they are
    // deliberately not compiled in from the projection.
    //
    // value_of() ignores TOML sections and returns the first line beginning with
    // the key, so every key looked up here is unique across the whole file
    // (deployment-config.md §4.1) — hence recon_username rather than username.
    let tank_capacity_l = scalar(&text, "tank_capacity_l");
    let k_factor = scalar(&text, "k_factor");
    let fuel_grade = toml_string(&text, "grade");
    let density_kg_per_l = scalar(&text, "density_kg_per_l");
    let sulphur_pct_mm = scalar(&text, "sulphur_pct_mm");
    let flashpoint_c = scalar(&text, "flashpoint_c");
    // The scenario values lifted from main.rs's literals, under the sim's names
    // (deployment-config.md §4.1): the dispenser's flow rate is the tank's
    // outflow rate and the road tanker's pump rate its inflow rate (sim.md §1.1).
    let overfill_interlock_pct = scalar(&text, "overfill_interlock_pct");
    let outflow_rate_l_per_min = scalar(&text, "outflow_rate_l_per_min");
    let inflow_rate_l_per_min = scalar(&text, "inflow_rate_l_per_min");
    let reorder_threshold_l = scalar(&text, "reorder_threshold_l");
    let delivery_volume_l = scalar(&text, "delivery_volume_l");
    let lead_time_s = scalar(&text, "lead_time_s");
    let supplier_poll_interval_s = scalar(&text, "supplier_poll_interval_s");
    let refuel_volume_min_l = scalar(&text, "refuel_volume_min_l");
    let refuel_volume_max_l = scalar(&text, "refuel_volume_max_l");
    let operator_card = first_array_element(&text, "operator_cards");
    // The operator credentials the sim presents at the HMI login; the digital
    // world verifies them against LDAP seeded from the same world.yaml. The sim
    // is the authenticating client, so it holds the plaintext.
    let operator_username = toml_string(&text, "username");
    let operator_password = toml_string(&text, "password");
    // The roster, as numbered keys (deployment-config.md §4.1): each tug's IMO
    // number, which the dock operator enters on the job and the FMS matches
    // against the vessel card's label; its name, which the BDN carries
    // alongside the IMO number; its card; and its mean time away, from which the
    // tug arrival generator draws its times away (sim.md §6). Every key up to
    // vessel_count is read, so a tug missing a field fails the build rather than
    // shortening the roster.
    let vessels = roster(&text);
    // The FMS session lifetime, from login: the reconciliation user's
    // retrieval reaches back by it for the LOGIN that opened a session.
    let session_timeout_s = scalar(&text, "session_timeout_s");
    // The reconciliation audit's period, by its world.yaml name, and how far
    // into the following period each audit runs: the port's procedure.
    let audit_period = toml_string(&text, "audit_period");
    let settling_delay_s = scalar(&text, "settling_delay_s");
    // The scenario's timings in operation that a compressed scenario scales;
    // render.py projects each already scaled (sim.md §2.2).
    let dock_operator_poll_s = scalar(&text, "dock_operator_poll_s");
    let admin_check_interval_s = scalar(&text, "admin_check_interval_s");
    let boundary_margin_s = scalar(&text, "boundary_margin_s");
    // The reconciliation user's: one username for both systems, a password for
    // the log server's Grafana and another for the workstation's directory login
    // (recon.md §7.1, §7.2).
    let recon_username = toml_string(&text, "recon_username");
    let recon_password = toml_string(&text, "recon_password");
    let recon_workstation_password = toml_string(&text, "recon_workstation_password");
    // The systems administrator authenticates to admin-01's local account and
    // verifies its host key before presenting the reused administrator password.
    let systems_administrator_username = toml_string(&text, "systems_administrator_username");
    let systems_administrator_password = toml_string(&text, "systems_administrator_password");
    let admin_host_key = toml_string(&text, "admin_host_key");

    // `{:?}` on the parsed f64 yields a valid Rust float literal (e.g. `50000.0`).
    let tank_capacity_l: f64 = parse(&tank_capacity_l, "tank_capacity_l");
    let k_factor: f64 = parse(&k_factor, "k_factor");
    let density_kg_per_l: f64 = parse(&density_kg_per_l, "density_kg_per_l");
    let sulphur_pct_mm: f64 = parse(&sulphur_pct_mm, "sulphur_pct_mm");
    let flashpoint_c: f64 = parse(&flashpoint_c, "flashpoint_c");
    let overfill_interlock_pct: f64 = parse(&overfill_interlock_pct, "overfill_interlock_pct");
    let outflow_rate_l_per_min: f64 = parse(&outflow_rate_l_per_min, "outflow_rate_l_per_min");
    let inflow_rate_l_per_min: f64 = parse(&inflow_rate_l_per_min, "inflow_rate_l_per_min");
    let reorder_threshold_l: f64 = parse(&reorder_threshold_l, "reorder_threshold_l");
    let delivery_volume_l: f64 = parse(&delivery_volume_l, "delivery_volume_l");
    let lead_time = duration(&lead_time_s, "lead_time_s");
    let supplier_poll = duration(&supplier_poll_interval_s, "supplier_poll_interval_s");
    let refuel_volume_min_l: f64 = parse(&refuel_volume_min_l, "refuel_volume_min_l");
    let refuel_volume_max_l: f64 = parse(&refuel_volume_max_l, "refuel_volume_max_l");
    let operator_card: u32 = parse(&operator_card, "operator_cards");
    let session_timeout_s: u64 = parse(&session_timeout_s, "session_timeout_s");
    let settling_delay = duration(&settling_delay_s, "settling_delay_s");
    let dock_operator_poll = duration(&dock_operator_poll_s, "dock_operator_poll_s");
    let admin_check_interval = duration(&admin_check_interval_s, "admin_check_interval_s");
    let boundary_margin = duration(&boundary_margin_s, "boundary_margin_s");

    // The internal certificate authority's root, by which the reconciliation
    // user verifies recon-01's remote-desktop certificate. Not a world.yaml
    // value but derived from it: deploy/scripts/build-ca.sh writes the committed
    // copy when it signs the root (deployment-config.md §3).
    let ca_root_path = Path::new(&manifest_dir).join("ca.pem");
    println!("cargo:rerun-if-changed={}", ca_root_path.display());
    if !ca_root_path.is_file() {
        panic!(
            "build.rs: cannot read {}. Run `make build-ca` in deploy/ to write it \
             from the internal certificate authority.",
            ca_root_path.display()
        );
    }
    let ca_root_path = ca_root_path.to_string_lossy();

    // `{:?}` on the String re-emits it as a correctly escaped Rust string
    // literal, so any special characters in the credential are handled safely.
    let generated = format!(
        "// @generated by build.rs from sim-constants.toml — do not edit.\n\
         pub const TANK_CAPACITY_L: f64 = {tank_capacity_l:?};\n\
         pub const FLOW_METER_K_FACTOR: f64 = {k_factor:?};\n\
         pub const FUEL_GRADE: &str = {fuel_grade:?};\n\
         pub const DENSITY_KG_PER_L: f64 = {density_kg_per_l:?};\n\
         pub const SULPHUR_PCT_MM: f64 = {sulphur_pct_mm:?};\n\
         pub const FLASHPOINT_C: f64 = {flashpoint_c:?};\n\
         pub const OVERFILL_INTERLOCK_PCT: f64 = {overfill_interlock_pct:?};\n\
         pub const OUTFLOW_RATE_L_PER_MIN: f64 = {outflow_rate_l_per_min:?};\n\
         pub const INFLOW_RATE_L_PER_MIN: f64 = {inflow_rate_l_per_min:?};\n\
         pub const REORDER_THRESHOLD_L: f64 = {reorder_threshold_l:?};\n\
         pub const DELIVERY_VOLUME_L: f64 = {delivery_volume_l:?};\n\
         pub const LEAD_TIME: core::time::Duration = {lead_time};\n\
         pub const SUPPLIER_POLL_INTERVAL: core::time::Duration = {supplier_poll};\n\
         pub const REFUEL_VOLUME_MIN_L: f64 = {refuel_volume_min_l:?};\n\
         pub const REFUEL_VOLUME_MAX_L: f64 = {refuel_volume_max_l:?};\n\
         pub const OPERATOR_CARD: u32 = {operator_card};\n\
         pub const OPERATOR_USERNAME: &str = {operator_username:?};\n\
         pub const OPERATOR_PASSWORD: &str = {operator_password:?};\n\
         pub const VESSELS: &[crate::physical::tug_arrival_generator::Vessel] = &[\n{vessels}];\n\
         pub const SESSION_TIMEOUT_S: u64 = {session_timeout_s};\n\
         pub const AUDIT_PERIOD: &str = {audit_period:?};\n\
         pub const SETTLING_DELAY: core::time::Duration = {settling_delay};\n\
         pub const DOCK_OPERATOR_POLL_INTERVAL: core::time::Duration = {dock_operator_poll};\n\
         pub const ADMIN_CHECK_INTERVAL: core::time::Duration = {admin_check_interval};\n\
         pub const BOUNDARY_MARGIN: core::time::Duration = {boundary_margin};\n\
         pub const RECON_USERNAME: &str = {recon_username:?};\n\
         pub const RECON_PASSWORD: &str = {recon_password:?};\n\
         pub const RECON_WORKSTATION_PASSWORD: &str = {recon_workstation_password:?};\n\
         pub const SYSTEMS_ADMINISTRATOR_USERNAME: &str = {systems_administrator_username:?};\n\
         pub const SYSTEMS_ADMINISTRATOR_PASSWORD: &str = {systems_administrator_password:?};\n\
         pub const ADMIN_HOST_KEY: &str = {admin_host_key:?};\n\
         pub const CA_ROOT_PEM: &[u8] = include_bytes!({ca_root_path:?});\n"
    );

    let out_dir = env::var("OUT_DIR").expect("OUT_DIR unset");
    fs::write(Path::new(&out_dir).join("world_constants.rs"), generated)
        .expect("build.rs: cannot write world_constants.rs");
}

/// The right-hand side of the sole `key = value` line, trimmed. The TOML is
/// machine-generated with a fixed, flat layout, so a line scan is sufficient
/// and avoids a build-dependency on a TOML parser.
fn scalar(text: &str, key: &str) -> String {
    value_of(text, key)
}

/// The first element of a `key = [a, b, ...]` array. The sim models a single
/// operator, so it consumes the first card.
fn first_array_element(text: &str, key: &str) -> String {
    let raw = value_of(text, key);
    let inner = raw
        .strip_prefix('[')
        .and_then(|s| s.strip_suffix(']'))
        .unwrap_or_else(|| panic!("build.rs: `{key}` is not a TOML array: {raw}"));
    inner
        .split(',')
        .next()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| panic!("build.rs: `{key}` array is empty"))
}

/// The content of a `key = "value"` double-quoted TOML string, with the
/// surrounding quotes removed. The generated TOML uses only simple basic
/// strings (render.py rejects a double quote in the value), so stripping the
/// outer quotes recovers the raw content without a TOML parser.
fn toml_string(text: &str, key: &str) -> String {
    let raw = value_of(text, key);
    raw.strip_prefix('"')
        .and_then(|s| s.strip_suffix('"'))
        .unwrap_or_else(|| panic!("build.rs: `{key}` is not a quoted TOML string: {raw}"))
        .to_string()
}

/// The roster's `Vessel` entries, one per tug from 1 to `vessel_count`, as Rust
/// source for the body of the `VESSELS` slice.
fn roster(text: &str) -> String {
    let count: usize = parse(&scalar(text, "vessel_count"), "vessel_count");
    let mut entries = String::new();
    for n in 1..=count {
        let key = |field: &str| format!("vessel_{n}_{field}");
        let imo_number: u32 = parse(&scalar(text, &key("imo_number")), &key("imo_number"));
        let name = toml_string(text, &key("name"));
        let rfid_card: u32 = parse(&scalar(text, &key("rfid_card")), &key("rfid_card"));
        let mean_time_away = duration(
            &scalar(text, &key("mean_time_away_s")),
            &key("mean_time_away_s"),
        );
        entries.push_str(&format!(
            "    crate::physical::tug_arrival_generator::Vessel {{\n\
             \x20       vessel_id: crate::actors::tug_engineer::VesselId({imo_number}),\n\
             \x20       name: {name:?},\n\
             \x20       rfid_card: crate::shared::RfidCardNumber({rfid_card}),\n\
             \x20       mean_time_away: {mean_time_away},\n\
             \x20   }},\n"
        ));
    }
    entries
}

fn value_of(text: &str, key: &str) -> String {
    for line in text.lines() {
        let line = line.trim();
        if let Some(rest) = line.strip_prefix(key) {
            let rest = rest.trim_start();
            if let Some(value) = rest.strip_prefix('=') {
                return value.trim().to_string();
            }
        }
    }
    panic!("build.rs: key `{key}` not found in sim-constants.toml");
}

fn parse<T: std::str::FromStr>(value: &str, key: &str) -> T
where
    T::Err: std::fmt::Display,
{
    value
        .parse()
        .unwrap_or_else(|e| panic!("build.rs: cannot parse `{key}` value {value:?}: {e}"))
}

/// A duration in seconds from `sim-constants.toml`, written as an exact
/// `Duration` constant. render.py projects a scaled duration as a decimal number
/// of seconds, which need not be whole.
fn duration(value: &str, key: &str) -> String {
    let seconds: f64 = parse(value, key);
    if !(seconds.is_finite() && seconds > 0.0) {
        panic!("build.rs: `{key}` is not a positive number of seconds: {value}");
    }
    let nanos = (seconds * 1e9).round() as u64;
    format!("core::time::Duration::from_nanos({nanos})")
}
