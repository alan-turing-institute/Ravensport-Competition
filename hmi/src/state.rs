use std::sync::Arc;

use crate::challenge::ChallengeStore;
use crate::fms::FmsClient;
use crate::rfid::RfidReader;

#[derive(Clone)]
pub struct AppState {
    pub fms: Arc<FmsClient>,
    pub challenges: ChallengeStore,
    pub rfid: RfidReader,
}
