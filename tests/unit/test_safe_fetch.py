"""Avatar downloads under the fetch policy (SEC-03, D11).

Two layers are tested separately. URL, redirect, size, type and deadline rules run against respx
(which replaces httpx's connection pool, so no socket is opened). The address check lives in the
connection layer itself, so it is tested against real sockets on 127.0.0.1 with a fake resolver:
that is what proves the bot connects to the address it checked.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpcore
import httpx
import pytest
import respx

from onbot.app import _fetch_policy
from onbot.clients.matrix import ApiClientMatrix
from onbot.config import OnbotConfig
from onbot.media import MediaUploader
from onbot.runtime import is_permanent_failure
from onbot.safe_fetch import (
    FetchPolicy,
    GuardedNetworkBackend,
    IPAddress,
    MediaFetchError,
    MediaFetchRejected,
    SafeImageFetcher,
    is_public_address,
)

PUBLIC_IP = "93.184.216.34"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _image(content: bytes = PNG, **headers: str) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "image/png", **headers})


# --- address classification ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # private
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # link-local: cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast (is_global calls it global)
        "239.255.255.250",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",  # IPv4-mapped
        "::ffff:169.254.169.254",
        "2002:7f00:1::",  # 6to4 wrapping 127.0.0.1
        "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
    ],
)
def test_internal_addresses_are_not_public(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [PUBLIC_IP, "1.1.1.1", "2606:4700:4700::1111", "64:ff9b::808:808"])
def test_public_addresses_are_public(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address))


def test_allowlist_releases_networks_and_hosts() -> None:
    policy = FetchPolicy.from_entries(["10.20.0.0/16", "fd00::1", "Assets.Internal."])
    assert policy.address_allowed(ipaddress.ip_address("10.20.5.5"))
    assert policy.address_allowed(ipaddress.ip_address("::ffff:10.20.5.5"))
    assert policy.address_allowed(ipaddress.ip_address("fd00::1"))
    assert not policy.address_allowed(ipaddress.ip_address("10.21.0.1"))
    assert policy.host_allowed("assets.internal")
    assert not policy.host_allowed("other.internal")


# --- URL, redirect, size, type and deadline rules (respx) ----------------------------------------


@asynccontextmanager
async def _fetcher(policy: FetchPolicy | None = None) -> AsyncIterator[SafeImageFetcher]:
    fetcher = SafeImageFetcher(policy)
    try:
        yield fetcher
    finally:
        await fetcher.aclose()


@respx.mock
async def test_happy_path_returns_bytes_and_type() -> None:
    respx.get("https://cdn.test/face.png").mock(return_value=_image())
    async with _fetcher() as fetcher:
        assert await fetcher.fetch("https://cdn.test/face.png") == (PNG, "image/png")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://cdn.test/face.png",
        "gopher://cdn.test/",
        "cdn.test/face.png",
        "https://user:pass@cdn.test/face.png",
    ],
)
@respx.mock(assert_all_called=False)
async def test_only_plain_http_urls_are_fetched(url: str) -> None:
    async with _fetcher() as fetcher:
        with pytest.raises(MediaFetchRejected):
            await fetcher.fetch(url)
    assert respx.calls.call_count == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/metrics",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.5/face.png",
    ],
)
@respx.mock(assert_all_called=False)
async def test_internal_ip_literals_are_refused_before_any_request(url: str) -> None:
    async with _fetcher() as fetcher:
        with pytest.raises(MediaFetchRejected, match="non-public address"):
            await fetcher.fetch(url)
    assert respx.calls.call_count == 0


@respx.mock
async def test_allowlisted_ip_literal_is_fetched() -> None:
    respx.get("http://10.0.0.5/face.png").mock(return_value=_image())
    async with _fetcher(FetchPolicy.from_entries(["10.0.0.0/8"])) as fetcher:
        assert (await fetcher.fetch("http://10.0.0.5/face.png"))[0] == PNG


@respx.mock
async def test_redirect_to_an_internal_address_is_refused() -> None:
    """The scenario from the review: a public URL that redirects to the metadata endpoint."""
    respx.get("https://cdn.test/face.png").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
    )
    metadata = respx.get("http://169.254.169.254/latest/meta-data/").mock(return_value=_image())
    async with _fetcher() as fetcher:
        with pytest.raises(MediaFetchRejected, match="non-public address"):
            await fetcher.fetch("https://cdn.test/face.png")
    assert not metadata.called


@respx.mock
async def test_redirect_to_another_scheme_is_refused() -> None:
    respx.get("https://cdn.test/face.png").mock(
        return_value=httpx.Response(301, headers={"location": "file:///etc/passwd"})
    )
    async with _fetcher() as fetcher:
        with pytest.raises(MediaFetchRejected, match="only http and https"):
            await fetcher.fetch("https://cdn.test/face.png")


@respx.mock
async def test_relative_redirects_are_followed_within_the_hop_limit() -> None:
    respx.get("https://cdn.test/a").mock(return_value=httpx.Response(302, headers={"location": "/b"}))
    respx.get("https://cdn.test/b").mock(return_value=httpx.Response(307, headers={"location": "c"}))
    respx.get("https://cdn.test/c").mock(return_value=_image())
    async with _fetcher() as fetcher:
        assert (await fetcher.fetch("https://cdn.test/a"))[0] == PNG


@respx.mock
async def test_redirect_loop_stops_at_the_hop_limit() -> None:
    loop = respx.get("https://cdn.test/loop").mock(
        return_value=httpx.Response(302, headers={"location": "/loop"})
    )
    async with _fetcher(FetchPolicy(max_redirects=3)) as fetcher:
        with pytest.raises(MediaFetchRejected, match="redirected more than 3 times"):
            await fetcher.fetch("https://cdn.test/loop")
    assert loop.call_count == 4  # the first request plus three redirects


@pytest.mark.parametrize("content_type", ["text/html", "application/json", "application/octet-stream", ""])
@respx.mock
async def test_non_image_content_type_is_refused(content_type: str) -> None:
    respx.get("https://cdn.test/face.png").mock(
        return_value=httpx.Response(200, content=b"{}", headers={"content-type": content_type})
    )
    async with _fetcher() as fetcher:
        with pytest.raises(MediaFetchRejected, match="not an image"):
            await fetcher.fetch("https://cdn.test/face.png")


@respx.mock
async def test_declared_size_over_the_cap_is_refused_up_front() -> None:
    respx.get("https://cdn.test/big.png").mock(return_value=_image(b"x" * 2048))
    async with _fetcher(FetchPolicy(max_bytes=1024)) as fetcher:
        with pytest.raises(MediaFetchRejected, match="2048 bytes, more than 1024"):
            await fetcher.fetch("https://cdn.test/big.png")


@respx.mock
async def test_undeclared_size_is_capped_while_streaming() -> None:
    chunks_sent = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal chunks_sent
        while True:
            chunks_sent += 1
            yield b"x" * 512

    respx.get("https://cdn.test/endless.png").mock(
        return_value=httpx.Response(200, content=endless(), headers={"content-type": "image/png"})
    )
    async with _fetcher(FetchPolicy(max_bytes=4096)) as fetcher:
        with pytest.raises(MediaFetchRejected, match="larger than 4096 bytes"):
            await fetcher.fetch("https://cdn.test/endless.png")
    assert chunks_sent <= 10  # stopped right after the cap, not after reading everything


@respx.mock
async def test_http_error_status_still_raises_http_status_error() -> None:
    respx.get("https://cdn.test/gone.png").mock(return_value=httpx.Response(404))
    async with _fetcher() as fetcher:
        with pytest.raises(httpx.HTTPStatusError):
            await fetcher.fetch("https://cdn.test/gone.png")


@respx.mock(assert_all_called=False)
async def test_media_uploader_uses_the_policy_by_default() -> None:
    client = ApiClientMatrix(server_url="https://matrix.test", access_token="tok", server_name="matrix.test")
    uploader = MediaUploader(client)
    try:
        with pytest.raises(MediaFetchRejected):
            await uploader.upload_from_url("http://169.254.169.254/latest/meta-data/iam/")
    finally:
        await uploader.aclose()
        await client.aclose()
    assert respx.calls.call_count == 0  # neither fetched nor uploaded


def test_a_refused_avatar_is_a_permanent_startup_failure() -> None:
    assert is_permanent_failure(MediaFetchRejected("refused"))
    assert not is_permanent_failure(MediaFetchError("deadline"))


def test_policy_comes_from_the_avatar_fetch_config_block() -> None:
    config = OnbotConfig.model_validate(
        {
            "synapse_server": {
                "server_name": "company.org",
                "server_url": "https://matrix.company.org",
                "bot_user_id": "@bot:company.org",
                "bot_access_token": "tok",
            },
            "authentik_server": {"url": "https://authentik.company.org/", "api_key": "key"},
            "avatar_fetch": {
                "allowed_internal_destinations": ["assets.lan", "10.0.0.0/8"],
                "max_size_bytes": 1000,
                "timeout_sec": 2.5,
            },
        }
    )
    policy = _fetch_policy(config)
    assert policy.allowed_hosts == frozenset({"assets.lan"})
    assert policy.allowed_networks == (ipaddress.ip_network("10.0.0.0/8"),)
    assert (policy.max_bytes, policy.timeout_sec) == (1000, 2.5)


# --- the connection layer, against real sockets --------------------------------------------------


class _Server:
    """A tiny HTTP server on 127.0.0.1 answering every request with ``respond(writer)``."""

    def __init__(self, respond: Callable[[asyncio.StreamWriter], Any]) -> None:
        self._respond = respond
        self.hits = 0
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _Server:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.hits += 1
        await reader.readuntil(b"\r\n\r\n")
        try:
            await self._respond(writer)
        except ConnectionError, asyncio.CancelledError:
            pass
        finally:
            writer.close()


async def _png(writer: asyncio.StreamWriter) -> None:
    head = f"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: {len(PNG)}\r\n\r\n"
    writer.write(head.encode() + PNG)
    await writer.drain()


def _resolver(table: dict[str, str]) -> Callable[[str, int], Any]:
    async def resolve(host: str, port: int) -> list[IPAddress]:
        return [ipaddress.ip_address(table.get(host, host))]

    return resolve


async def test_hostname_resolving_to_loopback_is_refused_at_connect() -> None:
    async with _Server(_png) as server:
        fetcher = SafeImageFetcher(resolver=_resolver({"avatar.test": "127.0.0.1"}))
        try:
            with pytest.raises(MediaFetchRejected, match=r"resolves to the non-public address 127\.0\.0\.1"):
                await fetcher.fetch(f"http://avatar.test:{server.port}/face.png")
        finally:
            await fetcher.aclose()
    assert server.hits == 0  # refused before a socket was opened


@pytest.mark.parametrize("allowed", ["127.0.0.0/8", "127.0.0.1", "avatar.test"])
async def test_allowlisted_internal_destination_is_fetched(allowed: str) -> None:
    """``avatar.test`` exists only in the fake resolver, so a success also proves the socket went
    to the address the guard resolved and checked, not to a second lookup."""
    async with _Server(_png) as server:
        fetcher = SafeImageFetcher(
            FetchPolicy.from_entries([allowed]), resolver=_resolver({"avatar.test": "127.0.0.1"})
        )
        try:
            assert await fetcher.fetch(f"http://avatar.test:{server.port}/face.png") == (PNG, "image/png")
        finally:
            await fetcher.aclose()
    assert server.hits == 1


async def test_redirect_hop_to_an_internal_host_is_refused_at_connect() -> None:
    async with _Server(_png) as internal:

        async def redirect(writer: asyncio.StreamWriter) -> None:
            location = f"http://internal.test:{internal.port}/secret"
            writer.write(f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode())
            await writer.drain()

        async with _Server(redirect) as public:
            fetcher = SafeImageFetcher(
                FetchPolicy.from_entries(["avatar.test"]),
                resolver=_resolver({"avatar.test": "127.0.0.1", "internal.test": "127.0.0.1"}),
            )
            try:
                with pytest.raises(MediaFetchRejected, match=r"internal\.test"):
                    await fetcher.fetch(f"http://avatar.test:{public.port}/face.png")
            finally:
                await fetcher.aclose()
            assert public.hits == 1
    assert internal.hits == 0


async def test_trickling_server_is_cut_off_by_the_total_deadline() -> None:
    """One byte every 0.1 s never trips a per-read timeout; the overall deadline still ends it."""

    async def trickle(writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100000\r\n\r\n")
        while True:
            writer.write(b"x")
            await writer.drain()
            await asyncio.sleep(0.1)

    async with _Server(trickle) as server:
        fetcher = SafeImageFetcher(
            FetchPolicy.from_entries(["127.0.0.1"], timeout_sec=0.6), resolver=_resolver({})
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with pytest.raises(MediaFetchError, match=r"took longer than 0\.6s"):
                await fetcher.fetch(f"http://127.0.0.1:{server.port}/slow.png")
        finally:
            await fetcher.aclose()
        assert loop.time() - started < 3


class _RecordingBackend:
    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect_tcp(self, host: str, port: int, **_: Any) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        raise httpcore.ConnectError("recorded only")

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - not used
        await asyncio.sleep(seconds)


async def test_guard_connects_to_the_checked_address_not_the_hostname() -> None:
    """DNS rebinding: no second lookup between the check and the connect."""
    lookups: list[str] = []

    async def rebinding(host: str, port: int) -> list[IPAddress]:
        lookups.append(host)
        # A rebinding resolver answers "public" first and "internal" on every later lookup.
        return [ipaddress.ip_address(PUBLIC_IP if len(lookups) == 1 else "127.0.0.1")]

    inner = _RecordingBackend()
    backend = GuardedNetworkBackend(FetchPolicy(), resolver=rebinding, inner=inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("cdn.test", 443)
    assert lookups == ["cdn.test"]
    assert inner.connected == [PUBLIC_IP]


async def test_guard_refuses_a_name_with_any_internal_address() -> None:
    async def mixed(host: str, port: int) -> list[IPAddress]:
        return [ipaddress.ip_address(PUBLIC_IP), ipaddress.ip_address("10.0.0.1")]

    inner = _RecordingBackend()
    backend = GuardedNetworkBackend(FetchPolicy(), resolver=mixed, inner=inner)
    with pytest.raises(MediaFetchRejected, match=r"10\.0\.0\.1"):
        await backend.connect_tcp("cdn.test", 443)
    assert inner.connected == []


def test_guard_refuses_to_build_when_httpx_moves_its_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard replaces httpx's private ``_pool``. If an httpx update renamed it, the assignment
    would leave the real, unguarded pool in use; the fetcher must refuse to exist instead."""
    original = httpx.AsyncHTTPTransport.__init__

    def renamed_pool(self: httpx.AsyncHTTPTransport, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self._connection_pool = self.__dict__.pop("_pool")  # type: ignore[attr-defined]

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", renamed_pool)
    with pytest.raises(RuntimeError, match="cannot be installed"):
        SafeImageFetcher()
