"""The production trace validator: TLC on the exported trace (D7).

Unlike ReplayValidator it does not reuse the code's own transition table: the
log is replayed against the actions of ``spec/Run.tla`` itself.
"""

import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from refinery.proc.supervisor import CallResult, Capture, Outcome, Supervisor
from refinery.run.archive import ValidationResult
from refinery.run.model import Event
from refinery.run.trace_export import export_trace

# TLC exit codes (tlc2.output.EC): 12 is an invariant violation, 11 a deadlock.
_VIOLATION, _DEADLOCK = 12, 11
_ACCEPTED = "Invariant TraceIncomplete is violated"


class TlcTraceValidator:
    """Accepts a log iff TLC reports TraceIncomplete violated."""

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        java: str,
        jar: Path,
        run_spec: Path,
        work_dir: Path,
        timeout_s: float,
    ) -> None:
        """``run_spec`` is the Run.tla factoryd implements."""
        self.supervisor = supervisor
        self.java = java
        self.jar = jar
        self.run_spec = run_spec
        self.work_dir = work_dir
        self.timeout_s = timeout_s

    async def validate(self, run_id: str, events: Sequence[Event]) -> ValidationResult:
        """Export, run TLC through the supervisor, map its result."""
        if not events:
            return ValidationResult(False, "empty log")
        module = export_trace(run_id, events)
        work = self._prepare(module.name, module.tla, module.cfg)
        try:
            call = await self.supervisor.run(
                [
                    self.java,
                    "-XX:+UseParallelGC",
                    "-jar",
                    str(self.jar),
                    "-workers",
                    "auto",
                    "-metadir",
                    str(work / "states"),
                    "-config",
                    f"{module.name}.cfg",
                    f"{module.name}.tla",
                ],
                # TLC resolves EXTENDS Run against its working directory.
                cwd=work,
                timeout_s=self.timeout_s,
                output=Capture(),
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return _verdict(call)

    def _prepare(self, name: str, tla: str, cfg: str) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="trace-", dir=self.work_dir))
        shutil.copyfile(self.run_spec, work / "Run.tla")
        (work / f"{name}.tla").write_text(tla, encoding="utf-8")
        (work / f"{name}.cfg").write_text(cfg, encoding="utf-8")
        return work


def _verdict(call: CallResult) -> ValidationResult:
    """Pass only on the one TLC result that means accepted; anything else fails."""
    stdout = call.stdout or ""
    if call.outcome is Outcome.EXITED:
        if call.exit_code == _VIOLATION and _ACCEPTED in stdout:
            return ValidationResult(True)
        if call.exit_code == _DEADLOCK:
            return ValidationResult(
                False, "the log is not a behaviour of Run (TLC: deadlock)"
            )
    detail = call.reason or _tail(stdout + (call.stderr or ""))
    code = f" {call.exit_code}" if call.exit_code is not None else ""
    return ValidationResult(False, f"TLC {call.outcome}{code}: {detail}")


def _tail(text: str, lines: int = 5) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])
