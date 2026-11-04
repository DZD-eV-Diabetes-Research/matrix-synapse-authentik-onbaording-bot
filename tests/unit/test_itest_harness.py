"""The integration harness's instance selection and start policy (review findings LIV-01, S0).

These run in the fast tier: they need neither Docker nor the stack.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.integration.stack_instance import (
    StackConfigError,
    StackInstance,
    StackUnavailable,
    skip_requested,
    start_stack,
)

COMPOSE_FILE = Path(__file__).parents[1] / "integration" / "stack" / "docker-compose.yml"


def test_instance_0_keeps_the_historic_project_and_ports() -> None:
    stack = StackInstance.from_env({})
    assert stack.project == "onbot-itest"
    assert (stack.synapse_port, stack.mas_port, stack.authentik_port) == (8008, 8080, 9000)
    assert stack.synapse_url == "http://localhost:8008"
    assert stack.mas_url == "http://localhost:8080"
    assert stack.authentik_url == "http://localhost:9000"


def test_one_variable_derives_project_and_every_port() -> None:
    stack = StackInstance.from_env({"ONBOT_ITEST_INSTANCE": "2"})
    assert stack.project == "onbot-itest-2"
    assert (stack.synapse_port, stack.mas_port, stack.authentik_port) == (8028, 8100, 9020)


def test_instances_never_share_a_port_or_project() -> None:
    stacks = [StackInstance.from_env({"ONBOT_ITEST_INSTANCE": str(n)}) for n in range(10)]
    ports = [p for s in stacks for p in (s.synapse_port, s.mas_port, s.authentik_port)]
    assert len(set(ports)) == len(ports)
    assert len({s.project for s in stacks}) == len(stacks)


def test_single_values_can_be_overridden() -> None:
    stack = StackInstance.from_env(
        {
            "ONBOT_ITEST_INSTANCE": "1",
            "ONBOT_ITEST_PROJECT": "mine",
            "ONBOT_ITEST_MAS_PORT": "18080",
        }
    )
    assert stack.project == "mine"
    assert (stack.synapse_port, stack.mas_port, stack.authentik_port) == (8018, 18080, 9010)


@pytest.mark.parametrize(
    "env",
    [
        {"ONBOT_ITEST_INSTANCE": "one"},
        {"ONBOT_ITEST_INSTANCE": "-1"},
        {"ONBOT_ITEST_SYNAPSE_PORT": "70000"},
        {"ONBOT_ITEST_SYNAPSE_PORT": "9000"},  # collides with Authentik
    ],
)
def test_unusable_values_are_rejected(env: dict[str, str]) -> None:
    with pytest.raises(StackConfigError):
        StackInstance.from_env(env)


def test_the_compose_file_reads_exactly_the_variables_the_harness_writes() -> None:
    """Harness and compose file must agree, or the tests talk to a port nothing listens on."""
    used = set(re.findall(r"\$\{(ONBOT_ITEST_[A-Z_]+)", COMPOSE_FILE.read_text()))
    assert used == set(StackInstance.from_env({}).compose_env())


def test_env_file_round_trips(tmp_path: Path) -> None:
    stack = StackInstance.from_env({"ONBOT_ITEST_INSTANCE": "3"})
    lines = stack.write_env_file(tmp_path / "stack.env").read_text().splitlines()
    assert dict(line.split("=", 1) for line in lines) == stack.compose_env()


def test_skip_needs_an_explicit_opt_out() -> None:
    assert skip_requested({"ONBOT_ITEST_SKIP": "1"})
    assert not skip_requested({})
    assert not skip_requested({"ONBOT_ITEST_SKIP": "0"})


class _FakeCompose:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")
        if self.failures:
            self.failures -= 1
            raise subprocess.CalledProcessError(1, ["docker", "compose", "up"], stderr=b"network not found")

    def stop(self, down: bool = True) -> None:
        self.calls.append(f"stop(down={down})")


_STACK = StackInstance.from_env({"ONBOT_ITEST_INSTANCE": "1"})


def test_start_succeeds_without_teardown() -> None:
    compose = _FakeCompose(failures=0)
    start_stack(compose, _STACK, docker_available=lambda: True)
    assert compose.calls == ["start"]


def test_stale_state_is_torn_down_and_the_start_retried() -> None:
    compose = _FakeCompose(failures=1)
    start_stack(compose, _STACK, docker_available=lambda: True)
    assert compose.calls == ["start", "stop(down=True)", "start"]


def test_a_stack_that_cannot_start_fails_with_both_errors() -> None:
    """LIV-01: this used to become pytest.skip, so the integration run exited 0."""
    compose = _FakeCompose(failures=2)
    with pytest.raises(StackUnavailable) as err:
        start_stack(compose, _STACK, docker_available=lambda: True)
    message = str(err.value)
    assert "onbot-itest-1" in message
    assert message.count("network not found") == 2


def test_missing_docker_fails_and_names_the_opt_out() -> None:
    compose = _FakeCompose(failures=0)
    with pytest.raises(StackUnavailable, match="ONBOT_ITEST_SKIP=1"):
        start_stack(compose, _STACK, docker_available=lambda: False)
    assert compose.calls == []
