"""SQLite database layer for Claude GitHub Runner.

This module provides the persistence layer for tracking runs, processed comments,
and polling cursors. It uses SQLite for simplicity and portability.

Schema documentation: docs/DATABASE.md

Key design decisions:
- Uses a partial unique index to enforce one active run per issue/PR
- Timestamps stored as ISO 8601 strings for human readability
- Enum values stored as lowercase strings matching Python enum values
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class RunStatus(Enum):
    """Status values for a run through its lifecycle.

    State transitions:
        QUEUED -> CLAIMED -> RUNNING -> SUCCEEDED | FAILED | NEEDS_HUMAN
    """

    QUEUED = "queued"  # Job created, waiting for available slot
    CLAIMED = "claimed"  # Slot acquired, about to start execution
    RUNNING = "running"  # Claude Code is actively processing
    SUCCEEDED = "succeeded"  # Run completed successfully
    FAILED = "failed"  # Run failed with an error
    NEEDS_HUMAN = "needs-human"  # Run requires human intervention


class JobType(Enum):
    """Types of jobs that can trigger a run."""

    ISSUE_READY = "issue_ready"  # Issue with 'ready' label picked up from queue
    MENTION_ISSUE = "mention_issue"  # Response to @mention in issue comment
    MENTION_PR = "mention_pr"  # Response to @mention in PR comment


@dataclass
class Run:
    """Represents a single execution run.

    Attributes:
        run_id: Unique identifier in format YYYYMMDD-HHMMSS-<random>
        repo: Repository in owner/repo format
        target_number: Issue or PR number being processed
        job_type: Type of job that triggered this run
        status: Current execution status
        pid: Process ID of the forked worker (if running)
        branch: Git branch created for this run (e.g., claude/42-fix-bug)
        pr_url: URL of created/updated pull request
        started_at: When the run started executing
        ended_at: When the run completed
        error: Error message if the run failed
    """

    run_id: str
    repo: str
    target_number: int
    job_type: JobType
    status: RunStatus
    pid: Optional[int] = None
    branch: Optional[str] = None
    pr_url: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class ProcessedComment:
    """Record of a GitHub comment that has been handled.

    Used to prevent duplicate processing of the same @mention.
    """

    repo: str
    target_number: int
    comment_id: int  # GitHub's unique comment ID
    comment_url: str
    comment_author: str
    created_at: datetime
    run_id: str  # ID of the run that processed this comment


@dataclass
class Cursor:
    """Polling cursor for a repository.

    Tracks the last successful poll time to fetch only new comments.
    """

    repo: str
    last_poll_at: datetime


class Database:
    """SQLite database manager for the runner.

    Handles all database operations including schema initialization,
    run management, comment tracking, and cursor updates.

    The schema is automatically created on first connection. See
    docs/DATABASE.md for detailed schema documentation.

    Thread safety: Each method opens its own connection, so the class
    can be used from multiple threads. However, SQLite itself may
    block concurrent writes.
    """

    # Schema definition using CREATE IF NOT EXISTS for idempotent initialization.
    # The partial unique index on runs ensures only one active run per target.
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        target_number INTEGER NOT NULL,
        job_type TEXT NOT NULL,
        status TEXT NOT NULL,
        pid INTEGER,
        branch TEXT,
        pr_url TEXT,
        started_at TEXT,
        ended_at TEXT,
        error TEXT
    );

    -- Partial unique index: ensures only one active run per issue/PR.
    -- This prevents race conditions where multiple workers might try to
    -- process the same issue simultaneously.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_target
    ON runs (repo, target_number)
    WHERE status IN ('queued', 'claimed', 'running');

    -- Tracks processed comments to prevent duplicate handling.
    -- The UNIQUE constraint on comment_id ensures each comment is
    -- processed exactly once, even if seen in multiple poll cycles.
    CREATE TABLE IF NOT EXISTS processed_comments (
        repo TEXT NOT NULL,
        target_number INTEGER NOT NULL,
        comment_id INTEGER NOT NULL UNIQUE,
        comment_url TEXT,
        comment_author TEXT,
        created_at TEXT,
        run_id TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_processed_comments_repo
    ON processed_comments (repo);

    -- Polling cursors: stores last poll timestamp per repository.
    -- Used to fetch only new comments since last check, reducing
    -- API calls and preventing reprocessing.
    CREATE TABLE IF NOT EXISTS cursors (
        repo TEXT PRIMARY KEY,
        last_poll_at TEXT
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)

    @contextmanager
    def _connect(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Run operations

    def create_run(self, run: Run) -> bool:
        """Create a new run record.

        Returns:
            True if the run was created successfully.
            False if an active run already exists for this target (due to
            the partial unique index on active runs).
        """
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO runs (run_id, repo, target_number, job_type, status, pid, branch, pr_url, started_at, ended_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.repo,
                        run.target_number,
                        run.job_type.value,
                        run.status.value,
                        run.pid,
                        run.branch,
                        run.pr_url,
                        run.started_at.isoformat() if run.started_at else None,
                        run.ended_at.isoformat() if run.ended_at else None,
                        run.error,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_run(self, run_id: str) -> Optional[Run]:
        """Get a run by ID."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row:
                return self._row_to_run(row)
        return None

    def get_active_run_for_target(self, repo: str, target_number: int) -> Optional[Run]:
        """Get active run for a specific target."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE repo = ? AND target_number = ?
                AND status IN ('queued', 'claimed', 'running')
                """,
                (repo, target_number),
            ).fetchone()
            if row:
                return self._row_to_run(row)
        return None

    def get_active_runs(self) -> list[Run]:
        """Get all active runs."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status IN ('queued', 'claimed', 'running')"
            ).fetchall()
            return [self._row_to_run(row) for row in rows]

    def get_running_runs(self) -> list[Run]:
        """Get all running runs."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status = 'running'"
            ).fetchall()
            return [self._row_to_run(row) for row in rows]

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        pid: Optional[int] = None,
        branch: Optional[str] = None,
        pr_url: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Update run status and optional fields.

        Automatically sets timestamps:
        - started_at is set when status becomes RUNNING
        - ended_at is set when status becomes SUCCEEDED, FAILED, or NEEDS_HUMAN
        """
        with self._connect() as conn:
            updates = ["status = ?"]
            values = [status.value]

            if pid is not None:
                updates.append("pid = ?")
                values.append(pid)

            if branch is not None:
                updates.append("branch = ?")
                values.append(branch)

            if pr_url is not None:
                updates.append("pr_url = ?")
                values.append(pr_url)

            if error is not None:
                updates.append("error = ?")
                values.append(error)

            if status == RunStatus.RUNNING:
                updates.append("started_at = ?")
                values.append(datetime.utcnow().isoformat())

            if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.NEEDS_HUMAN):
                updates.append("ended_at = ?")
                values.append(datetime.utcnow().isoformat())

            values.append(run_id)
            conn.execute(
                f"UPDATE runs SET {', '.join(updates)} WHERE run_id = ?",
                values,
            )

    def _row_to_run(self, row: sqlite3.Row) -> Run:
        """Convert a database row to a Run object."""
        return Run(
            run_id=row["run_id"],
            repo=row["repo"],
            target_number=row["target_number"],
            job_type=JobType(row["job_type"]),
            status=RunStatus(row["status"]),
            pid=row["pid"],
            branch=row["branch"],
            pr_url=row["pr_url"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            error=row["error"],
        )

    # Processed comments operations
    def is_comment_processed(self, comment_id: int) -> bool:
        """Check if a comment has been processed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_comments WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()
            return row is not None

    def mark_comment_processed(self, comment: ProcessedComment) -> bool:
        """Mark a comment as processed. Returns False if already processed."""
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO processed_comments (repo, target_number, comment_id, comment_url, comment_author, created_at, run_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        comment.repo,
                        comment.target_number,
                        comment.comment_id,
                        comment.comment_url,
                        comment.comment_author,
                        comment.created_at.isoformat(),
                        comment.run_id,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # Cursor operations

    def get_cursor(self, repo: str) -> Optional[datetime]:
        """Get the last poll time for a repo.

        Returns None if the repo has never been polled.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_poll_at FROM cursors WHERE repo = ?", (repo,)
            ).fetchone()
            if row and row["last_poll_at"]:
                return datetime.fromisoformat(row["last_poll_at"])
        return None

    def update_cursor(self, repo: str, last_poll_at: datetime):
        """Update the last poll time for a repo.

        Uses SQLite's ON CONFLICT clause for atomic upsert behavior.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cursors (repo, last_poll_at) VALUES (?, ?)
                ON CONFLICT(repo) DO UPDATE SET last_poll_at = ?
                """,
                (repo, last_poll_at.isoformat(), last_poll_at.isoformat()),
            )

    # Maintenance operations

    def get_stale_runs(self, stale_minutes: int) -> list[Run]:
        """Get runs that have been running longer than stale_minutes.

        Used by the reaper to identify potentially stuck runs that may
        need to be marked as failed.
        """
        cutoff = datetime.utcnow()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE status = 'running'
                AND started_at < datetime('now', '-' || ? || ' minutes')
                """,
                (stale_minutes,),
            ).fetchall()
            return [self._row_to_run(row) for row in rows]
