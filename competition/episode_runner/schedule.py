"""The first file-integrity scan of each monitored host (episodes.md §6.2,
first occurrences).

Each monitored host has a generator of its own, named
wazuh_first_file_integrity_scan/ followed by the host's name, such as
wazuh_first_file_integrity_scan/fms-01. The generator's seed (episodes.md §4.2,
derivation), divided by 2^64, gives a fraction. The host's first
file-integrity scan falls at that fraction of the compressed interval after the
simulator's start.
"""

from __future__ import annotations

from collections.abc import Iterable

from competition.episode_runner.seeds import scan_seed


def scan_fraction(episode_seed_hex: str, host: str) -> float:
    """The fraction of the compressed interval, at least 0 and less than 1."""
    return scan_seed(episode_seed_hex, host) / 2**64


def first_scans(
    episode_seed_hex: str, hosts: Iterable[str], interval_s: float
) -> dict[str, float]:
    """Seconds from the simulator's start to each host's first file-integrity
    scan, within one compressed interval."""
    return {host: scan_fraction(episode_seed_hex, host) * interval_s for host in hosts}


def scans_inside(
    times: dict[str, float], start_s: float, end_s: float
) -> list[tuple[float, str]]:
    """The scans whose time falls inside the episode, from start_s to end_s
    after the simulator's start, as (time, host) pairs in time order."""
    return sorted(
        (time_s, host) for host, time_s in times.items() if start_s <= time_s < end_s
    )
