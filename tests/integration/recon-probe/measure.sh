#!/usr/bin/env bash
# Measure the audit's payload and the clipboard read on the deployed system
# (sim-tests.md, reconciliation user, "Measuring the payload and the read"), to
# settle `initial_window` and `clipboard_deadline` (sim-impl.md, "The audit
# loop").
#
# Seeds DAYS days of records at a stated one dispense an hour, the rate of the
# 62.6 KB-a-day measurement rather than the configured arrival rate, each with
# its four bunkering events and the LOGIN that opened its session, through
# hist-01, so that they reach the replica by replication. They end 35 days
# before now, further back than any audit reaches (recon-tests.md §5): the
# window audited next is at most the previous calendar month, widened by the
# margin and the session lifetime, so a running sim's audits never retrieve
# them. At one dispense an hour, the records of 14 days come to about 876 KB,
# below the clipboard's ceiling of 1,048,575 bytes.
#
# Then, for one day and for 14 days (336 hours):
#   - the retrieval is run on recon-01 through incus exec, less its clipboard
#     pipe, and its bytes counted;
#   - the same retrieval is typed into a terminal on recon-01 by the probe inside
#     sim-host, on the actor's own route, and its read through the clipboard
#     timed.
#
# RECORD_BYTES_PER_ARRIVAL (sim-impl.md, "Splitting the window") is the day's
# bytes over the day's 24 arrivals; the actor derives initial_window and
# width_floor from it at the configured arrival rate. clipboard_deadline is a
# margin over the near-ceiling read.
#
# Run on the VM, from the repository root, with the probe built for musl (see
# README.md) and the two passwords from deploy/world.yaml:
#
#   RECON_PASS=<workstation_password> GRAFANA_PASS=<password> \
#       bash tests/integration/recon-probe/measure.sh
#
# Environment:
#   RECON_PASS     the reconciliation user's workstation password (required)
#   GRAFANA_PASS   the reconciliation user's Grafana password (required)
#   GRAFANA_USER   the reconciliation user's username   (default: recon-user)
#   HIST_HOST      hist-01's ingestion address         (default: 10.40.0.11)
#   DAYS           days of records seeded, at least 14 (default: 14)
#   PROBE          the musl probe binary               (default: the release build)
set -euo pipefail

: "${RECON_PASS:?the workstation password, from deploy/world.yaml}"
: "${GRAFANA_PASS:?the Grafana password, from deploy/world.yaml}"
GRAFANA_USER="${GRAFANA_USER:-recon-user}"
HIST_HOST="${HIST_HOST:-10.40.0.11}"
DAYS="${DAYS:-14}"
PROBE="${PROBE:-tests/integration/recon-probe/target/$(uname -m)-unknown-linux-musl/release/recon-probe}"

RUN="$(date -u +%Y%m%d%H%M%S)"
FIRST_DAY="$(date -u -d "$((DAYS + 35)) days ago" +%Y-%m-%d)"

# --- Seed -----------------------------------------------------------------------

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "==> Seeding $DAYS days of records from $FIRST_DAY (run $RUN)"
python3 - "$FIRST_DAY" "$DAYS" "$RUN" > "$SCRATCH/seed.jsonl" <<'PYEOF'
import datetime as dt
import json
import sys

first, days, run = sys.argv[1], int(sys.argv[2]), sys.argv[3]
start = dt.datetime.fromisoformat(first).replace(tzinfo=dt.timezone.utc)


