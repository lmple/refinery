"""The runner's configuration, from the environment only (runner D6).

Tokens come from the environment, never from flags or files (AGENTS.md).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from refinery.adapters.claude_code import AgentSettings

DEFAULT_URL = "http://127.0.0.1:8740"
DEFAULT_MAX_JOBS = 1
DEFAULT_POLL_S = 10.0
DEFAULT_CLAIM_INTERVAL_S = 5.0
DEFAULT_AGENT_TIMEOUT_S = 4 * 3600.0


class RunnerConfigError(Exception):
    """The environment does not configure a runner (e.g. no token)."""


@dataclass(frozen=True)
class RunnerConfig:
    """Everything one runner process needs."""

    url: str
    token: str
    directory: Path  # registry and jobs/<job>/{agent.log,mcp.json}
    max_jobs: int = DEFAULT_MAX_JOBS
    poll_s: float = DEFAULT_POLL_S
    claim_interval_s: float = DEFAULT_CLAIM_INTERVAL_S
    agent_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S
    agent: AgentSettings = field(default_factory=AgentSettings)
    env: Mapping[str, str] = field(default_factory=dict[str, str])  # the agents' base

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "RunnerConfig":
        """From REFINE_URL, REFINERY_RUNNER_TOKEN (required), REFINERY_RUNNER_DIR,
        REFINERY_RUNNER_MAX_JOBS, REFINERY_RUNNER_POLL,
        REFINERY_RUNNER_CLAIM_INTERVAL and REFINERY_AGENT_TIMEOUT (seconds)."""
        raise NotImplementedError("runner configuration (runner D6)")


def default_directory(environ: Mapping[str, str]) -> Path:
    """``$XDG_DATA_HOME/refinery-runner``, else ``~/.local/share/refinery-runner``."""
    raise NotImplementedError("runner directory (runner D6)")
