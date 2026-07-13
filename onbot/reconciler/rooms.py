"""Group → room projection (pure logic).

Ported from the legacy ``Bot._get_authentik_groups_to_synapse_room_mappings`` and
``_get_matrix_room_attrs_from_authentik_group``, split into side-effect-free functions so the
mapping rules (the valuable business logic per BATTLE_PLAN §3) can be unit-tested without any API.

Bugs fixed vs legacy:

* ``matrix_room_create_params_from_authentik_attribute`` is now split into a key path before lookup
  (legacy passed the raw dotted string to ``get_nested_dict_val_by_path``, so it never matched).
* the create-params attribute default is ``attributes.chatroom_params`` (legacy ``attribute.…`` typo).
"""

from __future__ import annotations

import json
from typing import Any

from onbot.config import MatrixDynamicRoomSettings, OnbotConfig
from onbot.identity import build_canonical
from onbot.logging import get_logger
from onbot.models import GroupRoomMap, MatrixRoom, RoomCreateAttributes
from onbot.utils import get_nested_dict_val_by_path

log = get_logger(__name__)


def filter_synced_groups(groups: list[dict[str, Any]], config: OnbotConfig) -> list[dict[str, Any]]:
    """Apply the selective group→room rules (G2.5): ignore list, name prefix, parentage.

    The attribute filter (``only_groups_with_attributes``) is applied at the Authentik query level,
    so it is not repeated here.
    """
    settings = config.sync_matrix_rooms_based_on_authentik_groups
    result = groups
    if config.authentik_group_id_ignore_list:
        ignore = set(config.authentik_group_id_ignore_list)
        result = [g for g in result if g["pk"] not in ignore]
    if settings.only_for_groupnames_starting_with:
        prefix = settings.only_for_groupnames_starting_with
        result = [g for g in result if g["name"].startswith(prefix)]
    if settings.only_for_children_of_groups_with_uid:
        parents = set(settings.only_for_children_of_groups_with_uid)
        result = [g for g in result if g.get("parent") in parents]
    return result


def resolve_room_settings(group_pk: str, config: OnbotConfig) -> MatrixDynamicRoomSettings:
    """Merge per-group overrides (G6.7) over the default room settings.

    Only the keys the operator actually set in the override are applied — hence
    ``exclude_unset=True``. Dumping the whole override model would fill every omitted key with the
    *class* default, so the right-hand side of the merge is complete and ``matrix_room_default_settings``
    is thrown away entirely (the config field's description promises the opposite).
    """
    defaults = config.matrix_room_default_settings
    override = config.per_authentik_group_pk_matrix_room_settings.get(group_pk)
    if override is None:
        return defaults
    return MatrixDynamicRoomSettings.model_validate(
        defaults.model_dump() | override.model_dump(exclude_unset=True)
    )


def compute_room_attributes(
    group: dict[str, Any], config: OnbotConfig, server_name: str
) -> RoomCreateAttributes:
    """Compute the desired Matrix room attributes for an Authentik group (G6.1-G6.6)."""
    settings = resolve_room_settings(group["pk"], config)

    alias_base = get_nested_dict_val_by_path(
        group, settings.matrix_alias_from_authentik_attribute.split("."), fallback_val=None
    )
    if not alias_base:
        alias_base = group["pk"]
    alias = f"{settings.alias_prefix or ''}{alias_base}"
    # Matrix room aliases keep things simple: drop dashes (legacy behaviour; preserves alias matching).
    alias = alias.replace("-", "")

    name = get_nested_dict_val_by_path(
        group, settings.matrix_name_from_authentik_attribute.split("."), fallback_val=None
    )
    if settings.name_prefix and name and not name.startswith(settings.name_prefix):
        name = f"{settings.name_prefix}{name}"

    topic: str | None = None
    if settings.matrix_topic_from_authentik_attribute:
        topic = get_nested_dict_val_by_path(
            group, settings.matrix_topic_from_authentik_attribute.split("."), fallback_val=None
        )
    if settings.topic_prefix:
        topic = f"{settings.topic_prefix}{topic or ''}"

    room_params = dict(settings.default_room_create_params or {})
    if settings.matrix_room_create_params_from_authentik_attribute:
        raw = get_nested_dict_val_by_path(
            group,
            settings.matrix_room_create_params_from_authentik_attribute.split("."),
            fallback_val=None,
        )
        if raw:
            try:
                room_params = room_params | json.loads(raw)
            except json.JSONDecodeError, TypeError:
                log.warning("Group %s has invalid room-create-params JSON: %r", group["pk"], raw)

    # Room avatar (icon): the configured attribute is a key inside the group's custom ``attributes``
    # (legacy semantics), holding an HTTP(S) URL. Uploaded + applied by the engine, deduped per URL.
    avatar_source_url: str | None = None
    avatar_attr = config.sync_matrix_rooms_based_on_authentik_groups.room_avatar_url_attribute
    if avatar_attr:
        avatar_source_url = (group.get("attributes") or {}).get(avatar_attr) or None

    return RoomCreateAttributes(
        alias=alias,
        canonical_alias=build_canonical(alias, server_name, "#"),
        name=name,
        topic=topic,
        room_params=room_params,
        encrypted=settings.end2end_encryption_enabled,
        avatar_source_url=avatar_source_url,
    )


