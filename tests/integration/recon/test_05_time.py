"""
Timekeeping (`recon-tests.md` §7), targeting `recon.md` §9.

**The audit window is no longer at risk from this host's clock.** An earlier
revision gave that as the reason to assert timekeeping here, when the browser
was the audit's client and a relative range was resolved against the displaying
machine's clock. `recon.md` §9 records that this no longer holds: the audit uses
`curl` and its period arrives as explicit bounds computed by the actor.

Two reasons remain. A clock behind a certificate's `notBefore` adds a fourth
case to the three §6.4 already records as indistinguishable from one another.
And the shell history of §7.2 is the audit's durable trace, which a free-running
clock makes impossible to place in order beside the rest of the fleet's.

Run on the VM: python -m pytest tests/integration/recon/test_05_time.py
"""

from __future__ import annotations

import pytest

from tests.integration.recon.conftest import Node

# narrative.md §5 requires clocks within one second. Asserted with margin rather
# than at the limit: a measurement at exactly the boundary is a flapping test.
MAX_OFFSET_SECONDS = 0.5


def _sources(recon: Node) -> list[str]:
    return [
        line
        for line in recon.stdout("chronyc", "-n", "sources").splitlines()
        if line.startswith(("^", "="))
    ]


def test_chrony_is_running(recon: Node) -> None:
    result = recon.run("systemctl", "is-active", "chrony")
    assert result.stdout.strip() == "active", (
        f"chrony is not running on recon-01: {result.stdout.strip() or result.stderr}"
    )


def test_time_source_is_the_site_time_source(recon: Node, corp_it_host: str) -> None:
    """`corp-it` is the site time source and this host is an ordinary client of
    it, reached intra-zone with no firewall between (`digital.md` §1.3)."""
    sources = _sources(recon)
    assert sources, "chrony reports no time sources at all"
    assert any(corp_it_host in line for line in sources), (
        f"{corp_it_host} is not among recon-01's time sources:\n" + "\n".join(sources)
    )


def test_clock_is_synchronised(recon: Node, corp_it_host: str) -> None:
    """Selected and within tolerance, not merely configured. chrony marks the
    selected source `^*`; a configured but unreachable one shows `^?` and still
    appears in the source list, so listing it is not evidence of anything."""
    selected = [line for line in _sources(recon) if line.startswith("^*")]
    assert selected, (
        "chrony has selected no time source. A configured source that is never "
        "reached leaves the clock free-running, which on this host silently "
        "changes which month the audit covers.\n" + "\n".join(_sources(recon))
    )
    assert corp_it_host in selected[0], (
        f"the selected source is not {corp_it_host}: {selected[0]}"
    )


def test_offset_is_within_tolerance(recon: Node) -> None:
    """narrative.md §5 requires clocks within one second of each other."""
    tracking = recon.stdout("chronyc", "-n", "tracking")
    offset_line = next(
        (line for line in tracking.splitlines() if line.startswith("System time")),
        None,
    )
    assert offset_line, f"chronyc tracking reported no system time:\n{tracking}"
    # "System time     : 0.000000012 seconds fast of NTP time"
    try:
        offset = float(offset_line.split(":")[1].strip().split()[0])
    except (IndexError, ValueError):  # pragma: no cover - format change
        pytest.fail(f"could not parse the offset from: {offset_line!r}")
    assert offset < MAX_OFFSET_SECONDS, (
        f"clock offset {offset}s exceeds {MAX_OFFSET_SECONDS}s: {offset_line}"
    )
