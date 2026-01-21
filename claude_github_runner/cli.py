"""CLI interface for Claude GitHub Runner."""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .database import Database, Run, RunStatus, JobType
from .discovery import Discovery, Job
from .github import GitHub, GitHubError
from .worker import Worker
from .workspace import Workspace

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False):
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def is_pid_alive(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class Runner:
    """Main runner orchestrator."""

    def __init__(self, config_path: Optional[str] = None):
        self.config = Config.load(config_path)
        self.db = Database(self.config.paths.db_path)
        self.github = GitHub()
        self.workspace_manager = Workspace(self.config)
        self.discovery = Discovery(self.config, self.db, self.github)
        self.worker = Worker(self.config, self.db, self.github, self.workspace_manager)

    def tick(self) -> dict:
        """Execute one scheduling cycle."""
        logger.info("Starting tick")

        # Validate config
        errors = self.config.validate()
        if errors:
            logger.error(f"Configuration errors: {errors}")
            return {"status": "error", "errors": errors}

        # Check available slots
        available_slots = self._get_available_slots()
        logger.info(f"Available slots: {available_slots}")

        if available_slots <= 0:
            logger.info("No available slots, exiting tick early")
            return {"status": "no_slots", "running": self.config.polling.max_concurrency}

        # Discover jobs
        jobs = self.discovery.discover_all()
        logger.info(f"Discovered {len(jobs)} jobs")

        if not jobs:
            logger.info("No jobs to process")
            return {"status": "no_jobs"}

        # Process jobs up to available slots using os.fork() for concurrency
        # Each forked process runs independently with its own context
        results = []
        for job in jobs[:available_slots]:
            try:
                # For mention jobs, mark comment as processed first
                if job.comment:
                    run_id = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-preview"
                    if not self.discovery.mark_comment_processed(job, run_id):
                        logger.info(f"Comment {job.comment.id} already processed, skipping")
                        continue

                # Use execute_async which forks and runs in background
                # This creates the DB record, forks, and returns immediately
                run_id = self.worker.execute_async(job)
                results.append({
                    "job": f"{job.repo}#{job.target_number}",
                    "run_id": run_id,
                    "status": "started",
                })
            except Exception as e:
                logger.exception(f"Failed to execute job {job.repo}#{job.target_number}: {e}")
                results.append({
                    "job": f"{job.repo}#{job.target_number}",
                    "status": "failed",
                    "error": str(e),
                })

        logger.info(f"Tick complete: {len(results)} jobs processed")
        return {"status": "ok", "jobs": results}

    def run_single(
        self, repo: str, number: int, comment_id: Optional[int] = None, run_id: Optional[str] = None
    ) -> str:
        """Execute a single job manually or as spawned worker."""
        logger.info(f"Run for {repo}#{number}" + (f" (run_id={run_id})" if run_id else " (manual)"))

        # Get issue details
        issue = self.github.get_issue(repo, number)

        # Determine job type
        if comment_id:
            # Find the comment
            comments = self.github.get_issue_comments(repo, number)
            comment = next((c for c in comments if c.id == comment_id), None)
            if not comment:
                raise ValueError(f"Comment {comment_id} not found")

            job_type = JobType.MENTION_PR if issue.is_pr else JobType.MENTION_ISSUE
            job = Job(
                repo=repo,
                target_number=number,
                job_type=job_type,
                issue=issue,
                comment=comment,
            )

            # Mark comment as processed (only for manual runs without existing run_id)
            if not run_id:
                run_id_preview = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-manual"
                self.discovery.mark_comment_processed(job, run_id_preview)
        else:
            job = Job(
                repo=repo,
                target_number=number,
                job_type=JobType.ISSUE_READY,
                issue=issue,
            )

        return self.worker.execute(job, run_id=run_id)

    def reap(self) -> dict:
        """Detect dead PIDs and mark stale runs."""
        logger.info("Starting reap")

        results = {
            "dead_pids": [],
            "stale_runs": [],
        }

        # Check running runs for dead PIDs
        running_runs = self.db.get_running_runs()
        for run in running_runs:
            if run.pid and not is_pid_alive(run.pid):
                logger.warning(f"Run {run.run_id} has dead PID {run.pid}")
                self.db.update_run_status(
                    run.run_id,
                    RunStatus.FAILED,
                    error=f"Process {run.pid} died unexpectedly",
                )
                results["dead_pids"].append(run.run_id)

                # Try to release GitHub claim
                try:
                    self.github.remove_label(run.repo, run.target_number, self.config.labels.in_progress)
                    login = self.github.get_authenticated_user()
                    self.github.unassign_issue(run.repo, run.target_number, login)
                except Exception as e:
                    logger.warning(f"Failed to release GitHub claim for {run.run_id}: {e}")

        # Check for stale runs
        stale_runs = self.db.get_stale_runs(self.config.timeouts.stale_run_minutes)
        for run in stale_runs:
            if run.run_id not in results["dead_pids"]:  # Don't double-count
                logger.warning(f"Run {run.run_id} is stale (started at {run.started_at})")
                self.db.update_run_status(
                    run.run_id,
                    RunStatus.FAILED,
                    error=f"Run timed out after {self.config.timeouts.stale_run_minutes} minutes",
                )
                results["stale_runs"].append(run.run_id)

                # Try to kill the process if it's still alive
                if run.pid and is_pid_alive(run.pid):
                    try:
                        os.kill(run.pid, 9)
                    except Exception:
                        pass

                # Release GitHub claim
                try:
                    self.github.remove_label(run.repo, run.target_number, self.config.labels.in_progress)
                    login = self.github.get_authenticated_user()
                    self.github.unassign_issue(run.repo, run.target_number, login)
                except Exception as e:
                    logger.warning(f"Failed to release GitHub claim for {run.run_id}: {e}")

        # Clean up old workspaces
        self.workspace_manager.cleanup_old_workspaces()

        logger.info(f"Reap complete: {len(results['dead_pids'])} dead PIDs, {len(results['stale_runs'])} stale runs")
        return results

    def _get_available_slots(self) -> int:
        """Calculate available concurrency slots."""
        running_runs = self.db.get_running_runs()

        # Filter to only runs with alive PIDs
        active_count = sum(1 for run in running_runs if run.pid and is_pid_alive(run.pid))

        return self.config.polling.max_concurrency - active_count

    def status(self) -> dict:
        """Get current status."""
        running_runs = self.db.get_running_runs()
        active_runs = self.db.get_active_runs()

        return {
            "config": {
                "repos": self.config.repos,
                "max_concurrency": self.config.polling.max_concurrency,
            },
            "running": len(running_runs),
            "active": len(active_runs),
            "available_slots": self._get_available_slots(),
            "runs": [
                {
                    "run_id": r.run_id,
                    "repo": r.repo,
                    "target": r.target_number,
                    "status": r.status.value,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                }
                for r in active_runs
            ],
        }


class RunningJob:
    """Tracks a running subprocess."""

    def __init__(self, run_id: str, job: Job, process: subprocess.Popen, log_file: str):
        self.run_id = run_id
        self.job = job
        self.process = process
        self.log_file = log_file
        self.started_at = datetime.utcnow()


class Daemon:
    """Long-running daemon that manages job execution via subprocesses.

    Unlike the tick-based Runner which uses os.fork(), this daemon:
    - Runs continuously in a single process
    - Spawns jobs as separate `cgr run` subprocess calls
    - Tracks running processes in memory
    - Properly inherits the process environment to Claude Code subprocesses
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.config = Config.load(config_path)
        self.db = Database(self.config.paths.db_path)
        self.github = GitHub()
        self.workspace_manager = Workspace(self.config)
        self.discovery = Discovery(self.config, self.db, self.github)

        # In-memory tracking of running processes
        self.running_jobs: Dict[str, RunningJob] = {}

        # Shutdown flag
        self._shutdown = False

    def run(self, poll_interval: Optional[int] = None):
        """Main daemon loop."""
        interval = poll_interval or self.config.polling.interval_seconds

        logger.info(f"Starting daemon with {interval}s poll interval, max_concurrency={self.config.polling.max_concurrency}")

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        while not self._shutdown:
            try:
                self._tick()
            except Exception as e:
                logger.exception(f"Error in daemon tick: {e}")

            # Sleep in small increments to allow responsive shutdown
            for _ in range(interval):
                if self._shutdown:
                    break
                time.sleep(1)

        logger.info("Daemon shutting down, waiting for running jobs...")
        self._wait_for_jobs(timeout=60)
        logger.info("Daemon stopped")

    def _handle_shutdown(self, signum, frame):
        """Handle shutdown signal."""
        logger.info(f"Received signal {signum}, initiating shutdown")
        self._shutdown = True

    def _tick(self):
        """Execute one scheduling cycle."""
        logger.debug("Daemon tick starting")

        # First, reap completed processes
        self._reap_completed()

        # Check for stale runs in DB (from previous daemon instances)
        self._cleanup_stale_runs()

        # Calculate available slots
        available_slots = self.config.polling.max_concurrency - len(self.running_jobs)
        logger.debug(f"Available slots: {available_slots}, running: {len(self.running_jobs)}")

        if available_slots <= 0:
            return

        # Discover new jobs
        jobs = self.discovery.discover_all()
        logger.debug(f"Discovered {len(jobs)} jobs")

        if not jobs:
            return

        # Start jobs up to available slots
        for job in jobs[:available_slots]:
            try:
                self._start_job(job)
            except Exception as e:
                logger.exception(f"Failed to start job {job.repo}#{job.target_number}: {e}")

    def _start_job(self, job: Job):
        """Start a job as a subprocess."""
        run_id = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

        logger.info(f"Starting job {job.repo}#{job.target_number} as run {run_id}")

        # For mention jobs, mark comment as processed first
        if job.comment:
            if not self.discovery.mark_comment_processed(job, run_id):
                logger.info(f"Comment {job.comment.id} already processed, skipping")
                return

        # Create run record in DB
        run = Run(
            run_id=run_id,
            repo=job.repo,
            target_number=job.target_number,
            job_type=job.job_type,
            status=RunStatus.QUEUED,
        )

        if not self.db.create_run(run):
            logger.error(f"Failed to create run: active run exists for {job.repo}#{job.target_number}")
            return

        # Build the cgr run command
        cmd = [
            sys.executable, "-m", "claude_github_runner",
            "run",
            "--repo", job.repo,
            "--number", str(job.target_number),
            "--run-id", run_id,
        ]

        if job.comment:
            cmd.extend(["--comment-id", str(job.comment.id)])

        if self.config_path:
            cmd.extend(["--config", self.config_path])

        # Create workspace directory for logs
        workspace_root = Path(self.config.paths.workspace_root)
        run_dir = workspace_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        log_file = run_dir / "daemon_spawn.log"

        # Spawn subprocess - this inherits full environment including TTY context
        logger.info(f"Spawning: {' '.join(cmd)}")

        with open(log_file, "w") as lf:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                # Don't set cwd - let the run command handle workspace setup
            )

        # Update DB with PID
        self.db.update_run_status(run_id, RunStatus.CLAIMED, pid=process.pid)

        # Track in memory
        self.running_jobs[run_id] = RunningJob(
            run_id=run_id,
            job=job,
            process=process,
            log_file=str(log_file),
        )

        logger.info(f"Started run {run_id} with PID {process.pid}")

    def _reap_completed(self):
        """Check for and handle completed processes."""
        completed = []

        for run_id, running_job in self.running_jobs.items():
            returncode = running_job.process.poll()

            if returncode is not None:
                logger.info(f"Run {run_id} completed with code {returncode}")
                completed.append(run_id)

                # The run command handles its own DB updates and GitHub comments,
                # so we just need to clean up our tracking
                if returncode != 0:
                    # Check if DB was updated by the run command
                    run = self.db.get_run(run_id)
                    if run and run.status == RunStatus.RUNNING:
                        # Run command crashed before updating status
                        self.db.update_run_status(
                            run_id,
                            RunStatus.FAILED,
                            error=f"Worker process exited with code {returncode}"
                        )
                        self._release_github_claim(running_job.job)

        for run_id in completed:
            del self.running_jobs[run_id]

    def _cleanup_stale_runs(self):
        """Clean up stale runs from DB (from previous daemon instances)."""
        stale_runs = self.db.get_stale_runs(self.config.timeouts.stale_run_minutes)

        for run in stale_runs:
            # Skip if we're tracking this run
            if run.run_id in self.running_jobs:
                continue

            # Check if PID is alive (could be from previous daemon)
            if run.pid and is_pid_alive(run.pid):
                # Try to kill it
                try:
                    os.kill(run.pid, signal.SIGTERM)
                    time.sleep(1)
                    if is_pid_alive(run.pid):
                        os.kill(run.pid, signal.SIGKILL)
                except Exception:
                    pass

            logger.warning(f"Marking stale run {run.run_id} as failed")
            self.db.update_run_status(
                run.run_id,
                RunStatus.FAILED,
                error=f"Run timed out after {self.config.timeouts.stale_run_minutes} minutes"
            )

            # Release GitHub claim
            try:
                self.github.remove_label(run.repo, run.target_number, self.config.labels.in_progress)
                login = self.github.get_authenticated_user()
                self.github.unassign_issue(run.repo, run.target_number, login)
            except Exception as e:
                logger.warning(f"Failed to release GitHub claim for {run.run_id}: {e}")

    def _release_github_claim(self, job: Job):
        """Release GitHub claim for a job."""
        try:
            self.github.remove_label(job.repo, job.target_number, self.config.labels.in_progress)
            login = self.github.get_authenticated_user()
            self.github.unassign_issue(job.repo, job.target_number, login)
        except Exception as e:
            logger.warning(f"Failed to release GitHub claim for {job.repo}#{job.target_number}: {e}")

    def _wait_for_jobs(self, timeout: int = 60):
        """Wait for running jobs to complete during shutdown."""
        if not self.running_jobs:
            return

        logger.info(f"Waiting for {len(self.running_jobs)} running jobs (timeout={timeout}s)")

        deadline = time.time() + timeout

        while self.running_jobs and time.time() < deadline:
            self._reap_completed()
            if self.running_jobs:
                time.sleep(1)

        # Force kill any remaining
        for run_id, running_job in list(self.running_jobs.items()):
            logger.warning(f"Force killing run {run_id}")
            running_job.process.kill()
            running_job.process.wait()


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="claude-github-runner",
        description="Headless GitHub automation service powered by Claude",
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to config file",
        default=None,
    )
    parser.add_argument(
        "-v", "--verbose",
        help="Enable verbose logging",
        action="store_true",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # tick command
    tick_parser = subparsers.add_parser(
        "tick",
        help="Run one scheduling cycle",
    )

    # run command
    run_parser = subparsers.add_parser(
        "run",
        help="Execute a single job",
    )
    run_parser.add_argument(
        "--repo",
        required=True,
        help="Repository (owner/name)",
    )
    run_parser.add_argument(
        "--number",
        required=True,
        type=int,
        help="Issue or PR number",
    )
    run_parser.add_argument(
        "--comment-id",
        type=int,
        help="Comment ID for mention jobs",
    )
    run_parser.add_argument(
        "--run-id",
        help="Existing run ID (created by tick before spawning)",
    )

    # reap command
    reap_parser = subparsers.add_parser(
        "reap",
        help="Clean up dead/stale runs",
    )

    # status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show current status",
    )

    # ui command
    ui_parser = subparsers.add_parser(
        "ui",
        help="Launch the terminal UI",
    )
    ui_parser.add_argument(
        "--db",
        help="Path to runner.sqlite",
    )
    ui_parser.add_argument(
        "--workspace-root",
        help="Path to workspace root",
    )
    ui_parser.add_argument(
        "--service-name",
        default="claude-github-runner",
        help="Systemd service name",
    )
    ui_parser.add_argument(
        "--timer-name",
        default="claude-github-runner.timer",
        help="Systemd timer name",
    )

    # daemon command
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run as a long-lived daemon (recommended over tick)",
    )
    daemon_parser.add_argument(
        "--poll-interval",
        type=int,
        help="Polling interval in seconds (default from config)",
    )

    args = parser.parse_args()

    # UI command doesn't need runner initialization
    if args.command == "ui":
        try:
            from .ui import RunnerUI

            # Get paths from config if available, then override with CLI args
            config = Config.load(args.config)

            db_path = args.db or config.paths.db_path
            workspace_root = args.workspace_root or config.paths.workspace_root

            app = RunnerUI(
                db_path=db_path,
                workspace_root=workspace_root,
                service_name=args.service_name,
                timer_name=args.timer_name,
            )
            app.run()
        except ImportError as e:
            print(f"Error: UI requires 'textual' package. Install with: pip install textual")
            print(f"Details: {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(0)
        return

    # Daemon command runs standalone
    if args.command == "daemon":
        setup_logging(args.verbose)
        try:
            daemon = Daemon(args.config)
            daemon.run(poll_interval=args.poll_interval)
        except KeyboardInterrupt:
            logger.info("Interrupted")
            sys.exit(130)
        except Exception as e:
            logger.exception(f"Fatal error: {e}")
            sys.exit(1)
        return

    setup_logging(args.verbose)

    try:
        runner = Runner(args.config)

        if args.command == "tick":
            result = runner.tick()
            print(f"Tick result: {result}")

        elif args.command == "run":
            run_id = runner.run_single(args.repo, args.number, args.comment_id, args.run_id)
            print(f"Run completed: {run_id}")

        elif args.command == "reap":
            result = runner.reap()
            print(f"Reap result: {result}")

        elif args.command == "status":
            result = runner.status()
            import json
            print(json.dumps(result, indent=2))

    except KeyboardInterrupt:
        logger.info("Interrupted")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
