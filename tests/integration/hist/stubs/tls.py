"""
TLS certificate inspection for the log server test harness (hist-tests.md §1-§4,
§7).

Every network-facing service on `hist-01`/`hist-dmz-01` now terminates TLS with a
certificate signed by the RavensPort internal CA (hist.md §9/§10.2/§10.3), and the
two hosts hold distinct keypairs. These helpers drive `openssl s_client`/`openssl
x509` to:

  - fetch the certificate a service presents, from inside a container (`incus
    exec`) for a loopback-bound service such as Grafana, or directly from the test
    runner for a service hist-01 exposes to the network (vmagent, vlagent); and
  - confirm it is issued by the RavensPort internal CA and carries the expected
    hostname SAN, or compare two hosts' certificates for distinctness.

Certificates carry a DNS SAN of the host's own hostname only, never an IP SAN
(digital.md §2.7), so a caller connecting by IP must pass `sni=` separately to
present the hostname the certificate was issued for — the same override `curl
--resolve` provides at the command line.

PEM inspection uses only the `openssl` CLI, run locally or via `incus exec`,
consistent with `deploy/tests/unit/test_ca_provisioning.py`'s use of the same
tool on the build host.
"""

from __future__ import annotations

import hashlib
import ssl
import subprocess

_BEGIN = "-----BEGIN CERTIFICATE-----"
_END = "-----END CERTIFICATE-----"

CA_COMMON_NAME = "RavensPort Internal CA"


def _run_sh(
    node: str | None, script: str, timeout: float
) -> subprocess.CompletedProcess:
    """Run a shell script locally, or inside `node` via `incus exec` when given.

    `node=None` is used for a service the test runner reaches directly across
    the network (hist-01's vmagent/vlagent, per hist-tests.md §0); a loopback-
    bound service (Grafana, hist-dmz-01's stores read via their own container)
    is fetched through `incus exec` instead.
    """
    if node is None:
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, timeout=timeout
        )
    return subprocess.run(
        ["incus", "exec", node, "--", "sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def s_client_output(
    node: str | None,
    host: str,
    port: int,
    *,
    sni: str | None = None,
    timeout: float = 15.0,
) -> str:
    """Run `openssl s_client` against `host:port`, from inside `node` if given or
    else directly from the test runner, and return its combined output: handshake
    detail, the verify return code, and the peer certificate.

    `sni` overrides the TLS server name sent (and so the name a strict verify
    checks the certificate against) independently of `host` — needed when `host`
    is an IP address but the certificate's SAN is a hostname. Defaults to `host`.
    """
    servername = sni or host
    script = (
        f"echo | openssl s_client -connect {host}:{port} -servername {servername} 2>&1"
    )
    result = _run_sh(node, script, timeout)
    return result.stdout + result.stderr


def certificate_pem(
    node: str | None,
    host: str,
    port: int,
    *,
    sni: str | None = None,
    timeout: float = 15.0,
) -> str:
    """Fetch the PEM certificate `host:port` presents, from inside `node` if given
    or else directly from the test runner. Raises if no certificate was returned
    (a handshake failure)."""
    servername = sni or host
    script = (
        f"echo | openssl s_client -connect {host}:{port} "
        f"-servername {servername} 2>/dev/null | openssl x509 -outform PEM"
    )
    result = _run_sh(node, script, timeout)
    pem = result.stdout
    if _BEGIN not in pem or _END not in pem:
        raise RuntimeError(
            f"no certificate returned from {host}:{port} via "
            f"{node or 'the test runner'} (stderr: {result.stderr.strip()})"
        )
    # openssl may prepend connection notes; trim to the certificate block.
    start = pem.index(_BEGIN)
    end = pem.index(_END) + len(_END)
    return pem[start:end] + "\n"


def fingerprint(pem: str) -> str:
    """SHA-256 fingerprint of a PEM certificate. Stdlib PEM->DER, no external
    dependency."""
    der = ssl.PEM_cert_to_DER_cert(pem)
    return hashlib.sha256(der).hexdigest()


def has_certificate(s_client_text: str) -> bool:
    """True if the s_client output contains a peer certificate — i.e. the TLS
    handshake completed."""
    return _BEGIN in s_client_text


def issuer_common_name(pem: str) -> str:
    """The issuer's Common Name, as `openssl x509 -noout -issuer` reports it."""
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-issuer"],
        input=pem,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if result.returncode != 0:
        raise RuntimeError(f"openssl x509 -issuer failed: {result.stderr.strip()}")
    return result.stdout.strip()


def is_signed_by_ravensport_ca(pem: str) -> bool:
    """True if the certificate's issuer is the RavensPort internal CA
    (digital.md §2.7) rather than self-signed or some other issuer."""
    return CA_COMMON_NAME in issuer_common_name(pem)


def has_dns_san(pem: str, name: str) -> bool:
    """True if the certificate carries a DNS subject alternative name of exactly
    `name` (digital.md §2.7: hostname-only SANs, no IP SAN)."""
    result = subprocess.run(
        ["openssl", "x509", "-noout", "-ext", "subjectAltName"],
        input=pem,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"openssl x509 -ext subjectAltName failed: {result.stderr.strip()}"
        )
    return f"DNS:{name}" in result.stdout
