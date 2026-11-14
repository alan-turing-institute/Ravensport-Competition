# Veeder-Root TLS Serial Protocol — Testbed Specification

Normative specification of the Veeder-Root Tank Level System (TLS) serial
protocol as implemented by the Ravensport testbed's Automatic Tank Gauge (ATG)
console (sim-side) and consumed by the Fuel Management System (FMS) and fuel
supplier actor (digital-side). It covers only the function codes the testbed
uses: `i101`, `i201`, `i205`.

The goal is byte-level fidelity with the real protocol, so that a genuine TLS
client would accept the console's responses and vice versa. Where the testbed
deliberately narrows or fixes a value, it is called out in §7. Open decisions
are in §8.

**Primary source.** Veeder-Root *Serial Interface Manual for TLS-300 and
TLS-350 UST Monitoring Systems and TLS-350R Environmental & Inventory
Management System*, Part No. 576013-635, Revision U. Page references below are
to that document. Serial-over-TCP transport (port 10001) is per Veeder-Root
*TCP/IP Interface Module Installation Guide*, document 577013-776.

- Manual (PDF): https://cdn.chipkin.com/files/liz/576013-635.pdf
- Manual (HTML mirror): https://usermanual.wiki/Document/PETROGaugeVeederRootTLS350SerialInterfaceManual.61185773/html
- TCP/IP module: https://www.veeder.com/us/sites/veeder.com.us/files/2020-09/577013-776%20-%20TCP-IP%20Interface%20Module.PDF

