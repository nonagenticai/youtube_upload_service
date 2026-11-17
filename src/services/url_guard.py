"""SSRF guard for external URL fetches.

Validates URLs (and each redirect hop) before HTTP requests reach the network:

* scheme must be http/https
* port must be in :data:`_DEFAULT_ALLOWED_PORTS` (or in the caller-provided set)
* the hostname must resolve (via :func:`socket.getaddrinfo`) only to public IPs;
  any private/loopback/link-local/multicast/reserved/unspecified address makes
  the URL fail closed — as does RFC 6598 shared address space
  (``100.64.0.0/10``), which on this fleet is the Tailscale tailnet and which
  :mod:`ipaddress` reports as neither private nor reserved

DNS-rebinding (TOCTOU) is closed by PINNING, not merely by checking: this module
used to validate the hostname and then hand that same hostname to :mod:`httpx`,
which resolved it a second time — so a resolver that flipped its answer between
the two lookups reached an internal address with the guard reporting "safe".
:func:`validate_url` now RETURNS the validated address and :func:`pin_request`
rewrites the request to connect to that literal IP (preserving the ``Host``
header and SNI), so the connect step has nothing left to re-resolve.

The old caveat named an egress firewall as the real mitigation. That firewall
does exist and does work — the ``w2v`` NetworkPolicy egress ``except:`` list is
enforced by kube-router (verified 2026-08-02: from a w2v pod, ``100.64.0.6:5000``
and ``169.254.169.254:80`` both RST, while ``1.1.1.1:80`` returns a real 301).
It is a genuine second layer, not a substitute for this one: it is scoped per
namespace, so a service running anywhere without that policy has only this guard.

⚠️ Measuring it requires care. In an ambient-mesh pod ``socket.connect()``
completes against ztunnel's in-pod listener BEFORE any policy decision, so a
connect-only probe reports every address as reachable on a perfectly enforcing
cluster. A valid probe must send bytes and read a reply; blocked shows up as a
reset, not a refused connect.

Explicitly trusted hosts (``trusted_hosts``) are NOT pinned: those are presigned
artifact URLs whose signature covers the ``Host``, and they are exempt from the
IP-range check by design.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse, urlsplit

_DEFAULT_ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})

# Ranges `ipaddress` does NOT classify as private/reserved (or classifies only
# partially) but which are reachable from inside the estate, so they are SSRF
# targets rather than "the public internet". 100.64.0.0/10 is the load-bearing
# one: RFC 6598 shared address space IS the Tailscale tailnet (worker4
# 100.64.0.5, worker3 100.64.0.6, master 100.64.0.7 all live there and
# every one of them reports `is_private is False`, so the flag list below waved
# the whole fleet through as "public").
_EXTRA_DENY_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 shared address space == Tailscale tailnet
    ipaddress.ip_network(
        "192.0.0.0/24"
    ),  # RFC 6890 IETF protocol assignments (only /29 is is_private)
    ipaddress.ip_network("198.18.0.0/15"),  # RFC 2544 benchmarking
    ipaddress.ip_network(
        "64:ff9b::/96"
    ),  # RFC 6052 NAT64 well-known prefix (is_global reports True)
    ipaddress.ip_network("64:ff9b:1::/48"),  # RFC 8215 NAT64 local-use prefix
)


class UnsafeURLError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


def _embedded_ipv4(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """Return the IPv4 address smuggled inside an IPv6 form, or ``None``.

    ``::ffff:100.64.0.1`` is the tailnet wearing an IPv6 hat — the v6 address
    itself is neither private nor reserved, so the v4 rules must be applied to
    the address it actually reaches. Covers IPv4-mapped, 6to4, Teredo and the
    NAT64 well-known prefix.
    """
    if not isinstance(ip, ipaddress.IPv6Address):
        return None
    for candidate in (ip.ipv4_mapped, ip.sixtofour):
        if candidate is not None:
            return candidate
    if ip.teredo is not None:
        # (server, client) — the client address is the one traffic reaches.
        return ip.teredo[1]
    if any(ip in net for net in _EXTRA_DENY_NETWORKS if net.version == 6):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_safe_ip(addr: str) -> bool:
    """Return True iff *addr* is a globally routable, non-special IP.

    An IPv4 address wrapped in an IPv6 form is unwrapped and re-checked against
    the whole rule set, so ``::ffff:100.64.0.1`` cannot smuggle a tailnet
    target past the v4 rules.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return _is_public_ip(ip)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True iff the IP is routable on the public internet."""
    embedded = _embedded_ipv4(ip)
    if embedded is not None:
        return _is_public_ip(embedded)

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    if any(ip in net for net in _EXTRA_DENY_NETWORKS if net.version == ip.version):
        return False
    # Backstop: `is_global` already excludes shared address space and the IANA
    # special-purpose registry, so anything it rejects is not a fetch target.
    return bool(ip.is_global)


def _resolve_host(host: str) -> list[str]:
    """Resolve *host* to all A/AAAA addresses.

    Pure literal IPs are returned as-is so callers can validate them too.
    """
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for host {host!r}: {exc}") from exc

    # info[4] is the sockaddr tuple; for AF_INET it's (host, port), for AF_INET6
    # it's (host, port, flowinfo, scopeid). The first element is always a str.
    return [str(info[4][0]) for info in infos]


def validate_url(
    url: str,
    *,
    allowed_ports: frozenset[int] | set[int] | None = None,
    trusted_hosts: frozenset[str] | set[str] | None = None,
) -> str | None:
    """Validate *url* against the SSRF policy.

    Raises :class:`UnsafeURLError` on any policy violation. The caller should
    re-invoke this on each redirect hop.

    Returns the single validated IP address that the connection MUST be pinned
    to — pass it to :func:`pin_request`. Returns ``None`` for an explicitly
    trusted host, which is deliberately left un-pinned so a presigned URL's
    ``Host``-covering signature survives.

    Returning the address is what closes the DNS-rebinding window: every
    resolved address was just checked, so connecting to one of them cannot
    reach anywhere the check did not approve. Callers that discard the return
    value keep the old, re-resolving behaviour — which is why
    ``_stream_with_redirects`` does not.

    *trusted_hosts* is an explicit allowlist of hostnames that are exempt from
    the private-IP and port checks (e.g. the in-cluster ``storage.internal``
    artifact host, which resolves to a private IP on a non-standard port). The
    scheme check still applies, and the host match is exact. The default empty
    set preserves the strict fail-closed behaviour everywhere it is not
    configured. Each redirect hop is re-validated against the same allowlist, so
    a trusted host that redirects to a non-allowlisted host gets full checks.
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported URL scheme: {parsed.scheme!r} (only http/https allowed)")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError(f"URL has no hostname: {url!r}")

    if trusted_hosts and host in trusted_hosts:
        # Explicitly trusted internal host — skip the port + IP-range checks,
        # and signal "do not pin" so a presigned URL keeps its original Host.
        return None

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    ports = frozenset(allowed_ports) if allowed_ports is not None else _DEFAULT_ALLOWED_PORTS
    if port not in ports:
        raise UnsafeURLError(
            f"Port {port} is not in the allowed set {sorted(ports)} for URL {url!r}"
        )

    addrs = _resolve_host(host)
    if not addrs:
        raise UnsafeURLError(f"Host {host!r} did not resolve to any address")

    unsafe = [addr for addr in addrs if not _is_safe_ip(addr)]
    if unsafe:
        raise UnsafeURLError(f"Host {host!r} resolves to non-public address(es) {unsafe}; refusing")

    # EVERY resolved address passed, so pinning to the first is safe — it is
    # also the one the transport would have tried first anyway. The point is
    # that it is now FIXED: the connect step cannot re-resolve to an address
    # this function never saw.
    return addrs[0]


def pin_request(url: str, ip: str | None) -> tuple[str, dict[str, str], dict[str, str]]:
    """Return ``(request_url, headers, extensions)`` for an IP-pinned request.

    Rewrites *url*'s host to the literal *ip* while preserving the original
    ``Host`` header and setting ``sni_hostname``, so TLS certificate validation
    and virtual-host routing still work against the real hostname. ``ip=None``
    (a trusted host) passes the URL through untouched.

    IPv6 literals are bracketed; an explicit port is preserved. Userinfo is
    intentionally dropped from the ``Host`` header value — ``urlsplit``'s
    ``hostname``/``port`` already exclude it, and echoing credentials into a
    header would leak them.
    """
    if ip is None:
        return url, {}, {}

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = f"{literal}:{parsed.port}" if parsed.port is not None else literal

    # Host must carry the ORIGINAL host (and port, when explicit) — this is what
    # the origin server routes on and what the TLS cert is checked against.
    host_header = f"{host}:{parsed.port}" if parsed.port is not None else host

    return parsed._replace(netloc=netloc).geturl(), {"Host": host_header}, {"sni_hostname": host}
