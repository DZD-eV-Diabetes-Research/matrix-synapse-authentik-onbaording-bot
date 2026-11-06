"""Strict config validation (MNT-03) and secret handling (SEC-11).

Every case here used to load without complaint and then misbehave at runtime: a typo left a
destructive default in place, a scheme-less URL failed on the first request, a full alias produced
``##space:company.org:company.org`` and a new room on every start, and so on.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from onbot.config import OnbotConfig, generate_example_config, load_config

_MINIMAL: dict[str, Any] = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik.company.org/", "api_key": "key"},
    "admin_room": {"enabled": False},
}
_OAUTH2 = {"token_endpoint": "https://mas.company.org/oauth2/token", "client_id": "cid", "client_secret": "s"}


def _with(path: str, value: object) -> dict[str, Any]:
    """``_MINIMAL`` with ``value`` set at the dotted ``path`` (missing blocks are created)."""
    data = copy.deepcopy(_MINIMAL)
    node = data
    *parents, leaf = path.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value
    return data


def _errors(data: dict[str, Any]) -> list[dict[str, Any]]:
    with pytest.raises(ValidationError) as excinfo:
        OnbotConfig.model_validate(data)
    return [dict(e) for e in excinfo.value.errors()]


def _locs(data: dict[str, Any]) -> list[str]:
    return [".".join(str(p) for p in e["loc"]) for e in _errors(data)]


def _messages(data: dict[str, Any]) -> str:
    return " | ".join(e["msg"] for e in _errors(data))


# --- unknown keys --------------------------------------------------------------------------------

_LIFECYCLE = "sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix"


@pytest.mark.parametrize(
    "path",
    [
        f"{_LIFECYCLE}.dry_rn",
        f"{_LIFECYCLE}.delete_aftr_n_sec",
        "sync_authentik_users_with_matrix_rooms.kick_matrix_room_members_not_in_mapped_authentik_group_anymor",
        "synapse_server.bot_acces_token",
        "authentik_server.apikey",
        "admin_room.admin_user_id",
        "matrix_room_default_settings.end2end_encryption",
        "per_authentik_group_pk_matrix_room_settings.somegrouppk.name_prefx",
        "create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.avatar",
        "avatar_fetch.allowed_hosts",
        "server_tick_rate",  # top level
    ],
)
def test_unknown_keys_fail_loudly_at_every_level(path: str) -> None:
    errors = _errors(_with(path, False))
    assert [".".join(str(p) for p in e["loc"]) for e in errors] == [path]
    assert errors[0]["msg"] == "Extra inputs are not permitted"


def test_unrelated_onbot_environment_variables_are_not_config_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forbidding extras must not trip over the harness and runtime variables sharing the prefix."""
    monkeypatch.setenv("ONBOT_HEARTBEAT_FILE", "/tmp/hb.json")
    monkeypatch.setenv("ONBOT_ITEST_INSTANCE", "4")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__SERVER_NAME", "company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__SERVER_URL", "https://matrix.company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_USER_ID", "@bot:company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__URL", "https://authentik.company.org")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__API_KEY", "key")
    monkeypatch.setenv("ONBOT_ADMIN_ROOM__ENABLED", "false")
    assert load_config().synapse_server.server_name == "company.org"


def test_a_typo_in_a_nested_environment_variable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__SERVER_NAME", "company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__SERVER_URL", "https://matrix.company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_USER_ID", "@bot:company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_AVATAR", "https://cdn.test/face.png")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__URL", "https://authentik.company.org")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__API_KEY", "key")
    with pytest.raises(ValidationError, match="bot_avatar"):
        load_config()


# --- bot credentials -----------------------------------------------------------------------------


@pytest.mark.parametrize("with_token", [True, False])
def test_the_removed_oauth2_block_is_named_not_reported_as_an_unknown_key(with_token: bool) -> None:
    """``synapse_server.oauth2`` was removed (RUN-11): MAS does not grant the Matrix API scopes to
    client_credentials. A config that still sets it gets a specific message, without its values."""
    data = _with("synapse_server.oauth2", {**_OAUTH2, "client_secret": "OAUTH_SECRETISH"})
    if not with_token:
        del data["synapse_server"]["bot_access_token"]
    errors = _errors(data)
    assert [".".join(str(p) for p in e["loc"]) for e in errors] == ["synapse_server"]
    message = errors[0]["msg"]
    assert "synapse_server.oauth2 is no longer supported" in message
    assert "bot_access_token" in message and "scripts/provision-bot-account.sh" in message
    assert "Extra inputs" not in message
    assert "OAUTH_SECRETISH" not in message and "mas.company.org" not in message


