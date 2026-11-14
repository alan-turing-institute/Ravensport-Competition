#!/usr/bin/env python3
"""Read-only observability dashboard for the deployed RavensPort testbed.

Runs on the Lima VM (where the Incus socket lives) and builds a live snapshot of
the deployment entirely from host-side, image-non-invasive observation:

  * `incus list --format json`         -> per-instance up/down, CPU, memory, NIC I/O
  * `incus exec <n> -- curl .../health` -> FMS and HMI application liveness
  * `incus exec fms-01 -- psql`         -> tank telemetry, jobs, transactions (read-only)
  * `incus exec siem-01 -- tail`        -> security monitoring alerts and archives

No container or VM image is modified: every datum comes from the Incus API or from
running an existing binary inside a node. All SQL is SELECT-only; the guarded tables
carry deny_mutation triggers regardless.

Usage (inside the VM, from the repo checkout):
    python3 deploy/dashboard/serve.py            # binds 0.0.0.0:8686
    PORT=9000 python3 deploy/dashboard/serve.py
Then open http://localhost:8686 on the host (Lima auto-forwards the port).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INCUS = os.environ.get("INCUS", "incus")
PORT = int(os.environ.get("PORT", "8686"))
FMS_NODE = "fms-01"
HMI_NODE = "hmi-01"
HIST_NODE = "hist-01"
HIST_DMZ_NODE = "hist-dmz-01"
SIEM_NODE = "siem-01"
SIM_NODE = "sim-host"
SIM_BINARY = "/usr/local/bin/sim"  # deploy/sim-host/init.d/sim `command`
SIM_LOG_PATH = "/var/log/sim.log"
SIM_LOG_LINES = 60  # tail depth pulled into each snapshot
# FMS, HMI and the two log servers are systemd services on Ubuntu (not OpenRC
# like sim-host), so their diagnostic output goes to the journal rather than a
# log file; read it with journalctl instead of a file tail.
FMS_LOG_UNITS = ("fms.service",)
HMI_LOG_UNITS = ("hmi.service",)
# hist-01 runs both stores, both ingestion agents and Grafana; hist-dmz-01 the
# two stores and Grafana (hist.md §2). Their units share one panel per node,
# read as described at collect_journal.
HIST_LOG_UNITS = (
    "victoria-metrics.service",
    "victoria-logs.service",
    "vmagent.service",
    "vlagent.service",
    "grafana-server.service",
)
HIST_DMZ_LOG_UNITS = (
    "victoria-metrics.service",
    "victoria-logs.service",
    "grafana-server.service",
)
# Total tail depth for a panel, divided evenly between that node's units.
JOURNAL_LINES = 60
# Deeper for the log servers, where the depth is split five and three ways:
# 30 lines per unit on hist-01 and 50 on hist-dmz-01.
HIST_JOURNAL_LINES = 150
# The security monitoring manager's two output files (siem.md §6), one JSON
# object per line. Shown unfiltered, so the depth is the only bound on volume.
SIEM_ALERTS_PATH = "/var/ossec/logs/alerts/alerts.json"
SIEM_ARCHIVES_PATH = "/var/ossec/logs/archives/archives.json"
SIEM_LINES = 60
TANK_CAPACITY_L = 50000  # world.yaml process.tank_capacity_l
HERE = Path(__file__).resolve().parent

# tracing writes ANSI colour codes into sim.log even when the sink is a file;
# strip them so the cause/panic parse and the dashboard render cleanly.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


# Zone assignment for the fleet grid (deploy/topology.yaml).
ZONES = {
    "fw-ext-dmz": "ext/dmz",
    "fw-dmz-it": "dmz/it",
    "fw-ot-dmz": "ot/dmz",
    "atg-relay-01": "dmz",
    "hist-dmz-01": "dmz",
    "corp-it": "it",
    "siem-01": "it",
    "recon-01": "it",
    "fms-01": "ot",
    "hmi-01": "ot",
    "hist-01": "ot",
    "sim-host": "ot",
}

# Previous CPU/network samples, keyed by instance name, for rate computation.
_prev: dict[str, dict] = {}


def run(cmd: list[str], timeout: float = 6.0) -> tuple[int, str, str]:
    """Run a command, never raising; returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        return 1, "", str(exc)


