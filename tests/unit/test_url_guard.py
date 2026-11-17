"""Tests for the SSRF guard in :mod:`src.services.url_guard`.

These tests exercise the *real* guard — they never patch ``validate_url``
itself. ``socket.getaddrinfo`` is patched to provide deterministic DNS
answers without network traffic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from src.services.url_guard import (
    UnsafeURLError,
    _is_safe_ip,
    pin_request,
    validate_url,
)


def _gai_returning(*addrs: str) -> Any:
    """Build a ``socket.getaddrinfo``-style fake that returns *addrs* as A records."""

    def _fake(_host: str, _port: Any, *_args: Any, **_kwargs: Any) -> list[Any]:
        # Mimic getaddrinfo's 5-tuple shape.
        del _host, _port, _args, _kwargs
        return [(2, 1, 6, "", (addr, 0)) for addr in addrs]

    return _fake


@pytest.fixture
def patch_dns() -> Iterator[Any]:
    """Yield a helper that installs a fake getaddrinfo with given addrs."""
    patcher = patch("src.services.url_guard.socket.getaddrinfo")
    mock = patcher.start()
    try:

        def _set(*addrs: str) -> None:
            mock.side_effect = _gai_returning(*addrs)

        yield _set
    finally:
        patcher.stop()


# ---------------------------------------------------------------------------
# _is_safe_ip
# ---------------------------------------------------------------------------


class TestIsSafeIP:
    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.5",
            "172.16.0.5",
            "192.168.1.5",
            "169.254.169.254",
            "224.0.0.1",  # multicast
            "::1",
            "fe80::1",  # link-local v6
            "fc00::1",  # unique-local v6 (private)
            "240.0.0.1",  # reserved
        ],
    )
    def test_rejects_special_ranges(self, addr: str) -> None:
        assert _is_safe_ip(addr) is False

    @pytest.mark.parametrize("addr", ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
    def test_accepts_public(self, addr: str) -> None:
        assert _is_safe_ip(addr) is True

    def test_invalid_address_is_unsafe(self) -> None:
        assert _is_safe_ip("not-an-ip") is False


# ---------------------------------------------------------------------------
# validate_url — scheme & port
# ---------------------------------------------------------------------------


class TestValidateScheme:
    @pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "data", "javascript"])
    def test_rejects_non_http(self, scheme: str) -> None:
        with pytest.raises(UnsafeURLError, match="Unsupported URL scheme"):
            validate_url(f"{scheme}://example.com/x")

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(UnsafeURLError, match="no hostname"):
            validate_url("http:///nohost")


class TestValidatePort:
    def test_rejects_disallowed_port(self, patch_dns: Any) -> None:
        patch_dns("1.1.1.1")
        with pytest.raises(UnsafeURLError, match="Port 22"):
            validate_url("http://example.com:22/x")

    def test_allows_explicit_opt_in_port(self, patch_dns: Any) -> None:
        patch_dns("1.1.1.1")
        # Should not raise
        validate_url("http://example.com:8080/x", allowed_ports={80, 8080})

    def test_default_https_port_allowed(self, patch_dns: Any) -> None:
        patch_dns("1.1.1.1")
        validate_url("https://example.com/x")  # implicit 443

    def test_default_http_port_allowed(self, patch_dns: Any) -> None:
        patch_dns("1.1.1.1")
        validate_url("http://example.com/x")  # implicit 80


# ---------------------------------------------------------------------------
# validate_url — IP / DNS
# ---------------------------------------------------------------------------


class TestValidateAddress:
    def test_localhost_literal_rejected(self) -> None:
        # Pure literal IPs skip getaddrinfo entirely.
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://127.0.0.1/x")

    def test_ipv6_loopback_rejected(self) -> None:
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://[::1]/x")

    def test_metadata_ip_rejected(self) -> None:
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://169.254.169.254/latest/meta-data/")

    @pytest.mark.parametrize("addr", ["10.0.0.5", "172.16.0.5", "192.168.1.5", "169.254.42.42"])
    def test_rfc1918_and_link_local_rejected(self, addr: str) -> None:
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url(f"http://{addr}/x")

    def test_dns_resolving_to_internal_rejected(self, patch_dns: Any) -> None:
        patch_dns("10.0.0.5")
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://internal.example.com/x")

    def test_dns_resolving_to_metadata_rejected(self, patch_dns: Any) -> None:
        patch_dns("169.254.169.254")
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://meta.example.com/x")

    def test_mixed_public_and_private_rejected(self, patch_dns: Any) -> None:
        # Any private address in the resolution set is fatal — DNS rebinding /
        # multi-A defense.
        patch_dns("1.1.1.1", "10.0.0.5")
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://mixed.example.com/x")

    def test_public_dns_allowed(self, patch_dns: Any) -> None:
        patch_dns("1.1.1.1")
        # Should not raise
        validate_url("http://public.example.com/x")

    def test_public_ip_literal_allowed(self) -> None:
        validate_url("http://1.1.1.1/x")

    def test_dns_failure_rejected(self) -> None:
        import socket

        with patch(
            "src.services.url_guard.socket.getaddrinfo",
            side_effect=socket.gaierror("no such host"),
        ):
            with pytest.raises(UnsafeURLError, match="DNS resolution failed"):
                validate_url("http://nonexistent.invalid/x")


# ---------------------------------------------------------------------------
# validate_url — trusted-host allowlist (storage.internal artifact fetch)
# ---------------------------------------------------------------------------


class TestTrustedHosts:
    _STORAGE = "http://storage.internal:8391/api/files/bucket/key.mp4"

    def test_default_no_allowlist_rejects_internal_host(self, patch_dns: Any) -> None:
        # Regression guard: with no trusted_hosts, the in-cluster host is rejected
        # (non-standard port 8391 fails the port check before DNS even matters).
        patch_dns("10.0.0.5")
        with pytest.raises(UnsafeURLError):
            validate_url(self._STORAGE)

    def test_trusted_host_bypasses_port_and_ip_checks(self) -> None:
        # Exact-host match short-circuits before DNS — getaddrinfo is never
        # called, so a private resolution and the odd port are both tolerated.
        validate_url(self._STORAGE, trusted_hosts={"storage.internal"})

    def test_non_trusted_internal_host_still_rejected(self, patch_dns: Any) -> None:
        # An allowlist for storage.internal must NOT exempt any other host.
        patch_dns("10.0.0.5")
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://other.internal/x", trusted_hosts={"storage.internal"})

    def test_trusted_host_redirect_to_private_ip_rejected(self) -> None:
        # Simulates a redirect hop: the redirect *target* (a literal private IP)
        # is not on the allowlist, so it gets the full check and is rejected.
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://10.0.0.5/x", trusted_hosts={"storage.internal"})

    def test_trusted_host_still_enforces_scheme(self) -> None:
        with pytest.raises(UnsafeURLError, match="Unsupported URL scheme"):
            validate_url("file://storage.internal/x", trusted_hosts={"storage.internal"})


# ---------------------------------------------------------------------------
# RFC 6598 shared address space (100.64.0.0/10) == the Tailscale tailnet
# ---------------------------------------------------------------------------
#
# `ipaddress.ip_address("100.64.0.5").is_private` is False, and so are
# is_loopback / is_link_local / is_multicast / is_reserved / is_unspecified.
# The pre-fix guard was a NOT over exactly those flags, so every machine on the
# estate (worker4 100.64.0.5, worker3 100.64.0.6, master 100.64.0.7,
# the mini/pgx GPU hosts) was declared "globally routable, non-special" and a
# `file_source` URL pointed at any internal service sailed through.


_TAILNET_ADDRS = (
    "100.64.0.1",  # bottom of the /10
    "100.64.0.5",  # worker4 — bare-metal postgres
    "100.64.0.6",  # worker3 — docker registry
    "100.64.0.7",  # master — k3s control plane
    "100.127.255.254",  # top of the /10
)


class TestTailnetCGNATIsBlocked:
    @pytest.mark.parametrize("addr", _TAILNET_ADDRS)
    def test_cgnat_literal_is_not_safe(self, addr: str) -> None:
        assert _is_safe_ip(addr) is False

    @pytest.mark.parametrize("addr", _TAILNET_ADDRS)
    def test_cgnat_literal_url_rejected(self, addr: str) -> None:
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url(f"http://{addr}/artifact.mp4")

    @pytest.mark.parametrize("addr", _TAILNET_ADDRS)
    def test_cgnat_via_dns_rejected(self, addr: str, patch_dns: Any) -> None:
        # Split-DNS makes *.internal resolve to a tailnet IP that looks public.
        patch_dns(addr)
        with pytest.raises(UnsafeURLError, match="non-public"):
            validate_url("http://artifacts.example.com/x")

    @pytest.mark.parametrize(
        "addr",
        [
            "::ffff:100.64.0.1",  # IPv4-mapped tailnet
            "::ffff:100.64.0.5",
            "2002:6471:2030::1",  # 6to4-wrapped 100.64.0.5
            "64:ff9b::6471:2030",  # NAT64 well-known prefix wrapping the tailnet
        ],
    )
    def test_ipv6_wrapped_tailnet_is_not_safe(self, addr: str) -> None:
        assert _is_safe_ip(addr) is False

    @pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34", "1.1.1.1"])
    def test_public_ips_still_accepted(self, addr: str) -> None:
        assert _is_safe_ip(addr) is True

    @pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34"])
    def test_public_ip_url_still_allowed(self, addr: str, patch_dns: Any) -> None:
        patch_dns(addr)
        validate_url("https://example.com/video.mp4")  # must not raise

    def test_public_neighbours_of_the_cgnat_block_still_allowed(self) -> None:
        # 100.63.255.255 and 100.128.0.0 bracket 100.64.0.0/10 — proof the deny
        # is the /10 and not a sloppy "anything starting with 100.".
        assert _is_safe_ip("100.63.255.255") is True
        assert _is_safe_ip("100.128.0.0") is True


class TestPinRequest:
    """``pin_request`` rewrites the connect target without losing identity.

    Pinning is what closes the DNS-rebinding window. Validating a hostname and
    then handing that hostname to httpx lets httpx resolve it a SECOND time --
    the guard approves a public address, the resolver flips, and the connect
    lands on an internal one. These tests lock the rewrite's shape, because a
    pin that mangles Host or SNI would break TLS and get reverted in a hurry.
    """

    def test_rewrites_host_to_literal_ip(self) -> None:
        url, headers, ext = pin_request("https://example.com/video.mp4", "93.184.216.34")
        assert url == "https://93.184.216.34/video.mp4"
        assert headers["Host"] == "example.com"
        assert ext["sni_hostname"] == "example.com"

    def test_preserves_explicit_port(self) -> None:
        url, headers, _ = pin_request("http://example.com:8080/a", "93.184.216.34")
        assert url == "http://93.184.216.34:8080/a"
        assert headers["Host"] == "example.com:8080"

    def test_brackets_ipv6_literal(self) -> None:
        url, _, ext = pin_request("https://example.com/x", "2606:2800:220:1:248:1893:25c8:1946")
        assert url == "https://[2606:2800:220:1:248:1893:25c8:1946]/x"
        assert ext["sni_hostname"] == "example.com"

    def test_preserves_query_and_fragment(self) -> None:
        url, _, _ = pin_request("https://example.com/v?sig=abc&x=1", "93.184.216.34")
        assert url == "https://93.184.216.34/v?sig=abc&x=1"

    def test_none_ip_passes_through_untouched(self) -> None:
        # A trusted host is deliberately NOT pinned: presigned artifact URLs
        # carry a signature computed over the original Host.
        url, headers, ext = pin_request("https://storage.internal/a.mp4", None)
        assert url == "https://storage.internal/a.mp4"
        assert headers == {} and ext == {}

    def test_does_not_leak_userinfo_into_host_header(self) -> None:
        _, headers, ext = pin_request("https://user:pw@example.com/x", "93.184.216.34")
        assert headers["Host"] == "example.com"
        assert "pw" not in headers["Host"]
        assert ext["sni_hostname"] == "example.com"


class TestValidateUrlReturnsPinAddress:
    """``validate_url`` must hand back the address it approved."""

    def test_returns_the_validated_address(self, patch_dns: Any) -> None:
        patch_dns("93.184.216.34")
        assert validate_url("https://example.com/v.mp4") == "93.184.216.34"

    def test_literal_ip_url_returns_that_ip(self) -> None:
        assert validate_url("https://93.184.216.34/v.mp4") == "93.184.216.34"

    def test_trusted_host_returns_none_meaning_do_not_pin(self) -> None:
        assert (
            validate_url("https://storage.internal/a.mp4", trusted_hosts={"storage.internal"})
            is None
        )

    def test_returned_address_is_always_one_that_passed(self, patch_dns: Any) -> None:
        # Multi-A hostname: every address is checked, so whichever is returned
        # is safe by construction.
        patch_dns("93.184.216.34", "1.1.1.1")
        assert validate_url("https://example.com/v.mp4") in {"93.184.216.34", "1.1.1.1"}
