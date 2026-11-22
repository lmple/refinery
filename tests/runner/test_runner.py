"""Scenarios of specs/runner/spec.md (runner): a real factoryd served in the
test's event loop, a fake ``claude``, and the runner in-process, or as a
``refinery runner`` subprocess where a signal or a crash is the point."""

import asyncio
import signal
from pathlib import Path

from typer.testing import CliRunner

from refinery.adapters.claude_code import AgentSettings
from refinery.auth.tokens import Role, TokenStore
from refinery.factoryd.cli import app as refinery_cli
from refinery.proc.supervisor import Supervisor
from refinery.run.model import Action, Phase
from refinery.runner.config import (
    DEFAULT_AGENT_TIMEOUT_S,
    DEFAULT_CLAIM_INTERVAL_S,
    DEFAULT_MAX_JOBS,
    DEFAULT_POLL_S,
    DEFAULT_URL,
    RunnerConfig,
)
from tests.factoryd.conftest import Factory, start
from tests.run.helpers import accepted, request
from tests.runner.conftest import (
    HUMAN_TOKEN,
    POLL_S,
    FakeClaude,
    alive,
    background,
    events,
    factoryd,
    free_port,
    runner_config,
    runner_dir,
    runner_subprocess_argv,
    runner_subprocess_env,
    running,
    stopped,
    until,
)

A = Action
P = Phase
GRACE_S = 2.0  # the supervisor's default wait between SIGTERM and SIGKILL


def runner_token(fx: Factory) -> str:
    return TokenStore(fx.data).create(Role.RUNNER).secret


# Requirement: Runner configuration


def test_defaults() -> None:
    config = RunnerConfig.from_env(
        {"REFINERY_RUNNER_TOKEN": "rft_r_x", "XDG_DATA_HOME": "/xdg"}
    )
    assert (config.url, config.token) == (DEFAULT_URL, "rft_r_x")
    assert DEFAULT_URL == "http://127.0.0.1:8740"
    assert (config.max_jobs, config.poll_s) == (DEFAULT_MAX_JOBS, DEFAULT_POLL_S)
    assert (DEFAULT_MAX_JOBS, DEFAULT_POLL_S, DEFAULT_CLAIM_INTERVAL_S) == (1, 10, 5)
    assert config.claim_interval_s == DEFAULT_CLAIM_INTERVAL_S
    assert config.agent_timeout_s == DEFAULT_AGENT_TIMEOUT_S == 4 * 3600
    assert config.directory == Path("/xdg/refinery-runner")
    assert config.agent == AgentSettings()
    assert config.env["XDG_DATA_HOME"] == "/xdg"


def test_settings_from_the_environment() -> None:
    config = RunnerConfig.from_env(
        {
            "REFINERY_RUNNER_TOKEN": "rft_r_x",
            "REFINE_URL": "http://factory:9000",
            "REFINERY_RUNNER_DIR": "/runner",
            "REFINERY_RUNNER_MAX_JOBS": "3",
            "REFINERY_RUNNER_POLL": "0.5",
            "REFINERY_RUNNER_CLAIM_INTERVAL": "2",
            "REFINERY_AGENT_TIMEOUT": "60",
            "REFINERY_AGENT_PERMISSION_MODE": "bypassPermissions",
        }
    )
    assert config.url == "http://factory:9000"
    assert config.directory == Path("/runner")
    assert (config.max_jobs, config.poll_s, config.claim_interval_s) == (3, 0.5, 2.0)
    assert config.agent_timeout_s == 60.0
    assert config.agent.permission_mode == "bypassPermissions"


# Requirement: The runner needs its token


def test_no_token() -> None:
    result = CliRunner().invoke(
        refinery_cli, ["runner"], env={"REFINERY_RUNNER_TOKEN": None}
    )
    assert result.exit_code != 0
    assert "REFINERY_RUNNER_TOKEN" in result.output


def test_empty_token() -> None:
    result = CliRunner().invoke(
        refinery_cli, ["runner"], env={"REFINERY_RUNNER_TOKEN": ""}
    )
    assert result.exit_code != 0
    assert "REFINERY_RUNNER_TOKEN" in result.output


# Requirements: Launching the agent; The agent never sees other tokens


