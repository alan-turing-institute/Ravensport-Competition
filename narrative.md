# Narrative
This doc **does** cover:
- What physical processes are in scope.
- What physical components are in scope.
- All human actors involved.
- How the physical components and human actors support the in-scope processes.
- The criteria used to determine whether the system is operating correctly.

Intentionally, it **does not** cover:
- Details about digital infrastructure that could support the in-scope physical processes. 
- How these physical components and human actors might be simulated, other than the compressed scenarios of §6.

The quantities, rates and frequencies given in this document, such as the tank's capacity, the volume of a delivery, the number of tugs and how often they refuel, describe the default scenario. A scenario may set them differently; the processes, actors, systems and correctness conditions remain as described.

This doc will be iterated if/when there are un-answered questions.


# On-shore Fuel Buffer Tank - Asset and Operations

<p align="center">
  <img src="./diagrams/Narrative_diagram.png?raw=true" alt="Ravensport narrative diagram" width="900">
</p>
<p align="center"><em>Figure 1. Ravensport Maritime Fuel Facility: equipment, dock office systems, and 
off-site 
connections, 
showing fuel flow,
hardwired safety, and digital communications. This diagram aims to show both the physical and digital entities in 
Ravensport at a 
high level. 
</em></p>


Ravensport is a large shipping port on the British coastline. To support port operations there are a fleet of tug boats 
(small, powerful 
boats) that are used to push and pull large ships to help them manouver in the port. The tug boats are owned and maintained by the port, and they refuel at a small fuel facility that has one large on-shore fuel tank. The port is operated by the Ravensport Harbour Authority, which owns the tugs and the fuel dock and issues each bunker delivery note in its own name. An external fuel supplier monitors the level of fuel in the tank, sending fuel deliveries by road tanker when the level is low.

A dock operator waits on the dock, handling both fuel deliveries and tug refuels when tankers and tugs arrive. The MARPOL Convention requires that all fuel transfers are recorded and that the records be kept for 3 years. As such, the dock operator collects paper delivery tickets from the road tanker drivers, and paper Bunker Delivery Notes (BDNs) from each tug refuel. The dock operator transcribes these documents into the dock's digital system.

At the end of each audit period, once the period's records have had time to reach the log server, an IT user carries out an audit, comparing the retained paper records with the digital records. The port sets the audit period as a calendar month, a week or a day; month-end is its usual practice.

---

#### Acronyms

MGO - Marine Gas Oil

ATG - Automatic Tank Gauge

FMS - Fuel Management System

BDN - Bunker Delivery Note

ESD - Emergency Shut Down

HMI - Human-machine Interface

NTP - Network Time protocol

RFID - Radio Frequency Identification

VMI - Vendor-managed inventory

PD flow meter - Positive Displacement flow meter

## 1. The asset

A ~50,000 L **Marine Gas Oil (MGO)** buffer tank on a port fuel dock, serving 2–3 harbour tugs. Topped up from road tankers every few days and drawn down by tugs every day.

## 2. The two processes

Two physical processes are in scope:

### 2.1 Filling the Tank

**What -** The fuel tank is re-filled by a road tanker with ~ 15-20,000l MGO.

**Why -** The tank level falls below the agreed reorder threshold, triggering the fuel supplier to schedule a road tanker delivery.

**When -** A short time after the level falls below the threshold, when the road tanker arrives at the dock.

**Who -** Dock operator and road tanker driver.

**How -**

*Pre-transfer:*

- The fuel supplier's system monitors ATG tank level continuously. When the level crosses the reorder threshold, the supplier schedules a road tanker delivery.
- When the tanker arrives the dock operator checks the level reported by the ATG to ensure there is enough space in the tank to accept the delivery.
- The dock operator uses the HMI to start a fuel delivery job in the FMS.
- Once the HMI reports that the fuel delivery job is started, the dock operator allows the road tanker driver to begin the process.

*Transfer:*

- The road tanker has a pump and meter onboard and drives the transfer.
- The tank on the dock has an overfill interlock:
    - A hardwired circuit is set up between the tanker's pump controller and the fill cabinet on the dock.
    - This circuit breaks automatically if the fuel level in the tank gets to ~95% to prevent overfill.
- The dock operator monitors the local ATG level indicator at the tank. The ESD button is available to press. It is a hardwired kill switch: on the dock side it drops power to dock pumps and closes valves; it also breaks the overfill interlock circuit at the fill cabinet, which causes the tanker's pump controller to stop the tanker pump. The tanker pump is powered from the tanker, not the dock — the interlock circuit is the only hardwired connection between the dock's safety systems and the tanker.

