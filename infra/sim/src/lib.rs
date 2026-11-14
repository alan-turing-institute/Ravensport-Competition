//! RavensPort simulated-physical world.
//!
//! The sim owns physical ground truth and meets the digital world only at
//! the documented sim<->digital boundary. Each entity is a self-contained
//! module exposing a single `spawn()` function together with its public
//! message and return types; all channels are pre-created at the construction
//! site and both ends are passed in (see sim-impl.md).

pub mod actors;
pub mod appliances;
pub mod checkpoint;
pub mod clock;
/// Shared physical constants compiled in from `sim-constants.toml` at build
/// time (the committed projection of `world.yaml`; see `build.rs` and
/// sim-network.md §7). `world.yaml` is the single source; these are not
/// authored here.
pub mod constants {
    include!(concat!(env!("OUT_DIR"), "/world_constants.rs"));
}
pub mod coupled;
pub mod orchestrator;
pub mod physical;
pub mod serial;
pub mod shared;
pub mod stop;

mod supervision;
