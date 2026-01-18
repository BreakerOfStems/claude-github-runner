"""Worker execution flow for processing jobs."""

import json
import logging
import os
import re
import subprocess
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

    def execute(self, job: Job, comment_id: Optional[int] = None) -> str:
        """Execute a job. Returns the run_id."""
        run_id = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

        logger.info(f"Starting run {run_id} for {job.repo}#{job.target_number} ({job.job_type.value})")

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

    def _execute_job(self, run_id: str, job: Job, paths: WorkspacePaths):
        """Execute the job steps."""
        # Update status to claimed
        self.db.update_run_status(run_id, RunStatus.CLAIMED, pid=os.getpid())

        # Claim on GitHub and post starting comment
        self._claim_on_github(job, run_id)

        # Clone repository
        logger.info(f"Cloning {job.repo}")
        self.github.clone_repo(job.repo, str(paths.repo))

        git = Git(paths.repo)

        # Checkout base branch
        git.checkout(self.config.branching.base_branch)

        # Create working branch
        slug = slugify(job.issue.title)
        branch_name = f"{self.config.branching.branch_prefix}/{job.target_number}-{slug}"
        self.db.update_run_status(run_id, RunStatus.RUNNING, branch=branch_name)

        # Check if branch already exists on remote
        git.fetch()
        if git.branch_exists(branch_name, remote=True):
            logger.info(f"Branch {branch_name} exists on remote, checking out")
            git.checkout(branch_name)
            # Try to rebase onto base branch
            if not git.rebase(f"origin/{self.config.branching.base_branch}"):
                logger.warning("Rebase failed, handling conflicts")
                git.abort_rebase()
                self._handle_merge_conflict(job, run_id, paths, git)
                return
        else:
            git.create_branch(branch_name)

        # Construct and write prompt
        prompt = self._build_prompt(job)
        paths.prompt_file.write_text(prompt)

        # Invoke Claude Code
        logger.info("Invoking Claude Code")
        self._invoke_claude(paths, run_id)

        # Check what Claude did - could be:
        # 1. Made uncommitted changes (git status shows changes)
        # 2. Already committed (check commits ahead of base)
        # 3. Already committed AND pushed (check for PR)
        # 4. Did nothing

        has_uncommitted = bool(git.status())
        has_new_commits = git.has_commits_ahead_of(f"origin/{self.config.branching.base_branch}")

        logger.info(f"Post-Claude state: uncommitted={has_uncommitted}, new_commits={has_new_commits}")

        # Check if Claude already created a PR (on our branch or any branch)
        existing_pr = self.github.get_pr_for_branch(job.repo, branch_name)
        if existing_pr:
            logger.info(f"Claude already created PR: {existing_pr.url}")
            self.db.update_run_status(run_id, RunStatus.SUCCEEDED, pr_url=existing_pr.url)
            self._handle_success(job, run_id, paths, existing_pr.url)
            self._write_summary(paths, run_id, job, "succeeded", pr_url=existing_pr.url)
            self.workspace_manager.cleanup(run_id, success=True)
            return

        # Also check the current branch (Claude might have renamed it)
        current_branch = git.get_current_branch()
        if current_branch != branch_name:
            logger.info(f"Branch changed: expected {branch_name}, got {current_branch}")
            existing_pr = self.github.get_pr_for_branch(job.repo, current_branch)
            if existing_pr:
                logger.info(f"Found PR on current branch: {existing_pr.url}")
                self.db.update_run_status(run_id, RunStatus.SUCCEEDED, pr_url=existing_pr.url)
                self._handle_success(job, run_id, paths, existing_pr.url)
                self._write_summary(paths, run_id, job, "succeeded", pr_url=existing_pr.url)
                self.workspace_manager.cleanup(run_id, success=True)
                return

        if not has_uncommitted and not has_new_commits:
            logger.info("No changes made by Claude")
            self._handle_no_changes(job, run_id, paths)
            return

        # If Claude made uncommitted changes, commit them
        if has_uncommitted:
            git.add_all()
            diff = git.diff(staged=True)
            paths.git_diff.write_text(diff)

            commit_message = self._build_commit_message(job)
            if not git.commit(commit_message):
                logger.info("Nothing to commit after staging")
                if not has_new_commits:
                    self._handle_no_changes(job, run_id, paths)
                    return

        # Push if there are unpushed commits
        if git.has_unpushed_commits(self.config.branching.base_branch):
            logger.info(f"Pushing branch {branch_name}")
            git.push("origin", branch_name, set_upstream=True)

        # Create or update PR
        pr_url = self._create_or_update_pr(job, branch_name, paths)
        self.db.update_run_status(run_id, RunStatus.SUCCEEDED, pr_url=pr_url)

        # Update labels on success
        self._handle_success(job, run_id, paths, pr_url)

        # Write summary
        self._write_summary(paths, run_id, job, "succeeded", pr_url=pr_url)

        # Cleanup
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
            "### What you MUST do:",
            "- Make reasonable decisions based on available context",
            "- Proceed with implementation without waiting for approval",
            "- Explore the codebase to understand patterns and conventions",
            "- Create or modify files as needed to complete the task",
            "- Keep changes minimal and focused",
            "- Follow existing code style and conventions",
            "",
            "### What you must NOT do:",
            "- Do NOT ask questions or wait for input",
            "- Do NOT push to main/master branch",
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

    def _invoke_claude(self, paths: WorkspacePaths, run_id: str):
        """Invoke Claude Code non-interactively."""
        # Build command for headless execution:
        # --dangerously-skip-permissions: skip tool permission checks (from config)
        # -p: provide the prompt
        prompt = paths.prompt_file.read_text()
        cmd = [
            self.config.claude.command,
            *self.config.claude.non_interactive_args,
            "-p", prompt,
        ]

        logger.info(f"Running Claude in: {paths.repo}")
        logger.info(f"Log file: {paths.claude_log}")

        # Stream output to files in real-time so we can tail them
        stdout_path = paths.root / "claude_stdout.log"
        stderr_path = paths.root / "claude_stderr.log"

        with open(stdout_path, "w") as stdout_file, open(stderr_path, "w") as stderr_file:
            process = subprocess.Popen(
                cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                cwd=paths.repo,
            )

            try:
                returncode = process.wait(timeout=self.config.timeouts.run_timeout_minutes * 60)
            except subprocess.TimeoutExpired:
                logger.warning(f"Claude timed out after {self.config.timeouts.run_timeout_minutes} minutes")
                process.kill()
                process.wait()
                returncode = -1

        # Combine into single log file for compatibility
        with open(paths.claude_log, "w") as log_file:
            log_file.write("=== STDOUT ===\n")
            log_file.write(stdout_path.read_text())
            log_file.write("\n=== STDERR ===\n")
            log_file.write(stderr_path.read_text())

        if returncode != 0:
            logger.warning(f"Claude exited with code {returncode}")
            # Log stderr for debugging
            stderr_content = stderr_path.read_text().strip()
            if stderr_content:
                for line in stderr_content.split('\n')[:10]:
                    logger.warning(f"Claude stderr: {line}")
            # Don't raise - Claude might have made partial progress

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

    def _create_or_update_pr(self, job: Job, branch_name: str, paths: WorkspacePaths) -> str:
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
                base_branch=self.config.branching.base_branch,
                working_dir=str(paths.repo),
            )

    def _build_pr_body(self, job: Job, paths: WorkspacePaths) -> str:
        """Build PR body."""
        lines = [
            f"## Summary",
            "",
            f"Automated implementation for #{job.target_number}.",
            "",
        ]

        # Add closing keyword for issues (not PRs)
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

        lines.append(f"**Type:** {job.job_type.value}")
        lines.append("")

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
