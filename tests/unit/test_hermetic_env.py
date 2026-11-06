"""The suite is hermetic: a developer's config.yml or exported ONBOT_* variables never reach a test.

Guards the autouse fixture in tests/conftest.py (review finding MNT-06).
"""

import os
from pathlib import Path

import pytest
import yaml

from onbot.config import CONFIG_FILE_ENV_VAR, OnbotConfig

_MINIMAL = {
    "synapse_server": {
        "server_name": "company.org",
        "server_url": "https://internal.matrix",
        "bot_user_id": "@bot:company.org",
        "bot_access_token": "tok",
    },
    "authentik_server": {"url": "https://authentik.company.org/", "api_key": "key"},
    "admin_room": {"enabled": False},
}


def test_config_yml_in_the_working_directory_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default config path is ./config.yml, exactly where developers keep a real one."""
    (tmp_path / "config.yml").write_text(
        yaml.safe_dump(
            {
                "log_level": "DEBUG",
                "server_tick_rate_sec": 7,
                "create_matrix_rooms_in_a_matrix_space": {"enabled": False},
                "global_room_admins": ["ops"],
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    cfg = OnbotConfig.model_validate(_MINIMAL)

    assert cfg.log_level == "INFO"
    assert cfg.server_tick_rate_sec == 300
    assert cfg.create_matrix_rooms_in_a_matrix_space.enabled is True


def test_no_ambient_onbot_settings_in_the_environment() -> None:
    leaked = [
        name
        for name in os.environ
        if name.upper().startswith("ONBOT_")
        and not name.upper().startswith("ONBOT_ITEST_")
        and name != CONFIG_FILE_ENV_VAR
    ]
    assert leaked == []
    assert not Path(os.environ[CONFIG_FILE_ENV_VAR]).exists()