**Traces -** 

- *Paper* *(legal* *record):*
    - The tanker meter prints a delivery ticket. This includes the time the delivery started, grade, volume, density, totalisers, tanker reg, driver ID.
    - The tanker driver and dock operator both sign the ticket, both keep a copy.
- *Digital* *(FMS):*
    - The operator transcribes ticket number, the time the delivery started, volume, product temperature, density, volume corrected to 15 °C, supplier, tanker registration number.
    - FMS logs these alongside timestamps, operator ID, and ATG level delta as cross-check. Paper ticket is not uploaded.

### 2.2 Emptying the Tank - Refilling a tug boat

**What —** The dock pumps MGO from the tank into a harbour tug via a dockside dispenser ~500–3,000 L

**Why —** Tugs need fuel to operate. Tugs refuel periodically.

**When —** Several times a day, when a tug arrives at the dock. If the dock operator is occupied or the dispenser is in use, the tug waits until both are available. The dock operator completes the current refuel or delivery before starting the next. Waiting tugs are handled in the order in which they arrived, and a road tanker waiting to deliver is handled before any waiting tug. A tug leaves the dock once the dock operator and the tug engineer have signed the BDN. A tug that is waiting or refuelling does not come for fuel again until it has left.

**Who —** Dock operator, tug engineer (present but does not authenticate to port systems).

**How —**

*Pre-transfer:*

- Dock operator enters the job on the HMI and tells the Tug engineer the job is active

- Tug engineer presents vessel RFID tag at dispenser reader

- Dock operator sets dispenser preset to the requested volume

- If the FMS refuses to start the refuel, the dock operator cancels the job, and the tug leaves without fuel. A job cannot be cancelled once fuel has moved.

*Transfer:*

- Dock's pump and Positive Displacement (PD) flow meter drive the transfer

