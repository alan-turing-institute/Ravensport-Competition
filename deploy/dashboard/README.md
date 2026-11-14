# Deployment monitor

A read-only dashboard for watching a running RavensPort deployment. It is
out-of-band researcher tooling: it observes the testbed from the host side and
**modifies no deployed image**. 

## Running

The server must run **inside the Lima VM**, where the Incus socket lives:

```sh
limactl shell ravensport
cd ~/path-to/RavensPort          # the repo, mounted from the host
python3 deploy/dashboard/serve.py # binds 0.0.0.0:8686
```

Then open <http://localhost:8686> on the host — Lima forwards the port
automatically. The page polls a fresh snapshot every 5 s.

Configuration is via environment variables:

- `PORT` — listen port (default `8686`).
- `INCUS` — Incus binary (default `incus`), matching the deploy scripts.

No third-party packages are required; the server is Python 3 standard library
only. It has no write path: all SQL is `SELECT`, and the FMS's `transactions`,
`jobs`, and `sessions` tables carry append-only triggers regardless.

## Fetching Data

Every datum comes from one of seven image-non-invasive taps:

| Tap | Source | Shows |
|-----|--------|-------|
| `incus list --format json` | Incus API (host-side) | per-instance up/down, CPU %, memory, NIC throughput |
| `incus exec <node> -- curl …/health` | existing FMS/HMI health endpoints | application liveness + FMS version |
| `incus exec fms-01 -- psql fms` | FMS Postgres over the local socket (peer auth), SELECT-only | tank level & alarm, current job, full transaction records, logins, active cards |
| `incus exec <node> -- ss -tanp` | live TCP sockets on the node (`netstat` fallback for sim-host) | click a fleet card to see its established connections and listening ports |
| `incus exec sim-host -- pgrep -f /usr/local/bin/sim` + `tail /var/log/sim.log` | simulator process state and its single log file | simulator liveness and, on a crash, the shutdown cause and panic |
| `incus exec <node> -- journalctl -u <unit> …` | systemd journal on `fms-01`, `hmi-01`, `hist-01` and `hist-dmz-01` | recent service logs for the FMS, the HMI and both log servers |
| `incus exec siem-01 -- tail /var/ossec/logs/{alerts/alerts,archives/archives}.json` | the security monitoring manager's two output files (`siem.md §6`) | recent alerts, and every record the manager received |

The per-node **▲/▼ throughput** figure is aggregate send/receive across the
node's NICs, derived from Incus's cumulative byte counters diffed over the poll
interval — it shows how much traffic is flowing, not what. Clicking a card
expands a drawer of that node's live TCP connections (state, local, peer,
process), which shows *who is talking to whom* without packet capture. `ss` is
present on the Ubuntu nodes; `sim-host` has only `iproute2-minimal`, so the
server falls back to busybox `netstat` there.

The **tank-level plot** carries litre (Y) and time (X) axes. By default the
time axis auto-scales to the whole buffered history — the first reading sits on
the left, the latest on the right, and the scale widens as readings accrue, so
every point is always visible. The axis is interactive: drag the axis
left/right to rescale (zoom), drag the plot body to pan through the buffered
history (up to 480 readings, ≈ 4 h at the 30 s ATG poll cadence), hover for an
exact reading, and double-click to return to the auto-scaled view. A manual pan
or rescale freezes the window until reset. The buffer is fetched each refresh;
the visible window is a client-side view, so dragging never re-queries the
database.

Clicking a **transaction row** expands its full record — grade, preset vs
dispensed volume, open/close timestamps and duration, the totaliser pulse
readings, the ATG tank volume before and after, and (for BDN rows) the vessel
the note links to. The detail includes a **meter-vs-tank reconciliation**: the
metered volume compared against the tank-level change the ATG observed over the
same job, flagged when the two disagree by more than 50 L or 2 %.

