//! Private implementation of the bund leak sensor. Not visible to other modules.

use tokio::sync::watch;

use super::BundReading;

pub(super) fn spawn(
    mut bund_wet_rx: watch::Receiver<bool>,
    bund_tx: watch::Sender<BundReading>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            bund_wet_rx
                .changed()
                .await
                .expect("bund_leak_sensor: physical-model bund_wet channel closed");
            let wet = *bund_wet_rx.borrow();
            bund_tx
                .send(BundReading { wet })
                .expect("bund_leak_sensor: ATG console reading channel closed");
        }
    })
}
