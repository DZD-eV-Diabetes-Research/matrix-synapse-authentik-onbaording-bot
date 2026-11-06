"""Unit tests for the configuration model."""

from pathlib import Path

import pytest
import yaml

from onbot.config import (
    CONFIG_FILE_ENV_VAR,
    MatrixDynamicRoomSettings,
    OnbotConfig,
    generate_example_config,
    load_config,
)

_MINIMAL = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik.company.org/", "api_key": "key"},
    # The admin room is on by default and then needs somebody who may command the bot (D19).
    "admin_room": {"admin_user_ids": ["@admin:company.org"]},
}


def test_minimal_validates_with_defaults() -> None:
    cfg = OnbotConfig.model_validate(_MINIMAL)
    assert cfg.log_level == "INFO"
    assert cfg.admin_room.enabled is True
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
        "ONBOT_ADMIN_ROOM__ENABLED": "false",
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


def test_visitor_lobby_requires_a_space() -> None:
    # A lobby's `restricted` join rule needs a space to be restricted to; enabling one while the
    # space is off is rejected at config-validation time rather than silently creating a dead room.
    with pytest.raises(ValueError, match="create_matrix_rooms_in_a_matrix_space"):
        OnbotConfig.model_validate(
            _MINIMAL
            | {
                "matrix_room_default_settings": {"visitor_lobby_enabled": True},
                "create_matrix_rooms_in_a_matrix_space": {"enabled": False},
            }
        )


def test_visitor_lobby_in_a_per_group_override_requires_a_space() -> None:
    with pytest.raises(ValueError, match="create_matrix_rooms_in_a_matrix_space"):
        OnbotConfig.model_validate(
            _MINIMAL
            | {
                "per_authentik_group_pk_matrix_room_settings": {"g1": {"visitor_lobby_enabled": True}},
                "create_matrix_rooms_in_a_matrix_space": {"enabled": False},
            }
        )


def test_visitor_lobby_with_a_space_is_accepted() -> None:
    cfg = OnbotConfig.model_validate(
        _MINIMAL | {"matrix_room_default_settings": {"visitor_lobby_enabled": True}}
    )
    assert cfg.matrix_room_default_settings.visitor_lobby_enabled is True


def test_an_enabled_admin_room_without_admins_is_rejected() -> None:
    """D19: the admin room is on by default, and a room nobody may command is a config error."""
    data = {k: v for k, v in _MINIMAL.items() if k != "admin_room"}
    with pytest.raises(ValueError) as excinfo:
        OnbotConfig.model_validate(data)
    message = str(excinfo.value)
    assert "admin_room.admin_user_ids" in message
    assert "admin_room.authentik_group_pks_granting_bot_admin" in message
    assert "admin_room.enabled: false" in message
    with pytest.raises(ValueError, match="nobody may command the bot"):
        OnbotConfig.model_validate(data | {"admin_room": {"enabled": True, "admin_user_ids": []}})


@pytest.mark.parametrize(
    "admin_room",
    [
        {"admin_user_ids": ["@admin:company.org"]},
        {"authentik_group_pks_granting_bot_admin": ["1120a6e1124f309bbe96c8be5fb09eab"]},
        {"enabled": False},
    ],
)
def test_an_admin_room_with_an_admin_source_or_disabled_is_accepted(admin_room: dict[str, object]) -> None:
    cfg = OnbotConfig.model_validate(_MINIMAL | {"admin_room": admin_room})
    assert cfg.admin_room.enabled is admin_room.get("enabled", True)


def test_generate_example_config_roundtrips() -> None:
    text = generate_example_config()
    data = yaml.safe_load(text)
    assert data["authentik_server"]["api_key"] is None
    assert data["sync_matrix_rooms_based_on_authentik_groups"]["enabled"] is True


def _write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_env_overrides_yaml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config file must not shadow the environment — that is where secrets come from."""
    path = _write_config(tmp_path, _MINIMAL)
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(path))
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN", "from-env")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__API_KEY", "key-from-env")

    cfg = load_config()

    assert cfg.synapse_server.bot_access_token is not None
    assert cfg.synapse_server.bot_access_token.get_secret_value() == "from-env"
    assert cfg.authentik_server.api_key.get_secret_value() == "key-from-env"
    # untouched keys still come from the file
    assert cfg.synapse_server.server_name == "company.org"


def test_secrets_may_be_omitted_from_the_yaml_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment pattern: a committed, secret-free config plus ONBOT_* for the credentials."""
    secret_free = {
        "synapse_server": {k: v for k, v in _MINIMAL["synapse_server"].items() if k != "bot_access_token"},
        "authentik_server": {"url": "https://authentik.company.org/"},
        "admin_room": {"enabled": False},
        "mas_admin": {"url": "https://mas/", "client_id": "cid", "client_secret": "placeholder"},
    }
    path = _write_config(tmp_path, secret_free)
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(path))
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__API_KEY", "key")
    monkeypatch.setenv("ONBOT_MAS_ADMIN__CLIENT_SECRET", "mas-secret")

    cfg = load_config()

    assert cfg.synapse_server.bot_access_token is not None
    assert cfg.synapse_server.bot_access_token.get_secret_value() == "tok"
    assert cfg.authentik_server.api_key.get_secret_value() == "key"
    # a nested env var merges into the file's block rather than replacing it
    assert cfg.mas_admin is not None
    assert (cfg.mas_admin.client_id, cfg.mas_admin.client_secret.get_secret_value()) == ("cid", "mas-secret")


def test_generate_example_config_ignores_ambient_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template shows defaults, never whatever config/env happens to be present."""
    path = _write_config(tmp_path, _MINIMAL | {"log_level": "DEBUG"})
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(path))
    monkeypatch.setenv("ONBOT_SERVER_TICK_RATE_SEC", "99")

    data = yaml.safe_load(generate_example_config())

    assert data["log_level"] == "INFO"
    assert data["server_tick_rate_sec"] == 300
    assert data["synapse_server"]["server_name"] is None


def test_welcome_messages_take_plain_text_and_entries_with_an_id() -> None:
    from onbot.config import WelcomeMessage

    cfg = OnbotConfig.model_validate(
        {**_MINIMAL, "welcome_new_users_messages": ["plain", {"id": "intro", "text": "with id"}]}
    )
    assert cfg.welcome_new_users_messages == ["plain", WelcomeMessage(id="intro", text="with id")]


def test_welcome_message_ids_must_be_unique() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="repeated: intro"):
        OnbotConfig.model_validate(
            {
                **_MINIMAL,
                "welcome_new_users_messages": [{"id": "intro", "text": "a"}, {"id": "intro", "text": "b"}],
            }
        )
