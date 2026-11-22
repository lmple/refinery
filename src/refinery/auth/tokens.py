"""Human and agent tokens: issue, store (hashes only), verify, revoke (D2).

A token is ``rft_<h|a>_<id>_<secret>``. The id (8 hex) names its file under
``<data>/tokens/``; the secret (64 hex) is only ever stored as its sha256.
Hex keeps "_" a pure separator, so a token always parses one way.
"""

import contextlib
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from mcp.server.auth.provider import AccessToken

_FORMAT = re.compile(r"rft_([ha])_([0-9a-f]{8})_([0-9a-f]{64})")
_LETTER = {"human": "h", "agent": "a"}


class Role(StrEnum):
    """What a token may do. Nothing ever carries the factoryd role."""

    HUMAN = "human"
    AGENT = "agent"
    RUNNER = "runner"  # the runner API only (runner D3)


@dataclass(frozen=True)
class Token:
    """A stored token, without its secret."""

    id: str
    role: Role
    run_id: str | None  # agent tokens only
    created_at: datetime
    revoked_at: datetime | None = None

    @property
    def revoked(self) -> bool:
        """Whether the token was revoked."""
        return self.revoked_at is not None


@dataclass(frozen=True)
class Issued:
    """A new token and its full string, which is shown only once."""

    token: Token
    secret: str


@dataclass(frozen=True)
class Principal:
    """Who is calling: the verified token's id, role and (agents) run."""

    token_id: str
    role: Role
    run_id: str | None


class TokenStore:
    """Tokens under ``<data>/tokens/``."""

    def __init__(self, data_dir: Path) -> None:
        """Tokens live in ``data_dir / "tokens"``."""
        self.directory = data_dir / "tokens"

    def create(self, role: Role, run_id: str | None = None) -> Issued:
        """A new token; agent tokens need ``run_id``, others must not have one."""
        if role is Role.RUNNER:
            raise NotImplementedError("runner tokens (runner D3)")
        if role is Role.AGENT and run_id is None:
            raise ValueError("an agent token must be bound to a run")
        if role is Role.HUMAN and run_id is not None:
            raise ValueError("a human token cannot be bound to a run")
        self.directory.mkdir(parents=True, exist_ok=True)
        token_id = secrets.token_hex(4)
        while self._path(token_id).exists():
            token_id = secrets.token_hex(4)
        secret = secrets.token_hex(32)
        token = Token(token_id, role, run_id, datetime.now(UTC))
        self._write(token, _digest(secret))
        return Issued(token, f"rft_{_LETTER[role.value]}_{token_id}_{secret}")

    def verify(self, presented: str) -> Token | None:
        """The token, if ``presented`` is a known, unrevoked token string."""
        match = _FORMAT.fullmatch(presented)
        if match is None:
            return None
        letter, token_id, secret = match.groups()
        loaded = self._load(token_id)
        if loaded is None:
            return None
        token, stored_digest = loaded
        if not hmac.compare_digest(_digest(secret), stored_digest):
            return None
        if token.revoked or _LETTER[token.role.value] != letter:
            return None
        return token

    def revoke(self, token_id: str) -> bool:
        """Revoke ``token_id``; False if there is no such token."""
        loaded = (
            self._load(token_id) if re.fullmatch(r"[0-9a-f]{8}", token_id) else None
        )
        if loaded is None:
            return False
        token, stored_digest = loaded
        if not token.revoked:
            self._write(
                dataclasses.replace(token, revoked_at=datetime.now(UTC)), stored_digest
            )
        return True

    def revoke_run(self, run_id: str, keep: str | None = None) -> list[str]:
        """Revoke every live agent token of ``run_id`` except ``keep``; their ids."""
        raise NotImplementedError("per-run revocation (runner D3)")

    def list(self) -> list[Token]:
        """Every token, by creation time."""
        if not self.directory.exists():
            return []
        tokens: list[Token] = []
        for path in self.directory.glob("[0-9a-f]*.json"):
            loaded = self._load(path.stem)
            if loaded is not None:
                tokens.append(loaded[0])
        return sorted(tokens, key=lambda t: (t.created_at, t.id))

    def _path(self, token_id: str) -> Path:
        return self.directory / f"{token_id}.json"

    def _write(self, token: Token, digest: str) -> None:
        """Atomically, readable by the owner only."""
        record = {
            "id": token.id,
            "role": token.role.value,
            "run_id": token.run_id,
            "sha256": digest,
            "created_at": token.created_at.isoformat(),
            "revoked_at": None
            if token.revoked_at is None
            else token.revoked_at.isoformat(),
        }
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(record, file, sort_keys=True)
                file.flush()
                os.fsync(file.fileno())
            tmp.replace(self._path(token.id))
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    def _load(self, token_id: str) -> tuple[Token, str] | None:
        try:
            raw = cast(
                dict[str, object], json.loads(self._path(token_id).read_text("utf-8"))
            )
        except (OSError, ValueError):
            return None
        revoked = raw.get("revoked_at")
        run_id = raw.get("run_id")
        token = Token(
            id=str(raw["id"]),
            role=Role(str(raw["role"])),
            run_id=None if run_id is None else str(run_id),
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            revoked_at=None
            if revoked is None
            else datetime.fromisoformat(str(revoked)),
        )
        return token, str(raw["sha256"])


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class RefineryTokenVerifier:
    """The MCP SDK's TokenVerifier, backed by an authenticate function."""

    def __init__(self, authenticate: Callable[[str], Principal | None]) -> None:
        """``authenticate`` also checks an agent token's run is still live."""
        self.authenticate = authenticate

    async def verify_token(self, token: str) -> AccessToken | None:
        """An AccessToken carrying the role (scope) and run (claim), or None."""
        principal = self.authenticate(token)
        if principal is None:
            return None
        return AccessToken(
            token=token,
            client_id=principal.token_id,
            scopes=[principal.role.value],
            claims=None if principal.run_id is None else {"run_id": principal.run_id},
        )
