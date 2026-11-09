"""The identity resolver: one Authentik user -> MXID resolution (D8, MNT-13, SEC-07).

Resolution order: MAS (authoritative), the recorded binding, the mapping attribute for users never
seen. Bindings survive renames; conflicts fail closed; every finding is reported once.
"""

from __future__ import annotations

from typing import Any

import pytest

from onbot.clients.mas_admin import UpstreamLink
from onbot.config import OnbotConfig
from onbot.events import Event, EventBus, Signal
from onbot.identity import IdentityResolver, IdentityUnavailable
from onbot.lifecycle.accounts import KnownUser, KnownUsers, LedgerUnreadable

SERVER = "company.org"
_BASE: dict[str, Any] = {
    "synapse_server": {
        "server_name": SERVER,
        "server_url": "https://internal.matrix",
        "bot_user_id": f"@bot:{SERVER}",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik/", "api_key": "key"},
    "admin_room": {"enabled": False},
}
_MAS = {"url": "https://mas", "client_id": "c", "client_secret": "s"}


def mx(local: str) -> str:
    return f"@{local}:{SERVER}"


def _config(*, mas: bool = False, attribute: str = "username", **mas_extra: Any) -> OnbotConfig:
    raw = dict(_BASE, mas_admin={**_MAS, **mas_extra}) if mas else dict(_BASE)
    config = OnbotConfig.model_validate(raw)
    config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute = attribute
    return config


def _user(pk: int, username: str, **extra: Any) -> dict[str, Any]:
    return {"pk": pk, "username": username, "uid": f"hash-{pk}", "attributes": {}, **extra}


class MemoryBindings:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self.known = KnownUsers(
            users={pk: KnownUser(mxid=m, first_seen=1.0) for pk, m in (bindings or {}).items()}
        )
        self.saves = 0
        self.broken = False

    async def load_known_users(self) -> KnownUsers:
        if self.broken:
            raise LedgerUnreadable("the known-users record does not validate (1 error(s))")
        return self.known.model_copy(deep=True)

    async def save_known_users(self, known: KnownUsers) -> None:
        self.known = known.model_copy(deep=True)
        self.saves += 1

    def mxids(self) -> dict[str, str]:
        return {pk: k.mxid for pk, k in self.known.users.items()}


class FakeMasLinks:
    """Upstream links ``subject -> username`` (one MAS user per username)."""

    def __init__(self, links: dict[str, str] | None = None, *, provider: str = "P1") -> None:
        self.links = links or {}
        self.extra: list[UpstreamLink] = []
        self.fail = False
        self.provider = provider
        self.asked_provider: list[str | None] = []

    async def list_upstream_links(self, *, provider_id: str | None = None) -> list[UpstreamLink]:
        self.asked_provider.append(provider_id)
        if self.fail:
            raise RuntimeError("MAS 502")
        links = [UpstreamLink(self.provider, subject, f"U-{name}") for subject, name in self.links.items()]
        return links + self.extra

    async def list_usernames(self) -> dict[str, str]:
        names = {f"U-{name}": name for name in self.links.values()}
        names.update({link.user_id: link.user_id.removeprefix("U-") for link in self.extra if link.user_id})
        return names


def _resolver(
    config: OnbotConfig, store: MemoryBindings | None = None, mas: Any = None
) -> tuple[IdentityResolver, list[str]]:
    bus = EventBus()
    reports: list[str] = []

    async def collect(event: Event) -> None:
        reports.append(event.payload["text"])

    bus.subscribe(Signal.identity_attention, collect)
    return IdentityResolver(config, store=store, mas=mas, events=bus, clock=lambda: 5.0), reports


# --- without MAS: the binding --------------------------------------------------------------------


async def test_a_user_never_seen_is_resolved_by_the_mapping_attribute_and_bound_on_first_sight() -> None:
    store = MemoryBindings()
    resolver, reports = _resolver(_config(), store)
    users = [_user(1, "alice"), _user(2, "bob")]
    result = await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("alice")})
    assert result.mxids == {"1": mx("alice"), "2": mx("bob")}
    # Only an existing account is recorded: bob has not logged in yet.
    assert store.mxids() == {"1": mx("alice")}
    assert store.known.users["1"].first_seen == 5.0
    assert reports == []


