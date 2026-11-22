"""Scenarios of specs/factoryd-server/spec.md (mcp-workflow)."""

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from refinery.factoryd.config import DEFAULT_HOST, DEFAULT_PORT, ServerConfig
from refinery.factoryd.workflow import Workflow
from refinery.gate.runner import Step
from refinery.proc import registry
from refinery.run.model import Action, Phase
from refinery.run.store import RunStore
from tests.factoryd.conftest import Factory
from tests.gate.conftest import RUN, drive_to_verify
from tests.proc.test_supervisor import group_alive, wait_for

A = Action
P = Phase


# Requirement: Serving


def test_default_address(tmp_path: Path) -> None:
    config = ServerConfig.from_env(
        {"REFINERY_DATA_DIR": str(tmp_path), "REFINERY_ROOTS": "/r"}
    )
    assert (
        (config.host, config.port)
        == (DEFAULT_HOST, DEFAULT_PORT)
        == ("127.0.0.1", 8740)
    )


def test_configuration_from_the_environment(tmp_path: Path) -> None:
    config = ServerConfig.from_env(
        {
            "REFINERY_DATA_DIR": str(tmp_path / "data"),
            "REFINERY_ROOTS": f"{tmp_path / 'a'}:{tmp_path / 'b'}",
            "REFINERY_HOST": "127.0.0.2",
            "REFINERY_PORT": "9999",
            "REFINERY_TLA_JAR": "/opt/tla2tools.jar",
            "REFINERY_RUN_SPEC": "/src/spec/Run.tla",
        }
    )
    assert config.data_dir == tmp_path / "data"
    assert config.roots == (tmp_path / "a", tmp_path / "b")
    assert (config.host, config.port) == ("127.0.0.2", 9999)
    assert config.tla_jar == Path("/opt/tla2tools.jar")
    assert config.run_spec == Path("/src/spec/Run.tla")


def test_default_data_directory(tmp_path: Path) -> None:
    xdg = ServerConfig.from_env(
        {"XDG_DATA_HOME": str(tmp_path), "REFINERY_ROOTS": "/r"}
    )
    assert xdg.data_dir == tmp_path / "refinery"
    home = ServerConfig.from_env({"HOME": str(tmp_path), "REFINERY_ROOTS": "/r"})
    assert home.data_dir == tmp_path / ".local" / "share" / "refinery"


# Requirement: Startup order


def test_restart_with_an_orphan_and_a_run_in_verify(fx: Factory) -> None:
    # An orphaned group whose leader is gone (re-parented, not our child).
    orphan_pid = fx.behaviours / "orphan.pid"
    leader = subprocess.Popen(
        ["sh", "-c", f"sleep 60 & echo $! > {orphan_pid}"], start_new_session=True
    )
    ticks = registry.process_start_ticks(leader.pid)
    leader.wait(timeout=10)
    assert ticks is not None
    orphan = int(orphan_pid.read_text())
    registry.write_entry(
        fx.data,
        registry.Entry(leader.pid, ticks, ("sleep", "60"), datetime.now(UTC)),
    )
    # What the gate's test step saw: was the orphan still alive when it ran?
    seen = fx.behaviours / "seen"
    check = f"if kill -0 {orphan} 2>/dev/null; then echo alive; else echo dead; fi"
    fx.behave(Step.TEST, f"({check}) > {seen}")

    async def leave_in_verify() -> None:
        store = await RunStore.open(fx.data, [fx.root])
        await drive_to_verify(store, fx.root, fx.head)
        store.close()

    asyncio.run(leave_in_verify())

    async def scenario(wf: Workflow) -> None:
        await wf.wait_for_gates()
        assert wf.store.events(RUN)[-1].name is A.PASS_GATE

    fx.run(scenario)
    assert seen.read_text().strip() == "dead"
    assert registry.list_entries(fx.data) == []


# Requirement: Shutdown


def test_shutdown_mid_gate(fx: Factory) -> None:
    pidfile = fx.behaviours / "test.pid"
    fx.behave(Step.TEST, f"echo $$ > {pidfile}; exec sleep 30")

    async def leave_in_verify() -> None:
        store = await RunStore.open(fx.data, [fx.root])
        await drive_to_verify(store, fx.root, fx.head)
        store.close()

    asyncio.run(leave_in_verify())

    async def go() -> int:
        wf = await Workflow.open(fx.config)
        pgid = int(await wait_for(pidfile))
        await wf.close()
        return pgid

    assert not group_alive(asyncio.run(go()))

    async def check() -> None:
        store = await RunStore.open(fx.data, [fx.root])
        assert store.get(RUN).phase is P.VERIFY
        assert store.events(RUN)[-1].name is A.COMPLETE_IMPL
        store.close()

    asyncio.run(check())
    assert registry.list_entries(fx.data) == []