The **simulator panel** shows whether the sim process is running and displays a
tail of `/var/log/sim.log`. The sim is not restarted on crash (`sim-network.md
§7`), so if it panics the process stays dead: the panel turns red, a banner
reports the crash, and the extracted shutdown `cause=` and first panic line are
shown above the log tail — the failure is surfaced immediately rather than being
buried by a respawn.

The **HMI, FMS and log-server panels** below the simulator each stream a tail
of that node's systemd journal. Unlike the sim-host, which is OpenRC and writes
to a single log file, these are systemd services on Ubuntu, so their diagnostic
output is read from the journal with `journalctl -u <unit> --no-pager -o short-iso-precise`.

The HMI and FMS panels carry one unit each (`hmi.service`, `fms.service`). The
two log-server panels carry several, because `hist-01` runs both stores, both
ingestion agents and Grafana, and `hist-dmz-01` the two stores and Grafana
(`hist.md §2`). Each unit is read separately with an equal share of the panel's
depth and the results merged by timestamp, so the agent's error and the store's
response sit adjacent rather than in separate panels. A single
`journalctl -u a -u b …` would be simpler but gives the whole tail to whichever
unit is noisiest: on the deployed log servers Grafana alone filled all 120
lines of both panels, and neither store nor agent appeared. The depth is 150
lines on those two nodes against 60 elsewhere — 30 per unit on `hist-01`, 50 on
`hist-dmz-01`.

The two **security monitoring panels** below the log servers show the last 60
records of `siem-01`'s `alerts.json` and `archives.json` respectively,
unfiltered. Each file holds one JSON object per line; the panel moves each
record's `timestamp` field to the front, as in the journal panels, and indents
the remainder. A line that is not valid JSON, such as a record cut mid-write at
the start of the tail, is shown as read. These files are the manager's consumer
interface; its management interface on TCP 55000 does not serve alerts
(`siem.md §6`). `archives.json` holds every record received, whether or not a
rule matched, so at 60 lines its panel covers seconds to minutes of activity
depending on load. The files rotate daily, so shortly after rotation either
panel may show only a few lines.

The dashboard reads the digital world's *belief* of the physical state (what the
instruments reported to the FMS), not the sim's internal ground truth. The
sim's true physical variables (ESD contact, overfill interlock, inflow, which
vessel is docked) sit on hardwired contacts and actor state that are on no
network and are deliberately not observable here.

## Notes and limitations

- **This tool reads `fms-01`'s Postgres, not the historians.** `hist-01` and
  `hist-dmz-01` run the real stack (`hist.md`) and the FMS pushes to them, but
  the process panels here read the FMS database directly, so what they show is
  the FMS's own state rather than the retained record. Use the log servers'
  Grafana (`hist.md §7.1`) for history: this tool is for live fleet health, and
  the two disagreeing is itself worth noticing.
- **Live dispense progress** (litres delivered so far) lives only in FMS
  memory and needs an authenticated API call, which would write a row to the
  `sessions` audit table. To avoid perturbing the simulation, the dashboard
  reads jobs from the database instead and shows "in progress" until the
  closing transaction row appears.
- **Authentication panel shows successful logins only, because it reads the
  database.** The FMS writes one `sessions` row per successful login (operator
  via LDAP bind to corp-it + active RFID card, or admin via local argon2). A
  failed attempt returns 401 and writes no row, so it cannot appear here.

  **This is a limitation of this tool, not of the deployment.** Failed attempts
  are recorded: the FMS emits a `LOGIN_FAILED` syslog message to `hist-01`
  (`hist.md §4.3`) carrying the username, the source address, and a `reason`
  distinguishing a bad password from a rejected card from no card at all — detail
  the generic 401 deliberately withholds from the caller. Read them on the log
  server, or in corp-it's `slapd` logs
  (`incus exec corp-it -- journalctl -u slapd`) for the LDAP bind specifically.

  The panel is a login audit, not a failed-attempt or live-session view: logout
  does not remove the row, and active-token state is held only in FMS memory.
- **macvlan** means the sim-host appliance flows are not visible on the host
  `vnet-ot` bridge; this dashboard therefore observes instrument data through
  the FMS rather than by sniffing the wire.
