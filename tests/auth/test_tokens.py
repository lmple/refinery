"""Scenarios of specs/access-tokens/spec.md (mcp-workflow) at store level."""

import re
import stat
from pathlib import Path

import pytest

from refinery.auth.tokens import Principal, RefineryTokenVerifier, Role, TokenStore
from tests.run.helpers import run_async

TOKEN = re.compile(r"rft_([ha])_([0-9a-f]{8})_([A-Za-z0-9_-]{40,})")


def test_human_token_format(tmp_path: Path) -> None:
    issued = TokenStore(tmp_path).create(Role.HUMAN)
    match = TOKEN.fullmatch(issued.secret)
    assert match, issued.secret
    assert match.group(1) == "h"
    assert match.group(2) == issued.token.id
    assert (issued.token.role, issued.token.run_id) == (Role.HUMAN, None)
    assert not issued.token.revoked


def test_agent_token_is_bound_to_a_run(tmp_path: Path) -> None:
    issued = TokenStore(tmp_path).create(Role.AGENT, run_id="demo.1")
    match = TOKEN.fullmatch(issued.secret)
    assert match
    assert match.group(1) == "a"
    assert (issued.token.role, issued.token.run_id) == (Role.AGENT, "demo.1")


def test_agent_token_needs_a_run_and_human_token_must_not_have_one(
    tmp_path: Path,
) -> None:
    store = TokenStore(tmp_path)
    with pytest.raises(ValueError, match="run"):
        store.create(Role.AGENT)
    with pytest.raises(ValueError, match="run"):
        store.create(Role.HUMAN, run_id="demo.1")


def test_verify_known_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    issued = store.create(Role.HUMAN)
    assert store.verify(issued.secret) == issued.token
    assert TokenStore(tmp_path).verify(issued.secret) == issued.token  # persisted


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "garbage",
        "rft_a_00000000_bogus",
        "Bearer rft_h_x",
        "rft_x_00000000_" + "a" * 43,
    ],
)
def test_unknown_or_malformed_tokens_fail(tmp_path: Path, presented: str) -> None:
    TokenStore(tmp_path).create(Role.HUMAN)
    assert TokenStore(tmp_path).verify(presented) is None


def test_wrong_secret_for_a_known_id_fails(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    issued = store.create(Role.HUMAN)
    prefix, _, secret = issued.secret.rpartition("_")
    tampered = f"{prefix}_{'A' if secret[0] != 'A' else 'B'}{secret[1:]}"
    assert store.verify(tampered) is None


def test_revoked_token_fails_and_is_listed_as_revoked(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    issued = store.create(Role.HUMAN)
    other = store.create(Role.HUMAN)
    assert store.revoke(issued.token.id)
    assert store.verify(issued.secret) is None
    assert store.verify(other.secret) == other.token
    listed = {t.id: t for t in store.list()}
    assert listed[issued.token.id].revoked
    assert not listed[other.token.id].revoked
    assert not store.revoke("ffffffff")


def test_only_hashes_are_stored(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    issued = store.create(Role.AGENT, run_id="demo.1")
    secret_part = issued.secret.rpartition("_")[2]
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files
    for path in files:
        text = path.read_bytes()
        assert issued.secret.encode() not in text
        assert secret_part.encode() not in text
    token_file = store.directory / f"{issued.token.id}.json"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_list_never_shows_secrets(tmp_path: Path) -> None:
    store = TokenStore(tmp_path)
    issued = store.create(Role.HUMAN)
    assert issued.secret not in repr(store.list())


# The SDK verifier: maps a token to an AccessToken, or rejects it.


def test_verifier_maps_roles_and_runs() -> None:
    principals = {
        "h-token": Principal("h0000001", Role.HUMAN, None),
        "a-token": Principal("a0000001", Role.AGENT, "demo.1"),
    }
    verifier = RefineryTokenVerifier(principals.get)
    human = run_async(verifier.verify_token("h-token"))
    agent = run_async(verifier.verify_token("a-token"))
    assert human is not None
    assert agent is not None
    assert (human.client_id, human.scopes) == ("h0000001", ["human"])
    assert (agent.client_id, agent.scopes) == ("a0000001", ["agent"])
    assert agent.claims == {"run_id": "demo.1"}
    assert run_async(verifier.verify_token("nope")) is None