def test_agent_launched(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("exit 0")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            config = runner_config(fx, fake, served.url, token)
            async with running(config) as runner:
                await until(
                    lambda: bool(events(fx, run_id, "Finish")),
                    task=runner.task,
                    what="the first job's Finish",
                )
            assert events(fx, run_id, "Claim")[0]["job"] == "demo.1.j1"
        argv = fake.argv(run_id)
        assert argv[0] == "-p"
        assert "get_task" in argv[1]
        mcp_file = Path(argv[argv.index("--mcp-config") + 1])
        assert mcp_file == runner_dir(fx) / "jobs" / "demo.1.j1" / "mcp.json"
        assert "--strict-mcp-config" in argv
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        assert argv[argv.index("-n") + 1] == run_id
        assert fake.recorded(run_id, "mcp_mode") == "600\n"
        server = fake.mcp_server(run_id)
        assert (server["type"], server["url"]) == ("http", served.url + "/mcp")
        assert fake.agent_token(run_id).startswith("rft_a_")
        assert fake.recorded(run_id, "cwd") == f"{fx.root}\n"
        assert (runner_dir(fx) / "jobs" / "demo.1.j1" / "agent.log").is_file()

    asyncio.run(scenario())


def test_tokens_stripped(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("exit 0")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: bool(fake.argv(run_id)), task=runner.task, what="a launch"
                )
        env = fake.env(run_id)
        assert "FAKE_DIR" in env
        assert "REFINERY_RUNNER_TOKEN" not in env
        assert "REFINE_TOKEN" not in env
        assert token not in "\n".join(env.values())
        assert HUMAN_TOKEN not in "\n".join(env.values())

    asyncio.run(scenario())


# Requirement: Concurrent jobs are bounded


def test_one_job_at_a_time_by_default(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("wait")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            a, _ = await start(served.workflow, fx, "a")
            b, _ = await start(served.workflow, fx, "b")
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: bool(fake.pids(a) or fake.pids(b)),
                    task=runner.task,
                    what="one launch",
                )
                await asyncio.sleep(10 * POLL_S)
                assert len(fake.pids(a)) + len(fake.pids(b)) == 1
                states = {served.workflow.jobs.view(r)["state"] for r in (a, b)}
                assert states == {"running", "waiting"}
                fake.release()
                await until(
                    lambda: bool(fake.pids(a) and fake.pids(b)),
                    task=runner.task,
                    what="the second run's launch",
                )

    asyncio.run(scenario())


# Requirement: Reporting the end of a job


def test_agent_exits_without_submitting(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("exit 1")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: bool(events(fx, run_id, "Finish")),
                    task=runner.task,
                    what="a Finish",
                )
                first = events(fx, run_id, "Finish")[0]
                assert (first["job"], first["outcome"], first["exit_code"]) == (
                    "demo.1.j1",
                    "exited",
                    1,
                )
                assert first["log"] == str(
                    runner_dir(fx) / "jobs" / "demo.1.j1" / "agent.log"
                )
                assert not (runner_dir(fx) / "jobs" / "demo.1.j1" / "mcp.json").exists()
                agent_token = fake.agent_token(run_id)
                assert served.workflow.authenticate(agent_token) is None

    asyncio.run(scenario())


def test_agent_exceeds_the_time_limit(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            config = runner_config(fx, fake, served.url, token, agent_timeout_s=1.0)
            async with running(config) as runner:
                await until(
                    lambda: bool(events(fx, run_id, "Finish")),
                    timeout_s=15,
                    task=runner.task,
                    what="a Finish after the time limit",
                )
                assert events(fx, run_id, "Finish")[0]["outcome"] == "timed_out"
                assert not alive(fake.pids(run_id)[0])

    asyncio.run(scenario())


def test_finish_is_retried_until_factoryd_answers(
    fx: Factory, fake: FakeClaude
) -> None:
    token = runner_token(fx)
    port = free_port()
    fake.behave("wait")

    async def scenario() -> None:
        async with factoryd(fx, port) as served:
            run_id, _ = await start(served.workflow, fx)
            runner = background(runner_config(fx, fake, served.url, token))
            await until(
                lambda: bool(fake.pids(run_id)), task=runner.task, what="launch"
            )
        fake.release()  # the agent exits while factoryd is down
        await asyncio.sleep(3 * POLL_S)
        assert events(fx, run_id, "Finish") == []
        async with factoryd(fx, port):
            await until(
                lambda: bool(events(fx, run_id, "Finish")),
                task=runner.task,
                what="the Finish after factoryd came back",
            )
            await stopped(runner)

    asyncio.run(scenario())


# Requirement: Stop when factoryd says so


def test_cancel_kills_the_agent(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: bool(fake.pids(run_id)), task=runner.task, what="a launch"
                )
                pid = fake.pids(run_id)[0]
                accepted(
                    await served.workflow.store.transition(run_id, request(A.CANCEL))
                )
                await until(
                    lambda: not alive(pid),
                    timeout_s=2 * POLL_S + GRACE_S + 3,
                    task=runner.task,
                    what="the agent killed after the cancel",
                )
                await until(
                    lambda: bool(events(fx, run_id, "Finish")),
                    task=runner.task,
                    what="the cancelled job's Finish",
                )

    asyncio.run(scenario())


def test_factoryd_down_does_not_stop_the_agent(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    port = free_port()
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, port) as served:
            run_id, _ = await start(served.workflow, fx)
            runner = background(runner_config(fx, fake, served.url, token))
            await until(
                lambda: bool(fake.pids(run_id)), task=runner.task, what="launch"
            )
        pid = fake.pids(run_id)[0]
        await asyncio.sleep(3 * POLL_S)  # factoryd is down
        assert alive(pid)
        async with factoryd(fx, port) as served:
            await until(
                lambda: (
                    served.workflow.jobs.view(run_id)["runner_last_seen"] is not None
                ),
                task=runner.task,
                what="a poll after factoryd came back",
            )
            assert alive(pid)
            assert events(fx, run_id, "Finish") == []
            await stopped(runner)
        assert not alive(pid)

    asyncio.run(scenario())


# Requirement: Reap, then hello, then claim


def test_factoryd_restart_does_not_kill_agents(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: bool(fake.pids(run_id)), task=runner.task, what="a launch"
                )
                pid = fake.pids(run_id)[0]
                Supervisor(fx.data).reap()  # factoryd's startup reap
                assert alive(pid)

    asyncio.run(scenario())


def test_restart_after_a_crash_reaps_the_agent(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            env = runner_subprocess_env(fx, fake, served.url, token)
            crashed = await asyncio.create_subprocess_exec(
                *runner_subprocess_argv(), env=env
            )
            try:
                await until(lambda: bool(fake.pids(run_id)), what="a launch")
            finally:
                crashed.kill()  # SIGKILL: no shutdown, the agent is orphaned
                await crashed.wait()
            orphan = fake.pids(run_id)[0]
            assert alive(orphan)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: len(fake.pids(run_id)) == 2,
                    task=runner.task,
                    what="the run claimed again",
                )
                assert not alive(orphan)
                assert fake.recorded(run_id, "alive_at_launch") is None
                released = events(fx, run_id, "Release")
                assert [(e["job"], e["why"]) for e in released] == [
                    ("demo.1.j1", "runner_restart")
                ]

    asyncio.run(scenario())


# Requirement: Runner shutdown


def test_shutdown_with_a_running_agent(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            env = runner_subprocess_env(fx, fake, served.url, token)
            proc = await asyncio.create_subprocess_exec(
                *runner_subprocess_argv(), env=env
            )
            try:
                await until(lambda: bool(fake.pids(run_id)), what="a launch")
                pid = fake.pids(run_id)[0]
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=GRACE_S + 10)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            assert not alive(pid)
            released = events(fx, run_id, "Release")
            assert [(e["job"], e["why"]) for e in released] == [
                ("demo.1.j1", "runner_shutdown")
            ]
            assert not served.workflow.jobs.stuck(run_id)
            assert not (runner_dir(fx) / "jobs" / "demo.1.j1" / "mcp.json").exists()

    asyncio.run(scenario())


def test_in_process_stop_releases_jobs(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("hang")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: bool(fake.pids(run_id)), task=runner.task, what="a launch"
                )
                pid = fake.pids(run_id)[0]
            assert not alive(pid)
            assert [e["why"] for e in events(fx, run_id, "Release")] == [
                "runner_shutdown"
            ]
            assert served.workflow.jobs.view(run_id)["state"] == "waiting"

    asyncio.run(scenario())


def test_no_process_left_in_the_runner_registry(fx: Factory, fake: FakeClaude) -> None:
    token = runner_token(fx)
    fake.behave("exit 0")

    async def scenario() -> None:
        async with factoryd(fx, free_port()) as served:
            run_id, _ = await start(served.workflow, fx)
            async with running(runner_config(fx, fake, served.url, token)) as runner:
                await until(
                    lambda: len(events(fx, run_id, "Finish")) == 2,
                    task=runner.task,
                    what="two attempts",
                )
            assert served.workflow.jobs.stuck(run_id)
        registry = runner_dir(fx) / "procs"  # the supervisor's registry
        leftovers = list(registry.glob("*")) if registry.exists() else []
        assert leftovers == []
        assert any(fake.directory.iterdir())  # the fake did run

    asyncio.run(scenario())
