"""The ``refinery`` admin CLI (access-tokens: human tokens from the admin
command), through Typer's CliRunner."""

import re
from pathlib import Path

from typer.testing import CliRunner

from refinery.auth.tokens import Role, TokenStore
from refinery.factoryd.cli import app

runner = CliRunner()
TOKEN = re.compile(r"rft_h_[0-9a-f]{8}_[A-Za-z0-9_-]+")


def test_create_prints_a_human_token_once(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["token", "create", "--role", "human", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    tokens = TOKEN.findall(result.output)
    assert len(tokens) == 1
    verified = TokenStore(tmp_path).verify(tokens[0])
    assert verified is not None
    assert verified.role is Role.HUMAN


def test_agent_tokens_cannot_be_created_by_hand(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["token", "create", "--role", "agent", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "start_workflow" in result.output
    assert TokenStore(tmp_path).list() == []


def test_list_never_shows_secrets(tmp_path: Path) -> None:
    created = runner.invoke(
        app, ["token", "create", "--role", "human", "--data-dir", str(tmp_path)]
    )
    secret = TOKEN.findall(created.output)[0]
    token_id = secret.split("_")[2]
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert listed.exit_code == 0, listed.output
    assert token_id in listed.output
    assert "human" in listed.output
    assert secret not in listed.output
    assert secret.rpartition("_")[2] not in listed.output


def test_revoke(tmp_path: Path) -> None:
    created = runner.invoke(
        app, ["token", "create", "--role", "human", "--data-dir", str(tmp_path)]
    )
    secret = TOKEN.findall(created.output)[0]
    token_id = secret.split("_")[2]
    revoked = runner.invoke(
        app, ["token", "revoke", token_id, "--data-dir", str(tmp_path)]
    )
    assert revoked.exit_code == 0, revoked.output
    assert TokenStore(tmp_path).verify(secret) is None
    listed = runner.invoke(app, ["token", "list", "--data-dir", str(tmp_path)])
    assert "revoked" in listed.output


def test_revoke_unknown_token_fails(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["token", "revoke", "ffffffff", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "ffffffff" in result.output


def test_data_dir_from_the_environment(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["token", "create", "--role", "human"],
        env={"REFINERY_DATA_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    assert len(TokenStore(tmp_path).list()) == 1