async def test_a_renamed_user_keeps_the_bound_account_and_the_rename_is_reported_once() -> None:
    store = MemoryBindings({"1": mx("alice")})
    resolver, reports = _resolver(_config(), store)
    for _ in range(3):
        await resolver.refresh()
        result = await resolver.resolve([_user(1, "alicia")], bind=["1"], matrix_mxids={mx("alice")})
        assert result.mxids == {"1": mx("alice")}
        assert result.held == {}
    assert store.mxids() == {"1": mx("alice")}  # never silently re-mapped
    assert len(reports) == 1
    assert "'alicia' (pk 1)" in reports[0]
    assert mx("alicia") in reports[0] and mx("alice") in reports[0]


async def test_a_changed_mapping_attribute_moves_nobody() -> None:
    """A mass re-mapping (for example a changed config) keeps every bound user on their account."""
    store = MemoryBindings({str(pk): mx(f"user{pk}") for pk in range(1, 31)})
    resolver, reports = _resolver(_config(attribute="attributes.matrix_name"), store)
    users = [_user(pk, f"user{pk}", attributes={"matrix_name": f"other{pk}"}) for pk in range(1, 31)]
    result = await resolver.resolve(users, bind=[str(pk) for pk in range(1, 31)])
    assert result.mxids == {str(pk): mx(f"user{pk}") for pk in range(1, 31)}
    assert len(reports) == 1  # one report for the whole pass, not thirty
    assert "30 Authentik user(s)" in reports[0] and "and 20 more" in reports[0]


async def test_two_users_resolving_to_one_account_are_both_held_and_reported() -> None:
    store = MemoryBindings()
    resolver, reports = _resolver(_config(attribute="attributes.mx"), store)
    users = [_user(1, "ann", attributes={"mx": "shared"}), _user(2, "bea", attributes={"mx": "shared"})]
    result = await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("shared")})
    assert result.mxids == {}
    assert result.held == {mx("shared"): ("1", "2")}
    assert result.held_pks == {"1", "2"}
    assert store.mxids() == {}  # neither is recorded
    assert len(reports) == 1 and "'ann' (pk 1), 'bea' (pk 2)" in reports[0]
    await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("shared")})
    assert len(reports) == 1  # once per process


async def test_a_newcomer_naming_a_bound_account_gets_nothing() -> None:
    """SEC-07: renaming oneself to somebody else's name is no way into their account. D21
    (2026-11-09): without MAS the recorded binding wins, so the newcomer alone is skipped."""
    store = MemoryBindings({"1": mx("alice")})
    resolver, reports = _resolver(_config(), store)
    # pk 1 (alice) is not in this call (out of scope, say); her binding still claims the account.
    result = await resolver.resolve([_user(2, "alice")], bind=["2"], matrix_mxids={mx("alice")})
    assert result.mxids == {}
    assert result.held == {}
    assert result.rejected == {"2": mx("alice")}
    assert store.mxids() == {"1": mx("alice")}
    (report,) = reports
    assert "'alice' (pk 2)" in report and "the one of pk 1 (a recorded binding)" in report


async def test_a_present_bound_owner_wins_over_a_guess_without_mas() -> None:
    store = MemoryBindings({"1": mx("alice")})
    resolver, _ = _resolver(_config(attribute="attributes.mx"), store)
    users = [_user(1, "alice", attributes={"mx": "alice"}), _user(2, "carl", attributes={"mx": "alice"})]
    result = await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("alice")})
    assert result.mxids == {"1": mx("alice")}
    assert result.held == {}
    assert result.rejected == {"2": mx("alice")}
    assert result.held_mxids == frozenset()


async def test_no_binding_for_the_bot_ignored_accounts_or_users_not_offered() -> None:
    store = MemoryBindings()
    config = _config()
    config.matrix_user_ignore_list = [mx("svc")]
    resolver, _ = _resolver(config, store)
    users = [_user(1, "bot"), _user(2, "svc"), _user(3, "carl")]
    await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("bot"), mx("svc"), mx("carl")})
    assert store.mxids() == {}


async def test_a_user_without_the_attribute_and_without_a_binding_is_left_out() -> None:
    resolver, _ = _resolver(_config(attribute="attributes.mx"), MemoryBindings())
    assert (await resolver.resolve([_user(1, "alice")])).mxids == {}


