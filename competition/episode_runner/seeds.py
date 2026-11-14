"""Episode inputs and seeds (episodes.md §4, episode inputs and seeds).

Each generator of an episode, a source of random draws, is named by the
function that draws from it (episodes.md §4.2, derivation).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SIM_TUG_ARRIVALS = "sim_tug_arrivals"
ATTACK_START_STEP = "attack_start_step"
ATTACK_WAITS = "attack_waits"
# The generators whose seeds the episode record keeps. Each monitored host's
# first file-integrity scan has a generator of its own as well, scan_generator.
GENERATORS = (SIM_TUG_ARRIVALS, ATTACK_START_STEP, ATTACK_WAITS)
SCAN_GENERATOR_PREFIX = "wazuh_first_file_integrity_scan/"


def derive_generator_seed(episode_seed_hex: str, generator: str) -> int:
    """The seed of one generator of an episode (episodes.md §4.2, derivation):
    the first eight bytes of HMAC-SHA256 keyed with the 32-byte episode seed
    over the generator's name in ASCII, read as an unsigned integer, most
    significant byte first."""
    code = hmac.new(
        bytes.fromhex(episode_seed_hex), generator.encode("ascii"), hashlib.sha256
    ).digest()
    return int.from_bytes(code[:8], "big")


def scan_generator(host: str) -> str:
    """The generator of a monitored host's first file-integrity scan, such as
    wazuh_first_file_integrity_scan/fms-01 (episodes.md §4.2, derivation)."""
    return SCAN_GENERATOR_PREFIX + host


def scan_seed(episode_seed_hex: str, host: str) -> int:
    """The seed of a monitored host's generator scan_generator(host)."""
    return derive_generator_seed(episode_seed_hex, scan_generator(host))


def fork_seed(living_port_seed_hex: str, fork: int) -> str:
    """The episode seed of a living port's fork (episodes.md §4.2, derivation):
    for fork k from 1, the whole HMAC-SHA256 keyed with the living port seed's
    32 bytes over `fork/` and k in decimal digits, in ASCII, as hexadecimal. The
    episode seed of fork 0 is the living port seed itself."""
    key = bytes.fromhex(living_port_seed_hex)
    if len(key) != 32:
        raise ValueError("a living port seed is 32 bytes")
    if fork < 0:
        raise ValueError(f"no fork {fork}")
    if fork == 0:
        return key.hex()
    return hmac.new(key, f"fork/{fork}".encode("ascii"), hashlib.sha256).hexdigest()


def new_episode_seed() -> str:
    """A living port seed of evaluation mode, which is also the episode seed
    of the living port's fork 0: 32 bytes from the operating system's secure
    random source, as hexadecimal (episodes.md §4.1, inputs; §4.2,
    derivation)."""
    return secrets.token_bytes(32).hex()


def derive_seeds(
    episode_seed_hex: str, generators: tuple[str, ...] = GENERATORS
) -> dict[str, int]:
    """The seed of each generator (episodes.md §4.2, derivation)."""
    return {
        generator: derive_generator_seed(episode_seed_hex, generator)
        for generator in generators
    }
