"""The Claude Code adapter: argv, MCP config and environment of a headless
``claude -p`` run for one job (runner D7)."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MCP_SERVER = "refinery"
# Never passed to the agent: it holds only its own run's agent token.
SECRET_VARIABLES = ("REFINERY_RUNNER_TOKEN", "REFINE_TOKEN")


@dataclass(frozen=True)
class AgentSettings:
    """From REFINERY_AGENT_COMMAND, _PERMISSION_MODE, _MODEL, _MAX_BUDGET_USD."""

    command: str = "claude"
    permission_mode: str = "auto"
    model: str | None = None
    max_budget_usd: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "AgentSettings":
        """Settings from the environment; unset or empty variables keep defaults."""
        raise NotImplementedError("agent settings (runner D7)")


def prompt(run_id: str) -> str:
    """The fixed prompt: call get_task, follow it, stop when it is not your turn."""
    raise NotImplementedError("agent prompt (runner D7)")


def argv(
    prompt_text: str, mcp_config: Path, run_id: str, settings: AgentSettings
) -> list[str]:
    """``claude -p ... --mcp-config <file> --strict-mcp-config ...``."""
    raise NotImplementedError("agent argv (runner D7)")


def mcp_config(url: str, token: str) -> dict[str, object]:
    """The MCP config naming server ``refinery`` at ``url`` with the token."""
    raise NotImplementedError("agent MCP config (runner D7)")


def agent_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` without the runner's and the human's tokens."""
    raise NotImplementedError("agent environment (runner D6)")
