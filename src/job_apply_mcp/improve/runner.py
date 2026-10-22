"""Command runner abstraction so git/gh/claude/aider/pytest are all fakeable offline."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RunResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


class CommandRunner(Protocol):
    async def run(self, argv: list[str], *, cwd: str | None = None, timeout: float = 600,
                  env: dict[str, str] | None = None) -> RunResult: ...


class SubprocessRunner:
    async def run(self, argv: list[str], *, cwd: str | None = None, timeout: float = 600,
                  env: dict[str, str] | None = None) -> RunResult:
        import os

        full_env = {**os.environ, **env} if env else None
        try:
            p = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=full_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return RunResult(127, "", f"command not found: {argv[0]}")
        try:
            out, err = await asyncio.wait_for(p.communicate(), timeout)
        except TimeoutError:
            p.kill()
            return RunResult(124, "", f"timeout after {timeout}s")
        return RunResult(p.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


@dataclass
class FakeRunner:
    """Test double: `handler(argv, cwd)` may return a RunResult; default is success. Records calls."""

    handler: object = None
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    async def run(self, argv, *, cwd=None, timeout=600, env=None) -> RunResult:
        self.calls.append((list(argv), cwd))
        if self.handler:
            r = self.handler(list(argv), cwd)
            if r is not None:
                return r
        return RunResult(0, "")
