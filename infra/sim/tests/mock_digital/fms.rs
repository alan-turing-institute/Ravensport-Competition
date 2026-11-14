//! Mock FMS (`fms-01`) for the integration test.
//!
//! A single Tokio task that owns job state and reacts to [`FmsCommand`]s from
//! the HMI. It is the active controller of the sim: a Modbus client to the
//! dispenser, a Veeder-Root TLS client to the ATG console, and a reader of the
//! vessel RFID serial port — all over the real transport.
//!
//! It is a faithful-enough stand-in for the real FMS we will build, not a
//! shortcut: a dispense genuinely drives the dispenser's preset/enable
//! registers and reads back the transaction totaliser to judge completion.

use std::collections::HashMap;
use std::net::SocketAddr;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_modbus::client::tcp;
use tokio_modbus::prelude::*;

use sim::serial::LoopbackHandle;

use super::{FmsCommand, JobStatus, Totaliser};

// Dispenser register map (dispenser-modbus-tcp-spec.md §8).
const PRESET_REGISTER: u16 = 0x0001;
const ENABLE_REGISTER: u16 = 0x0002;
const TRANSACTION_HI_REGISTER: u16 = 0x0005;

const ETX: u8 = 0x03;

/// The dispense start this mock reports for every job.
const MOCK_OPENED_AT: &str = "2025-01-01T00:00:00.5Z";

struct Job {
    status: String,
    /// The dispense start, `None` until `StartDispense` (fms.md §7.4). A fixed
    /// instant: this mock holds no clock.
    opened_at: Option<String>,
    /// Transaction pulse count at which a dispense is complete (0 for fills).
    target_pulses: u32,
}

