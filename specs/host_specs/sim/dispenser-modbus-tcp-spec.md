# Dispenser Modbus TCP Server — Specification and Justification

Normative specification of the Ravensport testbed dispenser's Modbus TCP
**server** (slave): the transport, addressing, function codes, exception
behaviour, data encoding, and register map it presents to the Fuel Management
System (FMS). The goal is wire-level fidelity — a real Modbus client must be
unable to tell it is talking to a simulation rather than a real fuel-dispense
controller — so every choice below is justified against the Modbus standard and
a real reference device.

This document is the single source of truth for the dispenser's Modbus
behaviour; sim.md, sim-impl.md, sim-tests.md and digital.md reference it.

## Reference devices

- **MODBUS Application Protocol Specification v1.1b3** (Modbus Organization) —
  the protocol standard: PDU structure, 0-based addressing, function codes,
  exception codes.
- **Toptech Systems MultiLoad II** (*Communications Guide*, Part #6077, firmware
  3/4.31.40, Nov 2018) — a real fuel-handling **preset/batch controller** (bulk
  terminal truck/rail loading automation) whose Modbus-TCP/IP interface exposes
  preset-volume, totaliser, and status registers, with a published register map.
  It is used here as the reference for the **Modbus interface conventions** —
  addressing base, register data-type encodings, function codes, exception
  subcodes, and unit-identifier handling — *not* as the dispenser's product
  identity. Cited below as *"MultiLoad II"*.

**On the choice of reference.** Our dispenser (narrative §4.3) is a dockside
dispenser pump, not a terminal loading rack. Real dockside/forecourt dispensers
do not natively speak Modbus TCP — they use vendor-proprietary serial protocols
(Gilbarco DART, OPW Petro-Net) — so the testbed dispenser exposing Modbus TCP is
itself a documented stand-in (digital.md §9, deviation 6). Given that decision,
the realism question is *how a real fuel device does Modbus*, and the MultiLoad
II — a genuine fuel device that speaks Modbus TCP with preset/totaliser/status
registers — is the authentic template. The testbed dispenser therefore adopts
MultiLoad II's **conventions** while exposing its own **minimal register map**
(§8: one meter, preset + enable + two totalisers + status); it does not
reproduce MultiLoad's specific register addresses or its full functionality.