# ── Fleet: incus-level telemetry ───────────────────────────────────────────────


def collect_fleet() -> dict:
    rc, out, err = run([INCUS, "list", "--format", "json"], timeout=8.0)
    if rc != 0:
        return {"error": err.strip() or f"incus list failed (rc={rc})", "nodes": []}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        return {"error": "could not parse incus output", "nodes": []}

    now = time.monotonic()
    nodes = []
    for inst in raw:
        name = inst.get("name", "?")
        status = inst.get("status", "Unknown")
        state = inst.get("state") or {}
        cpu_ns = ((state.get("cpu") or {}).get("usage")) or 0
        mem_bytes = ((state.get("memory") or {}).get("usage")) or 0
        net = state.get("network") or {}
        rx = tx = 0
        for nic, data in net.items():
            if nic == "lo":
                continue
            ctr = (data or {}).get("counters") or {}
            rx += ctr.get("bytes_received", 0) or 0
            tx += ctr.get("bytes_sent", 0) or 0

        cpu_pct = None
        rx_rate = tx_rate = None
        prev = _prev.get(name)
        if prev:
            dt = now - prev["t"]
            if dt > 0.1:
                cpu_pct = max(0.0, (cpu_ns - prev["cpu"]) / (dt * 1e9) * 100.0)
                rx_rate = max(0.0, (rx - prev["rx"]) / dt)
                tx_rate = max(0.0, (tx - prev["tx"]) / dt)
        _prev[name] = {"t": now, "cpu": cpu_ns, "rx": rx, "tx": tx}

        nodes.append(
            {
                "name": name,
                "zone": ZONES.get(name, "other"),
                "status": status,
                "up": status == "Running",
                "cpu_pct": round(cpu_pct, 1) if cpu_pct is not None else None,
                "mem_mb": round(mem_bytes / 1_048_576, 1) if mem_bytes else None,
                "rx_rate": rx_rate,
                "tx_rate": tx_rate,
            }
        )
    nodes.sort(key=lambda n: (n["zone"], n["name"]))
    return {"error": None, "nodes": nodes}


# ── Application liveness (existing /health endpoints) ───────────────────────────


def probe_health(node: str, url: str) -> dict:
    rc, out, _ = run(
        [INCUS, "exec", node, "--", "curl", "-fsk", "--max-time", "3", url],
        timeout=6.0,
    )
    ok = rc == 0 and '"status":"ok"' in out.replace(" ", "")
    version = None
    if ok:
        try:
            version = json.loads(out).get("version")
        except json.JSONDecodeError:
            pass
    return {"up": ok, "version": version}


# ── Process data: FMS Postgres (read-only, via incus exec + peer auth) ──────────