def lobby_enabled_for_group(group: dict[str, Any], settings: MatrixDynamicRoomSettings) -> bool:
    """Whether this group opts into a visitor lobby (ADR-0012).

    The Authentik attribute (``matrix_room_visitor_lobby_from_authentik_attribute``) overrides the
    configured ``visitor_lobby_enabled`` default, so a group owner can open a lobby without a config
    deploy. A value that is not a boolean is ignored with a warning naming the group and the default
    applies — the same shape as the create-params JSON path.
    """
    default = settings.visitor_lobby_enabled
    attr_path = settings.matrix_room_visitor_lobby_from_authentik_attribute
    if not attr_path:
        return default
    raw = get_nested_dict_val_by_path(group, attr_path.split("."), fallback_val=None)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
        return raw.strip().lower() == "true"
    log.warning(
        "Group %s has invalid visitor-lobby attribute value %r; using default %s",
        group["pk"],
        raw,
        default,
    )
    return default


# Lobby power levels: visitors can talk (users_default/events_default 0), but only the bot rewrites
# room state (state_default 100). The creator/bot is NOT named in a `users` map — under room version
# 12 the auth rules reject naming a creator there (ADR-0011); the server seats it above everyone.
_LOBBY_POWER_LEVELS = {"users_default": 0, "events_default": 0, "state_default": 100}


def compute_lobby_attributes(
    group_attrs: RoomCreateAttributes,
    settings: MatrixDynamicRoomSettings,
    server_name: str,
) -> RoomCreateAttributes:
    """Compute a lobby's desired attributes from its group room's attributes (ADR-0012).

    The lobby alias is the group alias plus ``visitor_lobby_alias_suffix`` — appended *after* the
    dash-stripping in :func:`compute_room_attributes`, so a ``-lobby`` suffix survives. The lobby's
    join rule is not set here (it is resolved from the runtime space id in
    :mod:`onbot.reconciler.join_rules`).
    """
    alias = f"{group_attrs.alias}{settings.visitor_lobby_alias_suffix}"
    group_name = group_attrs.name or group_attrs.alias
    name = f"{group_name}{settings.visitor_lobby_name_suffix}"
    try:
        topic = settings.visitor_lobby_topic_template.format(name=group_name)
    except KeyError, IndexError:
        # A template with a stray/unknown placeholder must not crash the reconcile.
        topic = settings.visitor_lobby_topic_template
    room_params: dict[str, Any] = {
        "preset": "private_chat",
        "visibility": "private",
        "power_level_content_override": dict(_LOBBY_POWER_LEVELS),
    }
    return RoomCreateAttributes(
        alias=alias,
        canonical_alias=build_canonical(alias, server_name, "#"),
        name=name,
        topic=topic,
        room_params=room_params,
        encrypted=settings.visitor_lobby_end2end_encryption_enabled,
        avatar_source_url=None,
    )


def build_group_room_maps(
    groups: list[dict[str, Any]],
    actual_rooms: list[MatrixRoom],
    config: OnbotConfig,
    server_name: str,
) -> list[GroupRoomMap]:
    """Project filtered groups to desired rooms, pairing each with its actual room if present.

    When a group opts into a lobby (:func:`lobby_enabled_for_group`), a second desired room is
    computed and paired with its actual lobby room, if one exists yet, by canonical alias.
    """
    rooms_by_alias = {r.canonical_alias: r for r in actual_rooms if r.canonical_alias}
    maps: list[GroupRoomMap] = []
    for group in filter_synced_groups(groups, config):
        settings = resolve_room_settings(group["pk"], config)
        desired = compute_room_attributes(group, config, server_name)
        lobby_desired: RoomCreateAttributes | None = None
        lobby: MatrixRoom | None = None
        if lobby_enabled_for_group(group, settings):
            lobby_desired = compute_lobby_attributes(desired, settings, server_name)
            lobby = rooms_by_alias.get(lobby_desired.canonical_alias)
        maps.append(
            GroupRoomMap(
                authentik_group=group,
                desired=desired,
                room=rooms_by_alias.get(desired.canonical_alias),
                lobby_desired=lobby_desired,
                lobby=lobby,
            )
        )
    return maps
