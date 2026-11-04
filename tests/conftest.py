"""Suite-wide fixtures.

Every ``OnbotConfig(...)`` in a test still consults the settings sources: ``ONBOT_*`` environment
variables and the YAML file at ``ONBOT_CONFIG_FILE_PATH`` (default ``./config.yml``, which is where a
developer keeps a real config and where pytest runs). Without isolation a local config or exported
variable changes test results and puts real credentials into test objects. The autouse fixture below
takes both sources away for every test; a test that wants them sets them explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from onbot.config import CONFIG_FILE_ENV_VAR

# The integration harness reads its own knobs from ONBOT_ITEST_* (see tests/README.md). They are not
# onbot settings, so they survive the cleanup.
_KEEP_PREFIX = "ONBOT_ITEST_"


@pytest.fixture(autouse=True)
def _hermetic_onbot_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hide ambient ``ONBOT_*`` variables and any ``config.yml`` from the config model."""
    for name in list(os.environ):
        upper = name.upper()  # pydantic-settings matches env names case-insensitively
        if upper.startswith("ONBOT_") and not upper.startswith(_KEEP_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(tmp_path / "no-such-config.yml"))
