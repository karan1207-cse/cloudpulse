"""Tests for URL validation and SSRF protection."""

import ipaddress
import socket

import pytest

from cloudpulse.errors import UnsafeURLError
from cloudpulse.url_safety import (
    BlockedDestinationError,
    UrlPolicy,
    is_blocked_address,
    redact_url,
    validate_url,
)


def resolver_for(*addresses: str):
    def resolve(host: str, port: int) -> list[str]:
        return list(addresses)

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "ftp://example.com",
        "file:///etc/passwd",
        "gopher://example.com",
        "http://",
        "example.com",
        "javascript:alert(1)",
        "http://exa mple.com",
        "http://example.com/\nX-Injected: 1",
        "http://example.com:99999/",
        "http://user:secret@example.com/",
        "https://" + "a" * 2050 + ".com",
    ],
)
def test_validate_url_rejects_malformed_or_unsafe_urls(url):
    with pytest.raises(UnsafeURLError):
        validate_url(url)


def test_validate_url_accepts_http_and_https():
    assert validate_url("http://example.com").hostname == "example.com"
    assert validate_url("HTTPS://Example.com:8443/health?x=1").port == 8443


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata endpoint
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00:ec2::254",  # AWS IPv6 metadata endpoint
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",
    ],
)
def test_non_public_addresses_are_blocked(address):
    assert is_blocked_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["93.184.215.14", "8.8.8.8", "2606:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert is_blocked_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.1.2.3/",
    ],
)
def test_policy_blocks_ip_literals(url):
    with pytest.raises(BlockedDestinationError):
        UrlPolicy().check_url(url)


def test_policy_blocks_hostnames_resolving_to_private_addresses():
    policy = UrlPolicy(resolver=resolver_for("93.184.215.14", "10.0.0.1"))

    # One private address among public ones is enough to block (DNS tricks).
    with pytest.raises(BlockedDestinationError, match="10.0.0.1"):
        policy.check_resolved("internal.example.com", 443)


def test_policy_blocks_localhost_by_resolution():
    policy = UrlPolicy(resolver=resolver_for("127.0.0.1"))

    with pytest.raises(BlockedDestinationError):
        policy.check_resolved("localhost", 80)


def test_policy_allows_public_resolution():
    policy = UrlPolicy(resolver=resolver_for("93.184.215.14"))

    assert policy.check_resolved("example.com", 443) == ["93.184.215.14"]


def test_policy_reports_empty_resolution_as_dns_error():
    with pytest.raises(socket.gaierror):
        UrlPolicy(resolver=resolver_for()).check_resolved("nothing.example", 80)


def test_policy_checks_connected_peer():
    with pytest.raises(BlockedDestinationError):
        UrlPolicy().check_peer("127.0.0.1")
    UrlPolicy().check_peer("93.184.215.14")


def test_allow_private_permits_local_testing():
    policy = UrlPolicy(allow_private=True, resolver=resolver_for("127.0.0.1"))

    policy.check_url("http://127.0.0.1:8080/health")
    assert policy.check_resolved("localhost", 8080) == ["127.0.0.1"]
    policy.check_peer("127.0.0.1")


def test_allow_private_still_validates_syntax():
    with pytest.raises(UnsafeURLError):
        UrlPolicy(allow_private=True).check_url("file:///etc/passwd")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:pw@example.com/a?token=s3cret#frag", "https://example.com/a"),
        ("http://example.com:8080/health", "http://example.com:8080/health"),
        ("http://[::1]:81/x?y", "http://[::1]:81/x"),
    ],
)
def test_redact_url_removes_credentials_and_query(url, expected):
    assert redact_url(url) == expected
