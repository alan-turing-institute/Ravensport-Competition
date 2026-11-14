use std::net::SocketAddr;
use std::sync::Arc;

use axum::Router;
use reqwest::Client;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;

/// A mock FMS HTTP server. Tests register routes and the server binds on a
/// random port. `base_url()` returns the address for HMI configuration.
pub struct MockFms {
    pub addr: SocketAddr,
}

impl MockFms {
    pub async fn start(router: Router) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        Self { addr }
    }

    pub fn base_url(&self) -> String {
        format!("http://{}", self.addr)
    }
}

/// A mock operator RFID reader. Holds one card number that it sends as
/// `"{card}\n"` to the first client that connects.
pub struct MockRfid {
    pub addr: SocketAddr,
}

impl MockRfid {
    pub async fn start(card: u32) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            stream
                .write_all(format!("{card}\n").as_bytes())
                .await
                .unwrap();
        });
        Self { addr }
    }
}

/// Spawn a real HMI server against the provided mock FMS and RFID addresses.
/// Returns the HMI base URL and a reqwest client.
pub async fn spawn_hmi(fms_url: &str, rfid_addr: SocketAddr) -> (String, Client) {
    use hmi::challenge::ChallengeStore;
    use hmi::fms::FmsClient;
    use hmi::rfid::RfidReader;
    use hmi::routes;
    use hmi::state::AppState;

    // MockFms serves plain HTTP, so no CA is needed for these tests.
    let fms = Arc::new(FmsClient::new(fms_url.to_string(), None).unwrap());
    // Start the background reader, as the HMI binary does, so the persistent
    // connection is established and card frames are delivered to `read_card`.
    let rfid = RfidReader::new(rfid_addr);
    rfid.start();
    let state = AppState {
        fms,
        challenges: ChallengeStore::new(30),
        rfid,
    };

    let router = routes::router(state);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });

    let client = Client::new();
    (format!("http://{addr}"), client)
}