/// Spawn the mock FMS task.
///
/// - `vessel_rfid`: the read end of the vessel RFID reader's serial line.
/// - `dispenser_addr`: the sim dispenser's Modbus TCP address.
/// - `atg_addr`: the sim ATG console's Veeder-Root TLS address.
/// - `k_factor`: dispenser meter pulses per litre (to convert totaliser pulses).
pub fn spawn(
    mut command_rx: mpsc::Receiver<FmsCommand>,
    vessel_rfid: LoopbackHandle,
    dispenser_addr: SocketAddr,
    atg_addr: SocketAddr,
    k_factor: f64,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut jobs: HashMap<String, Job> = HashMap::new();
        let mut next_id: u32 = 1;
        // The singleton live job the HMI's `GET /api/jobs/current` resolves to.
        // Jobs run strictly sequentially, so the most recently created job is
        // the current one.
        let mut current_job: Option<String> = None;

        while let Some(command) = command_rx.recv().await {
            match command {
                FmsCommand::Login { reply, .. } => {
                    let _ = reply.send("chal-1".to_string());
                }
                FmsCommand::ValidateCard { reply, .. } => {
                    let _ = reply.send("token-1".to_string());
                }
                FmsCommand::CreateRefuelJob { reply, .. } => {
                    let job_id = format!("job-{next_id}");
                    next_id += 1;
                    jobs.insert(
                        job_id.clone(),
                        Job {
                            status: "pending_vessel_rfid".to_string(),
                            opened_at: None,
                            target_pulses: 0,
                        },
                    );
                    current_job = Some(job_id.clone());
                    let _ = reply.send(job_id);
                }
                FmsCommand::GetJobStatus { reply } => {
                    // The vessel tag is validated once a card has been presented
                    // on the RFID serial line.
                    let card_present = !vessel_rfid.bytes().is_empty();
                    let status = current_job.as_ref().and_then(|job_id| {
                        jobs.get_mut(job_id).map(|job| {
                            if job.status == "pending_vessel_rfid" && card_present {
                                job.status = "rfid_validated".to_string();
                            }
                            JobStatus {
                                job_id: job_id.clone(),
                                status: job.status.clone(),
                                opened_at: job.opened_at.clone(),
                            }
                        })
                    });
                    let _ = reply.send(status);
                }
                FmsCommand::StartDispense {
                    job_id,
                    preset_volume_litres,
                    reply,
                } => {
                    let preset_x10 = (preset_volume_litres * 10.0).round() as u16;
                    let target_pulses = (preset_volume_litres * k_factor).round() as u32;
                    let mut ctx = tcp::connect(dispenser_addr)
                        .await
                        .expect("mock FMS: dispenser Modbus connect");
                    ctx.write_single_register(PRESET_REGISTER, preset_x10)
                        .await
                        .expect("mock FMS: Modbus write preset (transport)")
                        .expect("mock FMS: Modbus write preset (exception)");
                    ctx.write_single_register(ENABLE_REGISTER, 1)
                        .await
                        .expect("mock FMS: Modbus write enable (transport)")
                        .expect("mock FMS: Modbus write enable (exception)");
                    if let Some(job) = jobs.get_mut(&job_id) {
                        job.status = "dispensing".to_string();
                        job.opened_at = Some(MOCK_OPENED_AT.to_string());
                        job.target_pulses = target_pulses;
                    }
                    let _ = reply.send(());
                }
                FmsCommand::GetTotaliser { reply } => {
                    let totaliser = match current_job.clone() {
                        Some(job_id) => {
                            let target = jobs.get(&job_id).map_or(0, |job| job.target_pulses);
                            let mut ctx = tcp::connect(dispenser_addr)
                                .await
                                .expect("mock FMS: dispenser Modbus connect");
                            let registers = ctx
                                .read_holding_registers(TRANSACTION_HI_REGISTER, 2)
                                .await
                                .expect("mock FMS: Modbus read totaliser (transport)")
                                .expect("mock FMS: Modbus read totaliser (exception)");
                            let pulses = (u32::from(registers[0]) << 16) | u32::from(registers[1]);
                            let dispensed_litres = f64::from(pulses) / k_factor;
                            let status = if target > 0 && pulses >= target {
                                "complete"
                            } else {
                                "dispensing"
                            };
                            Some(Totaliser {
                                job_id,
                                dispensed_litres,
                                status: status.to_string(),
                            })
                        }
                        None => None,
                    };
                    let _ = reply.send(totaliser);
                }
                FmsCommand::EnterBdn { reply, .. } => {
                    let _ = reply.send(());
                }
                FmsCommand::GetTankLevel { reply } => {
                    let _ = reply.send(read_atg_volume(atg_addr).await);
                }
                FmsCommand::CreateFillJob { reply, .. } => {
                    let job_id = format!("job-{next_id}");
                    next_id += 1;
                    jobs.insert(
                        job_id.clone(),
                        Job {
                            status: "open".to_string(),
                            opened_at: None,
                            target_pulses: 0,
                        },
                    );
                    current_job = Some(job_id.clone());
                    let _ = reply.send(job_id);
                }
                FmsCommand::TranscribeTicket { reply, .. } => {
                    let _ = reply.send(());
                }
            }
        }
    })
}

/// Read the tank volume (i201 field 1) from the ATG console over TLS
/// (tls-protocol.md §6.2).
async fn read_atg_volume(atg_addr: SocketAddr) -> f64 {
    let mut stream = TcpStream::connect(atg_addr)
        .await
        .expect("mock FMS: ATG console connect");
    stream
        .write_all(b"\x01i20100")
        .await
        .expect("mock FMS: ATG i201 write");

    let mut response = Vec::new();
    loop {
        let mut byte = [0u8; 1];
        stream
            .read_exact(&mut byte)
            .await
            .expect("mock FMS: ATG response read");
        response.push(byte[0]);
        if byte[0] == ETX {
            break;
        }
    }

    // body = response[17..len-7]; Volume field = body[9..17] (8 hex chars).
    let end = response.len() - 7;
    let hex = &response[17..end][9..17];
    let text = std::str::from_utf8(hex).expect("mock FMS: ATG hex not UTF-8");
    let mut bytes = [0u8; 4];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16).expect("mock FMS: ATG bad hex");
    }
    f64::from(f32::from_be_bytes(bytes))
}
