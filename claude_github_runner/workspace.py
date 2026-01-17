"""Workspace isolation and cleanup management."""

import json
import logging
import os
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class WorkspacePaths:
    """Paths within a workspace."""
    root: Path
    repo: Path
    artifacts: Path
    runner_log: Path
    claude_log: Path
    git_diff: Path
    summary_json: Path
    prompt_file: Path


class Workspace:
    """Manages isolated workspaces for runs."""

    def __init__(self, config: Config):
        self.config = config
        self.workspace_root = Path(config.paths.workspace_root)

    def create(self, run_id: str) -> WorkspacePaths:
        """Create a new workspace for a run."""
        root = self.workspace_root / run_id
        repo = root / "repo"
        artifacts = root / "artifacts"

        # Create directories
        root.mkdir(parents=True, exist_ok=True)
        repo.mkdir(exist_ok=True)
        artifacts.mkdir(exist_ok=True)

        paths = WorkspacePaths(
            root=root,
            repo=repo,
            artifacts=artifacts,
            runner_log=artifacts / "runner.log",
            claude_log=artifacts / "claude.log",
            git_diff=artifacts / "git.diff",
            summary_json=artifacts / "summary.json",
            prompt_file=root / "prompt.md",
        )

        logger.info(f"Created workspace: {root}")
        return paths

    def cleanup(self, run_id: str, success: bool):
        """Clean up a workspace based on success/failure and config."""
        root = self.workspace_root / run_id

        if not root.exists():
            return

        policy = self.config.cleanup.on_success if success else self.config.cleanup.on_failure

        if policy == "delete":
            self._delete_workspace(root)
        elif policy == "archive":
            self._archive_workspace(root, run_id)
        elif policy == "keep":
            logger.info(f"Keeping workspace: {root}")
            # Could implement TTL cleanup here later
        else:
            logger.warning(f"Unknown cleanup policy: {policy}")

    def _delete_workspace(self, root: Path):
        """Delete a workspace directory."""
        try:
            shutil.rmtree(root)
            logger.info(f"Deleted workspace: {root}")
        except Exception as e:
            logger.error(f"Failed to delete workspace {root}: {e}")

    def _archive_workspace(self, root: Path, run_id: str):
        """Archive a workspace to tar.gz and delete original."""
        archive_path = self.workspace_root / f"{run_id}.tar.gz"

        try:
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(root, arcname=run_id)

            logger.info(f"Archived workspace to: {archive_path}")
            self._delete_workspace(root)
        except Exception as e:
            logger.error(f"Failed to archive workspace {root}: {e}")

    def cleanup_old_workspaces(self):
        """Clean up workspaces older than keep_hours."""
        if not self.workspace_root.exists():
            return

        cutoff = datetime.utcnow().timestamp() - (self.config.cleanup.keep_hours * 3600)

        for item in self.workspace_root.iterdir():
            if item.is_dir():
                try:
                    mtime = item.stat().st_mtime
                    if mtime < cutoff:
                        self._delete_workspace(item)
                except Exception as e:
                    logger.error(f"Error checking workspace {item}: {e}")


class Git:
    """Git operations within a workspace."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command."""
        cmd = ["git"] + args
        logger.debug(f"Running: {' '.join(cmd)}")
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=self.repo_path,
            check=check,
        )

    def checkout(self, branch: str):
        """Checkout a branch."""
        self._run(["checkout", branch])

    def create_branch(self, branch: str):
        """Create and checkout a new branch."""
        self._run(["checkout", "-b", branch])

    def branch_exists(self, branch: str, remote: bool = False) -> bool:
        """Check if a branch exists."""
        if remote:
            result = self._run(["ls-remote", "--heads", "origin", branch], check=False)
            return bool(result.stdout.strip())
        else:
            result = self._run(["rev-parse", "--verify", branch], check=False)
            return result.returncode == 0

    def fetch(self, remote: str = "origin"):
        """Fetch from remote."""
        self._run(["fetch", remote])

    def pull(self, remote: str = "origin", branch: Optional[str] = None):
        """Pull from remote."""
        args = ["pull", remote]
        if branch:
            args.append(branch)
        self._run(args)

    def add_all(self):
        """Stage all changes."""
        self._run(["add", "-A"])

    def commit(self, message: str) -> bool:
        """Commit staged changes. Returns False if nothing to commit."""
        result = self._run(["commit", "-m", message], check=False)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
                return False
            raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
        return True

    def push(self, remote: str = "origin", branch: Optional[str] = None, set_upstream: bool = False):
        """Push to remote."""
        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.append(remote)
        if branch:
            args.append(branch)
        self._run(args)

    def status(self) -> str:
        """Get git status."""
        result = self._run(["status", "--porcelain"])
        return result.stdout

    def diff(self, staged: bool = False) -> str:
        """Get git diff."""
        args = ["diff"]
        if staged:
            args.append("--staged")
        result = self._run(args)
        return result.stdout

    def get_current_branch(self) -> str:
        """Get current branch name."""
        result = self._run(["rev-parse", "--abbrev-ref", "HEAD"])
        return result.stdout.strip()

    def has_conflicts(self) -> bool:
        """Check if there are merge conflicts."""
        result = self._run(["diff", "--check"], check=False)
        return result.returncode != 0

    def get_conflict_files(self) -> list[str]:
        """Get list of files with conflicts."""
        result = self._run(["diff", "--name-only", "--diff-filter=U"], check=False)
        return result.stdout.strip().split("\n") if result.stdout.strip() else []

    def rebase(self, onto: str) -> bool:
        """Rebase current branch. Returns False if conflicts."""
        result = self._run(["rebase", onto], check=False)
        return result.returncode == 0

    def abort_rebase(self):
        """Abort an in-progress rebase."""
        self._run(["rebase", "--abort"], check=False)

    def log(self, count: int = 10, oneline: bool = True) -> str:
        """Get commit log."""
        args = ["log", f"-{count}"]
        if oneline:
            args.append("--oneline")
        result = self._run(args)
        return result.stdout

    def has_commits_ahead_of(self, base_ref: str) -> bool:
        """Check if current branch has commits ahead of base_ref."""
        # Fetch to make sure we have latest remote state
        self._run(["fetch", "origin"], check=False)
        # Count commits that are in HEAD but not in base_ref
        result = self._run(["rev-list", "--count", f"{base_ref}..HEAD"], check=False)
        try:
            count = int(result.stdout.strip())
            return count > 0
        except (ValueError, AttributeError):
            return False

    def has_unpushed_commits(self, base_branch: str = "main") -> bool:
        """Check if there are commits not pushed to origin.

        If the current branch doesn't exist on remote, checks against base_branch instead.
        """
        branch = self.get_current_branch()

        # First try comparing to the remote tracking branch
        result = self._run(["rev-list", "--count", f"origin/{branch}..HEAD"], check=False)
        if result.returncode == 0:
            try:
                count = int(result.stdout.strip())
                return count > 0
            except (ValueError, AttributeError):
                pass

        # Remote branch doesn't exist - compare to base branch
        # If we have commits ahead of origin/base_branch, we need to push
        result = self._run(["rev-list", "--count", f"origin/{base_branch}..HEAD"], check=False)
        try:
            count = int(result.stdout.strip())
            return count > 0
        except (ValueError, AttributeError):
            return False