def test_the_removed_oauth2_block_is_rejected_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__SERVER_NAME", "company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__SERVER_URL", "https://internal.matrix")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__BOT_USER_ID", "@bot:company.org")
    monkeypatch.setenv("ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_SECRET", "OAUTH_SECRETISH")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__URL", "https://authentik.company.org")
    monkeypatch.setenv("ONBOT_AUTHENTIK_SERVER__API_KEY", "key")
    with pytest.raises(ValidationError, match=r"synapse_server\.oauth2 is no longer supported"):
        load_config()


def test_missing_bot_access_token_is_rejected() -> None:
    data = copy.deepcopy(_MINIMAL)
    del data["synapse_server"]["bot_access_token"]
    assert _locs(data) == ["synapse_server.bot_access_token"]


@pytest.mark.parametrize("token", ["", "   "])
def test_blank_bot_access_token_is_rejected(token: str) -> None:
    data = _with("synapse_server.bot_access_token", token)
    assert _locs(data) == ["synapse_server.bot_access_token"]
    assert "must not be empty" in _messages(data)


# --- URLs, Matrix IDs, aliases -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "synapse_server.server_url",
        "synapse_server.bot_avatar_url",
        "authentik_server.url",
        "create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.avatar_url",
    ],
)
@pytest.mark.parametrize(
    "url", ["matrix.company.org", "ftp://matrix.company.org", "https://", "file:///etc/x"]
)
def test_urls_need_an_http_scheme_and_a_host(path: str, url: str) -> None:
    data = _with(path, url)
    assert _locs(data) == [path]
    assert "must be an http:// or https:// URL with a host" in _messages(data)


def test_mas_url_needs_an_http_scheme() -> None:
    data = _with("mas_admin", {"url": "mas.company.org", "client_id": "c", "client_secret": "s"})
    assert _locs(data) == ["mas_admin.url"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("synapse_server.bot_user_id", "bot"),
        ("synapse_server.bot_user_id", "@bot"),
        ("synapse_server.bot_user_id", "bot:company.org"),
        ("admin_room.admin_user_ids", ["@admin:company.org", "admin"]),
        ("matrix_user_ignore_list", ["root"]),
    ],
)
def test_matrix_ids_must_be_full_mxids(path: str, value: object) -> None:
    data = _with(path, value)
    assert _locs(data) == [path]
    assert "full Matrix user ID" in _messages(data)


def test_bot_must_live_on_server_name() -> None:
    data = _with("synapse_server.bot_user_id", "@bot:other.org")
    assert _locs(data) == ["synapse_server"]
    assert "bot_user_id must be an account on server_name" in _messages(data)


@pytest.mark.parametrize(
    "path",
    [
        "create_matrix_rooms_in_a_matrix_space.alias",
        "admin_room.alias",
        "matrix_room_default_settings.alias_prefix",
        "matrix_room_default_settings.visitor_lobby_alias_suffix",
    ],
)
@pytest.mark.parametrize("alias", ["#space:company.org", "#space", "space:company.org", "my space"])
def test_alias_fields_take_a_localpart(path: str, alias: str) -> None:
    data = _with(path, alias)
    assert _locs(data) == [path]
    assert "without '#', ':' or spaces" in _messages(data)


@pytest.mark.parametrize("path", ["create_matrix_rooms_in_a_matrix_space.alias", "admin_room.alias"])
def test_alias_localparts_must_not_be_empty(path: str) -> None:
    assert _locs(_with(path, "")) == [path]


# --- ranges and combinations ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("server_tick_rate_sec", 0),
        ("server_tick_rate_sec", -5),
        ("authentik_poll_rate_sec", -1),
        (f"{_LIFECYCLE}.deactivate_after_n_sec", -1),
        (f"{_LIFECYCLE}.delete_after_n_sec", -1),
        ("avatar_fetch.max_size_bytes", 0),
        ("avatar_fetch.timeout_sec", 0),
    ],
)
def test_intervals_and_limits_must_be_in_range(path: str, value: int) -> None:
    errors = _errors(_with(path, value))
    assert [".".join(str(p) for p in e["loc"]) for e in errors] == [path]
    assert errors[0]["msg"].startswith("Input should be greater than")


def test_poll_rate_zero_still_disables_the_poll() -> None:
    assert OnbotConfig.model_validate(_with("authentik_poll_rate_sec", 0)).authentik_poll_rate_sec == 0


def test_delete_disabled_rooms_without_disabling_rooms_is_rejected() -> None:
    data = _with("sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms", True)
    assert "enable that too" in _messages(data)
    data["sync_matrix_rooms_based_on_authentik_groups"][
        "disable_rooms_when_mapped_authentik_group_disappears"
    ] = True
    OnbotConfig.model_validate(data)


def test_onboarding_rooms_in_a_disabled_space_is_rejected() -> None:
    data = _with("place_onboarding_rooms_in_space", True)
    data["create_matrix_rooms_in_a_matrix_space"] = {"enabled": False}
    assert "there is no space to place the rooms in" in _messages(data)


