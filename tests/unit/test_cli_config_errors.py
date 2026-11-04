"""A config that does not validate is reported per field, without values, with exit code 2 (RUN-07).

pydantic's own rendering quotes the input, and for a block that misses a key the input is the whole
block: in the review, a ``mas_admin`` block without ``client_id`` printed the complete MAS admin client
secret to stderr, which container platforms ship to the log aggregator.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from onbot import cli
from onbot.config import CONFIG_FILE_ENV_VAR, load_config

REPO_ROOT = Path(__file__).resolve().parents[2]

# Short values on purpose: pydantic truncates long reprs in the middle, so short secrets are the ones
# that used to appear in full.
SECRETS = ("MAS_X9", "BOT_TOK_7", "AK_42")

BAD_CONFIG = """\
synapse_server:
  server_name: company.org
  server_url: https://matrix.company.org
  bot_access_token: BOT_TOK_7
authentik_server:
  url: https://authentik.company.org
  api_key: AK_42
mas_admin:
  url: https://mas.company.org
  client_secret: MAS_X9
sync_authentik_users_with_matrix_rooms:
  deactivate_disabled_authentik_users_in_matrix:
    dry_rn: false
"""

EXPECTED_LINES = [
    "  synapse_server.bot_user_id: Field required",
    "  mas_admin.client_id: Field required",
    "  sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.dry_rn: "
    "Extra inputs are not permitted",
]


def _run_onbot(
    config_file: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ONBOT_")}
    env[CONFIG_FILE_ENV_VAR] = str(config_file)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "onbot", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_pydantic_itself_would_print_the_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the test below: the raw error really does carry the secret, so hiding it is the CLI's job."""
    path = tmp_path / "config.yml"
    path.write_text(BAD_CONFIG)
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(path))
    with pytest.raises(ValidationError) as excinfo:
        load_config()
    assert "MAS_X9" in str(excinfo.value)


@pytest.mark.parametrize("command", [["reconcile-once"], ["run"], ["healthcheck", "--deep"]])
def test_invalid_config_prints_paths_and_no_values(tmp_path: Path, command: list[str]) -> None:
    path = tmp_path / "config.yml"
    path.write_text(BAD_CONFIG)

    result = _run_onbot(path, *command)

    assert result.returncode == cli.EXIT_CONFIG_INVALID == 2
    assert "Traceback" not in result.stderr
    for secret in SECRETS:
        assert secret not in result.stdout
        assert secret not in result.stderr
    lines = result.stderr.splitlines()
    assert "onbot: invalid configuration (3 errors):" in lines
    for expected in EXPECTED_LINES:
        assert expected in lines


def test_invalid_yaml_does_not_quote_the_offending_line(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text('synapse_server:\n  server_name: x\n  bot_access_token: "MAS_X9: [\n')

    result = _run_onbot(path, "reconcile-once")

    assert result.returncode == 2
    assert "MAS_X9" not in result.stderr + result.stdout
    assert "the config file is not valid YAML at line" in result.stderr


def test_unparsable_environment_variable_names_the_field_only(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(BAD_CONFIG)

    result = _run_onbot(path, "reconcile-once", extra_env={"ONBOT_SYNAPSE_SERVER": "{not json MAS_X9"})

    assert result.returncode == 2
    assert "MAS_X9" not in result.stderr + result.stdout
    assert 'field "synapse_server"' in result.stderr


def test_error_lines_strip_pydantic_prefixes_and_inputs() -> None:
    from onbot.config import OnbotConfig

    with pytest.raises(ValidationError) as excinfo:
        OnbotConfig.model_validate(
            {
                "synapse_server": {
                    "server_name": "company.org",
                    "server_url": "matrix.company.org",
                    "bot_user_id": "@bot:company.org",
                    "bot_access_token": "BOT_TOK_7",
                },
                "authentik_server": {"url": "https://a/", "api_key": "AK_42"},
                "visitor_lobby_enabled": True,
            }
        )
    lines = cli.config_error_lines(excinfo.value)
    assert lines == [
        "synapse_server.server_url: must be an http:// or https:// URL with a host, such as "
        "https://matrix.company.org",
        "visitor_lobby_enabled: Extra inputs are not permitted",
    ]


def test_model_level_errors_get_a_readable_path() -> None:
    from onbot.config import OnbotConfig

    with pytest.raises(ValidationError) as excinfo:
        OnbotConfig.model_validate(
            {
                "synapse_server": {
                    "server_name": "company.org",
                    "server_url": "https://matrix.company.org",
                    "bot_user_id": "@bot:company.org",
                    "bot_access_token": "BOT_TOK_7",
                },
                "authentik_server": {"url": "https://a/", "api_key": "AK_42"},
                "place_onboarding_rooms_in_space": True,
                "create_matrix_rooms_in_a_matrix_space": {"enabled": False},
            }
        )
    [line] = cli.config_error_lines(excinfo.value)
    assert line.startswith("config: place_onboarding_rooms_in_space requires")


@pytest.mark.parametrize("level", ["warning", "ERROR", "Critical"])
def test_cli_accepts_the_standard_log_levels(level: str) -> None:
    assert cli.build_parser().parse_args(["reconcile-once", "--log-level", level]).log_level == level.upper()


def test_cli_rejects_an_unknown_log_level() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--log-level", "VERBOSE", "reconcile-once"])


def test_unknown_log_level_in_the_environment_is_a_config_error(tmp_path: Path) -> None:
    """ONBOT_LOG_LEVEL used to crash logging setup with a traceback before the config was read."""
    path = tmp_path / "config.yml"
    path.write_text(BAD_CONFIG)

    result = _run_onbot(path, "reconcile-once", extra_env={"ONBOT_LOG_LEVEL": "VERBOSE"})

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "  log_level: Input should be 'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'" in result.stderr
