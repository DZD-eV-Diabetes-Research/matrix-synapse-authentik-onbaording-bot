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
    """Merge per-group overrides (G6.7) over the default room settings."""
    defaults = config.matrix_room_default_settings
    override = config.per_authentik_group_pk_matrix_room_settings.get(group_pk)
    if override is None:
        return defaults
    return MatrixDynamicRoomSettings.model_validate(defaults.model_dump() | override.model_dump())


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

    return RoomCreateAttributes(
        alias=alias,
        canonical_alias=build_canonical(alias, server_name, "#"),
        name=name,
        topic=topic,
        room_params=room_params,
        encrypted=settings.end2end_encryption_enabled,
    )


def build_group_room_maps(
    groups: list[dict[str, Any]],
    actual_rooms: list[MatrixRoom],
    config: OnbotConfig,
    server_name: str,
) -> list[GroupRoomMap]:
    """Project filtered groups to desired rooms, pairing each with its actual room if present."""
    rooms_by_alias = {r.canonical_alias: r for r in actual_rooms if r.canonical_alias}
    maps: list[GroupRoomMap] = []
    for group in filter_synced_groups(groups, config):
        desired = compute_room_attributes(group, config, server_name)
        maps.append(
            GroupRoomMap(
                authentik_group=group,
                desired=desired,
                room=rooms_by_alias.get(desired.canonical_alias),
            )
        )
    return maps
