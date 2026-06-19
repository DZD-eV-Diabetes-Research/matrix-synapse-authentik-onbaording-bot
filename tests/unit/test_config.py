"""Unit tests for the configuration model."""

import pytest
import yaml

from onbot.config import MatrixDynamicRoomSettings, OnbotConfig, generate_example_config

_MINIMAL = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik.company.org/", "api_key": "key"},
}


def test_minimal_validates_with_defaults() -> None:
    cfg = OnbotConfig.model_validate(_MINIMAL)
    assert cfg.log_level == "INFO"
    assert cfg.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute == "username"
    assert cfg.create_matrix_rooms_in_a_matrix_space.enabled is True
    # lifecycle settings live solely under the sync section (legacy top-level dup removed)
    assert not hasattr(cfg, "deactivate_disabled_authentik_users_in_matrix")


def test_env_overrides_nested(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "ONBOT_SYNAPSE_SERVER__SERVER_NAME": "company.org",
        "ONBOT_SYNAPSE_SERVER__SERVER_URL": "https://internal.matrix",
        "ONBOT_SYNAPSE_SERVER__BOT_USER_ID": "@bot:company.org",
        "ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN": "tok",
        "ONBOT_AUTHENTIK_SERVER__URL": "https://authentik/",
        "ONBOT_AUTHENTIK_SERVER__API_KEY": "key",
        "ONBOT_LOG_LEVEL": "DEBUG",
        "ONBOT_SERVER_TICK_RATE_SEC": "99",
    }.items():
        monkeypatch.setenv(key, value)
    cfg = OnbotConfig()  # type: ignore[call-arg]
    assert cfg.log_level == "DEBUG"
    assert cfg.server_tick_rate_sec == 99
    assert cfg.synapse_server.server_name == "company.org"


def test_per_group_override_is_typed() -> None:
    cfg = OnbotConfig.model_validate(
        _MINIMAL
        | {
            "per_authentik_group_pk_matrix_room_settings": {
                "pk-1": {"topic_prefix": "X:", "matrix_alias_from_authentik_attribute": "name"}
            }
        }
    )
    override = cfg.per_authentik_group_pk_matrix_room_settings["pk-1"]
    assert isinstance(override, MatrixDynamicRoomSettings)
    assert override.topic_prefix == "X:"


def test_generate_example_config_roundtrips() -> None:
    text = generate_example_config()
    data = yaml.safe_load(text)
    assert data["authentik_server"]["api_key"] is None
    assert data["sync_matrix_rooms_based_on_authentik_groups"]["enabled"] is True