**Contents:**
1. [Transport](#transport)
2. [Command message format](#command)
3. [Response message format](#response)
4. [Checksum](#checksum)
5. [ASCII floating-point format](#float)
6. [Function codes](#codes)
7. [Testbed parameters and simplifications](#testbed)
8. [Open decisions](#open)
9. [Corrections to prior documents](#corrections)

---

## 1. Transport<a id='transport'></a>

The console presents the serial protocol over TCP on port 10001 (serial-over-TCP).
Bytes are exchanged exactly as on an RS-232 line; the TCP stream carries the raw
protocol with no additional framing. Multiple clients (the FMS, and the fuel
supplier via `atg-relay-01`) connect as independent TCP clients.

The serial line settings the protocol assumes (manual §3.0, p5) are 7 or 8 data
bits, configurable parity, 1 start bit — immaterial over TCP, where each
protocol byte is one octet with the high bit clear (all protocol characters are
7-bit ASCII).

RS-232 security (a 6-digit code prefixing every command, manual §5.0 p6) is
**disabled** in the testbed (DIP switch 2 off, manual §4.1 p5); commands carry
no security code. Access control is the relay's source-IP whitelist
(digital.md §2.5), matching the real protocol's lack of authentication.

---

## 2. Command message format<a id='command'></a>

A command is (manual §5.0, p6):

```
SOH [security code] <function code> [data field]
```

- `SOH` — Start of Header, ASCII `0x01`.
- `security code` — optional 6 ASCII digits; **omitted** (security disabled).
- `function code` — a **six-character** code: a format letter, a three-digit
  function number, and a two-digit device number `TT`.
  - The **format letter** selects the kind of command and its response format.
    `i` and `I` are inquiry commands, which request a report: lower-case `i` =
    computer (packed) format; upper-case `I` = display format. `s` and `S` are
    set commands, which change the console's configuration and carry a data
    field, in computer and display format respectively. The testbed issues
    only computer-format inquiry commands.
  - `TT` is a two-character ASCII decimal device (tank/sensor) number: `00` = all
    devices, `01`–`16` = a specific device.
- `data field` — command-specific; **absent** for the read-only report codes
  used here.

There is **no line terminator**. The function code is fixed-length, so the
console reads exactly the six characters following `SOH` and dispatches. (Earlier
testbed drafts appended `\n`; that is not part of the protocol — a trailing byte
would be read as the start of the next command. See §9.)

The three commands the testbed issues (all tanks unless a specific tank is
named):

| Command bytes | Meaning |
|---|---|
| `\x01i10100` | System Status Report, all devices |
| `\x01i20100` | In-Tank Inventory Report, all tanks |
| `\x01i20500` | In-Tank Status Report, all tanks |

A command whose function code the console does not recognise is answered with
`\x019999FF1B\x03` (manual §5.0, p6), where `FF1B` is the checksum of `9999`.

---

## 3. Response message format<a id='response'></a>

The testbed uses **computer format** (manual §6.1, p7):

```
SOH <function code> <data field> && <checksum> ETX
```

- `SOH` — ASCII `0x01`.
- `function code` — the **echo** of the received six-character function code
  (e.g. a response to `\x01i20100` begins `\x01i20100…`).
- `data field` — every report begins with a 10-character `YYMMDDHHmm` timestamp
  (current console date and time: 2-digit year, month, day, hour, minute), then
  the code-specific body (§6). Computer format contains **no** carriage returns,
  line feeds, spaces, labels, or nulls (manual §6.1, p7; §7.0 p11).
- `&&` — the two-character data-termination tag (manual §6.1) signalling that the
  checksum follows.
- `checksum` — four ASCII-hex characters (§4).
- `ETX` — End of Text, ASCII `0x03` (manual §6.1; programmable but fixed to
  Control-C here).

Display format (`SOH <function code> <data> ETX`, with human formatting and no
checksum) is **not** used by the testbed.

---

## 4. Checksum<a id='checksum'></a>

From the manual (§6.1, p7), verbatim in substance:

> The Checksum is a series of four ASCII-hexadecimal characters which provide a
> check on the integrity of all the characters preceding it, **including the
> control characters**. The four characters represent a 16-bit binary count which
> is the **2's complemented sum** of the 8-bit binary representation of the
> message characters after the parity bit (if enabled) has been cleared.
> **Overflows are ignored.** The data integrity check can be done by converting
> the four checksum characters to the 16-bit binary number and adding the 8-bit
> binary representation of the message characters to it. **The binary result
> should be zero.**

Therefore:

- Let `S` = the sum of the 8-bit values of every byte **preceding** the checksum
  field — i.e. `SOH`, the echoed function code, the timestamp, the body, and both
  `&` characters — taken modulo 2^16 (overflows ignored). **`SOH` is included**;
  `ETX`, which follows the checksum, is not.
- The checksum value is the two's complement: `C = (0x10000 − (S mod 0x10000)) mod 0x10000`.
- It is emitted as **four upper-case ASCII hex digits** (`{:04X}`).
- Verification: `(S + C) mod 0x10000 == 0`.

(The conventional test `sum of every byte between SOH and ETX ≡ 0` is **not**
correct for this protocol: that would sum the ASCII codes of the checksum's hex
digits rather than the 16-bit value they denote. Verification must parse the four
hex digits into `C` and add `C` to `S`.)

---

## 5. ASCII floating-point format<a id='float'></a>

Numeric data fields are IEEE-754 single-precision (4-byte) floats, transmitted as
**eight upper-case ASCII-hex nibbles in big-endian byte order** — byte 1 (sign +
exponent MSB) first (manual §6.3, p8). The value `00000000` denotes `0.0` by
convention (manual §6.3.1.4). Worked example from the manual (§6.3.2.4):
`461C4000` = `10000.0`.

Each float occupies exactly 8 ASCII characters; fields are concatenated with no
separators.

---

## 6. Function codes<a id='codes'></a>

### 6.1 `i101` — System Status Report (manual §7.2.1, p40–46)

Reports all active alarms and warnings. Computer-format response:

```
\x01 i10100 YYMMDDHHmm [AANNTT]… && CCCC \x03
```

The body is zero or more 6-character `AANNTT` entries, concatenated:

| Field | Len | Encoding | Meaning |
|---|---|---|---|
| `AA` | 2 | ASCII hex | Alarm/warning category |
| `NN` | 2 | ASCII hex | Alarm type within the category |
| `TT` | 2 | ASCII dec | Tank or sensor number |

Category (`AA`) values include (manual p41): `00` All Functions Normal,
`01` System, `02` Tank, `03` Liquid Sensor, `04` Vapor Sensor, `05` Input,
`06` Volumetric Line Leak, `07` Groundwater Sensor, `08` Type A Sensor, … .

For the sensor categories (`AA` ∈ {`03`,`04`,`07`,`08`,`12`,`13`}), the type
(`NN`) values are (manual p43): `02` Setup Data Warning, **`03` Sensor Fuel
Alarm**, `04` Sensor Out, `05` Sensor Short, `06` Sensor Water, `07` Sensor Water
Out, `08` High Liquid, `09` Low Liquid, `10` Liquid Warning.

For tank alarms (`AA=02`), the `NN` values are (manual p42): `01` Setup Data
Warning, `02` Leak, `03` High Water, `04` Overfill, `05` Low Product,
`06` Sudden Loss, `07` High Product, `08` Invalid Fuel Level, `09` Probe Out, … .

**Testbed bund alarm.** The secondary-containment (bund) liquid sensor is sensor
number 1. When its fuel alarm is latched, the `i101` body contains exactly the
entry **`030301`** (Liquid Sensor Alarm / Sensor Fuel Alarm / sensor 1). This is
the only alarm the testbed console emits. When it is not latched, the body is the
single all-normal entry **`000000`** (§8).

### 6.2 `i201` — In-Tank Inventory Report (manual §7.2.2, p59)

Computer-format response:

```
\x01 i201TT YYMMDDHHmm [TT p ssss NN F×N]… && CCCC \x03
```

After the timestamp, one record **per tank** (one record only, for a specific
tank; repeated when `TT=00`):

| Field | Len | Encoding | Meaning |
|---|---|---|---|
| `TT` | 2 | ASCII dec | Tank number |
| `p` | 1 | ASCII `0x20`–`0x7E` | Product code character |
| `ssss` | 4 | ASCII hex | Tank status bits (below) |
| `NN` | 2 | ASCII hex | Number of 8-char float fields following |
| `F×N` | 8×N | ASCII-hex floats | Data fields (below) |

Tank status bits `ssss` (manual p59): bit 0 (LSB) Delivery in Progress, bit 1
Leak Test in Progress, bit 2 Invalid Fuel Height Alarm (magnetostrictive probes
only), bits 3–15 unused.

Data fields, in order, `NN = 07` (manual p59):

| # | Field | Testbed units |
|---|---|---|
| 1 | Volume | litres |
| 2 | TC Volume (temperature-corrected) | litres |
| 3 | Ullage | litres |
| 4 | Height | millimetres |
| 5 | Water | millimetres |
| 6 | Temperature | degrees Celsius |
| 7 | Water Volume | litres |

The FMS uses field 2 (TC Volume) for reconciliation.

### 6.3 `i205` — In-Tank Status Report (manual §7.2.2, p63)

Computer-format response:

```
\x01 i205TT YYMMDDHHmm [TT nn NN…]… && CCCC \x03
```

After the timestamp, one record **per tank**:

| Field | Len | Encoding | Meaning |
|---|---|---|---|
| `TT` | 2 | ASCII dec | Tank number |
| `nn` | 2 | ASCII hex | Number of active alarms for the tank (`00` = none) |
| `NN` | 2 each | ASCII hex | One alarm-type number per active alarm |

The alarm-type numbers `NN` are the tank-alarm (`AA=02`) values listed in §6.1
(manual p63 references the `i101` `AA=02` table). `i205` carries **no inventory
floats and no status bitfield** — it is purely the per-tank list of active tank
alarms. Sensor alarms (including the bund) appear in `i101`, not here.

---

## 7. Testbed parameters and simplifications<a id='testbed'></a>

- **Single tank**, tank number `01`. `i201`/`i205` therefore return one record;
  `TT=00` requests yield that single record.
- **Product code `p`**: a configured single character. The testbed uses `D`
  (distillate / Marine Gas Oil). Configurable.
- **Units**: metric (litres, millimetres, °C), per the console's strapping/units
  configuration.
- **`i201` data fields** are derived from the ATG probe reading and geometry:
  Volume = `level_mm × k_l_per_mm`; TC Volume = Volume (temperature correction is
  identity at constant 15 °C); Ullage = `tank_capacity_l − Volume`;
  Height = `level_mm`; Water = `0.0` (no water sensor); Temperature = probe
  temperature; Water Volume = `0.0`.
- **`i201` status bits `ssss`** = `0000`. The console has no delivery- or
  leak-test awareness in the testbed.
- **`i205`** returns no active tank alarms (`<TT>00`). Overfill is modelled as a
  hardwired interlock in the simulated-physical world, not as an ATG-computed
  tank alarm, and no tank-leak or other tank alarm is modelled. The bund alarm is
  a *sensor* alarm and is reported via `i101`.
- **Bund sensor number** = `1`, giving the `i101` entry `030301`.
- The console emits **computer format only**; display format is not implemented.
- The console reads the fixed-length 6-character function code after `SOH`; no
  command terminator is expected (§2).

---

## 8. Resolved decisions<a id='open'></a>

**`i101` body when no alarms are active.** *Resolved (human sign-off):* the
console emits a single **`000000`** entry — `AA=00` (All Functions Normal),
`NN=00`, `TT=00` — so the body between the timestamp and `&&` is exactly
`000000`. The manual defines `AA=00` as "All Functions Normal"; emitting it gives
clients an unambiguous "no alarms" record rather than an empty body. When the
bund alarm is latched the body is `030301` instead (a real console may report
several entries; the testbed emits at most this one, and never alongside
`000000`).

---

## 9. Corrections to prior documents<a id='corrections'></a>

This specification supersedes the TLS details in earlier repo documents, which
contained errors (sourced from the TCP/IP transport guide rather than the serial
command reference). Documents to bring into agreement:

- **`research/fms-wetstock-reconciliation-marine-port-design-and-justification.md` §6:**
  - `i201` was described as 6 fields with a `P ss` prefix and no per-tank status.
    Correct: per-tank `TT p ssss NN` + **7** fields (adds field 5 *Water* height
    and field 7 *Water Volume*; positions shift accordingly).
  - `i205` was described as carrying a `ssss` bitfield + the 6 inventory floats.
    Correct: `i205` is the per-tank active-tank-alarm list (`TT nn NN…`), no
    floats. The `ssss` status bits belong to `i201`.
  - The request envelope was given as `SOH <code> LF`. Correct: no `LF`.
  - The checksum range and verification should match §4 (includes `SOH`;
    value-based verification).
- **`sim.md` §4.1 / §7, `digital.md` §3 / §4:** update the `i201`/`i205` field
  descriptions and the request format to match §2–§6 here.
- **`sim-impl.md` (ATG console) and `sim-tests.md`:** update the `i201` response
  layout (per-tank record + 7 fields), the `i205` definition (status report, not
  inventory), the request bytes (no `LF`), the checksum test (value-based, §4),
  and the no-alarm `i101` body (`000000`, §8).
