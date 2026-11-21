"""Poll CI for a commit until it settles."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

from .config import PollConfig
from .github_client import Forge

Outcome = Literal["green", "red", "no-ci", "timeout"]


def wait_for_ci(
    forge: Forge,
    repo: str,
    sha: str,
    poll: PollConfig,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Outcome:
    start = now()
    last = None
    while True:
        state = forge.ci_state(repo, sha)
        if state != last:
            log(f"  CI for {repo}@{sha[:7]}: {state}")
            last = state
        if state == "passing":
            return "green"
        if state == "failing":
            return "red"
        elapsed = now() - start
        if state == "none" and elapsed >= poll.no_ci_grace_s:
            return "no-ci"
        if elapsed >= poll.timeout_s:
            return "timeout"
        sleep(poll.interval_s)
