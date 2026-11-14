"""Episode inputs and seeds (episodes.md §4, episode inputs and seeds;
episodes-tests.md §1, episode inputs and seeds).

The expected seeds come from OpenSSL's hash-based message authentication code
(HMAC) with the Secure Hash Algorithm 256 (SHA-256), an implementation
independent of Python's hmac module. The command below computes each
generator's code, keyed with the episode seed.

    printf %s <generator> | openssl dgst -sha256 -mac HMAC -macopt hexkey:<seed>

Run with: pytest competition/tests -q
"""

from __future__ import annotations

import pytest

from competition.episode_runner import seeds

EXPECTED = {
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f": {
        "sim_tug_arrivals": 0x24443102C20550C4,
        "attack_start_step": 0xE9B3DB4CC4C23A4C,
        "attack_waits": 0xECA7321378219204,
    },
    "a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5": {
        "sim_tug_arrivals": 0xD5C8CE446B6E1D58,
        "attack_start_step": 0xC6538D543E48349A,
        "attack_waits": 0x63C2F6D73F92187C,
    },
}

# The seed of each host's generator of its first file-integrity scan, for the
# first episode seed above.
EXPECTED_SCAN_SEED = {
    "fms-01": 0x3323D9373E9365B6,
    "corp-it": 0xFADC8ACE7C1AC714,
}


def test_seed_derivation_matches_hmac_sha256() -> None:
    for episode_seed, generators in EXPECTED.items():
        assert seeds.derive_seeds(episode_seed) == generators


def test_each_host_has_a_scan_generator_of_its_own() -> None:
    episode_seed = next(iter(EXPECTED))
    for host, seed in EXPECTED_SCAN_SEED.items():
        assert seeds.scan_generator(host) == f"wazuh_first_file_integrity_scan/{host}"
        assert seeds.scan_seed(episode_seed, host) == seed


def test_an_added_generator_leaves_existing_seeds_unchanged() -> None:
    for episode_seed, generators in EXPECTED.items():
        derived = seeds.derive_seeds(
            episode_seed, generators=(*seeds.GENERATORS, "added")
        )
        assert {g: derived[g] for g in seeds.GENERATORS} == generators


# The episode seed of each fork of a living port, the whole code keyed with the
# living port seed over `fork/` and the fork's number:
#
#     printf %s fork/<k> | openssl dgst -sha256 -mac HMAC -macopt hexkey:<seed>
EXPECTED_FORK_SEED = {
    ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f", 1): (
        "e5bfee0bcf01978597b5a6a89108e98698ad609da3fb915c9231c664eb12ac86"
    ),
    ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f", 12): (
        "e004a565fff55cc3af33bd74bc09c4cd00e4a379c22e2c2a4244ba7e3196ba07"
    ),
    ("6f1d8c2a9b3e4f5061728394a5b6c7d8e9fa0b1c2d3e4f5061728394a5b6c7d8", 1): (
        "79a0761479baa03f44f737f568a378e8f65f3c5dde104011f3f7e93daeec3936"
    ),
}


def test_fork_seeds_derive_from_the_living_port_seed() -> None:
    for (living_port_seed, k), expected in EXPECTED_FORK_SEED.items():
        assert seeds.fork_seed(living_port_seed, k) == expected
        assert seeds.fork_seed(living_port_seed, 0) == living_port_seed


def test_a_fork_seed_needs_a_32_byte_seed_and_a_fork_from_0() -> None:
    living_port_seed = next(iter(EXPECTED_FORK_SEED))[0]
    with pytest.raises(ValueError):
        seeds.fork_seed(living_port_seed[:62], 1)
    with pytest.raises(ValueError):
        seeds.fork_seed(living_port_seed, -1)


def test_evaluation_living_port_seeds_are_32_random_bytes() -> None:
    drawn = [seeds.new_episode_seed() for _ in range(100)]
    assert all(len(bytes.fromhex(seed)) == 32 for seed in drawn)
    assert len(set(drawn)) == len(drawn)
