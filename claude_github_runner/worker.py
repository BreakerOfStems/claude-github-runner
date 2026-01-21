"""Worker execution flow for processing jobs."""

import json
import logging
import multiprocessing
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config
from .database import Database, Run, RunStatus, JobType
from .discovery import Job
from .github import GitHub
from .workspace import Workspace, WorkspacePaths, Git

logger = logging.getLogger(__name__)


def slugify(text: str, max_length: int = 30) -> str:
    """Convert text to a URL-safe slug."""
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:max_length]


class Worker:
    """Executes a single job."""

    def __init__(self, config: Config, db: Database, github: GitHub, workspace_manager: Workspace):
        self.config = config
        self.db = db
        self.github = github
        self.workspace_manager = workspace_manager

    def execute(self, job: Job, comment_id: Optional[int] = None, run_id: Optional[str] = None) -> str:
        """Execute a job. Returns the run_id.

        If run_id is provided, uses the existing run record (created by tick before spawning).
        Otherwise creates a new run record (for manual runs).
        """
        if run_id:
            # Using existing run record created by tick()
            logger.info(f"Using existing run {run_id} for {job.repo}#{job.target_number} ({job.job_type.value})")
        else:
            # Manual run - create new run record
            run_id = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            logger.info(f"Starting run {run_id} for {job.repo}#{job.target_number} ({job.job_type.value})")

            run = Run(
                run_id=run_id,
                repo=job.repo,
                target_number=job.target_number,
                job_type=job.job_type,
                status=RunStatus.QUEUED,
            )

            if not self.db.create_run(run):
                logger.error(f"Failed to create run: active run exists for {job.repo}#{job.target_number}")
                raise RuntimeError("Active run exists for target")

        # Create workspace
        paths = self.workspace_manager.create(run_id)

        try:
            self._execute_job(run_id, job, paths)
        except Exception as e:
            logger.exception(f"Run {run_id} failed: {e}")
            self.db.update_run_status(run_id, RunStatus.FAILED, error=str(e))
            self._handle_failure(job, run_id, paths, str(e))
            self.workspace_manager.cleanup(run_id, success=False)
            raise

        return run_id

    def execute_async(self, job: Job, comment_id: Optional[int] = None) -> str:
        """Execute a job in a background process. Returns the run_id immediately."""
        run_id = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

        logger.info(f"Starting async run {run_id} for {job.repo}#{job.target_number} ({job.job_type.value})")

        # Create run record
        run = Run(
            run_id=run_id,
            repo=job.repo,
            target_number=job.target_number,
            job_type=job.job_type,
            status=RunStatus.QUEUED,
        )

        if not self.db.create_run(run):
            logger.error(f"Failed to create run: active run exists for {job.repo}#{job.target_number}")
            raise RuntimeError("Active run exists for target")

        # Create workspace before forking
        paths = self.workspace_manager.create(run_id)

        # Fork to a child process
        pid = os.fork()

        if pid == 0:
            # Child process - execute the job
            try:
                # Set up file-based logging for the child process
                # This is critical because stdout/stderr may be closed after parent exits
                log_file = paths.root / "worker.log"
                file_handler = logging.FileHandler(log_file)
                file_handler.setFormatter(logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                ))
                root_logger = logging.getLogger()
                root_logger.handlers.clear()
                root_logger.addHandler(file_handler)
                root_logger.setLevel(logging.INFO)

                logger.info(f"Child process started for run {run_id}")

                # Reconnect to database (connection isn't safe to share across fork)
                self.db = Database(self.config.paths.db_path)
                self.github = GitHub()

                logger.info("Database and GitHub reconnected")

                self._execute_job(run_id, job, paths)
            except Exception as e:
                logger.exception(f"Run {run_id} failed: {e}")
                self.db.update_run_status(run_id, RunStatus.FAILED, error=str(e))
                self._handle_failure(job, run_id, paths, str(e))
                self.workspace_manager.cleanup(run_id, success=False)
            finally:
                # Child must exit to avoid returning to parent's control flow
                os._exit(0)
        else:
            # Parent process - record the child PID and return immediately
            logger.info(f"Spawned child process {pid} for run {run_id}")
            self.db.update_run_status(run_id, RunStatus.CLAIMED, pid=pid)
            return run_id

    def _execute_job(self, run_id: str, job: Job, paths: WorkspacePaths):
        """Execute the job steps."""
        # Update status to claimed
        self.db.update_run_status(run_id, RunStatus.CLAIMED, pid=os.getpid())

        # Claim on GitHub and post starting comment
        self._claim_on_github(job, run_id)

        # Set up repository and determine branches
        git, base_branch = self._setup_repository(job, paths)

        # Create working branch
        branch_name = self._get_branch_name(job)
        self.db.update_run_status(run_id, RunStatus.RUNNING, branch=branch_name)

        # Checkout or create the working branch
        if not self._setup_working_branch(git, branch_name, base_branch, job, run_id, paths):
            return  # Merge conflict handled

        # Construct and write prompt
        prompt = self._build_prompt(job)
        paths.prompt_file.write_text(prompt)

        # Invoke Claude Code
        logger.info("Invoking Claude Code")
        self._invoke_claude(paths, run_id)

        # Handle Claude's output and finalize
        self._handle_claude_output(git, job, run_id, paths, branch_name, base_branch)

    def _setup_repository(self, job: Job, paths: WorkspacePaths) -> tuple[Git, str]:
        """Clone repository and determine base branch.

        Returns:
            Tuple of (Git instance, base_branch name)
        """
        # Clone repository
        logger.info(f"Cloning {job.repo}")
        self.github.clone_repo(job.repo, str(paths.repo))

        git = Git(paths.repo)

        # Determine base branch - use config override or auto-detect from repo
        base_branch = self._determine_base_branch(job.repo)

        # Checkout base branch
        git.checkout(base_branch)

        return git, base_branch

    def _determine_base_branch(self, repo: str) -> str:
        """Determine the base branch for the repository."""
        base_branch = self.config.branching.base_branch
        if base_branch == "main":
            # Default value - auto-detect the actual default branch
            try:
                detected_branch = self.github.get_default_branch(repo)
                if detected_branch:
                    base_branch = detected_branch
                    logger.info(f"Auto-detected default branch: {base_branch}")
            except Exception as e:
                logger.warning(f"Failed to detect default branch, using 'main': {e}")
        return base_branch

    def _get_branch_name(self, job: Job) -> str:
        """Generate the working branch name for a job."""
        slug = slugify(job.issue.title)
        return f"{self.config.branching.branch_prefix}/{job.target_number}-{slug}"

    def _setup_working_branch(
        self,
        git: Git,
        branch_name: str,
        base_branch: str,
        job: Job,
        run_id: str,
        paths: WorkspacePaths,
    ) -> bool:
        """Set up the working branch, handling existing branches and conflicts.

        Returns:
            True if setup succeeded, False if merge conflict occurred (already handled).
        """
        git.fetch()
        if git.branch_exists(branch_name, remote=True):
            logger.info(f"Branch {branch_name} exists on remote, checking out")
            git.checkout(branch_name)
            # Try to rebase onto base branch
            if not git.rebase(f"origin/{base_branch}"):
                logger.warning("Rebase failed, handling conflicts")
                git.abort_rebase()
                self._handle_merge_conflict(job, run_id, paths, git)
                return False
        else:
            git.create_branch(branch_name)
        return True

    def _handle_claude_output(
        self,
        git: Git,
        job: Job,
        run_id: str,
        paths: WorkspacePaths,
        branch_name: str,
        base_branch: str,
    ):
        """Handle Claude's output after invocation - check for changes, PRs, and finalize."""
        has_uncommitted = bool(git.status())
        has_new_commits = git.has_commits_ahead_of(f"origin/{base_branch}")

        logger.info(f"Post-Claude state: uncommitted={has_uncommitted}, new_commits={has_new_commits}")

        # Check if Claude already created a PR
        existing_pr = self._find_existing_pr(git, job, branch_name)
        if existing_pr:
            logger.info(f"Claude already created PR: {existing_pr.url}")
            self._finalize_success(job, run_id, paths, existing_pr.url)
            return

        if not has_uncommitted and not has_new_commits:
            logger.info("No changes made by Claude")
            self._handle_no_changes(job, run_id, paths)
            return

        # Commit uncommitted changes if any
        if has_uncommitted:
            if not self._commit_changes(git, job, paths):
                if not has_new_commits:
                    self._handle_no_changes(job, run_id, paths)
                    return

        # Push and create PR
        self._push_and_create_pr(git, job, run_id, paths, branch_name, base_branch)

    def _find_existing_pr(self, git: Git, job: Job, branch_name: str):
        """Find an existing PR created by Claude on the expected or current branch."""
        # Check expected branch
        existing_pr = self.github.get_pr_for_branch(job.repo, branch_name)
        if existing_pr:
            return existing_pr

        # Check current branch (Claude might have renamed it)
        current_branch = git.get_current_branch()
        if current_branch != branch_name:
            logger.info(f"Branch changed: expected {branch_name}, got {current_branch}")
            existing_pr = self.github.get_pr_for_branch(job.repo, current_branch)
            if existing_pr:
                return existing_pr

        return None

    def _commit_changes(self, git: Git, job: Job, paths: WorkspacePaths) -> bool:
        """Stage and commit uncommitted changes.

        Returns:
            True if commit succeeded, False if nothing to commit.
        """
        git.add_all()
        diff = git.diff(staged=True)
        paths.git_diff.write_text(diff)

        commit_message = self._build_commit_message(job)
        if not git.commit(commit_message):
            logger.info("Nothing to commit after staging")
            return False
        return True

    def _push_and_create_pr(
        self,
        git: Git,
        job: Job,
        run_id: str,
        paths: WorkspacePaths,
        branch_name: str,
        base_branch: str,
    ):
        """Push changes and create/update PR."""
        # Push if there are unpushed commits
        if git.has_unpushed_commits(base_branch):
            logger.info(f"Pushing branch {branch_name}")
            git.push("origin", branch_name, set_upstream=True)

        # Create or update PR
        pr_url = self._create_or_update_pr(job, branch_name, paths, base_branch)
        self._finalize_success(job, run_id, paths, pr_url)

    def _finalize_success(self, job: Job, run_id: str, paths: WorkspacePaths, pr_url: str):
        """Finalize a successful run - update status, labels, summary, and cleanup."""
        self.db.update_run_status(run_id, RunStatus.SUCCEEDED, pr_url=pr_url)
        self._handle_success(job, run_id, paths, pr_url)
        self._write_summary(paths, run_id, job, "succeeded", pr_url=pr_url)
        self.workspace_manager.cleanup(run_id, success=True)

    def _claim_on_github(self, job: Job, run_id: str):
        """Claim the issue/PR on GitHub and post starting comment."""
        try:
            # Remove ready label and add in-progress
            self.github.remove_label(job.repo, job.target_number, self.config.labels.ready)
            self.github.add_label(job.repo, job.target_number, self.config.labels.in_progress)

            # Assign to self
            login = self.github.get_authenticated_user()
            self.github.assign_issue(job.repo, job.target_number, login)

            # Post starting comment
            if job.comment:
                # Responding to a mention
                self.github.create_comment(
                    job.repo,
                    job.target_number,
                    f"🤖 Thanks @{job.comment.author}! I'm starting work on this now.\n\n`Run ID: {run_id}`"
                )
            else:
                # Starting from ready label
                self.github.create_comment(
                    job.repo,
                    job.target_number,
                    f"🤖 I'm starting work on this issue now.\n\n`Run ID: {run_id}`"
                )

            logger.info(f"Claimed {job.repo}#{job.target_number}")
        except Exception as e:
            logger.warning(f"Failed to claim on GitHub: {e}")

    def _build_prompt(self, job: Job) -> str:
        """Build the prompt for Claude Code."""
        lines = [
            "# Task",
            "",
            f"Repository: {job.repo}",
            f"Issue/PR #{job.target_number}: {job.issue.title}",
            "",
            "## Description",
            "",
            job.issue.body or "(No description provided)",
            "",
        ]

        if job.comment:
            lines.extend([
                "## Request Comment",
                "",
                f"From @{job.comment.author}:",
                "",
                job.comment.body,
                "",
                f"Comment URL: {job.comment.url}",
                "",
            ])

        lines.extend([
            "## Execution Mode: Unattended/Headless",
            "",
            "You are running as an automated agent. There is no human watching. Your output will be captured and reviewed later.",
            "",
            "### Your workflow:",
            "1. Explore the codebase to understand patterns and conventions",
            "2. Implement the requested changes",
            "3. Commit your changes with a descriptive message",
            "4. Push to the current branch (you're already on a feature branch)",
            f"5. Create a Pull Request with a clear description that includes `Closes #{job.target_number}` to link it to this issue",
            "",
            "### PR Description Requirements:",
            "Your PR description MUST include:",
            "- A summary of what was implemented",
            f"- The text `Closes #{job.target_number}` (this links and auto-closes the issue when merged)",
            "- Any important implementation notes or decisions made",
            "",
            "### What you MUST do:",
            "- Make reasonable decisions based on available context",
            "- Proceed with implementation without waiting for approval",
            "- Keep changes minimal and focused",
            "- Follow existing code style and conventions",
            "- Create a PR when done - do not just leave uncommitted changes",
            "",
            "### What you must NOT do:",
            "- Do NOT ask questions or wait for input",
            "- Do NOT push to main/master branch directly",
            "- Do NOT make changes if requirements are fundamentally ambiguous",
            "",
            "### If you cannot complete the task:",
            "",
            "If you are genuinely blocked and cannot proceed, you MUST output a clear handoff message.",
            "Your final output should explain:",
            "",
            "1. **What you tried** - what steps you took, what you looked at",
            "2. **Why you're blocked** - specific missing info, ambiguity, or technical issue",
            "3. **What you need** - specific questions or information that would unblock you",
            "4. **Suggested next steps** - what the human should do or clarify",
            "",
            "This output will be posted as a comment on the issue so the human knows exactly how to help.",
            "",
        ])

        return "\n".join(lines)

    def _invoke_claude(self, paths: WorkspacePaths, run_id: str, retry_count: int = 0):
        """Invoke Claude Code non-interactively."""
        cmd = self._build_claude_command(paths)
        stdout_path, stderr_path = self._get_claude_log_paths(paths)

        logger.info(f"Running Claude in: {paths.repo}")
        logger.info(f"Log file: {paths.claude_log}")

        returncode = self._run_claude_process(cmd, paths, stdout_path, stderr_path)

        # Check for auth errors and retry if needed
        if self._has_auth_error(stdout_path):
            if retry_count < self.config.retry.max_retries:
                return self._retry_with_backoff(paths, run_id, retry_count)
            else:
                logger.error("Auth error persists after retry - token may need manual refresh")

        self._combine_log_files(paths, stdout_path, stderr_path)
        self._log_claude_errors(returncode, stderr_path)

    def _build_claude_command(self, paths: WorkspacePaths) -> list[str]:
        """Build the command for headless Claude execution."""
        prompt = paths.prompt_file.read_text()
        return [
            self.config.claude.command,
            *self.config.claude.non_interactive_args,
            "-p", prompt,
        ]

    def _get_claude_log_paths(self, paths: WorkspacePaths) -> tuple[Path, Path]:
        """Get paths for Claude stdout and stderr log files."""
        stdout_path = paths.root / "claude_stdout.log"
        stderr_path = paths.root / "claude_stderr.log"
        return stdout_path, stderr_path

    def _run_claude_process(
        self,
        cmd: list[str],
        paths: WorkspacePaths,
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:
        """Run the Claude subprocess and return the exit code."""
        with open(stdout_path, "w") as stdout_file, open(stderr_path, "w") as stderr_file:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                cwd=paths.repo,
            )

            try:
                return process.wait(timeout=self.config.timeouts.run_timeout_minutes * 60)
            except subprocess.TimeoutExpired:
                logger.warning(f"Claude timed out after {self.config.timeouts.run_timeout_minutes} minutes")
                process.kill()
                process.wait()
                return -1

    def _has_auth_error(self, stdout_path: Path) -> bool:
        """Check if the output contains an auth error."""
        stdout_content = stdout_path.read_text()
        return "authentication_error" in stdout_content or "OAuth token has expired" in stdout_content

    def _retry_with_backoff(self, paths: WorkspacePaths, run_id: str, retry_count: int):
        """Retry Claude invocation with exponential backoff."""
        import time

        delay = self.config.retry.initial_delay_seconds * (
            self.config.retry.backoff_multiplier ** retry_count
        )
        logger.warning(
            f"Auth error detected, retrying (attempt {retry_count + 2}/{self.config.retry.max_retries + 1}) "
            f"after {delay:.1f}s delay..."
        )
        time.sleep(delay)
        return self._invoke_claude(paths, run_id, retry_count + 1)

    def _combine_log_files(self, paths: WorkspacePaths, stdout_path: Path, stderr_path: Path):
        """Combine stdout and stderr into a single log file."""
        with open(paths.claude_log, "w") as log_file:
            log_file.write("=== STDOUT ===\n")
            log_file.write(stdout_path.read_text())
            log_file.write("\n=== STDERR ===\n")
            log_file.write(stderr_path.read_text())

    def _log_claude_errors(self, returncode: int, stderr_path: Path):
        """Log errors from Claude execution if any."""
        if returncode != 0:
            logger.warning(f"Claude exited with code {returncode}")
            stderr_content = stderr_path.read_text().strip()
            if stderr_content:
                for line in stderr_content.split('\n')[:10]:
                    logger.warning(f"Claude stderr: {line}")

    def _build_commit_message(self, job: Job) -> str:
        """Build a commit message."""
        lines = [
            f"#{job.target_number}: {job.issue.title}",
            "",
            f"Closes #{job.target_number}" if job.job_type == JobType.ISSUE_READY else f"Related to #{job.target_number}",
            "",
            "---",
            "Automated by claude-github-runner",
        ]
        return "\n".join(lines)

    def _create_or_update_pr(self, job: Job, branch_name: str, paths: WorkspacePaths, base_branch: str) -> str:
        """Create or update a PR. Returns PR URL."""
        # Check if PR already exists
        existing_pr = self.github.get_pr_for_branch(job.repo, branch_name)

        pr_body = self._build_pr_body(job, paths)

        if existing_pr:
            logger.info(f"Updating existing PR #{existing_pr.number}")
            self.github.update_pr(job.repo, existing_pr.number, body=pr_body)
            # Add comment about the update
            self.github.add_pr_comment(
                job.repo,
                existing_pr.number,
                f"🤖 Updated based on {'comment' if job.comment else 'issue'} request.\n\nRun ID: `{paths.root.name}`"
            )
            return existing_pr.url
        else:
            logger.info("Creating new PR")
            pr_title = f"[Claude] #{job.target_number}: {job.issue.title}"
            return self.github.create_pr(
                repo=job.repo,
                title=pr_title,
                body=pr_body,
                head_branch=branch_name,
                base_branch=base_branch,
                working_dir=str(paths.repo),
            )

    def _build_pr_body(self, job: Job, paths: WorkspacePaths) -> str:
        """Build PR body for fallback when Claude doesn't create a PR itself."""
        lines = [
            f"## Summary",
            "",
            f"Automated implementation for #{job.target_number}: {job.issue.title}",
            "",
        ]

        # Add closing keyword for issues (this is critical for auto-close)
        if job.job_type == JobType.ISSUE_READY:
            lines.extend([
                f"Closes #{job.target_number}",
                "",
            ])
        else:
            lines.extend([
                f"Related to #{job.target_number}",
                "",
            ])

        if job.comment:
            lines.extend([
                "**Triggered by comment:**",
                f"> {job.comment.body[:200]}{'...' if len(job.comment.body) > 200 else ''}",
                "",
            ])

        lines.extend([
            "---",
            "",
            "🤖 *This PR was automatically generated by [claude-github-runner](https://github.com/anthropics/claude-github-runner)*",
        ])

        return "\n".join(lines)

    def _handle_success(self, job: Job, run_id: str, paths: WorkspacePaths, pr_url: str):
        """Handle successful completion."""
        try:
            # Remove in-progress label
            self.github.remove_label(job.repo, job.target_number, self.config.labels.in_progress)

            # Optionally add done label
            # self.github.add_label(job.repo, job.target_number, self.config.labels.done)

            # Comment on issue
            if job.job_type == JobType.ISSUE_READY:
                self.github.create_comment(
                    job.repo,
                    job.target_number,
                    f"🤖 I've created a PR to address this issue: {pr_url}\n\nPlease review and let me know if changes are needed."
                )
        except Exception as e:
            logger.warning(f"Failed to update GitHub on success: {e}")

    def _handle_failure(self, job: Job, run_id: str, paths: WorkspacePaths, error: str):
        """Handle job failure."""
        try:
            # Remove in-progress, add needs-human
            self.github.remove_label(job.repo, job.target_number, self.config.labels.in_progress)
            self.github.add_label(job.repo, job.target_number, self.config.labels.needs_human)

            # Unassign the bot
            login = self.github.get_authenticated_user()
            self.github.unassign_issue(job.repo, job.target_number, login)

            # Comment on issue
            self.github.create_comment(
                job.repo,
                job.target_number,
                f"🤖 I encountered an error while working on this:\n\n```\n{error[:500]}\n```\n\nRun ID: `{run_id}`"
            )
        except Exception as e:
            logger.warning(f"Failed to update GitHub on failure: {e}")

        self._write_summary(paths, run_id, job, "failed", error=error)

    def _handle_no_changes(self, job: Job, run_id: str, paths: WorkspacePaths):
        """Handle case where Claude made no changes."""
        self.db.update_run_status(run_id, RunStatus.NEEDS_HUMAN)

        # Read Claude's output to include in the handoff comment
        claude_output = ""
        if paths.claude_log.exists():
            try:
                log_content = paths.claude_log.read_text()
                # Extract stdout section (Claude's actual response)
                if "=== STDOUT ===" in log_content:
                    stdout_start = log_content.index("=== STDOUT ===") + len("=== STDOUT ===\n")
                    stdout_end = log_content.index("\n=== STDERR ===") if "\n=== STDERR ===" in log_content else len(log_content)
                    claude_output = log_content[stdout_start:stdout_end].strip()
            except Exception as e:
                logger.warning(f"Failed to read claude.log: {e}")

        try:
            # Remove in-progress, add needs-human (ready was already removed at claim time)
            self.github.remove_label(job.repo, job.target_number, self.config.labels.in_progress)
            self.github.add_label(job.repo, job.target_number, self.config.labels.needs_human)

            # Unassign the bot
            login = self.github.get_authenticated_user()
            self.github.unassign_issue(job.repo, job.target_number, login)

            # Build comment with Claude's output if available
            if claude_output:
                # Truncate if too long for GitHub comment
                if len(claude_output) > 3000:
                    claude_output = claude_output[:3000] + "\n\n... (truncated)"
                comment = f"🤖 I wasn't able to complete this task. Here's what happened:\n\n{claude_output}\n\n---\n`Run ID: {run_id}`"
            else:
                comment = f"🤖 I analyzed this issue but didn't identify any code changes to make. This might need human review or more specific guidance.\n\n`Run ID: {run_id}`"

            self.github.create_comment(job.repo, job.target_number, comment)
        except Exception as e:
            logger.warning(f"Failed to update GitHub on no-changes: {e}")

        self._write_summary(paths, run_id, job, "no_changes")
        # Keep workspace for debugging (treat as failure for cleanup purposes)
        self.workspace_manager.cleanup(run_id, success=False)

    def _handle_merge_conflict(self, job: Job, run_id: str, paths: WorkspacePaths, git: Git):
        """Handle merge conflicts."""
        self.db.update_run_status(run_id, RunStatus.NEEDS_HUMAN, error="Merge conflict")

        conflict_files = git.get_conflict_files()

        try:
            # Add needs-human label
            self.github.add_label(job.repo, job.target_number, self.config.labels.needs_human)

            # Comment
            files_list = "\n".join(f"- `{f}`" for f in conflict_files)
            self.github.create_comment(
                job.repo,
                job.target_number,
                f"🤖 I encountered merge conflicts while rebasing:\n\n{files_list}\n\nPlease resolve these conflicts manually and I can continue working on this."
            )
        except Exception as e:
            logger.warning(f"Failed to update GitHub on conflict: {e}")

        self._write_summary(paths, run_id, job, "conflict", error=f"Conflict in: {', '.join(conflict_files)}")
        self.workspace_manager.cleanup(run_id, success=False)

    def _write_summary(
        self,
        paths: WorkspacePaths,
        run_id: str,
        job: Job,
        outcome: str,
        pr_url: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Write summary.json for the run."""
        summary = {
            "run_id": run_id,
            "repo": job.repo,
            "target_number": job.target_number,
            "job_type": job.job_type.value,
            "outcome": outcome,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if pr_url:
            summary["pr_url"] = pr_url
        if error:
            summary["error"] = error

        paths.summary_json.write_text(json.dumps(summary, indent=2))
