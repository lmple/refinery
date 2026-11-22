"""The Claude Code adapter (runner spec, "The Claude Code adapter"; D7)."""

from pathlib import Path

from refinery.adapters.claude_code import (
    AgentSettings,
    agent_env,
    argv,
    mcp_config,
    prompt,
)

MCP = Path("/runner/jobs/demo.1.j1/mcp.json")


def test_defaults() -> None:
    text = prompt("demo.1")
    built = argv(text, MCP, "demo.1", AgentSettings())
    assert built[:3] == ["claude", "-p", text]
    assert built[built.index("--mcp-config") + 1] == str(MCP)
    assert "--strict-mcp-config" in built
    assert built[built.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in built
    assert built[built.index("--permission-mode") + 1] == "auto"
    assert built[built.index("-n") + 1] == "demo.1"
    assert "--model" not in built
    assert "--max-budget-usd" not in built


def test_optional_flags() -> None:
    settings = AgentSettings(
        command="/opt/claude",
        permission_mode="bypassPermissions",
        model="opus",
        max_budget_usd="5",
    )
    built = argv("p", MCP, "demo.1", settings)
    assert built[0] == "/opt/claude"
    assert built[built.index("--permission-mode") + 1] == "bypassPermissions"
    assert built[built.index("--model") + 1] == "opus"
    assert built[built.index("--max-budget-usd") + 1] == "5"


def test_settings_from_the_environment() -> None:
    assert AgentSettings.from_env({}) == AgentSettings()
    assert AgentSettings.from_env({"REFINERY_AGENT_MODEL": ""}) == AgentSettings()
    settings = AgentSettings.from_env(
        {
            "REFINERY_AGENT_COMMAND": "/opt/claude",
            "REFINERY_AGENT_PERMISSION_MODE": "acceptEdits",
            "REFINERY_AGENT_MODEL": "sonnet",
            "REFINERY_AGENT_MAX_BUDGET_USD": "2.5",
        }
    )
    assert settings == AgentSettings("/opt/claude", "acceptEdits", "sonnet", "2.5")


def test_prompt() -> None:
    text = prompt("demo.1")
    assert "demo.1" in text
    assert "get_task" in text
    assert "refinery" in text
    assert "stop" in text.lower()


def test_mcp_config() -> None:
    config = mcp_config("http://127.0.0.1:8740/mcp", "rft_a_1234abcd_" + "0" * 64)
    assert config == {
        "mcpServers": {
            "refinery": {
                "type": "http",
                "url": "http://127.0.0.1:8740/mcp",
                "headers": {"Authorization": "Bearer rft_a_1234abcd_" + "0" * 64},
            }
        }
    }


def test_agent_env_strips_the_runner_and_human_tokens() -> None:
    env = agent_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/me",
            "REFINERY_RUNNER_TOKEN": "rft_r_x",
            "REFINE_TOKEN": "rft_h_x",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/me"}
