//! Scripted actors. Each actor has a digital interface (real protocol
//! requests to the digital world) and a physical interface (state mutations
//! on sim objects).

pub mod dock_operator;
pub mod fuel_supplier;
pub mod reconciliation_user;
pub mod road_tanker_driver;
pub mod systems_administrator;
pub mod tug_engineer;