@pytest.mark.parametrize(
    ("given", "stored"), [("WARNING", "WARNING"), ("warning", "WARNING"), ("error", "ERROR")]
)
def test_standard_log_levels_are_accepted(given: str, stored: str) -> None:
    assert OnbotConfig.model_validate(_with("log_level", given)).log_level == stored


def test_unknown_log_level_is_rejected() -> None:
    assert _locs(_with("log_level", "VERBOSE")) == ["log_level"]


# --- avatar fetch allowlist ----------------------------------------------------------------------


@pytest.mark.parametrize("entry", ["https://assets.lan/", "assets.lan:8080", "assets.lan/path", "bad host"])
def test_fetch_allowlist_entries_are_hosts_or_networks(entry: str) -> None:
    data = _with("avatar_fetch.allowed_internal_destinations", [entry])
    assert _locs(data) == ["avatar_fetch.allowed_internal_destinations"]
    assert "must be a hostname (without scheme, port or path)" in _messages(data)


def test_fetch_allowlist_accepts_hosts_addresses_and_networks() -> None:
    entries = ["assets.lan", "minio_1", "10.0.0.0/8", "192.168.1.5", "fd00::/8", "::1"]
    cfg = OnbotConfig.model_validate(_with("avatar_fetch.allowed_internal_destinations", entries))
    assert cfg.avatar_fetch.allowed_internal_destinations == entries


def test_error_messages_never_echo_the_value() -> None:
    """The CLI prints these messages verbatim (RUN-07), so they must not contain the input."""
    data = _with("synapse_server.server_url", "SECRETISH-url-value")
    data["synapse_server"]["bot_user_id"] = "SECRETISH-bot"
    data["admin_room"] = {"alias": "#SECRETISH:x", "admin_user_ids": ["SECRETISH-user"]}
    data["avatar_fetch"] = {"allowed_internal_destinations": ["SECRETISH host"]}
    data["log_level"] = "SECRETISH"
    messages = _messages(data)
    assert len(_errors(data)) == 6
    assert "SECRETISH" not in messages


# --- secrets (SEC-11) ----------------------------------------------------------------------------

_SECRETS = {
    "bot_access_token": "syt_SECRET_BOT_TOKEN",
    "api_key": "AK_SECRET_KEY",
    "mas_client_secret": "MAS_SECRET",
}


def _mas_config() -> OnbotConfig:
    data = _with("synapse_server.bot_access_token", _SECRETS["bot_access_token"])
    data["authentik_server"]["api_key"] = _SECRETS["api_key"]
    data["mas_admin"] = {
        "url": "https://mas/",
        "client_id": "c",
        "client_secret": _SECRETS["mas_client_secret"],
    }
    return OnbotConfig.model_validate(data)


def _token_config() -> OnbotConfig:
    return OnbotConfig.model_validate(_with("synapse_server.bot_access_token", _SECRETS["bot_access_token"]))


@pytest.mark.parametrize("build", [_mas_config, _token_config])
def test_secrets_never_show_in_repr_str_or_dumps(build: Any) -> None:
    cfg = build()
    for rendered in (
        repr(cfg),
        str(cfg),
        cfg.model_dump_json(),
        str(cfg.model_dump(mode="json")),
        yaml.safe_dump(cfg.model_dump(mode="json")),
        f"{cfg.synapse_server!r} {cfg.mas_admin!r} {cfg.authentik_server!r}",
    ):
        for secret in _SECRETS.values():
            assert secret not in rendered


def test_secrets_never_show_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _mas_config()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").debug("config: %s / %r", cfg, cfg)
    for secret in _SECRETS.values():
        assert secret not in caplog.text


def test_secret_values_stay_reachable_for_the_clients() -> None:
    cfg = _mas_config()
    assert cfg.authentik_server.api_key.get_secret_value() == _SECRETS["api_key"]
    assert cfg.mas_admin is not None
    assert cfg.mas_admin.client_secret.get_secret_value() == _SECRETS["mas_client_secret"]
    token = _token_config().synapse_server.bot_access_token
    assert token.get_secret_value() == _SECRETS["bot_access_token"]


def test_generated_template_validates_once_required_values_are_filled() -> None:
    """``onbot generate-config`` output stays a usable starting point under the strict model."""
    data = yaml.safe_load(generate_example_config())
    data["synapse_server"].update(
        server_name="company.org",
        server_url="https://matrix.company.org",
        bot_user_id="@bot:company.org",
        bot_access_token="tok",
    )
    data["authentik_server"].update(url="https://authentik.company.org", api_key="key")
    # The admin room is on by default, so the template also needs an admin (D19).
    data["admin_room"]["admin_user_ids"] = ["@admin:company.org"]
    OnbotConfig.model_validate(data)