DB_QUERIES = [
    (
        "atg",
        "SELECT volume_l, temperature_c, coalesce(alarm_flags,'------'), "
        "to_char(recorded_at,'HH24:MI:SS') "
        "FROM atg_readings ORDER BY recorded_at DESC LIMIT 1",
    ),
    # Buffer more history than is shown by default so the level plot's time axis
    # can be dragged to zoom out (480 readings ≈ 4 h at the 30 s ATG poll cadence).
    (
        "spark",
        "SELECT to_char(recorded_at,'HH24:MI'), volume_l FROM "
        "(SELECT volume_l, recorded_at FROM atg_readings "
        "ORDER BY recorded_at DESC LIMIT 480) s ORDER BY recorded_at",
    ),
    (
        "txsum",
        "SELECT count(*) FILTER (WHERE volume_dispensed_l IS NOT NULL), "
        "count(*) FILTER (WHERE delivered_volume_l IS NOT NULL), count(*), "
        "coalesce(to_char(max(created_at),'HH24:MI:SS'),'-') FROM transactions",
    ),
    # Full transaction detail. A self-join resolves the vessel a BDN row links
    # to (references_transaction_id -> the dispense's counterparty). Column order
    # is parsed positionally in collect_db; keep the two in step.
    (
        "txfeed",
        "SELECT to_char(t.created_at,'HH24:MI:SS'), "
        "CASE WHEN t.references_transaction_id IS NOT NULL THEN 'bdn' "
        "WHEN t.volume_dispensed_l IS NOT NULL THEN 'dispense' "
        "WHEN t.delivered_volume_l IS NOT NULL THEN 'fill' ELSE 'other' END, "
        "coalesce(t.counterparty, t.supplier, ''), coalesce(t.grade,''), "
        "coalesce(t.volume_dispensed_l, t.delivered_volume_l, 0), t.preset_volume_l, "
        "coalesce(t.operator,''), coalesce(t.tanker_registration,''), "
        "coalesce(t.ticket_number,''), coalesce(t.bdn_number,''), "
        "t.atg_volume_at_open_l, t.atg_volume_at_close_l, "
        "t.start_totaliser_pulses, t.end_totaliser_pulses, "
        "to_char(t.opened_at,'HH24:MI:SS'), to_char(t.closed_at,'HH24:MI:SS'), "
        "coalesce(ref.counterparty,''), left(t.transaction_id::text,8), "
        "left(t.job_id::text,8) "
        "FROM transactions t LEFT JOIN transactions ref "
        "ON ref.transaction_id = t.references_transaction_id "
        "ORDER BY t.created_at DESC LIMIT 10",
    ),
    (
        "job",
        "SELECT j.type, j.counterparty, j.operator, to_char(j.created_at,'HH24:MI:SS'), "
        "EXISTS(SELECT 1 FROM transactions t WHERE t.job_id=j.job_id AND "
        "(t.volume_dispensed_l IS NOT NULL OR t.delivered_volume_l IS NOT NULL)) "
        "FROM jobs j ORDER BY j.created_at DESC LIMIT 1",
    ),
    (
        "sessions",
        "SELECT count(*), coalesce(to_char(max(logged_in_at),'HH24:MI:SS'),'-') "
        "FROM sessions",
    ),
    # Authentication audit: one row per successful login (auth.rs writes here;
    # failed binds and inactive-card rejections never reach this table).
    (
        "logins",
        "SELECT to_char(logged_in_at,'HH24:MI:SS'), username, role "
        "FROM sessions ORDER BY logged_in_at DESC LIMIT 8",
    ),
    (
        "cards",
        "SELECT count(*) FROM (SELECT DISTINCT ON (card_id) active FROM card_table "
        "ORDER BY card_id, enrolled_at DESC) s WHERE active",
    ),
]


