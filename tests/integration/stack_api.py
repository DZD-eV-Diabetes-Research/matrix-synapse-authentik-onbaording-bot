"""Helpers for driving the live integration stack (Phase 7b).

Two things live here:

* :class:`AuthentikAdmin` — a thin wrapper over the Authentik API (using the bootstrapped admin
  token) to create the *desired* state the reconciler converges towards: groups, users, membership,
  and the disable/enable transitions the lifecycle + §7 Q1 experiment need.
* :func:`mas_login` — performs a **real** OIDC authorization-code login through MAS into the
  Authentik upstream IdP, exactly as a Matrix client would. This is the only way to provision a
  Matrix account under the MAS topology (ADR-0006: the bot never pre-creates accounts). It walks the
  browser flow headlessly (Authentik's flow executor + MAS's link/consent forms) and returns the
  provisioned MXID and a working access token.

The in-network issuer hostnames (``mas:8080`` / ``authentik-server:9000``) are rewritten to
``localhost`` on each browser hop so the host-side helper and the containers agree on one identity.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import httpx

# Fixed host ports published by tests/integration/stack/docker-compose.yml.
SYNAPSE_URL = "http://localhost:8008"
MAS_URL = "http://localhost:8080"
AUTHENTIK_URL = "http://localhost:9000"
SERVER_NAME = "onbot.test"
BOT_USER_ID = "@onbot:onbot.test"

# Matches tests/integration/stack/*: the bootstrapped Authentik admin token and the MAS test client.
AUTHENTIK_TOKEN = "onbot-authentik-bootstrap-token"
TEST_CLIENT_ID = "0000000000000000000000TEST"
TEST_CLIENT_SECRET = "557eddece563d23e6d6effec55b10f331f02faddb108208d427b389d3aa2e47f"
TEST_REDIRECT_URI = "http://localhost:9999/callback"

# The bot's MAS admin client (client_credentials -> urn:mas:admin), matches mas/config.yaml.
MAS_ADMIN_CLIENT_ID = "0000000000000000000MASADMN"
MAS_ADMIN_CLIENT_SECRET = "04ca2b8aa47c1d270cd6fa3d72f5e8750ca5e66cb57a6783ccb01be6fffa483b"

# Browser-hop host rewrites: containers advertise in-network names; the host reaches them on
# published localhost ports.
_REWRITE = {"mas:8080": "localhost:8080", "authentik-server:9000": "localhost:9000"}


def _rewrite(url: str) -> str:
    for internal, local in _REWRITE.items():
        url = url.replace("//" + internal, "//" + local)
    return url


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class LoginError(RuntimeError):
    """A real MAS/Authentik login did not complete (e.g. a disabled upstream user)."""


@dataclass(frozen=True)
class LoginResult:
    mxid: str
    access_token: str
    device_id: str


class AuthentikAdmin:
    """Minimal Authentik API client for shaping the desired state the reconciler reads."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            base_url=f"{AUTHENTIK_URL}/api/v3",
            headers={"Authorization": f"Bearer {AUTHENTIK_TOKEN}"},
            timeout=30.0,
        )

    def close(self) -> None:
        self._http.close()

    def create_group(self, name: str, *, attributes: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._http.post("/core/groups/", json={"name": name, "attributes": attributes or {}})
        r.raise_for_status()
        return r.json()

    def create_user(
        self,
        username: str,
        *,
        password: str,
        email: str | None = None,
        groups: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
        is_superuser: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "username": username,
            "name": username,
            "email": email or f"{username}@onbot.test",
            "is_active": True,
            "path": "users",
            "groups": groups or [],
            "attributes": attributes or {},
            "type": "internal",
        }
        r = self._http.post("/core/users/", json=body)
        r.raise_for_status()
        user = r.json()
        if is_superuser:
            self.set_superuser(user["pk"], True)
            user = self.get_user(user["pk"])
        sp = self._http.post(f"/core/users/{user['pk']}/set_password/", json={"password": password})
        sp.raise_for_status()
        return user

    def get_user(self, pk: int) -> dict[str, Any]:
        r = self._http.get(f"/core/users/{pk}/")
        r.raise_for_status()
        return r.json()

    def set_active(self, pk: int, active: bool) -> None:
        r = self._http.patch(f"/core/users/{pk}/", json={"is_active": active})
        r.raise_for_status()

    def set_superuser(self, pk: int, value: bool) -> None:
        # Superuser status in Authentik comes from group membership; create/join a superuser group.
        grp = self._http.post(
            "/core/groups/", json={"name": f"superusers-{secrets.token_hex(4)}", "is_superuser": value}
        )
        grp.raise_for_status()
        gpk = grp.json()["pk"]
        r = self._http.post(f"/core/groups/{gpk}/add_user/", json={"pk": pk})
        r.raise_for_status()


def _new_executor_url(flow_slug: str, query: str) -> str:
    return f"{AUTHENTIK_URL}/api/v3/flows/executor/{flow_slug}/?query={quote(query, safe='')}"


def mas_login(username: str, password: str) -> LoginResult:
    """Drive a real authorization-code login (client -> MAS -> Authentik) and return the session.

    Raises :class:`LoginError` if the flow fails to complete (e.g. the Authentik account is
    disabled), which the §7 Q1 experiment relies on to observe whether upstream disable blocks login.
    """
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state, nonce = secrets.token_hex(8), secrets.token_hex(8)
    device = "onbotITEST" + secrets.token_hex(6)
    scope = (
        "openid urn:matrix:org.matrix.msc2967.client:api:* "
        f"urn:matrix:org.matrix.msc2967.client:device:{device}"
    )
    authorize = (
        MAS_URL
        + "/authorize?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": TEST_CLIENT_ID,
                "redirect_uri": TEST_REDIRECT_URI,
                "scope": scope,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    hdr = {"Accept": "application/json"}
    with httpx.Client(follow_redirects=False, timeout=30.0) as c:
        flow_slug, flow_query = _follow_to_flow(c, authorize)
        cur = _new_executor_url(flow_slug, flow_query)
        leave = _drive_flow(c, cur, username, password, hdr)
        code = _follow_to_code(c, leave)
        token = c.post(
            MAS_URL + "/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": TEST_REDIRECT_URI,
                "client_id": TEST_CLIENT_ID,
                "client_secret": TEST_CLIENT_SECRET,
                "code_verifier": verifier,
            },
        )
        if token.status_code != 200:
            raise LoginError(f"token exchange failed: {token.status_code} {token.text[:200]}")
        access = token.json()["access_token"]
    who = httpx.get(
        SYNAPSE_URL + "/_matrix/client/v3/account/whoami",
        headers={"Authorization": f"Bearer {access}"},
        timeout=30.0,
    )
    if who.status_code != 200:
        raise LoginError(f"whoami failed: {who.status_code} {who.text[:200]}")
    body = who.json()
    return LoginResult(mxid=body["user_id"], access_token=access, device_id=body.get("device_id", device))


def _follow_to_flow(c: httpx.Client, start: str) -> tuple[str, str]:
    """Follow MAS -> Authentik redirects until the Authentik flow interface; return (slug, query)."""
    url = start
    for _ in range(20):
        r = c.get(url)
        if r.status_code not in (301, 302, 303, 307, 308):
            raise LoginError(f"expected redirect before flow, got {r.status_code}: {r.text[:200]}")
        loc = _rewrite(urljoin(str(r.request.url), r.headers["location"]))
        parsed = urlparse(loc)
        if parsed.path.startswith("/if/flow/"):
            slug = parsed.path.split("/if/flow/")[1].strip("/").split("/")[0]
            return slug, parsed.query
        url = loc
    raise LoginError("never reached the Authentik flow interface")


def _drive_flow(c: httpx.Client, cur: str, username: str, password: str, hdr: dict[str, str]) -> str:
    """Run the Authentik flow executor (identification + password) and return the leaving URL."""

    def resolve(resp: httpx.Response) -> tuple[str, Any, str]:
        # Authentik signals each next stage with a 302 back into the executor; follow those.
        while resp.status_code in (301, 302, 303, 307, 308):
            dest = _rewrite(urljoin(str(resp.request.url), resp.headers["location"]))
            if "/api/v3/flows/executor/" not in dest and "/if/flow/" not in dest:
                return "leave", dest, ""
            resp = c.get(dest, headers=hdr)
        if not resp.headers.get("content-type", "").startswith("application/json"):
            raise LoginError(f"non-JSON executor response: {resp.status_code} {resp.text[:200]}")
        return "challenge", resp.json(), str(resp.request.url)

    kind, value, cur = resolve(c.get(cur, headers=hdr))
    for _ in range(20):
        if kind == "leave":
            return str(value)
        challenge = value
        component = challenge.get("component")
        if component == "ak-stage-identification":
            kind, value, cur = resolve(c.post(cur, json={"uid_field": username}, headers=hdr))
        elif component == "ak-stage-password":
            kind, value, cur = resolve(c.post(cur, json={"password": password}, headers=hdr))
        elif component == "xak-flow-redirect":
            dest = _rewrite(urljoin(cur, challenge["to"]))
            if "/api/v3/flows/executor/" in dest or "/if/flow/" in dest:
                kind, value, cur = resolve(c.get(dest, headers=hdr))
            else:
                return dest
        else:
            # e.g. ak-stage-access-denied, or a password stage carrying response_errors.
            raise LoginError(f"login not permitted at stage {component!r}: {str(challenge)[:200]}")
    raise LoginError("Authentik flow did not complete")


def _follow_to_code(c: httpx.Client, leave: str) -> str:
    """Follow the post-auth chain (MAS link/consent forms) to the final code at the redirect URI."""
    r = c.get(leave)
    for _ in range(25):
        if r.status_code in (301, 302, 303, 307, 308):
            loc = _rewrite(urljoin(str(r.request.url), r.headers["location"]))
            if loc.startswith(TEST_REDIRECT_URI):
                code = parse_qs(urlparse(loc).query).get("code", [None])[0]
                if not code:
                    raise LoginError(f"redirect without code: {loc}")
                return code
            r = c.get(loc)
            continue
        cur_url = str(r.request.url)
        csrf = re.search(r'name="csrf"[^>]*value="([^"]*)"', r.text)
        if csrf and "/upstream/link/" in cur_url:  # first-SSO account provisioning
            r = c.post(cur_url, data={"csrf": csrf.group(1), "action": "register"})
            continue
        if csrf and "/consent/" in cur_url:  # client authorization consent
            r = c.post(cur_url, data={"csrf": csrf.group(1)})
            continue
        raise LoginError(f"stuck on non-redirect page {cur_url}: {r.status_code}")
    raise LoginError("did not reach the redirect URI with a code")


def uniq(prefix: str) -> str:
    """A short unique suffix so tests sharing the session stack don't collide."""
    return f"{prefix}{secrets.token_hex(4)}"


DEFAULT_PASSWORD = "onbot-Compl3x-Pw-123"


def whoami_status(access_token: str) -> int:
    """HTTP status of a CS /whoami with the given token (200 active, 401 revoked)."""
    return httpx.get(
        f"{SYNAPSE_URL}/_matrix/client/v3/account/whoami",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15.0,
    ).status_code


def wait_revoked(access_token: str, *, timeout: float = 15.0) -> bool:
    """Poll until the token is rejected (revocation is near-instant via MAS, but allow slack)."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if whoami_status(access_token) == 401:
            return True
        time.sleep(0.5)
    return whoami_status(access_token) == 401


def provision(
    admin: AuthentikAdmin,
    username: str,
    *,
    groups: list[str] | None = None,
    attributes: dict[str, Any] | None = None,
    is_superuser: bool = False,
    password: str = DEFAULT_PASSWORD,
) -> tuple[dict[str, Any], LoginResult]:
    """Create an Authentik user and provision their Matrix account via a real MAS login."""
    user = admin.create_user(
        username, password=password, groups=groups, attributes=attributes, is_superuser=is_superuser
    )
    return user, mas_login(username, password)
