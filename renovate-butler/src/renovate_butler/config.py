"""Configuration: butler.yaml + environment variables."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .confidence import LEVELS


@dataclass
class MergeConfig:
    method: str = "squash"
    authors: list[str] = field(default_factory=lambda: ["renovate[bot]", "app/renovate"])
    min_confidence: str = "high"
    allowed_update_types: list[str] = field(default_factory=lambda: ["patch", "minor"])
    skip_labels: list[str] = field(default_factory=lambda: ["do-not-merge"])


@dataclass
class FixConfig:
    model: str = "anthropic/claude-sonnet-5.5"
    max_attempts: int = 3
    max_tool_turns: int = 40
    allow_local_checks: bool = False  # let the model run the workflow's `run:` steps locally
    label: str = "renovate-butler"


@dataclass
class PollConfig:
    interval_s: int = 30
    timeout_s: int = 3600
    no_ci_grace_s: int = 300  # how long to wait for checks to appear at all


@dataclass
class Config:
    repos: list[str]
    main_branch: str = "main"
    merge: MergeConfig = field(default_factory=MergeConfig)
    fix: FixConfig = field(default_factory=FixConfig)
    poll: PollConfig = field(default_factory=PollConfig)
    dry_run: bool = False
    log_file: str = "butler.log.jsonl"

    def validate(self) -> None:
        if not self.repos:
            raise ValueError("config: 'repos' must list at least one owner/repo")
        for r in self.repos:
            if r.count("/") != 1:
                raise ValueError(f"config: repo {r!r} must look like owner/repo")
        if self.merge.min_confidence not in LEVELS:
            raise ValueError(f"config: merge.min_confidence must be one of {LEVELS}")
        if self.merge.method not in ("merge", "squash", "rebase"):
            raise ValueError("config: merge.method must be merge, squash or rebase")
        if self.fix.max_attempts < 1:
            raise ValueError("config: fix.max_attempts must be >= 1")


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config(
        repos=list(raw.get("repos") or []),
        main_branch=raw.get("main_branch", "main"),
        merge=MergeConfig(**(raw.get("merge") or {})),
        fix=FixConfig(**(raw.get("fix") or {})),
        poll=PollConfig(**(raw.get("poll") or {})),
        dry_run=bool(raw.get("dry_run", False)),
        log_file=raw.get("log_file", "butler.log.jsonl"),
    )
    cfg.validate()
    return cfg


def github_token() -> str:
    load_dotenv()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    raise RuntimeError("No GitHub token: set GITHUB_TOKEN or run `gh auth login`")


def openrouter_key() -> str:
    load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (see .env.example)")
    return key
