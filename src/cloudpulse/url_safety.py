"""Target URL validation and SSRF protection.

CloudPulse fetches URLs from configuration, so a misconfigured or malicious
target must never be able to reach internal infrastructure (loopback, private
networks, link-local addresses such as the cloud metadata service).

Protection happens in three layers:

1. :func:`validate_url` — syntax, scheme allow-list, no embedded credentials,
   and IP-literal hosts checked directly.
2. :meth:`UrlPolicy.check_resolved` — the hostname is resolved and *every*
   returned address must be allowed.
3. :meth:`UrlPolicy.check_peer` — the address the socket actually connected
   to is checked, which closes the DNS-rebinding gap between steps 2 and the
   real connection. The HTTP transport connects to the exact address that
   was validated, so DNS rebinding cannot swap it. Redirect targets go
   through all three layers again.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

from cloudpulse.errors import UnsafeURLError


class BlockedDestinationError(UnsafeURLError):
    """The URL is well-formed but targets a non-public address."""


ALLOWED_SCHEMES = ("http", "https")
MAX_URL_LENGTH = 2048

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Iterable[str]]


def _default_resolver(host: str, port: int) -> list[str]:
    """Resolve a hostname to the list of IP address strings."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def is_blocked_address(address: IPAddress) -> bool:
    """Return True for any address that is not publicly routable.

    This blocks loopback, RFC 1918 private ranges, link-local (including the
    169.254.169.254 metadata endpoint), carrier-grade NAT, multicast,
    reserved, unspecified, and IPv6 unique-local / IPv4-mapped equivalents.
    """
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped or address.sixtofour
        if mapped is not None:
            return is_blocked_address(mapped)
    return (
        not address.is_global
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    )


def _parse_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def validate_url(url: str) -> SplitResult:
    """Validate URL syntax and return the parsed result.

    Raises :class:`UnsafeURLError` with a human-readable reason.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("A valid HTTP or HTTPS URL is required.")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError(f"URL is longer than {MAX_URL_LENGTH} characters.")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise UnsafeURLError("URL must not contain whitespace or control characters.")

    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
        raise UnsafeURLError("A valid HTTP or HTTPS URL is required.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URLs must not embed credentials.")
    try:
        parsed.port  # noqa: B018 - raises ValueError for invalid ports
    except ValueError as exc:
        raise UnsafeURLError(f"Invalid port in URL: {exc}") from exc
    return parsed


def default_port(parsed: SplitResult) -> int:
    """Return the explicit port or the scheme's default."""
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme.lower() == "https" else 80


def redact_url(url: str) -> str:
    """Return the URL without credentials, query string or fragment.

    Used for anything that leaves the process (logs, notifications), because
    query strings frequently carry tokens.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


@dataclass(frozen=True)
class UrlPolicy:
    """Decide which destinations may be contacted.

    ``allow_private=True`` disables the address checks. It exists only for
    local testing against controlled endpoints (e.g. a server on 127.0.0.1)
    and is refused by configuration when ``CLOUDPULSE_ENV=production``.
    """

    allow_private: bool = False
    resolver: Resolver = field(default=_default_resolver, compare=False)

    def check_url(self, url: str) -> SplitResult:
        """Validate syntax and, unless allowed, IP-literal hosts."""
        parsed = validate_url(url)
        if not self.allow_private:
            literal = _parse_ip(parsed.hostname or "")
            if literal is not None and is_blocked_address(literal):
                raise BlockedDestinationError(
                    f"Destination {literal} is a blocked (non-public) address."
                )
        return parsed

    def check_resolved(self, host: str, port: int) -> list[str]:
        """Resolve ``host`` and ensure every returned address is allowed.

        DNS failures propagate as ``socket.gaierror`` so callers can classify
        them as DNS errors.
        """
        literal = _parse_ip(host)
        if literal is not None:
            addresses = [str(literal)]
        else:
            addresses = list(self.resolver(host, port))
        if not addresses:
            raise socket.gaierror(f"No addresses found for {host}")
        if not self.allow_private:
            for raw in addresses:
                address = _parse_ip(raw)
                if address is None or is_blocked_address(address):
                    raise BlockedDestinationError(
                        f"Host {host} resolves to a blocked address ({raw})."
                    )
        return addresses

    def check_peer(self, peer_ip: str) -> None:
        """Validate the IP address a socket actually connected to."""
        if self.allow_private:
            return
        address = _parse_ip(peer_ip)
        if address is None or is_blocked_address(address):
            raise BlockedDestinationError(
                f"Connection reached a blocked address ({peer_ip})."
            )
