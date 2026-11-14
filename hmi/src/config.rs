use std::net::SocketAddr;

pub struct Config {
    // HAProxy terminates TLS externally; the HMI binary itself has no listener-side
    // certificate. ca_cert_path is for the outbound client leg to the FMS (below).
    pub listen_addr: SocketAddr,
    pub fms_url: String,
    pub ca_cert_path: String,
    pub operator_rfid_addr: SocketAddr,
    pub challenge_timeout_secs: u64,
}

impl Config {
    pub fn from_env() -> anyhow::Result<Self> {
        Ok(Self {
            // HAProxy listens on 443 externally and forwards here on plain HTTP.
            listen_addr: std::env::var("HMI_LISTEN_ADDR")
                .unwrap_or_else(|_| "127.0.0.1:8080".to_string())
                .parse()?,
            fms_url: std::env::var("HMI_FMS_URL").unwrap_or_else(|_| "https://fms-01".to_string()),
            ca_cert_path: std::env::var("HMI_CA_CERT_PATH")
                .unwrap_or_else(|_| "/etc/hmi/tls/ca.pem".to_string()),
            operator_rfid_addr: std::env::var("HMI_OPERATOR_RFID_ADDR")
                .unwrap_or_else(|_| "10.40.0.54:9002".to_string())
                .parse()?,
            challenge_timeout_secs: std::env::var("HMI_CHALLENGE_TIMEOUT_SECS")
                .unwrap_or_else(|_| "30".to_string())
                .parse()?,
        })
    }
}
