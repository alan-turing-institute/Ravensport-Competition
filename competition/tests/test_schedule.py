"""The first file-integrity scan of each monitored host (episodes.md §6.2,
first occurrences; episodes-tests.md §3, start state, living ports and forks).

The expected codes come from OpenSSL's hash-based message authentication code
(HMAC) with the Secure Hash Algorithm 256 (SHA-256), an implementation
independent of Python's hmac module. The command below computes each code,
keyed with the episode seed over the name of the host's generator.

    printf %s wazuh_first_file_integrity_scan/<host> \
        | openssl dgst -sha256 -mac HMAC -macopt hexkey:<seed>

The first eight bytes of each code, divided by 2^64, give the fraction of the
compressed interval after the simulator's start.

Run with: pytest competition/tests -q
"""

from __future__ import annotations

from competition.episode_runner import schedule

# A compression factor of 5 shortens the 12 hours in operation to 8,640
# seconds.
INTERVAL_S = 12 * 3600 / 5

CODES = {
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f": {
        "fms-01": 0x3323D9373E9365B6,
        "hmi-01": 0xDCCAC6A91439EEDF,
        "hist-01": 0x6B3E20002CA65995,
        "hist-dmz-01": 0x89969A099CC75E41,
        "atg-relay-01": 0xF5A053A4B83E679D,
        "dmz-bastion-01": 0xAFEDB716E24C15DF,
        "corp-it": 0xFADC8ACE7C1AC714,
        "recon-01": 0xD05B7F2D3A27737B,
        "admin-01": 0x75B5626C1406B2E5,
        "fw-ext-dmz": 0x5BCC2BE1376C4604,
        "fw-dmz-it": 0x4CFC4E8BF850B3A3,
        "fw-ot-dmz": 0xBFD47A5F886545DF,
    },
    "a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5": {
        "fms-01": 0x3BE8C182AE0E7319,
        "hmi-01": 0xB5D80BC808893314,
        "hist-01": 0x873444145BEA1D98,
        "hist-dmz-01": 0x255D3141D5E5D125,
        "atg-relay-01": 0x3265491AF47566A5,
        "dmz-bastion-01": 0x14DE85EEDD29ED2B,
        "corp-it": 0xD6C40816231B9696,
        "recon-01": 0xA4ABDFECC3D6361B,
        "admin-01": 0x06B8D5ED69834297,
        "fw-ext-dmz": 0x9291309314FC2BE3,
        "fw-dmz-it": 0xF672025305E82679,
        "fw-ot-dmz": 0x25AB756D4F9AB602,
    },
}


def test_scan_draws_match_hmac_sha256() -> None:
    for episode_seed, hosts in CODES.items():
        times = schedule.first_scans(episode_seed, list(hosts), INTERVAL_S)
        assert times == {
            host: code / 2**64 * INTERVAL_S for host, code in hosts.items()
        }


def test_a_host_left_out_leaves_every_other_time_unchanged() -> None:
    for episode_seed, hosts in CODES.items():
        everyone = schedule.first_scans(episode_seed, list(hosts), INTERVAL_S)
        for left_out in hosts:
            rest = [host for host in hosts if host != left_out]
            assert schedule.first_scans(episode_seed, rest, INTERVAL_S) == {
                host: everyone[host] for host in rest
            }


def test_every_time_falls_within_the_compressed_interval() -> None:
    for episode_seed, hosts in CODES.items():
        for time_s in schedule.first_scans(
            episode_seed, list(hosts), INTERVAL_S
        ).values():
            assert 0 <= time_s < INTERVAL_S


def test_only_scans_inside_the_episode_are_requested() -> None:
    times = {"fms-01": 100.0, "hmi-01": 700.0, "hist-01": 40.0, "corp-it": 160.0}
    # The episode runs from 100 s to 700 s after the simulator's start.
    assert schedule.scans_inside(times, 100.0, 700.0) == [
        (100.0, "fms-01"),
        (160.0, "corp-it"),
    ]
