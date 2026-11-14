use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::Mutex;
use uuid::Uuid;

pub struct PendingChallenge {
    pub username: String,
    pub password: String,
    expires_at: Instant,
}

#[derive(Clone)]
pub struct ChallengeStore {
    inner: Arc<Mutex<HashMap<String, PendingChallenge>>>,
    timeout: Duration,
}

impl ChallengeStore {
    pub fn new(timeout_secs: u64) -> Self {
        Self {
            inner: Arc::new(Mutex::new(HashMap::new())),
            timeout: Duration::from_secs(timeout_secs),
        }
    }

    pub async fn insert(&self, username: String, password: String) -> String {
        let id = Uuid::new_v4().to_string();
        self.inner.lock().await.insert(
            id.clone(),
            PendingChallenge {
                username,
                password,
                expires_at: Instant::now() + self.timeout,
            },
        );
        id
    }

    /// Remove and return the challenge if it exists and has not expired.
    pub async fn take(&self, id: &str) -> Option<PendingChallenge> {
        let mut map = self.inner.lock().await;
        match map.remove(id) {
            Some(c) if c.expires_at > Instant::now() => Some(c),
            _ => None,
        }
    }
}
