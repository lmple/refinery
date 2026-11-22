"""The runner loop: reap, hello, claim, launch, poll, finish (runner D6).

``spec/Runner.tla`` models it: Start, Reap, Hello, Claim, Launch, Stop,
Finish and Shutdown. Agents run through the supervisor, whose registry lives
in the runner's own directory, never factoryd's.
"""

import asyncio

from refinery.runner.config import RunnerConfig


class Runner:
    """One runner process."""

    def __init__(self, config: RunnerConfig) -> None:
        """Its supervisor and job files live under ``config.directory``."""
        self.config = config

    async def run(self, stop: asyncio.Event) -> None:
        """Reap, hello, then claim and run jobs until ``stop`` is set; then
        cancel every agent, delete their MCP files and release their jobs."""
        raise NotImplementedError("runner loop (runner D6)")
