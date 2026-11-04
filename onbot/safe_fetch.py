"""Download images from URLs that somebody other than the operator may choose (SEC-03, D11).

Room avatars come from an Authentik group attribute, so whoever may edit a group picks a URL the bot
then fetches from inside the deployment network and republishes as Matrix media every room member
can read. Without limits that is a server-side request forgery primitive: cloud metadata endpoints,
MAS, Authentik metrics, anything reachable from the bot's container. This module is the one place
the bot downloads such URLs, and it enforces:

* **Scheme:** ``http`` or ``https`` only, on every hop.
* **Destination:** the host is resolved and every address must be public: private, loopback,
  link-local, multicast, unspecified, carrier-grade NAT and the other special-purpose ranges are
  refused, including when they are wrapped in an IPv6 form (IPv4-mapped, 6to4, Teredo, NAT64).
  ``avatar_fetch.allowed_internal_destinations`` releases specific hostnames or networks.
* **Redirects:** never followed by httpx. Each ``Location`` is checked like the first URL and at
  most :data:`MAX_REDIRECTS` hops are taken.
* **Size:** ``Content-Length`` is checked up front and the body is counted while streaming (after
  content decoding, so a compressed bomb is caught too); the download stops at the cap.
* **Time:** one deadline for the whole download including redirects, so a server trickling one byte
  at a time cannot hold the reconcile pass.
* **Type:** the response must declare an ``image/*`` content type.

DNS rebinding: the address check runs inside the HTTP connection layer (a wrapped httpcore network
backend), on the very addresses the socket is then opened to. There is no second lookup between
check and connect, so a resolver that answers "public" first and "internal" a moment later gains
nothing. TLS still verifies the certificate against the hostname of the URL.

Environment proxies (``HTTP_PROXY`` and friends) are deliberately ignored for these downloads: behind
a proxy the bot would only check the proxy's address, not the destination's.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpcore
import httpx

from onbot.logging import get_logger

log = get_logger(__name__)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 30.0
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


class MediaFetchError(Exception):
    """A download did not produce an image (base class; may be transient, such as a timeout)."""


class MediaFetchRejected(MediaFetchError):
    """The fetch policy refused the URL or its response.

    Permanent for the same URL: retrying fails the same way until the URL, the remote file or the
    operator's allowlist changes.
    """


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 transition form carries, if any (mapped, 6to4, Teredo, NAT64)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(ip: IPAddress) -> bool:
    """Whether ``ip`` is a globally routable unicast address (and so safe to fetch from).

    ``is_global`` already excludes private, loopback, link-local, unspecified, reserved, shared
    (100.64.0.0/10) and documentation ranges; multicast is global by that definition and is refused
    separately. IPv6 forms that embed an IPv4 address are judged by the embedded address too.
    """
    if ip.is_multicast or not ip.is_global:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return is_public_address(embedded)
    return True


def _normalise_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """What :class:`SafeImageFetcher` may download. The defaults are the safe ones."""

    allowed_hosts: frozenset[str] = frozenset()
    allowed_networks: tuple[IPNetwork, ...] = ()
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    max_redirects: int = MAX_REDIRECTS

    @classmethod
    def from_entries(cls, entries: Iterable[str], **kwargs: Any) -> FetchPolicy:
        """Build a policy from allowlist entries: IP addresses, CIDR networks or hostnames."""
        hosts: set[str] = set()
        networks: list[IPNetwork] = []
        for entry in entries:
            try:
                networks.append(ipaddress.ip_network(entry.strip(), strict=False))
            except ValueError:
                hosts.add(_normalise_host(entry))
        return cls(allowed_hosts=frozenset(hosts), allowed_networks=tuple(networks), **kwargs)

    def host_allowed(self, host: str) -> bool:
        """Whether the operator released this hostname, whatever it resolves to."""
        return _normalise_host(host) in self.allowed_hosts

    def address_allowed(self, ip: IPAddress) -> bool:
        if is_public_address(ip):
            return True
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return any(ip.version == net.version and ip in net for net in self.allowed_networks)


async def system_resolver(host: str, port: int) -> list[IPAddress]:
    """Resolve ``host`` with the system resolver, in the order the resolver prefers."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise httpcore.ConnectError(f"could not resolve {host}: {exc}") from exc
    addresses: list[IPAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if ip not in addresses:
            addresses.append(ip)
    return addresses


class _Backend(Protocol):
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream: ...

    async def sleep(self, seconds: float) -> None: ...


class GuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    """An httpcore network backend that only opens sockets to addresses the policy allows.

    Resolution happens here, and the socket is opened to the checked address itself, which is what
    closes the DNS rebinding window (see the module docstring).
    """

    def __init__(self, policy: FetchPolicy, resolver: Resolver = system_resolver, inner: Any = None) -> None:
        self._policy = policy
        self._resolve = resolver
        self._inner: _Backend = inner if inner is not None else httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolve(host, port)
        if not addresses:
            raise httpcore.ConnectError(f"{host} did not resolve to any address")
        if not self._policy.host_allowed(host):
            blocked = [ip for ip in addresses if not self._policy.address_allowed(ip)]
            if blocked:
                raise MediaFetchRejected(
                    f"refusing to fetch from {host}: it resolves to the non-public address {blocked[0]}. "
                    "Add the host or its network to avatar_fetch.allowed_internal_destinations if this "
                    "is intended."
                )
        last_error: Exception | None = None
        for ip in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(ip), port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - httpx never asks for one here
        raise MediaFetchRejected("unix sockets are not a valid avatar source")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _GuardedTransport(httpx.AsyncHTTPTransport):
    """httpx's default transport with the connection pool swapped for one on the guarded backend.

    httpx does not take a network backend as a parameter, so the pool it built is replaced (it has
    opened nothing yet). ``trust_env=False``: no proxy, see the module docstring.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=5.0,
            http1=True,
            http2=False,
            network_backend=backend,
        )


def _checked_url(raw: str | httpx.URL, policy: FetchPolicy) -> httpx.URL:
    """Parse ``raw`` and refuse anything but an http(s) URL with a host.

    A host given as an IP literal is checked right here, so the refusal names the URL instead of
    surfacing from the connection layer; hostnames are checked when the connection is opened.
    """
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise MediaFetchRejected(f"not a valid URL: {exc}") from exc
    if url.scheme not in _ALLOWED_SCHEMES:
        scheme = url.scheme or "scheme-less"
        raise MediaFetchRejected(f"refusing to fetch a {scheme} URL: only http and https are allowed")
    if not url.host:
        raise MediaFetchRejected("refusing to fetch a URL without a host")
    if url.userinfo:
        raise MediaFetchRejected("refusing to fetch a URL carrying credentials")
    try:
        literal: IPAddress | None = ipaddress.ip_address(url.host)
    except ValueError:
        literal = None
    if literal is not None and not policy.host_allowed(url.host) and not policy.address_allowed(literal):
        raise MediaFetchRejected(
            f"refusing to fetch from the non-public address {literal}. Add it to "
            "avatar_fetch.allowed_internal_destinations if this is intended."
        )
    return url


class SafeImageFetcher:
    """Download one image under a :class:`FetchPolicy`. Owns its HTTP client; call :meth:`aclose`."""

    def __init__(self, policy: FetchPolicy | None = None, *, resolver: Resolver = system_resolver) -> None:
        self.policy = policy or FetchPolicy()
        transport = _GuardedTransport(GuardedNetworkBackend(self.policy, resolver))
        self._http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(min(10.0, self.policy.timeout_sec)),
            headers={"Accept": "image/*"},
        )

    async def fetch(self, url: str) -> tuple[bytes, str]:
        """Return the image bytes and their content type, or raise.

        Raises :class:`MediaFetchRejected` when the policy refuses the URL or response,
        :class:`MediaFetchError` when the overall deadline passes, and the usual httpx errors for
        HTTP error statuses and network failures.
        """
        try:
            async with asyncio.timeout(self.policy.timeout_sec):
                return await self._fetch(url)
        except TimeoutError as exc:
            raise MediaFetchError(
                f"downloading {url} took longer than {self.policy.timeout_sec:g}s (avatar_fetch.timeout_sec)"
            ) from exc

    async def _fetch(self, url: str) -> tuple[bytes, str]:
        current = _checked_url(url, self.policy)
        for _hop in range(self.policy.max_redirects + 1):
            async with self._http.stream("GET", current) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaFetchRejected(f"{current} redirected without a Location header")
                    current = _checked_url(current.join(location), self.policy)
                    continue
                response.raise_for_status()
                return await self._read_image(current, response)
        raise MediaFetchRejected(f"{url} redirected more than {self.policy.max_redirects} times")

    async def _read_image(self, url: httpx.URL, response: httpx.Response) -> tuple[bytes, str]:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise MediaFetchRejected(f"{url} is not an image (content type {content_type or 'missing'!r})")
        cap = self.policy.max_bytes
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise MediaFetchRejected(
                f"{url} is {declared} bytes, more than {cap} (avatar_fetch.max_size_bytes)"
            )
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > cap:
                raise MediaFetchRejected(f"{url} is larger than {cap} bytes (avatar_fetch.max_size_bytes)")
        return bytes(body), content_type

    async def aclose(self) -> None:
        await self._http.aclose()