**Contents:**
1. [Transport](#transport)
2. [MBAP header](#mbap)
3. [Addressing model](#addressing)
4. [Function codes](#codes)
5. [Exception responses](#exceptions)
6. [Unit identifier](#unit)
7. [Data encoding](#encoding)
8. [Register map](#map)
9. [Behaviours](#behaviours)
10. [Realism decisions](#decisions)
11. [Sources](#sources)

---

## 1. Transport<a id='transport'></a>

The dispenser is a Modbus TCP **server** on TCP port **502** (the registered
Modbus port; MultiLoad II §1.6 likewise sends "all MODBUS/TCP ADU … to
registered port 502"). The FMS is the only client. Connections are persistent
and served independently — a real FMS polls repeatedly over one connection.

---

## 2. MBAP header<a id='mbap'></a>

Each request/response is a 7-byte MBAP header + PDU (`FN` + data). Fields,
matching MultiLoad II §1.6 and the standard:

| Field | Bytes | Notes |
|---|---|---|
| Transaction Identifier | 2, big-endian | Request/response pairing; the server **copies it into the response from the received request**. |
| Protocol Identifier | 2, big-endian | `0x0000` = Modbus. |
| Length | 2, big-endian | Byte count of the following fields = Unit Identifier (1) + PDU. |
| Unit Identifier | 1 | See §6. |

All fields are big-endian (MultiLoad II §1.6: *"the different fields are encoded
in Big-endian"*). The server reads the 6-byte prefix (transaction id, protocol
id, length), then reads `length` further bytes (unit id + PDU); its response
echoes the transaction id and unit id, sets protocol id `0x0000`, and sets
length to `1 + response_PDU_len`.

---

## 3. Addressing model<a id='addressing'></a>

Modbus PDU data addresses are **0-based** (`0x0000`–`0xFFFF`); the `4xxxx` /
"register N" notation is a 1-based human reference, where holding register
`40001` is wire address `0x0000` (Modbus spec; NI).

**Decision — register numbers are the on-wire PDU addresses.** This follows the
reference device exactly. MultiLoad II §1.6 states:

> "The formal specification of Modbus lists the starting Holding Register
> address as 40001 … Referencing Holding Register 40001 is addressed as register
> 0000 in the register address field … to avoid confusion in this manual, the
> Modbus Holding Register addresses listed are the value in the register address
> field in the message. If it is necessary to know the formal Modbus Holding
> Register address, simply add 40001 to the register addresses listed."

Its register-map summary (§1.6.6) then lists wire-address blocks beginning at
**1** (`1–999` configuration, `3000–3999` totalizers, `7000–7799` status/alarms,
`8000–8999` preset definitions) — i.e. the device documents wire addresses
directly and its map starts at address `1`, leaving `0` (formal `40001`) as the
unused base.

The testbed dispenser does the same: its register numbers `0x0001`–`0x0007` are
the literal PDU address-field values. Address `0x0000` and any address
`≥ 0x0008` are unmapped (→ Illegal Data Address, §5). This keeps the documented
address identical to the wire address (no register-number/PDU off-by-one), and
mirrors a real controller's map that starts at `1`. The FMS and the integration
tests pass PDU addresses `0x0001`–`0x0007` to a standard Modbus client
(`tokio-modbus`) unchanged.

---

## 4. Function codes<a id='codes'></a>

MultiLoad II implements fn `3` (Read Holding Registers), `6` (Write Single
Register), `8` (loopback), and `16` (Write Multiple Registers), noting fn 6
*"cannot be used for 32-bit registers"* and that 32-bit values are read together
via fn 3 / written together via fn 16.

The testbed dispenser implements the two it needs; any other function code
returns Illegal Function (§5). It never needs fn 16 because its only writable
registers (preset, enable) are single 16-bit registers.

### 4.1 Read Holding Registers — `0x03`

```
Request PDU:   0x03  start_addr(2 BE)  quantity(2 BE)
Response PDU:  0x03  byte_count(1)     register_values(2 BE × quantity)
```

`byte_count = 2 × quantity`. `quantity` must be 1–125; the whole range
`[start_addr, start_addr + quantity)` must lie within the mapped registers
(§5). A 32-bit totaliser is read as two consecutive registers (e.g.
`start_addr = 0x0003, quantity = 2`).

### 4.2 Write Single Register — `0x06`

```
Request PDU:   0x06  address(2 BE)  value(2 BE)
Response PDU:  0x06  address(2 BE)  value(2 BE)   (echo of the request)
```

Only the writable registers (preset `0x0001`, enable `0x0002`) accept a write; a
write to any other mapped or unmapped address returns Illegal Data Address (§5).

---

## 5. Exception responses<a id='exceptions'></a>

An exception replaces the PDU with `FN | 0x80` and a one-byte code:

```
Exception PDU:  (function_code | 0x80)  exception_code(1)
```

The codes match MultiLoad II §1.6.5 (subcodes 1/2/3) and the standard:

| Code | Name | When |
|---|---|---|
| `0x01` | Illegal Function | Function code other than `0x03` / `0x06`. |
| `0x02` | Illegal Data Address | Address (or any register in a read range) not mapped; write to a read-only register. |
| `0x03` | Illegal Data Value | Read `quantity` outside 1–125. |

The exception response carries a normal MBAP (echoed transaction id / unit id,
length covering the 2-byte exception PDU).

*Note on 32-bit pair atomicity.* MultiLoad II also returns Illegal Data Address
for *"a read or write to a single register of a 32-bit value"* — it enforces
that 32-bit pairs are accessed together. The testbed dispenser takes the more
permissive (and equally common) view: the holding-register space is a flat array
of 16-bit registers, and any in-range read succeeds — the "32-bit-ness" of the
totalisers is the FMS's interpretation of two adjacent registers. A client that
reads the documented pairs sees correct values; this is standard Modbus
behaviour and is not detectable as simulation.

---

## 6. Unit identifier<a id='unit'></a>

The dispenser is a direct TCP end node, not a serial gateway, so the unit
identifier carries no routing meaning. Per MultiLoad II §1.6, the field is *"set
by the MODBUS Client in the request and must be returned with the same value in
the response by the server."* The dispenser therefore **accepts any unit
identifier and echoes it** — it never rejects on unit id. (`tokio-modbus`'s
default for a TCP device is `0xFF`; `0x01` is also common.)

---

## 7. Data encoding<a id='encoding'></a>

Register data types follow MultiLoad II §7.1.1:

- **Registers are 16-bit, big-endian** on the wire.
- **Preset volume** uses MultiLoad's **"unsigned div10"** type — a value with one
  decimal place stored ×10 (their example: `1234.5 → 12345`). Our preset is
  litres to 0.1 L resolution: `100.0 L → 1000`. As a single 16-bit register the
  range is 0–6553.5 L, comfortably above the ≤ 3000 L tug dispenses.
- **Pump enable** uses MultiLoad's **"disable enable"** enum: `0 = DISABLED`,
  `1 = ENABLED`.
- **Totalisers** are 32-bit **"unsigned int"** values occupying a consecutive
  register pair, **high word at the lower address** (big-endian word order,
  consistent with the big-endian register encoding); the FMS reconstructs
  `(high_word << 16) | low_word`. They count **pulses** (the FMS divides by the
  K-factor to obtain litres) — this keeps the raw meter count auditable, per the
  narrative's measurement-integrity requirement.
- **Status** is a 16-bit bitfield (positive logic, LSB = bit 0); only bit 0
  (Running = `solenoid_open && pump_energised`) is meaningful, other bits zero.

---

## 8. Register map<a id='map'></a>

PDU data addresses (0-based, §3). "R" = readable via `0x03`; "W" = writable via
`0x06`.

| Address | Access | Width | Type | Contents |
|---|---|---|---|---|
| `0x0001` | R/W | 16-bit | unsigned div10 | Preset volume (litres × 10) |
| `0x0002` | R/W | 16-bit | enable/disable | Pump enable (1 = enable, 0 = disable) |
| `0x0003` | R | 16-bit | uint32 hi | Cumulative totaliser, high word (bits 31–16) |
| `0x0004` | R | 16-bit | uint32 lo | Cumulative totaliser, low word (bits 15–0) |
| `0x0005` | R | 16-bit | uint32 hi | Transaction totaliser, high word (bits 31–16) |
| `0x0006` | R | 16-bit | uint32 lo | Transaction totaliser, low word (bits 15–0) |
| `0x0007` | R | 16-bit | bitfield | Status flags (bit 0 = Running) |

The two write registers are also readable (a holding register can be read back);
the totalisers and status are read-only. Addresses `0x0000` and `≥ 0x0008` are
unmapped.

---

## 9. Behaviours<a id='behaviours'></a>

- **Pump enable.** Writing `1` to `0x0002` opens the solenoid and energises the
  pump immediately (no intermediate "authorised" state); writing `0`, or
  reaching the auto-stop target, clears both. The dispenser publishes its
  solenoid/pump state to the physical model on every change.
- **Auto-stop.** Fires when the transaction pulse count reaches
  `(preset_register / 10) × k_factor` — i.e. preset litres × pulses-per-litre.
- **Transaction totaliser** (`0x0005`–`0x0006`) resets to zero each time a new
  preset is written to `0x0001` (the natural start-of-transaction boundary; the
  FMS writes the preset before enabling the pump).
- **Cumulative totaliser** (`0x0003`–`0x0004`) is monotonically non-decreasing
  across transactions and is never cleared by auto-stop; any reset would be an
  audited FMS event (narrative §5 measurement-integrity condition 4).
- **K-factor** is carried on each pulse from the flow meter, so the dispenser's
  volume arithmetic always uses the meter's own operating K-factor.

---

## 10. Realism decisions<a id='decisions'></a>

Each choice below, made so a real Modbus client cannot distinguish the dispenser
from a real appliance, with its justification:

- **Register numbers = wire PDU addresses; map starts at `0x0001`** — matches
  MultiLoad II, which documents wire address-field values and starts its map at
  `1` (§3).
- **Unit identifier accepted and echoed** — MultiLoad II / standard require the
  server to return the client's value (§6).
- **Exceptions `0x01`/`0x02`/`0x03`** — match MultiLoad II error subcodes 1/2/3
  and the standard (§5).
- **Preset `unsigned div10`, enable `enable/disable`, totalisers 32-bit `uint`
  pairs (big-endian, high word first)** — match MultiLoad II §7.1.1 register
  data types (§7).
- **Flat 16-bit register array; any in-range read allowed** — standard Modbus;
  a documented, permissive alternative to MultiLoad's pair-atomic enforcement
  (§5 note).
- **Persistent connections, never dropped idle** — a real polled device.
- **No Read Device Identification (`0x2B/0x0E`)** — not implemented; a probe of
  that function returns Illegal Function, as on many minimal devices. (If the
  testbed later needs the device to present vendor/product strings, this is
  where to add it.)

---

## 11. Sources<a id='sources'></a>

- Modbus Organization, *MODBUS Application Protocol Specification V1.1b3* —
  PDU structure, 0-based addressing, function codes `0x03`/`0x06`, exception codes.
  https://www.modbus.org/docs/Modbus_Application_Protocol_V1_1b3.pdf
- Toptech Systems, *MultiLoad II Communications Guide*, Part #6077 (firmware
  3/4.31.40, Nov 2018) — §1.6 Modbus-TCP/IP protocol (MBAP, addressing, function
  codes, error subcodes, unit identifier), §1.6.6 register map summary, §7.1.1
  register data types. A real fuel-terminal preset/batch loading controller.
- Chipkin, *Modbus TCP MBAP Header & Message Format Reference*.
  https://docs.chipkin.com/articles/modbus-tcp-mbap-header-and-message-format-reference/
- National Instruments, *The Modbus Protocol In-Depth* — 0-based PDU addressing
  and the `4xxxx` convention.
  https://www.ni.com/en/shop/seamlessly-connect-to-third-party-devices-and-supervisory-system/the-modbus-protocol-in-depth.html
