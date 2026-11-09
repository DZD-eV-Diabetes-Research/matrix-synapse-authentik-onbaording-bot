"""Authentik user to Matrix ID resolution (ADR-0006, D8): the critical MAS integration contract.

Under MAS, accounts are auto-provisioned on first login and the MXID localpart is fixed by MAS at
that moment, from an upstream Authentik claim. The bot does **not** create accounts; it must find the
account each Authentik user really owns. A username can change later (marriage, a name change), so
the identity is anchored on the immutable Authentik user (``pk``), never on the mutable attribute
alone. :class:`IdentityResolver` is the one place that turns an Authentik user into an MXID
(MNT-13); every caller (room sync, offboarding, bot admins, global room admins) goes through it.

Resolution order for one Authentik user:

1. **MAS** (with ``mas_admin`` and ``mas_admin.upstream_subject_attribute``): the upstream OAuth link
   whose subject is the user's subject, then the MAS user it belongs to; its username is the
   localpart. This is authoritative. A recorded binding that disagrees is replaced and reported.
2. **The binding** onbot recorded when it first saw the user active, in scope and mapped to an
   existing account (``authentik pk -> MXID`` in the bot's account data, the ``KnownUsers`` record of
   the offboarding lifecycle). It is kept when the mapping attribute changes: a renamed user keeps
   their account, rooms and power levels. The change is reported, never silently re-mapped.
3. **The mapping attribute** (``authentik_username_mapping_attribute``), only for a user never seen
   before (:func:`compute_mxid`).

Several Authentik users that resolve to one MXID (D21): when exactly one of them is linked to it
through MAS and the others only guess by the mapping attribute (never seen, no MAS link), the
MAS-linked owner is managed normally and the guessers are skipped and reported. A recorded binding
wins over guesses in the same way where MAS cannot contradict it: MAS is not configured, or MAS links
that account to nobody (added 2026-11-09). Otherwise (two MAS links, two bindings, a MAS link against
another user's binding, a binding against a guess while MAS is configured but unreadable or links the
account elsewhere) the conflict fails closed: all of them are skipped (no join, no kick, no
power-level change, no lock or erase) and the conflict is reported.

A guess never takes an account MAS links to somebody else, even when that owner is not part of the
call and was never bound (disabled or outside the sync scope): the guesser is skipped and reported
(D21, wave 3 review).

While MAS is configured but cannot be read, a guess cannot be checked against the links. Users onbot
has never seen are then **deferred**: not resolved, not bound, and the account their mapping
attribute names is left alone for the pass (no join, kick, power-level change, lock or erase), like
an account in a conflict. Users with a recorded binding are resolved from it as usual. Recording a
guess meanwhile would turn it into a binding, which later counts as authoritative evidence against
the real, MAS-linked owner. A guess of an account that does not exist is not deferred (it touches
nobody), so users who never logged in keep counting as users in scope for the guard.

A wrong ``upstream_subject_attribute`` (links that match nobody) is only reported: telling it apart
from a pass whose in-scope users simply have not logged in yet is not possible per pass. Meanwhile
every user never seen whose attribute names a MAS-linked account is rejected as above (skipped, not
bound); the guard's input check catches the mass effect on a deployment where nobody is bound yet.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from onbot.config import OnbotConfig
from onbot.events import EventBus, Signal
from onbot.lifecycle.accounts import KnownUser, KnownUsers, LedgerUnreadable
from onbot.logging import get_logger
from onbot.utils import get_nested_dict_val_by_path

log = get_logger(__name__)

SigilT = Literal["@", "#"]

# How many users one identity report names before it only counts the rest.
_REPORT_NAMES = 10


def build_canonical(local_part: str, server_name: str, sigil: SigilT = "@") -> str:
    """Build a fully-qualified Matrix identifier: ``<sigil><local_part>:<server_name>``.

    ``@`` → user ID, ``#`` → room alias. **Not for room IDs.** A user ID and a room *alias* are
    ``localpart:server_name`` and keep their ``:server`` component. A room *ID* (sigil ``!``) does
    **not**: since room version 12 it is a hash of the ``m.room.create`` event with no ``:domain``
    part at all (e.g. ``!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ``). Room IDs are opaque tokens
    the server hands us — never construct one from parts, and never split one on ``:`` to recover a
    server name (there is none). See ``docs/adr/0011-room-version-12.md``.
    """
    return f"{sigil}{local_part}:{server_name}"


def compute_mxid(
    authentik_user: dict[str, Any],
    *,
    username_attribute: str,
    server_name: str,
) -> str:
    """Compute the MXID the mapping attribute names for an Authentik user (G1.2, ADR-0006).

    This is the fallback for users onbot has never seen (see :class:`IdentityResolver`); callers
    resolve users through the resolver, not through this function.

    ``username_attribute`` is a dotted path into the Authentik user object (e.g. ``username`` or
    ``attributes.matrix_name``). Raises ``KeyError`` if the attribute is missing — a user we cannot
    map deterministically must surface loudly, never be silently skipped.
    """
    local_part = get_nested_dict_val_by_path(authentik_user, username_attribute.split("."))
    if local_part is None or local_part == "":
        raise KeyError(username_attribute)
    return build_canonical(str(local_part), server_name, "@")


def is_ignored(authentik_user: Mapping[str, Any], config: OnbotConfig) -> bool:
    """Whether the user is on ``authentik_user_ignore_list`` (matched against the raw username)."""
    return authentik_user.get("username") in config.authentik_user_ignore_list


def attribute_mxid(authentik_user: dict[str, Any], config: OnbotConfig) -> str | None:
    """The MXID the mapping attribute names, or ``None`` when the user does not carry it."""
    try:
        return compute_mxid(
            authentik_user,
            username_attribute=config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute,
            server_name=config.synapse_server.server_name,
        )
    except KeyError:
        return None


class BindingStore(Protocol):
    """Where the ``authentik pk -> MXID`` bindings live (the lifecycle's known-users record)."""

    async def load_known_users(self) -> KnownUsers: ...

    async def save_known_users(self, known: KnownUsers) -> None: ...


class IdentityUnavailable(RuntimeError):
    """The binding record cannot be read; resolving without it could move users to other accounts."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving a set of Authentik users."""

    #: ``pk -> MXID`` of every user that resolved to exactly one account nobody else claims, and of
    #: an owner (MAS-linked, or bound where MAS cannot contradict it) whose only rivals were
    #: attribute guesses (D21).
    mxids: Mapping[str, str] = field(default_factory=dict)
    #: ``MXID -> the pks that claim it``: conflicts, every claimant skipped (fail closed).
    held: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: ``pk -> MXID`` of attribute guesses that lost an account to its owner (D21): those users are
    #: skipped, the account is managed for its owner.
    rejected: Mapping[str, str] = field(default_factory=dict)
    #: ``pk -> the MXID its mapping attribute names`` of users never seen, while MAS is configured but
    #: cannot be read: skipped this pass, and the account is left alone (part of :attr:`held_mxids`).
    deferred: Mapping[str, str] = field(default_factory=dict)

    def mxid(self, pk: object) -> str | None:
        return self.mxids.get(str(pk))

    @property
    def held_mxids(self) -> frozenset[str]:
        """Accounts nobody may touch this pass: conflicts and the targets of deferred guesses."""
        return frozenset(self.held) | frozenset(self.deferred.values())

    @property
    def held_pks(self) -> frozenset[str]:
        return frozenset(pk for pks in self.held.values() for pk in pks)


@dataclass(slots=True)
class _MasView:
    """What MAS says this pass: ``subject -> the MXIDs linked to it``."""

    by_subject: dict[str, set[str]]
    links: int

    @property
    def linked(self) -> set[str]:
        """Every MXID some upstream link points to."""
        return {mxid for mxids in self.by_subject.values() for mxid in mxids}

    def subjects_of(self, mxid: str) -> list[str]:
        """The upstream subjects linked to ``mxid``."""
        return sorted(subject for subject, mxids in self.by_subject.items() if mxid in mxids)


class IdentityResolver:
    """Resolves Authentik users to MXIDs: through MAS, the recorded binding, then the attribute.

    Call :meth:`refresh` once per reconcile pass; it reads the bindings and, with MAS, the upstream
    links. :meth:`resolve` then answers from that view (it refreshes on first use). Reports go to the
    log at WARNING and, once per finding and process, to :attr:`Signal.identity_attention`.
    """

    def __init__(
        self,
        config: OnbotConfig,
        *,
        store: BindingStore | None = None,
        mas: Any | None = None,
        events: EventBus | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        mas_cfg = config.mas_admin
        self.subject_attribute = mas_cfg.upstream_subject_attribute if mas_cfg is not None else None
        self.provider_id = mas_cfg.upstream_provider_id if mas_cfg is not None else None
        # Without a subject attribute MAS cannot be asked who is who.
        self.mas = mas if self.subject_attribute else None
        self.events = events
        self.clock = clock
        self.server_name = config.synapse_server.server_name
        self._known = KnownUsers()
        self._mas_view: _MasView | None = None
        self._loaded = False
        #: Why nobody can be resolved this pass (the binding record is unreadable), else ``None``.
        self.unavailable: str | None = None
        self._reported: set[tuple[Any, ...]] = set()
        self._mas_failing = False
        # Users whose missing mapping attribute was already logged at WARNING.
        self._unmappable_logged: set[str] = set()

    async def refresh(self) -> None:
        """Read the bindings and the MAS links for this pass.

        A binding record that cannot be read is reported and sets :attr:`unavailable`: until it is
        repaired, :meth:`resolve` raises :class:`IdentityUnavailable`, because resolving from the
        attribute alone could put renamed users on other accounts. A MAS that cannot be read is
        reported; the pass resolves bound users from their bindings and defers users never seen (see
        the module docstring).
        """
        self.unavailable = None
        if self.store is not None:
            try:
                self._known = await self.store.load_known_users()
            except LedgerUnreadable as exc:
                self.unavailable = (
                    f"the record of Authentik users and their Matrix accounts cannot be read ({exc}); "
                    "no user is joined, kicked, given or denied a power level or offboarded until the "
                    "bot's known_users account data is repaired"
                )
                self._known = KnownUsers()
                await self._report(("unreadable", str(exc)), self.unavailable)
        self._mas_view = await self._read_mas() if self.mas is not None and self.unavailable is None else None
        self._loaded = True

    async def _read_mas(self) -> _MasView | None:
        assert self.mas is not None
        try:
            links = await self.mas.list_upstream_links(provider_id=self.provider_id)
            usernames: dict[str, str] = await self.mas.list_usernames() if links else {}
        except Exception as exc:
            message = (
                f"could not read the upstream links from MAS ({type(exc).__name__}: {exc}); until it "
                "answers, users are resolved from onbot's recorded bindings, and users onbot has not "
                "seen before are skipped (the account their mapping attribute names is left alone)"
            )
            log.warning("identity: %s", message)
            if not self._mas_failing:
                self._mas_failing = True
                await self._report(("mas-unavailable",), message, log_it=False)
            return None
        if self._mas_failing:
            self._mas_failing = False
            self._reported.discard(("mas-unavailable",))
            log.info("identity: MAS answers again; users are resolved through MAS")
        by_subject: dict[str, set[str]] = defaultdict(set)
        for link in links:
            username = usernames.get(link.user_id) if link.user_id else None
            if username is not None:
                by_subject[link.subject].add(build_canonical(username, self.server_name))
        return _MasView(by_subject=dict(by_subject), links=len(links))

    def _subject(self, user: dict[str, Any]) -> str | None:
        assert self.subject_attribute is not None
        value = get_nested_dict_val_by_path(user, self.subject_attribute.split("."), fallback_val=None)
        return None if value is None or value == "" else str(value)

    async def resolve(
        self,
        users: Iterable[dict[str, Any]],
        *,
        bind: Collection[str] = (),
        matrix_mxids: Collection[str] = (),
    ) -> Resolution:
        """Resolve ``users`` (raw Authentik user objects) to MXIDs.

        ``bind`` are the pks that may be recorded as a new binding (active, in scope, not ignored);
        a binding is recorded only for an account in ``matrix_mxids`` that nobody else claims, and
        saved at once. Every binding claims its MXID, also for users not in ``users``, so a newcomer
        whose attribute names an account bound to somebody else is a conflict.
        """
        if not self._loaded:
            await self.refresh()
        if self.unavailable is not None:
            raise IdentityUnavailable(self.unavailable)
        users = list(users)
        known = self._known.users
        candidates: dict[str, str] = {}
        # pk -> what its candidate rests on: "mas" (an upstream link), "binding" or "guess" (the
        # mapping attribute of a user never seen). Decides who keeps a contested account (D21).
        evidence: dict[str, str] = {}
        names: dict[str, str] = {}
        renamed: list[str] = []
        rebound: list[str] = []
        mas_conflicts: list[str] = []
        # pk -> the accounts MAS links its subject to, when that is more than one (held on all).
        mas_multi: dict[str, set[str]] = {}
        mas_matches = 0
        changed = False
        for user in users:
            pk = str(user["pk"])
            name = f"{user.get('username')!r} (pk {pk})"
            names[pk] = name
            computed = attribute_mxid(user, self.config)
            bound = known.get(pk)
            from_mas: set[str] = set()
            if self._mas_view is not None:
                subject = self._subject(user)
                if subject is not None:
                    from_mas = self._mas_view.by_subject.get(subject, set())
            if len(from_mas) > 1:
                mas_conflicts.append(f"{name}: {', '.join(sorted(from_mas))}")
                mas_multi[pk] = from_mas
                evidence[pk] = "mas"
                mas_matches += 1
                continue
            if from_mas:
                mxid = next(iter(from_mas))
                evidence[pk] = "mas"
                mas_matches += 1
                if bound is not None and bound.mxid != mxid:
                    rebound.append(f"{name}: MAS links {mxid}, the recorded binding was {bound.mxid}")
                    known[pk] = KnownUser(mxid=mxid, first_seen=bound.first_seen)
                    changed = True
            elif bound is not None:
                mxid = bound.mxid
                evidence[pk] = "binding"
            elif computed is not None:
                mxid = computed
                evidence[pk] = "guess"
            else:
                if pk not in self._unmappable_logged:
                    self._unmappable_logged.add(pk)
                    log.warning(
                        "cannot map an MXID for Authentik user %s: no %r on it and no recorded binding",
                        name,
                        self.config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute,
                    )
                continue
            if computed is not None and computed != mxid and (bound is not None or from_mas):
                renamed.append(f"{name}: the mapping attribute names {computed}, the account is {mxid}")
            candidates[pk] = mxid

        claims: dict[str, set[str]] = defaultdict(set)
        for pk, binding in known.items():
            claims[binding.mxid].add(pk)
        for pk, mxid in candidates.items():
            claims[mxid].add(pk)
        for pk, mxids in mas_multi.items():
            for mxid in mxids:
                claims[mxid].add(pk)
        held: dict[str, tuple[str, ...]] = {}
        rejected: dict[str, str] = {}
        # contested MXID -> (its owner, "mas" or "binding"): who kept it against guesses (D21)
        owner_of: dict[str, tuple[str, str]] = {}
        linked_mxids = self._mas_view.linked if self._mas_view is not None else set()
        for mxid, pks in claims.items():
            if len(pks) < 2:
                continue
            # A binding of a user not in this call is authoritative evidence too, backed by MAS when
            # MAS links the account at all (the room stage sees active users only, so a disabled
            # owner is often absent; a guesser has no link, so it cannot be the one MAS means).
            linked = mxid in linked_mxids
            owners = [pk for pk in pks if evidence.get(pk, "binding") != "guess"]
            owner_evidence = evidence.get(owners[0], "mas" if linked else "binding") if owners else None
            # A recorded binding wins like a MAS link where MAS cannot contradict it: MAS is not
            # configured, or it reads and links the account to nobody (D21, 2026-11-09). While MAS is
            # configured but unreadable it might, so that conflict still fails closed.
            binding_wins = owner_evidence == "binding" and (
                self.mas is None or (self._mas_view is not None and not linked)
            )
            if len(owners) == 1 and (owner_evidence == "mas" or binding_wins) and owners[0] not in mas_multi:
                # D21: one claimant's account is positively theirs (a MAS link or a recorded
                # binding); the others only guessed by the mapping attribute. The owner keeps it,
                # the guessers are skipped.
                for pk in pks - {owners[0]}:
                    rejected[pk] = mxid
                owner_of[mxid] = (owners[0], "mas" if owner_evidence == "mas" else "binding")
                continue
            held[mxid] = tuple(sorted(pks))
        for mxids in mas_multi.values():
            # One Authentik subject linked to several accounts: held on each, even unclaimed ones.
            for mxid in mxids:
                held.setdefault(mxid, tuple(sorted(claims[mxid])))
        held_pks = {pk for pks in held.values() for pk in pks}
        # MAS links, but none matches any user of a full pass: possibly a wrong subject setting
        # (reported below). Possibly not: the active users in scope may just not have logged in yet
        # while the one linked owner is disabled, so this changes nothing about the resolution.
        mas_no_match = bool(
            bind and self._mas_view is not None and self._mas_view.links and users and not mas_matches
        )
        # MAS is configured but unreadable: a guess cannot be checked against the links, so a user
        # never seen is deferred rather than resolved (and bound) on the attribute alone. A binding
        # recorded now would later count as evidence against the MAS-linked owner. A guess of an
        # account that does not exist (when the caller says which do) touches nobody and still
        # counts, so users who never logged in do not vanish from the guard's count.
        mas_unreadable = self.mas is not None and self._mas_view is None
        existing = set(matrix_mxids)
        deferred: dict[str, str] = {}
        for pk, mxid in candidates.items():
            if evidence[pk] != "guess" or pk in held_pks or pk in rejected:
                continue
            if mas_unreadable:
                if not matrix_mxids or mxid in existing:
                    deferred[pk] = mxid
            elif mxid in linked_mxids:
                # MAS links the account to another upstream subject (the guesser's own subject has no
                # link, else it would not be a guess). That owner may be absent from this call and
                # never bound (disabled or outside the scope); the account is still theirs (D21).
                rejected[pk] = mxid
        resolved = {
            pk: mxid
            for pk, mxid in candidates.items()
            if pk not in held_pks and pk not in rejected and pk not in deferred
        }

        excluded = {self.config.synapse_server.bot_user_id, *self.config.matrix_user_ignore_list}
        now = self.clock()
        for pk in bind:
            target = resolved.get(pk)
            if target is None or pk in known or target not in existing or target in excluded:
                continue
            known[pk] = KnownUser(mxid=target, first_seen=now)
            changed = True
        if changed and self.store is not None:
            await self.store.save_known_users(self._known)

        await self._report_findings(users, renamed, rebound, mas_conflicts, held)
        for pk, mxid in sorted(rejected.items()):
            if self._first(("rejected", pk, mxid)):
                guesser = names.get(pk, f"pk {pk}")
                if mxid in owner_of:
                    owner_pk, how = owner_of[mxid]
                    owner = names.get(owner_pk, f"pk {owner_pk}")
                    whose = (
                        f"MAS links that account to {owner}"
                        if how == "mas"
                        else f"onbot recorded that account as the one of {owner} (a recorded binding)"
                    )
                    tail = f"onbot manages {mxid} for its owner and skips {guesser}."
                else:
                    assert self._mas_view is not None
                    subjects = ", ".join(self._mas_view.subjects_of(mxid))
                    whose = f"MAS links that account to another Authentik user (upstream subject {subjects})"
                    tail = f"onbot skips {guesser}."
                await self._report(
                    None,
                    f"Authentik user {guesser} names the Matrix account {mxid} by the mapping "
                    f"attribute, but {whose}. {tail}",
                )
        if deferred:
            # Every pass while MAS cannot be read (the outage itself is reported once); the users are
            # retried on every pass.
            log.warning(
                "identity: MAS cannot be read; %d Authentik user(s) onbot has not seen before are "
                "skipped this pass and the accounts their mapping attribute names are left alone: %s",
                len(deferred),
                ", ".join(f"{names[pk]} -> {mxid}" for pk, mxid in sorted(deferred.items())),
            )
        if mas_no_match:
            assert self._mas_view is not None
            await self._report(
                ("mas-no-match", self.subject_attribute),
                f"MAS has {self._mas_view.links} upstream link(s), but none matches the "
                f"`{self.subject_attribute}` of any Authentik user. Check "
                "`mas_admin.upstream_subject_attribute` against the subject mode of the Authentik "
                "provider (and `mas_admin.upstream_provider_id`); until then users are resolved from "
                "onbot's recorded bindings and the mapping attribute, and a user onbot has not seen "
                "before whose attribute names an account MAS links is skipped",
            )
        return Resolution(mxids=resolved, held=held, rejected=rejected, deferred=deferred)

    async def _report_findings(
        self,
        users: list[dict[str, Any]],
        renamed: list[str],
        rebound: list[str],
        mas_conflicts: list[str],
        held: Mapping[str, tuple[str, ...]],
    ) -> None:
        names = {str(u["pk"]): f"{u.get('username')!r} (pk {u['pk']})" for u in users}
        new_renamed = [r for r in renamed if self._first(("renamed", r))]
        if new_renamed:
            await self._report(
                None,
                _listing(
                    f"{len(new_renamed)} Authentik user(s) now map to another Matrix ID by "
                    "`authentik_username_mapping_attribute` than the account they own. onbot keeps "
                    "their account, rooms and power levels (a rename does not move a user):",
                    new_renamed,
                ),
            )
        new_rebound = [r for r in rebound if self._first(("rebound", r))]
        if new_rebound:
            await self._report(
                None,
                _listing(
                    f"{len(new_rebound)} recorded binding(s) disagreed with MAS. MAS is authoritative, "
                    "onbot follows it from now on:",
                    new_rebound,
                ),
            )
        for mxid, pks in sorted(held.items()):
            if self._first(("held", mxid, pks)):
                who = ", ".join(names.get(pk, f"pk {pk}") for pk in pks)
                await self._report(
                    None,
                    f"{len(pks)} Authentik users resolve to the Matrix account {mxid}: {who}. onbot "
                    "skips all of them (no join, kick, power-level change, lock or erase of that "
                    "account) until an admin resolves the conflict in Authentik or in onbot's "
                    "known_users record.",
                )
        new_conflicts = [c for c in mas_conflicts if self._first(("mas-conflict", c))]
        if new_conflicts:
            await self._report(
                None,
                _listing(
                    "MAS links one Authentik subject to several accounts; those users are skipped "
                    "(set `mas_admin.upstream_provider_id` when MAS has several upstream providers):",
                    new_conflicts,
                ),
            )

    def _first(self, key: tuple[Any, ...]) -> bool:
        if key in self._reported:
            return False
        self._reported.add(key)
        return True

    async def _report(self, key: tuple[Any, ...] | None, text: str, *, log_it: bool = True) -> None:
        """Log ``text`` and post it to the admin room, once per ``key`` (``None``: caller checked)."""
        if key is not None and not self._first(key):
            return
        if log_it:
            log.warning("identity: %s", text)
        if self.events is not None:
            await self.events.emit(Signal.identity_attention, text=text)


def _listing(head: str, items: list[str]) -> str:
    lines = [head, *(f"- {item}" for item in items[:_REPORT_NAMES])]
    if len(items) > _REPORT_NAMES:
        lines.append(f"- and {len(items) - _REPORT_NAMES} more (see the log)")
        for item in items[_REPORT_NAMES:]:
            log.warning("identity: %s", item)
    return "\n".join(lines)