def collect_db() -> dict:
    args = [
        INCUS,
        "exec",
        FMS_NODE,
        "--",
        "sudo",
        "-u",
        "postgres",
        "psql",
        "fms",
        "-q",
        "-t",
        "-A",
        "-F",
        "|",
        "-P",
        "pager=off",
        "--no-psqlrc",
    ]
    for tag, sql in DB_QUERIES:
        args += ["-c", rf"\echo @{tag}", "-c", sql]
    rc, out, err = run(args, timeout=8.0)
    if rc != 0:
        return {"error": err.strip() or f"psql failed (rc={rc})"}

    sections: dict[str, list[str]] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("@"):
            cur = line[1:].strip()
            sections[cur] = []
        elif cur is not None and line != "":
            sections[cur].append(line)

    def rows(tag):
        return [ln.split("|") for ln in sections.get(tag, [])]

    result: dict = {"error": None}

    atg = rows("atg")
    if atg:
        r = atg[0]
        result["tank"] = {
            "volume_l": float(r[0]),
            "temp_c": float(r[1]),
            "alarm": r[2],
            "at": r[3],
        }
    result["spark"] = [
        {"t": r[0], "v": float(r[1])} for r in rows("spark") if len(r) >= 2
    ]

    ts = rows("txsum")
    if ts:
        r = ts[0]
        result["tx"] = {
            "dispenses": int(r[0]),
            "fills": int(r[1]),
            "total": int(r[2]),
            "last": r[3],
        }

    def _num(s):
        return float(s) if s not in ("", None) else None

    def _int(s):
        return int(s) if s not in ("", None) else None

    feed = []
    for r in rows("txfeed"):
        if len(r) < 19:
            continue
        feed.append(
            {
                "at": r[0],
                "kind": r[1],
                "party": r[2],
                "grade": r[3],
                "volume": _num(r[4]) or 0.0,
                "preset": _num(r[5]),
                "operator": r[6],
                "tanker": r[7],
                "ticket": r[8],
                "bdn": r[9],
                "atg_open": _num(r[10]),
                "atg_close": _num(r[11]),
                "start_pulses": _int(r[12]),
                "end_pulses": _int(r[13]),
                "opened": r[14],
                "closed": r[15],
                "ref_party": r[16],
                "tx_id": r[17],
                "job_id": r[18],
            }
        )
    result["txfeed"] = feed

    job = rows("job")
    if job:
        r = job[0]
        result["job"] = {
            "type": r[0],
            "counterparty": r[1],
            "operator": r[2],
            "at": r[3],
            "closed": r[4] == "t",
        }
    sess = rows("sessions")
    if sess:
        result["sessions"] = {"count": int(sess[0][0]), "last": sess[0][1]}
    result["logins"] = [
        {"at": r[0], "user": r[1], "role": r[2]} for r in rows("logins") if len(r) >= 3
    ]
    cards = rows("cards")
    if cards:
        result["cards_active"] = int(cards[0][0])
    return result


# ── Per-node live connections (flow-level "see the traffic", non-invasive) ──────
#
# `ss` ships in the Ubuntu nodes; sim-host has only iproute2-minimal (no ss), so
# fall back to busybox netstat. Both run inside the node via `incus exec`,
# installing nothing. Established sockets show who is talking to whom; listening
# sockets show which services are up.


