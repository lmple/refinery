"""The runner's HTTP client of factoryd's /api/runner (runner D5)."""

from collections.abc import Sequence
from types import TracebackType
from typing import Self

type Json = dict[str, object]


class FactorydUnreachableError(Exception):
    """factoryd did not answer (down, or the connection failed)."""


class FactorydRefusedError(Exception):
    """factoryd answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        """The HTTP status and the API's error."""
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class FactorydClient:
    """One runner's calls to factoryd, authenticated with its runner token."""

    def __init__(self, url: str, token: str) -> None:
        """``url`` is factoryd's base URL (REFINE_URL)."""
        self.url = url
        self.token = token

    async def __aenter__(self) -> Self:
        """Open the HTTP client."""
        raise NotImplementedError("runner client (runner D5)")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the HTTP client."""
        raise NotImplementedError("runner client (runner D5)")

    async def hello(self) -> Json:
        """POST hello."""
        raise NotImplementedError("runner client (runner D5)")

    async def claim(self, holding: Sequence[str]) -> Json | None:
        """POST claim; None on 204."""
        raise NotImplementedError("runner client (runner D5)")

    async def poll(self, job: str) -> Json:
        """POST jobs/{job}/poll."""
        raise NotImplementedError("runner client (runner D5)")

    async def finish(
        self, job: str, outcome: str, exit_code: int | None, log: str
    ) -> Json:
        """POST jobs/{job}/finish."""
        raise NotImplementedError("runner client (runner D5)")

    async def release(self, job: str) -> Json:
        """POST jobs/{job}/release."""
        raise NotImplementedError("runner client (runner D5)")