async def test_an_unreadable_binding_record_makes_resolution_unavailable() -> None:
    store = MemoryBindings()
    store.broken = True
    resolver, reports = _resolver(_config(), store)
    await resolver.refresh()
    assert resolver.unavailable is not None
    with pytest.raises(IdentityUnavailable):
        await resolver.resolve([_user(1, "alice")])
    assert len(reports) == 1 and "known_users" in reports[0]
    store.broken = False
    await resolver.refresh()
    assert resolver.unavailable is None
    assert (await resolver.resolve([_user(1, "alice")])).mxids == {"1": mx("alice")}


# --- with MAS: the upstream link -----------------------------------------------------------------


async def test_mas_is_authoritative_over_the_mapping_attribute() -> None:
    store = MemoryBindings()
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, reports = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(1, "alicia")], bind=["1"], matrix_mxids={mx("alice")})
    assert result.mxids == {"1": mx("alice")}
    assert store.mxids() == {"1": mx("alice")}
    assert len(reports) == 1 and f"the mapping attribute names {mx('alicia')}" in reports[0]


async def test_mas_wins_over_a_disagreeing_binding_and_says_so() -> None:
    store = MemoryBindings({"1": mx("wrong")})
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, reports = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(1, "alice")], bind=["1"], matrix_mxids={mx("alice")})
    assert result.mxids == {"1": mx("alice")}
    assert store.mxids() == {"1": mx("alice")}
    assert store.known.users["1"].first_seen == 1.0  # the first sight stays
    assert any(f"MAS links {mx('alice')}, the recorded binding was {mx('wrong')}" in r for r in reports)


async def test_without_a_mas_link_the_binding_then_the_attribute_apply() -> None:
    store = MemoryBindings({"1": mx("alice")})
    mas = FakeMasLinks({})
    resolver, _ = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(1, "alicia"), _user(2, "bob")])
    assert result.mxids == {"1": mx("alice"), "2": mx("bob")}


async def test_a_mas_that_cannot_be_read_degrades_to_the_bindings_and_reports_once() -> None:
    store = MemoryBindings({"1": mx("alice")})
    mas = FakeMasLinks({"hash-1": "alice"})
    mas.fail = True
    resolver, reports = _resolver(_config(mas=True), store, mas)
    for _ in range(2):
        await resolver.refresh()
        assert (await resolver.resolve([_user(1, "alicia")])).mxids == {"1": mx("alice")}
    assert sum("could not read the upstream links" in r for r in reports) == 1


async def test_links_that_match_nobody_point_at_the_subject_setting() -> None:
    mas = FakeMasLinks({"alice": "alice"})  # the provider sends usernames, onbot looks for uid
    store = MemoryBindings({"2": mx("bob")})
    resolver, reports = _resolver(_config(mas=True), store, mas)
    users = [_user(1, "alice"), _user(2, "bob")]
    result = await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("alice"), mx("bob")})
    assert any("upstream_subject_attribute" in r for r in reports)
    # A pass whose in-scope users have no link of their own cannot be told apart from a wrong
    # setting: the guess of a MAS-linked account is skipped and never bound; bound users resolve.
    assert result.mxids == {"2": mx("bob")}
    assert result.rejected == {"1": mx("alice")}
    assert result.deferred == {}
    assert store.mxids() == {"2": mx("bob")}


async def test_the_subject_attribute_and_provider_are_configurable() -> None:
    mas = FakeMasLinks({"1": "alice"})
    config = _config(mas=True, upstream_subject_attribute="pk", upstream_provider_id="P1")
    resolver, _ = _resolver(config, MemoryBindings(), mas)
    assert (await resolver.resolve([_user(1, "x")])).mxids == {"1": mx("alice")}
    assert mas.asked_provider == ["P1"]


async def test_a_subject_linked_to_two_accounts_is_held_on_both() -> None:
    mas = FakeMasLinks({"hash-1": "alice"})
    mas.extra = [UpstreamLink("P2", "hash-1", "U-other")]
    resolver, reports = _resolver(_config(mas=True), MemoryBindings(), mas)
    result = await resolver.resolve([_user(1, "alice")], bind=["1"], matrix_mxids={mx("alice"), mx("other")})
    assert result.mxids == {}
    assert result.held_mxids == {mx("alice"), mx("other")}
    assert any("several accounts" in r for r in reports)


