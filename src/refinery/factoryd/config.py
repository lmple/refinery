"""factoryd configuration, from CLI options and REFINERY_* variables."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from refinery.gate.runner import GateConfig
from refinery.review.repo import Tools

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8740
DEFAULT_SHUTDOWN_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class ServerConfig:
    """Everything factoryd needs to run."""

    data_dir: Path
    roots: tuple[Path, ...]
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    tla_jar: Path | None = None
    run_spec: Path | None = None
    gate: GateConfig = field(default_factory=GateConfig)
    tools: Tools = field(default_factory=Tools)  # git, gh, openspec
    # How long open requests get to finish on shutdown before they are cut off.
    shutdown_timeout_s: float = DEFAULT_SHUTDOWN_TIMEOUT_S

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "ServerConfig":
        """From REFINERY_DATA_DIR, REFINERY_ROOTS (colon-separated), REFINERY_HOST,
        REFINERY_PORT, REFINERY_TLA_JAR, REFINERY_RUN_SPEC and
        REFINERY_SHUTDOWN_TIMEOUT (seconds)."""
        data_dir = environ.get("REFINERY_DATA_DIR")
        roots = environ.get("REFINERY_ROOTS", "")
        return cls(
            data_dir=Path(data_dir) if data_dir else default_data_dir(environ),
            roots=tuple(Path(r) for r in roots.split(":") if r),
            host=environ.get("REFINERY_HOST", DEFAULT_HOST),
            port=int(environ.get("REFINERY_PORT", DEFAULT_PORT)),
            tla_jar=_path(environ.get("REFINERY_TLA_JAR")),
            run_spec=_path(environ.get("REFINERY_RUN_SPEC")),
            shutdown_timeout_s=float(
                environ.get("REFINERY_SHUTDOWN_TIMEOUT", DEFAULT_SHUTDOWN_TIMEOUT_S)
            ),
        )


def default_data_dir(environ: Mapping[str, str]) -> Path:
    """``$XDG_DATA_HOME/refinery``, else ``~/.local/share/refinery``."""
    xdg = environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "refinery"
    home = environ.get("HOME")
    return (Path(home) if home else Path.home()) / ".local" / "share" / "refinery"


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None