- The dock meter is the legal custody-transfer instrument (reverse of §2.1 where the tanker's meter was)

- Dispenser auto-stops when preset volume is reached

- No hardwired overfill interlock

- ESD button available as in §2.1. During tug refuelling there is no tanker present and no interlock circuit connected, so the ESD affects dock-side equipment only (dock pump and valves).

**Traces —**

- *Paper (legal record):*

  - Dock operator hand-writes a BDN (Bunker Delivery Note). MARPOL Annex VI requires one for ships of 400 gross tonnage and above; the port issues one for every dispense as its own practice, whatever the tug's tonnage. It is retained on the vessel for three years.

  - The BDN records the tug's name and its International Maritime Organization (IMO) number, as MARPOL Annex VI Appendix V requires. The IMO number, not the name, identifies the tug on the FMS job record.

  - The BDN's time of commencement is the dispense start recorded by the FMS, which the dock operator reads from the HMI.

  - Dock operator and tug engineer both sign and keep a copy

- *Digital (FMS):*

  - FMS transaction log references the BDN number and mirrors its key fields for digital audit and reconciliation

---

## 3. People and shifts

This section covers the human aspect of the Ravensport narrative. It is realistic that not everything is automated; in many ports in the UK, many processes are done manually and use paper trails rather than digital systems.

*People Overview:*

- **Fuel supplier** monitors ATG tank level remotely and triggers road tanker delivery under a vendor-managed inventory (VMI) arrangement.
- **Road tanker driver** a counterparty, physically present but no interaction with the port's digital systems.
- **Dock operator** interacts with counterparties and port systems on the dock during fuel delivery and tug refuelling.
- **Tug engineer** a counterparty, presents RFID tag at the dock and requests a refuel.
- **Reconciliation user** Performs an audit at the end of each audit period by signing in to an office workstation and retrieving the period's data from the log-server replica over the network, then reconciling it against paper records. A summary of the outcome of each audit is recorded on the workstation: the period audited, whether the records agreed, and the document numbers of any that did not. The conclusions are retained; the period's records themselves are not.
- **Systems administrator** Maintains the port-managed systems on the dock network from an administration workstation on the corporate IT network. Administrative access to systems on the dock network uses Secure Shell (SSH) through a bastion in the demilitarised zone. A routine check confirms that each system's services are running, its disks have space, its clock is synchronised and its journal shows no new errors. The check changes nothing.

| Role | Location | Network | Credential |
| --- | --- | --- | --- |
| Fuel supplier | Off-site | External | n/a |
| Road tanker driver | On dock | n/a | Paper delivery ticket |
| Dock operator | On dock | OT | User/password + RFID at HMI |
| Tug engineer | On tug | n/a | Vessel RFID tag |
| Reconciliation user | IT Office | IT | User/password on office workstation; separate user/password on log-server replica |
| Systems administrator | IT Office | IT | User/password on administration workstation; SSH key for port-managed systems |

*Shifts:*
- **Continuous**:
    - Dock operator on the dock handling fuel deliveries and tug refueling.
    - The fuel supplier's system monitors ATG tank level.
- **At the configured administration-check interval**:
    - The systems administrator carries out a routine check of the port-managed systems on the dock network from the administration workstation. The interval is measured in hours so that a compressed scenario can scale it with the other process timings.
- **End of each audit period** (month-end by default):
    - The reconciliation user audits FMS records (via the DMZ log-server replica)
    - They cross-check against paper BDNs and paper delivery tickets.
- **Planned maintenance windows**:
    - The port's IT and OT staff take the port's digital systems down together in a planned maintenance window and restart them. Once a window opens, no new delivery or refuel begins; transfers already under way are completed and their paperwork finished. A tug or road tanker arriving during the window waits and is served when it ends. The dock equipment is shut down first, then the port's systems in turn, and everything is then restarted. The security monitoring records the systems stopping and starting.

---

## 4. Systems and instrumentation

Dock systems and instruments by physical location. Where an item is **hardwired** (not on the 
network), it is flagged inline in the text below.

<details>

<summary><strong>Show Ravensport Narrative Diagram</strong></summary>

<p align="center">
  <img src="./diagrams/Narrative_diagram.png?raw=true" alt="Figure 1. Ravensport Maritime Fuel Facility: equipment, dock office systems, and 
off-site 
connections, 
showing fuel flow,
hardwired safety, and digital communications. This diagram aims to show both the physical and digital entities in 
Ravensport at a 
high level. ">
</p>

<p align="center"><em>Figure 1. Ravensport Narrative Diagram.</em></p>

</details>

Every port host is built and maintained by the department responsible for it: the systems administrator for the systems on the dock network and in the demilitarised zone, and the corporate IT department for the hosts on the corporate IT network. No port host installs software updates by itself or makes a connection to any service outside the port. The responsible department tests and applies updates during planned maintenance windows.

### 4.1 On the dock - shared between both processes

- **Tank** - ~50,000 L MGO buffer tank, sitting in a bund (secondary containment).
- **ATG probe (Automatic Tank Gauge)** - Permanent level + temperature probe in the top of the tank. Feeds the ATG console in the office. Includes a local level indicator mounted at the tank, readable by the dock operator on the dock without going to the office. Not on the network.
- **Bund leak sensor** - Detects fuel in the secondary containment. The alarm is networked to the ATG console.
- **ESD button** - Emergency shutdown. On the dock side, drops power to dock pumps and closes valves regardless of software state. Also breaks the overfill interlock circuit at the fill cabinet, stopping the tanker pump during a road tanker delivery (the interlock circuit is the only hardwired connection between the dock's safety systems and the tanker). During tug refuelling, no interlock circuit is connected, so the ESD affects dock-side equipment only. **Hardwired — not on the network.**

### 4.2 On the dock - fuel delivery side (road tanker → tank)

- **Fill cabinet** - The physical connection point for the tanker, adjacent to the tank.
- **Overfill probe and socket** - The ATG probe also drives an independent hardwired interlock. A standardised socket on the fill cabinet is connected to the probe. Before the tanker can pump, the driver plugs the tanker's overfill cable into this socket; the tanker's pump controller requires the circuit to be healthy, and the circuit breaks at ~95% tank level, cutting out the tanker pump. **Hardwired — not on the network, and independent of the ATG console and FMS.**

The tanker's own onboard pump and Weights-and-Measures-sealed meter are out of scope (see §1).

### 4.3 On the dock - tug refuelling side (tank → tug)

- **Dispenser** - A commercial fuel pump on the dock with a flow meter (the custody-transfer instrument for tug deliveries) and an auto-stop that cuts flow when the authorised preset volume is reached. The dispenser controller communicates with the FMS over the OT network: the FMS sends the authorised preset volume before the pump will run; on completion, the dispenser sends the end-of-dispense totaliser values back to the FMS to close the transaction record.
- **Vessel Radio-Frequency Identification (RFID) reader** - mounted at the dispenser; reads the tug's vessel RFID tag and sends the tag identifier to the FMS over the OT network. The tag is validated against the vessel on the job record before the preset is issued.

### 4.4 In the dock office

- **ATG console** - An appliance that polls the ATG probe and the bund leak sensor; exposes level, 
  temperature, 
  and alarm state to the FMS and HMI.
- **FMS (Fuel Management System)** - This is a PC/server running dispenser management, user authentication, transaction logging, and preset control for the tug dispenser.
- **Log-server (primary)** - This does time-series and event logging on the OT network. Stores ATG readings, dispense transactions, authorisation events, and alarms. Retention aligned with the 3-year MARPOL requirement on the paper BDNs it mirrors.
- **HMI (Human-Machine Interface)** - The dock operator's workstation. Single pane for ATG state and FMS transactions.

### 4.5 Off-site - DMZ, Corporate IT, and External

- **Log-server replica** - A read-only mirror of the OT log-server, in the DMZ. Queried by the reconciliation user on IT.
- **ATG data relay** — A relay in the DMZ that accepts inbound connections from the fuel supplier's monitoring platform and forwards them to the ATG console using the ATG's standard monitoring protocol. This is the internet-facing endpoint on the dock's network and the boundary point for the external VMI data path.
- **SSH bastion** — A Linux host in the DMZ through which the systems administrator reaches the port-managed systems on the dock network using SSH. It is separate from the ATG data relay because the two systems support different operational functions and maintenance schedules.
- **Administration workstation** — A workstation on the corporate IT network from which the systems administrator maintains the port-managed systems on the dock network. It is the only host that holds the administrator's SSH private key.
- **Reconciliation user's workstation** — This is on IT. Used for the periodic audits via the log-server replica, and holds the accumulated record of those audits.
- **Supplier monitoring system** — The fuel supplier's remote monitoring platform connects to the ATG data relay to read tank level and alarm data, and schedules road tanker deliveries when the level falls below the reorder threshold.

### 4.6 Authentication

- **Dock operator** - username + password + RFID tag at the HMI / FMS.
- **Reconciliation user** - username + password to sign in to the office workstation, and a separate username + password on the log-server replica. The two are distinct: the workstation and the replica are different systems, each holding its own record of who may use it.
- **Systems administrator** - local username + password to sign in to the administration workstation, and an SSH key for the bastion and the port-managed systems on the dock network. The workstation password is the same password used by the FMS application administrator represented in the corporate directory; the usernames remain distinct.
- **Tug engineer** - identified by a vessel RFID tag on the dock.
- **Road tanker driver** — not authenticated to any port system; identified by the paper delivery ticket.

### 4.7 Security monitoring

The port operates centralised security monitoring across its digital systems. Each networked host reports its own activity — service and authentication events, configuration changes, and the network connections it makes and accepts — to a monitoring system in the corporate IT zone, which evaluates the collected records against a set of detection rules and retains both the alerts and the underlying records for later investigation. At the boundaries between network zones the same system additionally inspects the content of traffic crossing between zones, including the external fuel-supplier data path, verifying that it conforms to the protocol the boundary permits. The monitoring is a detection channel only: it observes and records, and does not control any physical process or intervene in a transaction. The sealed vendor appliances on the dock — the tank gauge console, the dispenser, the card readers — are not instrumented directly; each is observed through the digital host it communicates with.

---

## 5. Operational correctness conditions

The system is operating correctly when all of the following hold. There are **high-level outcomes**, and the **detailed conditions** that together uphold those outcomes. Violation of any condition
constitutes the system failing the users.

### High-level outcomes

- **No oil spills.** Fuel leaks from primary containment are promptly identified and fuel never escapes secondary containment (bund).
- **Accurate custody transfer.** Every litre moved in or out is measured and recorded within tolerance; digital and paper records agree.
- **Only authenticated transactions occur.** No fuel moves without a valid operator-initiated job record matching the 
  counterparty and volume.
- **Counterparty identity matches the authorisation.** Fuel goes to the tug (or comes from the tanker) it was authorised for.
- **Records are complete and retained.** Every transaction produces a signed paper BDN or delivery ticket and a matching FMS/log-server record, retained for the MARPOL 3-year period.
- **The asset is available during operating hours.** No malfunction prevents deliveries or dispenses.

### Detailed conditions

#### Authorisation & identity

1. **Counterparty identity bound**: tug RFID tag on dispense must match the vessel's IMO number on the job record.

#### Measurement integrity

1. **Dispense volume on the BDN = dispenser meter totaliser delta**, within the meter's calibrated tolerance (±0.5%).
2. **ATG level delta is consistent with meter reading** within ±1%, temperature-corrected, on both fills and dispenses. That is: the change in tank level (converted to volume) should match the volume the meter says moved - the tanker's delivery ticket on a fill, the dispenser meter on a dispense. A mismatch means one of the two is wrong or tampered.
3. **No concurrent transactions** on the tank (prevents co-mingling that hides short-measure).
4. **Meter totaliser is monotonically non-decreasing**; any reset is itself an audited event.
5. **ATG readings are plausible.** Tank level changes only while an authorised flow is in progress; between transactions the level is static within sensor noise. Uncommanded step changes, or drift faster than physically possible, are alarmed. (Standard behaviour on modern ATG consoles - static leak tests and rate-of-change checks.)

#### Temporal & procedural

1. **Every transaction has a signed paper BDN or delivery ticket** recorded in FMS before the job closes; the FMS transaction references the paper document number.
2. **Clocks synchronised** across FMS and log-server via NTP (Network Time Protocol) within 1 second. The ATG console is a sealed appliance with no network time source: its clock is set at the front panel at commissioning, timestamps in console reports may differ from FMS and log-server records by up to five minutes.
3. **FMS transaction log is append-only.** Corrections are new entries that reference the original record; there are no in-place edits or deletions. (Standard behaviour on commercial FMS products.)
4. **The log-server copy of a record never delays fuel movement.** The FMS writes its own append-only record first and forwards a copy to the log-server afterwards. If the log-server is unreachable the FMS continues to dispense and receive fuel, retrying delivery with increasing delay, and a copy that cannot be delivered is eventually lost. The enforced record is the FMS's own; the log-server copy is complete in normal operation but is not a precondition for operating. Where the high-level outcomes "Records are complete and retained" and "The asset is available during operating hours" conflict, availability takes precedence. A transaction present in the FMS with no matching log-server record therefore has a benign cause as well as a malicious one, and distinguishing a log-server outage from deliberate suppression is a detection problem this environment poses rather than one it resolves.

#### Safety-adjacent

1. **Fill auto-stops at 95% tank level** - This is enforced by the hardwired overfill probe interlock, independent of the ATG console and FMS.
2. **The FMS must not enable the dispense pump unless the current and projected closing tank volumes are strictly above the cavitation threshold** (~10% tank volume). It obtains the current volume from an on-demand ATG reading and calculates the projected closing volume by subtracting the preset. This software-only control has no hardwired backup and relies on trustworthy ATG and dispenser data.
3. **Leaks into secondary containment are detected and acknowledged within one shift (≤12 h).** The bund is designed to catch a primary-containment failure, so a leak into it is a contained fault rather than a spill.

---

## 6. Deviations from real-world practice

**Scope note.** Although we only model a single buffer tank, the tank is understood as one small component of a **large, modernised port**. Any practice that is realistic at a big, well-equipped port is therefore in scope as "realistic" - not a deviation - even if it would be unusual at a
small standalone fuel dock. This covers, for example: a time-series log-server with a read-only DMZ replica for reconciliation, networked bund leak detection feeding the tank gauge console, a modern Fuel Management System that enforces separation of duties and structured time-bounded authorisation records, an IT / DMZ / OT network architecture proper, and centralised security monitoring across the digital systems within that architecture.

What follows are genuine deviations - elements that are **not yet standard even at large modernised ports**.

1. **Vendor-managed inventory (VMI) for tank replenishment.**
    - *Real world:* VMI driven by ATG level data is well-established in road fuel retail and commercial fleet operations. Its application at a small marine fuel dock is not standard practice; most small docks order on a fixed schedule or via a phone call based on a manual level check.
    - *Justification:* creates an explicit, realistic IT/OT boundary crossing — the ATG console's level data must reach an external system (the supplier's monitoring platform) — without which the scenario has no IT/OT boundary transit.

2. **Tight digital linkage between the paper BDN and the FMS transaction record.**
    - *Real world:* paper BDNs and digital FMS records typically coexist with loose coupling — a ticket number handwritten somewhere, end-of- month reconciliation — rather than the structured per-transaction reference, mass-balance check, and log-server retention we specify.
    - *Justification:* makes the reconciliation user a meaningful detection channel for fraud and log-server tampering, rather than a paperwork-only role.

One further deviation concerns the pace at which a scenario runs the port's operation.

3. **Compressed scenarios.**
    - *Real world:* a port runs in real time, and its audit period follows the calendar (§3).
    - *Justification:* lets a short run hold the same port activity, in the same proportions, as a longer span of operation. A compressed scenario shortens every interval and duration in the port's operation by one stated factor, and raises every rate of fuel flow by the same factor. Its audit period is a fixed interval measured in hours, which the factor divides.

Items that remain **true to current real practice** at ports of any size include: paper BDNs and paper delivery tickets, the hardwired overfill probe interlock, hardwired ESD, the Weights-and-Measures-sealed tanker meter as custody-transfer authority for inbound fuel, username + password

- RFID / TOTP authentication (no PKI), and physical MARPOL retained samples.