async def test_the_lookup_is_off_without_a_subject_attribute() -> None:
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, _ = _resolver(_config(mas=True, upstream_subject_attribute=None), MemoryBindings(), mas)
    assert resolver.mas is None
    assert (await resolver.resolve([_user(1, "bob")])).mxids == {"1": mx("bob")}
    assert mas.asked_provider == []


# --- D21: who keeps a contested account ------------------------------------------------------------


async def test_a_mas_linked_owner_wins_over_an_attribute_guess() -> None:
    store = MemoryBindings()
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, reports = _resolver(_config(mas=True, attribute="attributes.mx"), store, mas)
    users = [_user(1, "alice", attributes={"mx": "alice"}), _user(2, "carl", attributes={"mx": "alice"})]
    result = await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("alice")})
    assert result.mxids == {"1": mx("alice")}
    assert result.held == {}
    assert result.rejected == {"2": mx("alice")}
    assert store.mxids() == {"1": mx("alice")}
    assert sum("'carl' (pk 2)" in r and "MAS links that account to 'alice' (pk 1)" in r for r in reports) == 1
    await resolver.resolve(users, bind=["1", "2"], matrix_mxids={mx("alice")})
    assert len(reports) == 1  # once per process


async def test_an_absent_owner_whose_account_mas_links_still_beats_a_guess() -> None:
    """The room stage resolves active users only: a disabled alice is absent, her binding is not."""
    store = MemoryBindings({"1": mx("alice")})
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, _ = _resolver(_config(mas=True, attribute="attributes.mx"), store, mas)
    result = await resolver.resolve([_user(2, "carl", attributes={"mx": "alice"})])
    assert result.mxids == {}
    assert result.held == {}
    assert result.rejected == {"2": mx("alice")}


async def test_a_binding_wins_over_a_guess_when_mas_does_not_link_the_account() -> None:
    """D21 (2026-11-09): before, the binding against a guess failed closed for both."""
    store = MemoryBindings({"1": mx("alice")})
    mas = FakeMasLinks({})
    resolver, _ = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(2, "alice")])
    assert result.held == {}
    assert result.rejected == {"2": mx("alice")}


async def test_a_binding_against_a_guess_fails_closed_while_mas_cannot_be_read() -> None:
    """MAS might link the account to the guesser's subject; without its answer nobody wins."""
    store = MemoryBindings({"1": mx("alice")})
    mas = FakeMasLinks({"hash-2": "alice"})
    mas.fail = True
    resolver, _ = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(2, "alice")])
    assert result.held == {mx("alice"): ("1", "2")}
    assert result.rejected == {}


async def test_a_binding_against_a_guess_fails_closed_when_mas_links_the_account_elsewhere() -> None:
    """MAS links @alice to a third subject: neither the bound alice (her own subject has no link)
    nor the guess is its owner."""
    store = MemoryBindings({"1": mx("alice")})
    mas = FakeMasLinks({"hash-9": "alice"})
    resolver, _ = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(1, "alice"), _user(2, "alice")])
    assert result.mxids == {}
    assert result.held == {mx("alice"): ("1", "2")}


async def test_a_mas_link_against_another_users_binding_fails_closed() -> None:
    store = MemoryBindings({"2": mx("alice")})
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, _ = _resolver(_config(mas=True), store, mas)
    result = await resolver.resolve([_user(1, "alice"), _user(2, "carl")], matrix_mxids={mx("alice")})
    assert result.mxids == {}
    assert result.held == {mx("alice"): ("1", "2")}
    assert result.rejected == {}


async def test_two_mas_links_to_one_account_fail_closed() -> None:
    mas = FakeMasLinks({"hash-1": "alice", "hash-2": "alice"})
    resolver, _ = _resolver(_config(mas=True), MemoryBindings(), mas)
    result = await resolver.resolve([_user(1, "alice"), _user(2, "carl")])
    assert result.mxids == {}
    assert result.held == {mx("alice"): ("1", "2")}


