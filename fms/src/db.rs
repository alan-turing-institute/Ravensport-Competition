use sqlx::PgPool;
use uuid::Uuid;

pub async fn connect(database_url: &str) -> anyhow::Result<PgPool> {
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(10)
        .connect(database_url)
        .await?;
    Ok(pool)
}

pub async fn run_migrations(pool: &PgPool) -> anyhow::Result<()> {
    sqlx::migrate!("./migrations").run(pool).await?;
    Ok(())
}

/// Enrol each seed card whose `card_number` has no row in card_table yet,
/// returning how many were inserted. Cards already present — whether their most
/// recent event is an enrol or a revoke — are left untouched, so a card revoked
/// at runtime stays revoked across a restart rather than being resurrected. The
/// insert is guarded by `NOT EXISTS` in the same statement, making it atomic and
/// idempotent: re-running enrols only the genuinely-new cards, so a restart (or a
/// crash midway) converges without duplicating the append-only enrol log.
///
/// This runs once at startup and uses a runtime-checked query, so it adds
/// nothing to the compile-time `.sqlx` cache the checked macros depend on.
pub async fn seed_cards(pool: &PgPool, cards: &[(i32, String)]) -> anyhow::Result<usize> {
    let mut inserted = 0;
    for (card_number, label) in cards {
        let result = sqlx::query(
            "INSERT INTO card_table (card_id, card_number, label, active, action)
             SELECT $1, $2, $3, true, 'enrol'
             WHERE NOT EXISTS (SELECT 1 FROM card_table WHERE card_number = $2)",
        )
        .bind(Uuid::new_v4())
        .bind(card_number)
        .bind(label)
        .execute(pool)
        .await?;
        if result.rows_affected() > 0 {
            inserted += 1;
        }
    }
    Ok(inserted)
}

/// Look up the most recent card_table row for a given card_number and return
/// whether the card is currently active and enrolled to `holder`. A card's
/// label names its holder (fms.md §8.2): the directory username for an
/// operator's card (digital.md §5.3), the vessel's IMO number for a vessel tag.
/// Login passes the username and the vessel RFID gate the job's vessel, so a card
/// enrolled to anyone else fails the check even while active.
pub async fn is_card_active_for(
    pool: &PgPool,
    card_number: i32,
    holder: &str,
) -> sqlx::Result<bool> {
    let row = sqlx::query!(
        r#"
        SELECT active, label
        FROM card_table
        WHERE card_number = $1
        ORDER BY enrolled_at DESC
        LIMIT 1
        "#,
        card_number
    )
    .fetch_optional(pool)
    .await?;

    Ok(row.is_some_and(|r| r.active && r.label == holder))
}
