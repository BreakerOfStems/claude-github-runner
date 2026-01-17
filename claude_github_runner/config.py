"""Configuration management for Claude GitHub Runner."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class LabelsConfig:
    ready: str = "ready"
    in_progress: str = "in-progress"
    blocked: list[str] = field(default_factory=lambda: ["blocked", "needs-info"])
    needs_human: str = "needs-human"
    done: str = "done"


@dataclass
class BranchingConfig:
    base_branch: str = "main"
    branch_prefix: str = "claude"


@dataclass
class PollingConfig:
    interval_seconds: int = 300
    max_concurrency: int = 1


@dataclass
class TimeoutsConfig:
    run_timeout_minutes: int = 60
    stale_run_minutes: int = 180


@dataclass
class PathsConfig:
    workspace_root: str = "/home/claude/workspace/_runs"
    db_path: str = "/home/claude/workspace/runner.sqlite"


@dataclass
class ClaudeConfig:
    command: str = "claude"
    non_interactive_args: list[str] = field(default_factory=lambda: ["--dangerously-skip-permissions"])


@dataclass
class PrioritiesConfig:
    mention_first: bool = True
    oldest_first: bool = True


@dataclass
class CleanupConfig:
    on_success: str = "delete"  # delete | keep | archive
    on_failure: str = "keep"    # delete | keep | archive
    keep_hours: int = 24


@dataclass
class Config:
    repos: list[str] = field(default_factory=list)
    labels: LabelsConfig = field(default_factory=LabelsConfig)
    branching: BranchingConfig = field(default_factory=BranchingConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    timeouts: TimeoutsConfig = field(default_factory=TimeoutsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    priorities: PrioritiesConfig = field(default_factory=PrioritiesConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Create Config from a dictionary (parsed YAML)."""
        config = cls()

        if "repos" in data:
            config.repos = data["repos"]

        if "labels" in data:
            labels = data["labels"]
            config.labels = LabelsConfig(
                ready=labels.get("ready", config.labels.ready),
                in_progress=labels.get("in_progress", config.labels.in_progress),
                blocked=labels.get("blocked", config.labels.blocked),
                needs_human=labels.get("needs_human", config.labels.needs_human),
                done=labels.get("done", config.labels.done),
            )

        if "branching" in data:
            branching = data["branching"]
            config.branching = BranchingConfig(
                base_branch=branching.get("base_branch", config.branching.base_branch),
                branch_prefix=branching.get("branch_prefix", config.branching.branch_prefix),
            )

        if "polling" in data:
            polling = data["polling"]
            config.polling = PollingConfig(
                interval_seconds=polling.get("interval_seconds", config.polling.interval_seconds),
                max_concurrency=min(polling.get("max_concurrency", config.polling.max_concurrency), 3),
            )

        if "timeouts" in data:
            timeouts = data["timeouts"]
            config.timeouts = TimeoutsConfig(
                run_timeout_minutes=timeouts.get("run_timeout_minutes", config.timeouts.run_timeout_minutes),
                stale_run_minutes=timeouts.get("stale_run_minutes", config.timeouts.stale_run_minutes),
            )

        if "paths" in data:
            paths = data["paths"]
            config.paths = PathsConfig(
                workspace_root=paths.get("workspace_root", config.paths.workspace_root),
                db_path=paths.get("db_path", config.paths.db_path),
            )

        if "claude" in data:
            claude = data["claude"]
            config.claude = ClaudeConfig(
                command=claude.get("command", config.claude.command),
                non_interactive_args=claude.get("non_interactive_args", config.claude.non_interactive_args),
            )

        if "priorities" in data:
            priorities = data["priorities"]
            config.priorities = PrioritiesConfig(
                mention_first=priorities.get("mention_first", config.priorities.mention_first),
                oldest_first=priorities.get("oldest_first", config.priorities.oldest_first),
            )

        if "cleanup" in data:
            cleanup = data["cleanup"]
            config.cleanup = CleanupConfig(
                on_success=cleanup.get("on_success", config.cleanup.on_success),
                on_failure=cleanup.get("on_failure", config.cleanup.on_failure),
                keep_hours=cleanup.get("keep_hours", config.cleanup.keep_hours),
            )

        return config

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        """Load configuration from file."""
        if config_path is None:
            # Default locations to check
            candidates = [
                Path("/etc/claude-github-runner/config.yml"),
                Path("/etc/claude-github-runner/config.yaml"),
                Path.home() / ".config" / "claude-github-runner" / "config.yml",
                Path("config.yml"),
            ]
            for candidate in candidates:
                if candidate.exists():
                    config_path = str(candidate)
                    break

        if config_path and Path(config_path).exists():
            with open(config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            return cls.from_dict(data)

        return cls()

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []

        if not self.repos:
            errors.append("No repositories configured")

        if self.polling.max_concurrency < 1:
            errors.append("max_concurrency must be at least 1")

        if self.polling.max_concurrency > 3:
            errors.append("max_concurrency capped at 3 for testing phase")

        return errors