async def test_two_bindings_to_one_account_fail_closed() -> None:
    store = MemoryBindings({"1": mx("alice"), "2": mx("alice")})
    resolver, _ = _resolver(_config(), store)
    result = await resolver.resolve([_user(1, "alice"), _user(2, "carl")])
    assert result.mxids == {}
    assert result.held == {mx("alice"): ("1", "2")}


async def test_a_mas_owner_against_a_guess_and_a_binding_fails_closed() -> None:
    """Only a single authoritative claimant beats guesses; a second authoritative one holds all."""
    store = MemoryBindings({"3": mx("alice")})
    mas = FakeMasLinks({"hash-1": "alice"})
    resolver, _ = _resolver(_config(mas=True, attribute="attributes.mx"), store, mas)
    users = [_user(1, "alice", attributes={"mx": "alice"}), _user(2, "carl", attributes={"mx": "alice"})]
    result = await resolver.resolve(users)
    assert result.mxids == {}
    assert result.held == {mx("alice"): ("1", "2", "3")}
    assert result.rejected == {}


async def test_a_guess_never_takes_an_account_mas_links_to_an_absent_unbound_owner() -> None:
    """Wave 3 review: alice owns @alice through MAS but is disabled or outside the scope (absent from
    the call) and was never bound. carl's attribute names @alice: carl is skipped, never bound."""
    store = MemoryBindings()
    mas = FakeMasLinks({"hash-1": "alice", "hash-3": "bob"})
    resolver, reports = _resolver(_config(mas=True, attribute="attributes.mx"), store, mas)
    carl = _user(2, "carl", attributes={"mx": "alice"})
    bob = _user(3, "bob", attributes={"mx": "bob"})
    result = await resolver.resolve([carl, bob], bind=["2", "3"], matrix_mxids={mx("alice"), mx("bob")})
    assert result.mxids == {"3": mx("bob")}
    assert result.held == {}
    assert result.rejected == {"2": mx("alice")}
    assert result.deferred == {}
    assert store.mxids() == {"3": mx("bob")}
    assert sum("'carl' (pk 2)" in r and "upstream subject hash-1" in r for r in reports) == 1


# --- MAS configured but unreadable -----------------------------------------------------------------


async def test_while_mas_cannot_be_read_users_never_seen_are_deferred_not_bound() -> None:
    """Wave 3 review: a guess recorded during a MAS outage would later count as authoritative evidence
    against the MAS-linked owner. Users never seen are skipped and their target is left alone; bound
    users resolve as usual. Once MAS answers, they are resolved through it."""
    store = MemoryBindings({"3": mx("bob")})
    mas = FakeMasLinks({"hash-1": "alice", "hash-9": "alice2"})
    mas.fail = True
    resolver, reports = _resolver(_config(mas=True), store, mas)
    renamed = _user(1, "alice2")  # never bound, the attribute names somebody else's account
    newcomer = _user(4, "dora")  # never logged in: no account, so the guess touches nobody
    users = [renamed, _user(3, "bob"), newcomer]
    existing = {mx("alice"), mx("alice2"), mx("bob")}
    result = await resolver.resolve(users, bind=["1", "3", "4"], matrix_mxids=existing)
    assert result.mxids == {"3": mx("bob"), "4": mx("dora")}
    assert result.deferred == {"1": mx("alice2")}
    assert mx("alice2") in result.held_mxids  # no join, kick, lock or erase of that account
    assert store.mxids() == {"3": mx("bob")}
    assert any("not seen before are skipped" in r for r in reports)

    mas.fail = False
    await resolver.refresh()
    result = await resolver.resolve(users, bind=["1", "3", "4"], matrix_mxids=existing)
    assert result.mxids == {"1": mx("alice"), "3": mx("bob"), "4": mx("dora")}
    assert result.deferred == {}
    assert store.mxids() == {"1": mx("alice"), "3": mx("bob")}


async def test_without_mas_users_never_seen_are_still_resolved_by_the_attribute() -> None:
    store = MemoryBindings()
    resolver, _ = _resolver(_config(), store)
    result = await resolver.resolve([_user(1, "alice")], bind=["1"], matrix_mxids={mx("alice")})
    assert result.mxids == {"1": mx("alice")}
    assert result.deferred == {}
