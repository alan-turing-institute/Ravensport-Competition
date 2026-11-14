# Viewing the log servers' Grafana from a workstation

`hist-01` and `hist-dmz-01` each serve Grafana over HTTPS on port 443 and each
carries one provisioned dashboard (`hist.md` §7.1). Neither is reachable from
outside the deployment by design. This describes how to reach them temporarily
from a workstation outside the Incus host, and how to put things back afterwards.

This is a debugging procedure, not part of the deployment. It is not what the
reconciliation user does: that account reaches `hist-dmz-01` from the information
technology zone over the network the topology already provides.

## What the procedure changes

Two things, neither of which touches a container, a configuration file, or an
image.

**On the Incus host**, an address is added to one or both bridges. The bridges are
created with `ipv4.address=none`, so the host holds no address on any of the
testbed's networks and cannot originate traffic to them. Adding one is what makes
the log server reachable from the host.

**On the workstation**, a Secure Shell tunnel forwards a local port into the host,
so that a port on the workstation behaves as a port on the log server.

## Why the first one must be undone

The testbed's firewall exists to govern what can reach what between zones. An
Incus host holding an address on both the operational-technology bridge and the
demilitarised-zone bridge is a machine on both segments at once, and traffic
between them by that route is never seen by the firewall.

While those addresses are in place, no statement about reachability between zones
holds — including the ones the integration suite asserts. The procedure is
sound as a deliberate, temporary measure and unsound left in place.

The addresses do not survive a reboot of the Incus host, but that is not a
substitute for removing them.

## 1. Open the path, on the Incus host

Add only the network required. `hist-01` is on the operational-technology bridge,
`hist-dmz-01` on the demilitarised-zone bridge.

```bash
sudo ip addr add 10.40.0.1/24 dev vnet-ot    # for hist-01
sudo ip addr add 10.20.0.1/24 dev vnet-dmz   # for hist-dmz-01
```

Confirm each answers before continuing. `302` is the expected response — Grafana
redirecting an unauthenticated request to its login page.

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://10.40.0.11
curl -sk -o /dev/null -w '%{http_code}\n' https://10.20.0.20
```

A request that hangs rather than answering means the address for that bridge is
absent.

## 2. Open the tunnel, on the workstation

```bash
ssh -F ~/.lima/incus-env/ssh.config -f -N \
    -o ControlMaster=no -o ControlPath=none \
    -o ExitOnForwardFailure=yes \
    -L 8443:10.40.0.11:443 \
    -L 8444:10.20.0.20:443 \
    lima-incus-env
```

`-f` returns the prompt once the forwards are established. `ExitOnForwardFailure`
makes a failed forward terminate the connection rather than leaving a session that
forwards nothing. The two `Control` overrides bypass any existing multiplexed
connection, over which added forwards can fail without reporting it.

Confirm from the workstation — both should return `302`:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8443
curl -sk -o /dev/null -w '%{http_code}\n' https://localhost:8444
```

Run these on the workstation. From the Incus host they always fail: the tunnel's
listening end is on the workstation.

## 3. Log in

| Address | Account | Dashboard |
|---|---|---|
| `https://localhost:8443/d/ravensport-log-server-ops` | `identities.hist_admin` | Log server operations |
| `https://localhost:8444/d/ravensport-bunkering-ops` | `identities.reconciliation_user` | Bunkering operations |

Credentials are seeded from `deploy/world.yaml` at image-build time (`hist.md`
§7). Read them from there rather than from a copy, since they are derived from
that file and a copy would drift:

```bash
python3 -c "
import yaml
w = yaml.safe_load(open('deploy/world.yaml'))
for k in ('hist_admin', 'reconciliation_user'):
    e = w['identities'][k]
    print(k, e['username'], e['password'])
"
```

The browser reports two certificate problems on each host, both expected. The
certificate is self-signed (`hist.md` §10.2, self-signed pending a certificate
authority), and its subject alternative name is the node and its zone address
rather than `localhost`, which the tunnel does not and cannot satisfy.

Each account exists only on its own instance. The reconciliation user being
rejected by `hist-01` is correct behaviour, asserted by
`tests/integration/hist/test_06_grafana_auth.py`.

## 4. Close it down

On the workstation. The tunnel detached from the terminal and does not stop when
the terminal closes. Find it before killing it, so that nothing is killed
unintentionally and so that a pattern matching nothing is visible rather than
silent:

```bash
pgrep -fl 'ssh.*10\.(40\.0\.11|20\.0\.20):443'
```

One process carries however many forwards were requested, so there is normally a
single line whichever host or hosts were tunnelled. Kill it by process
identifier, then repeat the search to confirm it reports nothing.

A pattern naming one forwarded port would miss a tunnel opened for the other host
alone, and `pkill` reports no match, so the step would appear to succeed while
leaving the tunnel up.

On the Incus host:

```bash
sudo ip addr del 10.40.0.1/24 dev vnet-ot    2>/dev/null || true
sudo ip addr del 10.20.0.1/24 dev vnet-dmz   2>/dev/null || true
```

## 5. Confirm it is closed

```bash
# workstation — expected to produce no output
pgrep -fl 'ssh.*10\.(40\.0\.11|20\.0\.20):443'
lsof -nP -iTCP:8443 -sTCP:LISTEN
lsof -nP -iTCP:8444 -sTCP:LISTEN

# Incus host — both expected to report clean
ip addr show dev vnet-ot  | grep 'inet ' || echo "vnet-ot: clean"
ip addr show dev vnet-dmz | grep 'inet ' || echo "vnet-dmz: clean"
```

The process check is listed first because it is the stronger of the two. On macOS
`lsof` commonly emits `can't stat() hfs file system …` followed by "Output
information may be incomplete", which concerns filesystem entries rather than
network sockets and does not affect the result — but a warning of incompleteness
is unsatisfactory in a step whose purpose is confirming an absence. `pgrep`
examines the process rather than the socket table and carries no such caveat.

## Interaction with the integration suite

`make -C tests/integration test-hist` adds `10.40.0.1/24` to `vnet-ot` through its
`configure-hist` target, and `make -C tests/integration teardown` removes it. A
test run therefore removes the path to `hist-01` even if this procedure put it
there, and a dashboard that stops loading after a test run needs the address
adding again.

Neither target knows about `vnet-dmz`. The hist suite reaches `hist-dmz-01` by
`incus exec` rather than over the network, so nothing in the repository places the
host on that bridge. That address is only ever added by this procedure and must be
removed by it.
