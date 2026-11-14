use std::time::Duration;

#[derive(Debug, Clone)]
pub struct Config {
    pub database_url: String,
    pub listen_port: u16,
    pub tls_cert_path: String,
    pub tls_key_path: String,

    pub admin_username: String,
    pub admin_password_hash: String,

    pub ldap_uri: String,
    pub ldap_base_dn: String,

    /// RFID cards to enrol at startup if absent from the card_table, as
    /// `(card_number, label)` pairs. Empty when `FMS_SEED_CARDS` is unset.
    pub seed_cards: Vec<(i32, String)>,

    pub atg_host: String,
    pub atg_port: u16,
    pub atg_poll_interval: Duration,

    pub dispenser_host: String,
    pub dispenser_port: u16,
    pub modbus_poll_interval: Duration,

    pub vessel_rfid_host: String,
    pub vessel_rfid_tcp_port: u16,

    /// vmagent's import endpoint on `hist-01`, for ATG metrics (fms.md §5.4).
    pub vmagent_url: String,
    /// vlagent's JSON-line endpoint on `hist-01`, for domain events (fms.md §5.4).
    pub vlagent_jsonline_url: String,
    /// vlagent's syslog listener on `hist-01`, for authentication and card-table
    /// events (fms.md §4, §7.5). TLS over TCP, not UDP (RFC 5425): the messages
    /// are LF-terminated on a persistent connection (hist.md §5 path 3). The host
    /// is also the name hist-01's certificate is verified against.
    pub vlagent_syslog_host: String,
    pub vlagent_syslog_port: u16,

    pub session_timeout: Duration,
    pub tank_capacity_l: f64,
    pub cavitation_threshold_pct: f64,
    pub k_factor: f64,
    /// The fuel grade the tank holds (fms.md §6). A job for any other grade is
    /// refused (§7.4), so every transaction is of this grade.
    pub grade: String,
    /// Reference density of the configured grade at 15 °C (fms.md §5.2, §6),
    /// written onto every dispense transaction row.
    pub density_kg_per_l: f64,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        Ok(Self {
            database_url: require("FMS_DATABASE_URL")?,
            listen_port: env_u16("FMS_LISTEN_PORT", 443),
            tls_cert_path: env_str("FMS_TLS_CERT", "/etc/fms/tls/cert.pem"),
            tls_key_path: env_str("FMS_TLS_KEY", "/etc/fms/tls/key.pem"),

            admin_username: env_str("FMS_ADMIN_USERNAME", "fms-admin"),
            admin_password_hash: require("FMS_ADMIN_PASSWORD_HASH")?,

            ldap_uri: require("FMS_LDAP_URI")?,
            ldap_base_dn: env_str("FMS_LDAP_BASE_DN", "dc=ravensport,dc=local"),

            seed_cards: parse_seed_cards(&env_str("FMS_SEED_CARDS", ""))?,

            atg_host: require("FMS_ATG_HOST")?,
            atg_port: env_u16("FMS_ATG_PORT", 10001),
            atg_poll_interval: Duration::from_secs(env_u64("FMS_ATG_POLL_INTERVAL", 30)),

            dispenser_host: require("FMS_DISPENSER_HOST")?,
            dispenser_port: env_u16("FMS_DISPENSER_PORT", 502),
            modbus_poll_interval: Duration::from_secs(env_u64("FMS_MODBUS_POLL_INTERVAL", 5)),

            vessel_rfid_host: require("FMS_VESSEL_RFID_HOST")?,
            vessel_rfid_tcp_port: env_u16("FMS_VESSEL_RFID_TCP_PORT", 9001),

            vmagent_url: env_str("FMS_VMAGENT_URL", DEFAULT_VMAGENT_URL),
            vlagent_jsonline_url: env_str("FMS_VLAGENT_JSONLINE_URL", DEFAULT_VLAGENT_JSONLINE_URL),
            vlagent_syslog_host: env_str("FMS_VLAGENT_SYSLOG_HOST", "hist-01"),
            vlagent_syslog_port: env_u16("FMS_VLAGENT_SYSLOG_PORT", DEFAULT_VLAGENT_SYSLOG_PORT),

            session_timeout: Duration::from_secs(env_u64("FMS_SESSION_TIMEOUT", 1800)),
            tank_capacity_l: env_f64("FMS_TANK_CAPACITY_L", 50_000.0),
            cavitation_threshold_pct: env_f64("FMS_CAVITATION_THRESHOLD_PCT", 10.0),
            k_factor: env_f64("FMS_K_FACTOR", 100.0),
            grade: env_str("FMS_GRADE", "MGO"),
            density_kg_per_l: env_f64("FMS_DENSITY_KG_PER_L", 0.840),
        })
    }

    pub fn cavitation_volume_l(&self) -> f64 {
        self.tank_capacity_l * self.cavitation_threshold_pct / 100.0
    }
}

fn require(key: &str) -> anyhow::Result<String> {
    std::env::var(key).map_err(|_| anyhow::anyhow!("required env var {key} is not set"))
}

