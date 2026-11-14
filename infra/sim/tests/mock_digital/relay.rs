//! Mock ATG relay (`atg-relay-01`) for the integration test.
//!
//! A transparent TCP proxy: the external-facing endpoint the fuel supplier
//! connects to, forwarding the byte stream to the ATG console on the OT network
//! (digital.md §2.5). It parses nothing — the Veeder-Root TLS bytes pass through
//! unchanged in both directions.

use std::net::SocketAddr;

use tokio::net::{TcpListener, TcpStream};

pub struct MockRelay {
    pub addr: SocketAddr,
}

impl MockRelay {
    /// Bind synchronously and spawn the accept loop. Each inbound connection is
    /// proxied bidirectionally to `backend_addr` (the ATG console).
    pub fn start(backend_addr: SocketAddr) -> MockRelay {
        let std_listener = std::net::TcpListener::bind("127.0.0.1:0").expect("mock relay bind");
        std_listener
            .set_nonblocking(true)
            .expect("mock relay set_nonblocking");
        let addr = std_listener.local_addr().unwrap();
        let listener = TcpListener::from_std(std_listener).expect("mock relay adopt listener");

        tokio::spawn(async move {
            while let Ok((mut inbound, _)) = listener.accept().await {
                tokio::spawn(async move {
                    if let Ok(mut outbound) = TcpStream::connect(backend_addr).await {
                        let _ = tokio::io::copy_bidirectional(&mut inbound, &mut outbound).await;
                    }
                });
            }
        });
        MockRelay { addr }
    }
}
