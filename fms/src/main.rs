#[tokio::main]
async fn main() -> anyhow::Result<()> {
    fms::run().await
}