def z(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


for day in range(days):
    for hour in range(24):
        at = start + dt.timedelta(days=day, hours=hour, minutes=17)
        job = f"measure-{run}-{day:02d}{hour:02d}"
        session = f"{job}-s"
        closed = at + dt.timedelta(minutes=2)
        envelope = {
            "job_id": job, "type": "dispense", "operator": "jrenard",
            "session_id": session, "counterparty": "1999997", "grade": "MGO",
        }
        # The LOGIN as the store holds a syslog record: its structured data
        # under the identifier, and the envelope fields vlagent adds.
        records = [
            {
                "_time": z(at - dt.timedelta(minutes=10)), "_msg": "login",
                "msg_id": "LOGIN", "app_name": "fms", "hostname": "fms-01",
                "facility": "1", "facility_keyword": "user", "format": "rfc5424",
                "level": "info", "priority": "14", "proc_id": "-", "severity": "6",
                "fms@99999.username": "jrenard", "fms@99999.role": "operator",
                "fms@99999.session_id": session,
                "fms@99999.source_address": "10.40.0.12",
                "fms@99999.result": "success",
            },
            {
                **envelope, "_time": z(at - dt.timedelta(minutes=1)),
                "_msg": "dispense job opened", "event_type": "job_opened",
                "created_at": z(at - dt.timedelta(minutes=1)),
                "atg_volume_at_open_l": 35000.0,
            },
            {
                **envelope, "_time": z(at), "_msg": "dispense transaction opened",
                "event_type": "transaction_open",
                "authorisation_id": f"{job}-a", "preset_volume_l": 500.0,
                "atg_volume_at_open_l": 35000.0, "opened_at": z(at),
            },
            {
                **envelope, "_time": z(closed),
                "_msg": "dispense transaction closed",
                "event_type": "transaction_closed",
                "transaction_id": f"{job}-t", "authorisation_id": f"{job}-a",
                "preset_volume_l": 500.0, "start_totaliser_pulses": 100000,
                "end_totaliser_pulses": 150000, "volume_dispensed_l": 500.0,
                "temperature_c": 15.0, "density_kg_per_l": 0.84,
                "temp_corrected_volume_l": 500.0,
                "atg_volume_at_open_l": 35000.0, "atg_volume_at_close_l": 34500.0,
                "opened_at": z(at), "closed_at": z(closed),
            },
            {
                **envelope, "_time": z(closed + dt.timedelta(minutes=1)),
                "_msg": "bdn linked", "event_type": "bdn_linked",
                "transaction_id": f"{job}-l", "references_transaction_id": f"{job}-t",
                "bdn_number": f"BDN-{day:02d}{hour:02d}",
            },
        ]
        for record in records:
            print(json.dumps(record))
PYEOF
echo "    $(wc -l < "$SCRATCH/seed.jsonl") records, $(wc -c < "$SCRATCH/seed.jsonl") bytes as posted"
# The content type is stated: curl's default form type has the server parse the
# body as form values, and nothing is ingested though the post returns 200.
incus exec hist-01 -- curl -sS --fail-with-body \
    --resolve "hist-01:9429:${HIST_HOST}" \
    -H 'Content-Type: application/stream+json' \
    --data-binary @- https://hist-01:9429/insert/jsonline < "$SCRATCH/seed.jsonl"
echo "==> Waiting 30 s for replication to hist-dmz-01"
sleep 30

# --- Measure --------------------------------------------------------------------

# The actor's retrieval command for a span (sim-impl.md, "Retrieval"), less its
# clipboard pipe when $3 is empty.
retrieval() {
    local start="$1" end="$2" pipe="$3"
    printf "{ curl -sS --fail-with-body -u '%s:%s' -G 'https://hist-dmz-01/api/datasources/proxy/uid/monthly-audit-logs/select/logsql/query' --data-urlencode 'query=_time:[%s, %s)' 2>&1; echo \"retrieval-end \$?\"; }%s" \
        "$GRAFANA_USER" "$GRAFANA_PASS" "$start" "$end" "$pipe"
}

incus file push "$PROBE" sim-host/tmp/recon-probe --mode 0755

for hours in 24 336; do
    start="${FIRST_DAY}T00:00:00Z"
    end="$(date -u -d "$FIRST_DAY 00:00 UTC + $hours hours" +%Y-%m-%dT%H:%M:%SZ)"
    echo
    echo "==> $hours hours: $start to $end"

    incus exec recon-01 -- bash -c "$(retrieval "$start" "$end" "")" > "$SCRATCH/response"
    echo "    retrieved $(wc -c < "$SCRATCH/response") bytes in $(($(wc -l < "$SCRATCH/response") - 1)) records, ending: $(tail -n 1 "$SCRATCH/response")"

    # Opened by the keyboard binding, as the actor opens its terminal.
    incus exec sim-host --env RECON_PASS="$RECON_PASS" -- /tmp/recon-probe \
        "ready 20000" "wait 2000" "key ctrl+alt+t" "wait 3000" \
        "clear" \
        "type $(retrieval "$start" "$end" " | xclip -selection clipboard")" \
        "key enter" "read 300000" \
        "type exit" "key enter" "wait 1000" \
        | grep -E "read completed|nothing arrived|bytes|characters" || true
done