/// Parse `FMS_SEED_CARDS` into `(card_number, label)` pairs. The format is
/// `number:label` entries joined by commas (as rendered by render.py); the
/// first colon splits each entry, so a label may contain colons but not
/// commas. An unset or empty variable yields no cards. A malformed entry is a
/// deployment misconfiguration, so it aborts startup rather than silently
/// dropping a card the operator or a vessel then fails to authenticate with.
fn parse_seed_cards(raw: &str) -> anyhow::Result<Vec<(i32, String)>> {
    let mut cards = Vec::new();
    for entry in raw.split(',') {
        let entry = entry.trim();
        if entry.is_empty() {
            continue;
        }
        let (number, label) = entry.split_once(':').ok_or_else(|| {
            anyhow::anyhow!("FMS_SEED_CARDS entry {entry:?} has no ':' separator")
        })?;
        let card_number: i32 = number.trim().parse().map_err(|_| {
            anyhow::anyhow!("FMS_SEED_CARDS entry {entry:?} has a non-integer card number")
        })?;
        let label = label.trim();
        if label.is_empty() {
            anyhow::bail!("FMS_SEED_CARDS entry {entry:?} has an empty label");
        }
        cards.push((card_number, label.to_owned()));
    }
    Ok(cards)
}

// Defaults for the three pushes to hist-01, per fms.md §6. All three are TLS
// (fms.md §3), so a default that names plain HTTP or the plain syslog port
// would fail against hist-01 rather than merely differ from the deployment.
const DEFAULT_VMAGENT_URL: &str = "https://hist-01:8429/api/v1/import";
const DEFAULT_VLAGENT_JSONLINE_URL: &str = "https://hist-01:9429/insert/jsonline";
const DEFAULT_VLAGENT_SYSLOG_PORT: u16 = 6514;

fn env_str(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_owned())
}

fn env_u16(key: &str, default: u16) -> u16 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_u64(key: &str, default: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_f64(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn config_with(capacity: f64, threshold_pct: f64) -> Config {
        Config {
            database_url: String::new(),
            listen_port: 443,
            tls_cert_path: String::new(),
            tls_key_path: String::new(),
            admin_username: String::new(),
            admin_password_hash: String::new(),
            ldap_uri: String::new(),
            ldap_base_dn: String::new(),
            seed_cards: Vec::new(),
            atg_host: String::new(),
            atg_port: 10001,
            atg_poll_interval: Duration::from_secs(30),
            dispenser_host: String::new(),
            dispenser_port: 502,
            modbus_poll_interval: Duration::from_secs(5),
            vessel_rfid_host: String::new(),
            vessel_rfid_tcp_port: 9001,
            vmagent_url: String::new(),
            vlagent_jsonline_url: String::new(),
            vlagent_syslog_host: String::new(),
            vlagent_syslog_port: 6514,
            session_timeout: Duration::from_secs(1800),
            tank_capacity_l: capacity,
            cavitation_threshold_pct: threshold_pct,
            k_factor: 100.0,
            grade: "MGO".to_owned(),
            density_kg_per_l: 0.840,
        }
    }

    #[test]
    fn log_server_defaults_match_the_specified_tls_endpoints() {
        // fms.md §6's defaults for FMS_VMAGENT_URL, FMS_VLAGENT_JSONLINE_URL and
        // FMS_VLAGENT_SYSLOG_PORT. 6514 is the IANA-registered syslog-over-TLS
        // port (RFC 5425); 514 is the plain one.
        assert_eq!(DEFAULT_VMAGENT_URL, "https://hist-01:8429/api/v1/import");
        assert_eq!(
            DEFAULT_VLAGENT_JSONLINE_URL,
            "https://hist-01:9429/insert/jsonline"
        );
        assert_eq!(DEFAULT_VLAGENT_SYSLOG_PORT, 6514);
    }

    #[test]
    fn cavitation_volume_l_standard() {
        let c = config_with(50_000.0, 10.0);
        assert_eq!(c.cavitation_volume_l(), 5_000.0);
    }

    #[test]
    fn cavitation_volume_l_zero_threshold() {
        let c = config_with(50_000.0, 0.0);
        assert_eq!(c.cavitation_volume_l(), 0.0);
    }

    #[test]
    fn parse_seed_cards_empty_yields_no_cards() {
        assert!(parse_seed_cards("").unwrap().is_empty());
        assert!(parse_seed_cards("   ").unwrap().is_empty());
    }

    #[test]
    fn parse_seed_cards_parses_pairs_and_preserves_label_spaces() {
        let cards = parse_seed_cards("1000002:jrenard,1000001:1999997").unwrap();
        assert_eq!(
            cards,
            vec![
                (1000002, "jrenard".to_owned()),
                (1000001, "1999997".to_owned()),
            ]
        );
    }

    #[test]
    fn parse_seed_cards_allows_colon_in_label_and_skips_trailing_comma() {
        // split_once(':') means only the first colon separates number from
        // label, so a label may itself contain a colon.
        let cards = parse_seed_cards("42:MV Foo: Bar,").unwrap();
        assert_eq!(cards, vec![(42, "MV Foo: Bar".to_owned())]);
    }

    #[test]
    fn parse_seed_cards_rejects_malformed_entries() {
        assert!(parse_seed_cards("1000002").is_err()); // no separator
        assert!(parse_seed_cards("abc:label").is_err()); // non-integer number
        assert!(parse_seed_cards("1000002:").is_err()); // empty label
    }
}
