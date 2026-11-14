//! The harness's supervision of the FMS's spawned tasks.
//!
//! The FMS's pollers, pushers and HTTP server are spawned tasks that run for
//! the whole test. A task that panics does not fail the test by itself: tokio
//! catches the panic and the test continues without that task. `full_cycle.rs`
//! therefore awaits `BackgroundTasks::await_first_failure` alongside the sim's
//! `SimHandles::await_first_failure`.

use std::future::pending;
use std::time::Duration;

use sim_hmi_fms::BackgroundTasks;

#[tokio::test]
async fn a_panicking_task_is_reported_with_its_name_and_panic_message() {
    let mut tasks = BackgroundTasks::default();
    tasks.spawn("fms atg poller", pending());
    // Printed by the panic hook under `--nocapture`; worded so that it cannot be
    // mistaken for a real failure in a passing run.
    tasks.spawn("fms syslog pusher", async {
        panic!("deliberate panic from the supervision test")
    });

    let cause = tokio::time::timeout(Duration::from_secs(5), tasks.await_first_failure())
        .await
        .expect("a panicked task was not reported");
    assert!(cause.contains("fms syslog pusher"), "{cause}");
    assert!(
        cause.contains("deliberate panic from the supervision test"),
        "{cause}"
    );
}

#[tokio::test]
async fn a_task_that_returns_is_reported() {
    // A supervised task is expected to run for the whole test, so returning is
    // also a failure: `syslog::run`, for example, logs an error and returns
    // when its host is not a valid TLS server name.
    let mut tasks = BackgroundTasks::default();
    tasks.spawn("fms atg poller", pending());
    tasks.spawn("fms syslog pusher", async {});

    let cause = tokio::time::timeout(Duration::from_secs(5), tasks.await_first_failure())
        .await
        .expect("a task that returned was not reported");
    assert!(cause.contains("fms syslog pusher"), "{cause}");
    assert!(cause.contains("exited"), "{cause}");
}

#[tokio::test]
async fn nothing_is_reported_while_every_task_runs() {
    let mut tasks = BackgroundTasks::default();
    tasks.spawn("fms atg poller", pending());
    tasks.spawn("fms syslog pusher", pending());

    let reported =
        tokio::time::timeout(Duration::from_millis(200), tasks.await_first_failure()).await;
    assert!(
        reported.is_err(),
        "a failure was reported while every task was running: {reported:?}"
    );
}
