//! Task supervision helpers (see sim-impl.md "Error handling and task
//! supervision").
//!
//! Every entity is meant to run for the whole sim, so a supervised task that
//! finishes — for any reason other than teardown cancellation — is an error.
//! A panic is re-raised in the supervising (parent) task with its original
//! payload via [`std::panic::resume_unwind`], so it propagates up to the
//! binary's top-level orchestrator, which attributes it to the entity.

use tokio::task::{JoinError, JoinSet};

/// Re-raise a panic from a finished child task. A normal (`Ok`) finish is the
/// expected end of, for example, a per-connection handler whose client
/// disconnected, so it is ignored; a cancellation (teardown) is likewise
/// ignored. Used in an accept loop's `select!` arm that drains a child
/// `JoinSet` of per-connection handlers.
pub(crate) fn propagate_connection_panic(joined: Result<(), JoinError>) {
    if let Err(error) = joined {
        if error.is_panic() {
            std::panic::resume_unwind(error.into_panic());
        }
        // Cancellation only occurs during teardown — nothing to do.
    }
}

/// Supervise a set of long-lived sibling tasks. The first to finish for any
/// reason other than cancellation is fatal: a panic is propagated with its
/// original payload, and an unexpected normal exit panics naming `context`.
pub(crate) async fn supervise(mut set: JoinSet<()>, context: &'static str) {
    // The first sibling to finish is decisive: an empty set or a cancellation
    // (teardown) ends supervision quietly; anything else is fatal.
    if let Some(joined) = set.join_next().await {
        match joined {
            Err(error) if error.is_panic() => std::panic::resume_unwind(error.into_panic()),
            Err(_) => {} // cancelled: the set is being torn down
            Ok(()) => panic!("{context}: a supervised task exited unexpectedly"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Join the single task in `set`, returning its result.
    async fn join_one(set: &mut JoinSet<()>) -> Result<(), JoinError> {
        set.join_next().await.expect("the set has one task")
    }

    #[tokio::test]
    #[should_panic(expected = "boom from child")]
    async fn propagate_re_raises_a_child_panic() {
        let mut set: JoinSet<()> = JoinSet::new();
        set.spawn(async { panic!("boom from child") });
        let joined = join_one(&mut set).await;
        propagate_connection_panic(joined);
    }

    #[tokio::test]
    async fn propagate_ignores_a_normal_finish() {
        let mut set: JoinSet<()> = JoinSet::new();
        set.spawn(async {});
        let joined = join_one(&mut set).await;
        propagate_connection_panic(joined); // a clean finish must not panic
    }

    #[tokio::test]
    async fn propagate_ignores_a_cancelled_task() {
        let mut set: JoinSet<()> = JoinSet::new();
        set.spawn(std::future::pending::<()>());
        set.abort_all();
        let joined = join_one(&mut set).await;
        assert!(joined.is_err());
        propagate_connection_panic(joined); // cancellation (teardown) must not panic
    }

    #[tokio::test]
    #[should_panic(expected = "boom from sibling")]
    async fn supervise_propagates_a_panic() {
        let mut set: JoinSet<()> = JoinSet::new();
        set.spawn(async { panic!("boom from sibling") });
        supervise(set, "test").await;
    }

    #[tokio::test]
    #[should_panic(expected = "exited unexpectedly")]
    async fn supervise_rejects_an_unexpected_exit() {
        let mut set: JoinSet<()> = JoinSet::new();
        set.spawn(async {});
        supervise(set, "test").await;
    }

    #[tokio::test]
    async fn supervise_returns_quietly_on_empty_set() {
        let set: JoinSet<()> = JoinSet::new();
        supervise(set, "test").await; // nothing to supervise
    }
}