def _parse_conns(out: str, tool: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        tok = line.split()
        if not tok or tok[0] in ("State", "Netid", "Active", "Proto", "Recv-Q"):
            continue
        proc = ""
        if tool == "ss":
            # State Recv-Q Send-Q Local:Port Peer:Port [users:(("proc",...))]
            if len(tok) < 5:
                continue
            state, local, peer = tok[0], tok[3], tok[4]
            m = re.search(r'"([^"]+)"', line)
            if m:
                proc = m.group(1)
        else:  # netstat: Proto Recv-Q Send-Q Local Foreign State
            if len(tok) < 6:
                continue
            local, peer, state = tok[3], tok[4], tok[5]
        if state in ("ESTAB", "ESTABLISHED"):
            norm = "ESTAB"
        elif state == "LISTEN":
            norm = "LISTEN"
        else:
            continue  # drop TIME-WAIT, SYN-SENT, etc. to cut noise
        rows.append({"state": norm, "local": local, "peer": peer, "proc": proc})
    rows.sort(key=lambda r: (r["state"] != "ESTAB", r["peer"]))
    return rows


def collect_connections(node: str) -> dict:
    if node not in ZONES:
        return {"error": "unknown node", "conns": []}
    attempts = [
        (["ss", "-tanp"], "ss"),
        (["ss", "-tan"], "ss"),
        (["netstat", "-tan"], "netstat"),
    ]
    last_err = ""
    for cmd, tool in attempts:
        rc, out, err = run([INCUS, "exec", node, "--"] + cmd, timeout=6.0)
        if rc == 0 and out.strip():
            return {
                "error": None,
                "tool": cmd[0],
                "node": node,
                "conns": _parse_conns(out, tool),
            }
        last_err = err.strip() or f"{cmd[0]} rc={rc}"
    return {
        "error": f"no socket tool on {node} ({last_err})",
        "node": node,
        "conns": [],
    }


# ── Simulator: liveness + crash cause (sim-host is OpenRC, not systemd) ─────────
#
# The sim service is not supervised (deploy/sim-host/init.d/sim): a crash leaves
# the process dead and the panic in /var/log/sim.log. We report whether the
# process is alive, and if it has stopped, whether the log carries a shutdown
# cause or panic so the dashboard can surface the reason rather than showing an
# apparently-idle node.


def collect_sim() -> dict:
    # Match the full command path, not the process name. sim-host is BusyBox,
    # whose `pgrep -x` compares the pattern against the whole command string —
    # so `-x sim` never matches `/usr/local/bin/sim` and reported a healthy sim
    # as stopped. `-f` with the absolute path is exact without being wrong:
    # a bare `pgrep sim` would also match the sim-net service.
    rc_p, out_p, _ = run(
        [INCUS, "exec", SIM_NODE, "--", "pgrep", "-f", SIM_BINARY], timeout=6.0
    )
    running = rc_p == 0 and bool(out_p.strip())

    rc_l, out_l, err_l = run(
        [INCUS, "exec", SIM_NODE, "--", "tail", "-n", str(SIM_LOG_LINES), SIM_LOG_PATH],
        timeout=6.0,
    )
    reachable = rc_p != 127 and "not found" not in (err_l or "").lower()
    log = [_strip_ansi(ln) for ln in out_l.splitlines()] if rc_l == 0 else []

    # The orchestrator logs `sim shutting down cause=<reason>` immediately before
    # exit(1); the first `panicked at` line names the entity and location.
    cause = None
    panic = None
    for ln in log:
        if "sim shutting down" in ln and "cause=" in ln:
            cause = ln.split("cause=", 1)[1].strip()
        elif panic is None and "panicked at" in ln:
            panic = ln.strip()

    crashed = (not running) and (cause is not None or panic is not None)

    return {
        "reachable": reachable,
        "running": running,
        "crashed": crashed,
        "cause": cause,
        "panic": panic,
        "log": log,
        "error": None if reachable else (err_l.strip() or "sim-host unreachable"),
    }


# ── FMS / HMI / log servers: systemd journal tail (Ubuntu nodes) ───────────────
#
# Unlike the sim-host (OpenRC, single log file), these nodes run their services
# under systemd, so recent diagnostic output is read from the journal.
# `--no-pager` is required because journalctl paginates when it detects a TTY.
#
# Where a node runs several units, each is read separately and given an equal
# share of the tail depth, and the results are merged by timestamp. A single
# `journalctl -u a -u b …` would be simpler, but it gives the whole tail to
# whichever unit is noisiest: measured on the deployed log servers, Grafana
# alone filled all 120 lines on both hosts and neither store nor agent appeared
# at all. An ingestion or replication failure is only legible if the quiet units
# cannot be crowded off the panel.
#
# `-o short-iso-precise` is used throughout — a full sortable timestamp is what
# makes the merge a lexical sort, and one format across all four panels is
# clearer than two. The microsecond resolution is what orders entries logged
# within the same second: at whole-second resolution a lexical sort falls back
# to comparing the message text, which would put an agent's error and the
# store's response in the wrong order precisely when they are worth reading
# together.

_ISO_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def collect_journal(
    node: str, units: tuple[str, ...], lines: int = JOURNAL_LINES
) -> dict:
    per_unit = lines if len(units) == 1 else max(1, lines // len(units))
    script = "; ".join(
        "journalctl -u {} -n {} --no-pager -o short-iso-precise".format(
            shlex.quote(unit), per_unit
        )
        for unit in units
    )
    rc, out, err = run([INCUS, "exec", node, "--", "sh", "-c", script], timeout=8.0)
    reachable = rc != 127 and "not found" not in (err or "").lower()
    if rc != 0:
        return {
            "units": list(units),
            "log": [],
            "reachable": reachable,
            "error": err.strip() or f"journalctl failed (rc={rc})",
        }
    # Drop journalctl's own "-- No entries --" and boot markers: they carry no
    # timestamp, so they have no position in the merge.
    log = [
        _strip_ansi(ln) for ln in out.splitlines() if _ISO_LINE_RE.match(ln.lstrip())
    ]
    if len(units) > 1:
        log.sort()  # all units share the node's timezone, so this orders by time
    return {"units": list(units), "log": log, "reachable": True, "error": None}


# ── Security monitoring: tail of the manager's output files ────────────────────
#
# alerts.json and archives.json are the manager's consumer interface (siem.md
# §6); the management interface does not serve alerts. Both are read with a
# plain tail inside siem-01 and shown unfiltered.
#
# Each line is one JSON object carrying a top-level `timestamp`. It is lifted
# to the front of the record so the panels read like the journal panels, and
# the remainder is indented for legibility. A line that does not parse — the
# first line of a tail can be cut mid-record during a write — is shown as read.


def _format_record(line: str) -> str:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return line
    if not isinstance(rec, dict):
        return line
    ts = rec.pop("timestamp", None) or "-"
    return f"{ts} " + json.dumps(rec, indent=2, ensure_ascii=False)


def collect_json_tail(node: str, path: str, lines: int) -> dict:
    rc, out, err = run(
        [INCUS, "exec", node, "--", "tail", "-n", str(lines), path], timeout=6.0
    )
    if rc != 0:
        return {
            "path": path,
            "log": [],
            "error": err.strip() or f"tail failed (rc={rc})",
        }
    log = [_format_record(ln) for ln in out.splitlines() if ln.strip()]
    return {"path": path, "log": log, "error": None}


def build_snapshot() -> dict:
    with ThreadPoolExecutor(max_workers=11) as pool:
        f_fleet = pool.submit(collect_fleet)
        f_fms = pool.submit(probe_health, FMS_NODE, "https://127.0.0.1/api/v1/health")
        f_hmi = pool.submit(probe_health, HMI_NODE, "https://127.0.0.1/health")
        f_db = pool.submit(collect_db)
        f_sim = pool.submit(collect_sim)
        f_fmslog = pool.submit(collect_journal, FMS_NODE, FMS_LOG_UNITS)
        f_hmilog = pool.submit(collect_journal, HMI_NODE, HMI_LOG_UNITS)
        f_histlog = pool.submit(
            collect_journal, HIST_NODE, HIST_LOG_UNITS, HIST_JOURNAL_LINES
        )
        f_histdmzlog = pool.submit(
            collect_journal, HIST_DMZ_NODE, HIST_DMZ_LOG_UNITS, HIST_JOURNAL_LINES
        )
        f_alerts = pool.submit(
            collect_json_tail, SIEM_NODE, SIEM_ALERTS_PATH, SIEM_LINES
        )
        f_archives = pool.submit(
            collect_json_tail, SIEM_NODE, SIEM_ARCHIVES_PATH, SIEM_LINES
        )
    return {
        "ts": time.strftime("%H:%M:%S"),
        "capacity_l": TANK_CAPACITY_L,
        "fleet": f_fleet.result(),
        "apps": {"fms": f_fms.result(), "hmi": f_hmi.result()},
        "process": f_db.result(),
        "sim": f_sim.result(),
        "fms_log": f_fmslog.result(),
        "hmi_log": f_hmilog.result(),
        "hist_log": f_histlog.result(),
        "hist_dmz_log": f_histdmzlog.result(),
        "siem_alerts": f_alerts.result(),
        "siem_archives": f_archives.result(),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/api/snapshot"):
            body = json.dumps(build_snapshot()).encode()
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/node"):
            node = ""
            if "?" in self.path:
                q = parse_qs(urlparse(self.path).query)
                node = (q.get("node") or [""])[0]
            body = json.dumps(collect_connections(node)).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(
                200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8"
            )
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):  # silence per-request logging
        pass


def main():
    print(f"RavensPort dashboard on http://0.0.0.0:{PORT}  (incus={INCUS})")
    print("Open http://localhost:%d on the host (Lima forwards the port)." % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
