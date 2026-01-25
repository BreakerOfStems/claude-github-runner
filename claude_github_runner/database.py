"""SQLite database layer for Claude GitHub Runner.

This module provides the persistence layer for tracking runs, processed comments,
and polling cursors. It uses SQLite for simplicity and portability.

Schema documentation: docs/DATABASE.md

Key design decisions:
- Uses a partial unique index to enforce one active run per issue/PR
- Timestamps stored as ISO 8601 strings for human readability
- Enum values stored as lowercase strings matching Python enum values
"""

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
    stderr_output: Optional[str] = None


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


class ConnectionPool:
    """Thread-local connection pool for SQLite.

    Each thread gets its own connection that is reused across operations.
    This avoids opening/closing connections for every database operation
    while maintaining thread safety (SQLite connections should not be shared
    across threads).

    The pool also tracks the process ID to handle fork() safely - child
    processes automatically get fresh connections.

    When disable_pooling is True, every get_connection() returns a fresh
    connection that must be closed by the caller. This is used in forked
    child processes to avoid any potential issues with inherited SQLite state.
    """

    def __init__(self, db_path: str, disable_pooling: bool = False):
        self.db_path = db_path
        self._local = threading.local()
        self._pid = os.getpid()
        self._disable_pooling = disable_pooling

    def get_connection(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread.

        Returns an existing connection if one exists for this thread,
        otherwise creates a new one. Also handles process fork by
        creating new connections in child processes.

        If pooling is disabled, always returns a fresh connection.
        """
        # Check if we've forked - child processes need fresh connections
        current_pid = os.getpid()
        if current_pid != self._pid:
            # We're in a forked child process - reset the thread local
            self._local = threading.local()
            self._pid = current_pid

        # If pooling is disabled, always create a fresh connection
        if self._disable_pooling:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            return conn

        if not hasattr(self._local, 'connection') or self._local.connection is None:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent access
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.connection = conn

        return self._local.connection

    def close_connection(self):
        """Close the connection for the current thread if it exists."""
        if hasattr(self._local, 'connection') and self._local.connection is not None:
            try:
                self._local.connection.close()
            except Exception:
                pass
            self._local.connection = None

    def close_all(self):
        """Close connection in the current thread.

        Note: This only closes the connection in the calling thread.
        Thread-local connections in other threads will be closed when
        those threads exit or call close_connection().
        """
        self.close_connection()


class Database:
    """SQLite database manager for the runner.

    Uses connection pooling with thread-local storage to efficiently
    reuse connections while maintaining thread safety.

    Handles all database operations including schema initialization,
    run management, comment tracking, and cursor updates.

    The schema is automatically created on first connection. See
    docs/DATABASE.md for detailed schema documentation.

    Thread safety: Each method opens its own connection, so the class
    can be used from multiple threads. However, SQLite itself may
    block concurrent writes.

    Fork safety: When forked_child=True, connection pooling is disabled
    to ensure each database operation uses a fresh connection. This
    avoids potential issues with SQLite's internal state being inherited
    across fork boundaries.
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
        error TEXT,
        stderr_output TEXT
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

    -- Session tracking: stores Claude session IDs per repository.
    -- Used for session resumption to maintain context across runs,
    -- reducing startup time and improving run quality.
    CREATE TABLE IF NOT EXISTS sessions (
        repo TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str, forked_child: bool = False):
        self.db_path = db_path
        self._forked_child = forked_child
        # Disable connection pooling in forked child processes to ensure
        # each operation uses a fresh connection with no inherited state
        self._pool = ConnectionPool(db_path, disable_pooling=forked_child)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema."""
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn):
        """Run database migrations for schema updates."""
        # Check if stderr_output column exists
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = {row[1] for row in cursor.fetchall()}
        if "stderr_output" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN stderr_output TEXT")

    @contextmanager
    def _connect(self):
        """Context manager for database connections.

        Uses pooled connections - the same connection is reused for
        multiple operations within the same thread. This significantly
        reduces overhead when max_concurrency > 1.

        When running in a forked child process (forked_child=True), pooling
        is disabled and each operation uses a fresh connection that is
        closed after use. This ensures reliable writes after fork.
        """
        conn = self._pool.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            # When pooling is disabled (forked child), close connection after use
            if self._forked_child:
                try:
                    conn.close()
                except Exception:
                    pass

    def close(self):
        """Close the pooled connection for the current thread.

        Call this when done with database operations in the current thread,
        especially in forked child processes before exiting.
        """
        self._pool.close_connection()

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
                    INSERT INTO runs (run_id, repo, target_number, job_type, status, pid, branch, pr_url, started_at, ended_at, error, stderr_output)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        run.stderr_output,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def claim_job(self, run_id: str, repo: str, target_number: int, job_type: JobType) -> bool:
        """Atomically claim a job by creating a run record.

        This is the safe way to claim a job - it uses the database's unique index
        on (repo, target_number) for active runs to prevent race conditions.
        Two workers calling this for the same target will have only one succeed.

        Returns True if the job was claimed, False if another run already exists.
        """
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO runs (run_id, repo, target_number, job_type, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        repo,
                        target_number,
                        job_type.value,
                        RunStatus.QUEUED.value,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                # Another run already exists for this target
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
        stderr_output: Optional[str] = None,
    ):
        """Update run status and optional fields.

        Automatically sets timestamps:
        - started_at is set when status becomes RUNNING
        - ended_at is set when status becomes SUCCEEDED, FAILED, or NEEDS_HUMAN

        When running in a forked child process, logs debug information to
        help diagnose any persistence issues.
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

            if stderr_output is not None:
                updates.append("stderr_output = ?")
                values.append(stderr_output)

            if status == RunStatus.RUNNING:
                updates.append("started_at = ?")
                values.append(datetime.utcnow().isoformat())

            if status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.NEEDS_HUMAN):
                updates.append("ended_at = ?")
                values.append(datetime.utcnow().isoformat())

            values.append(run_id)
            cursor = conn.execute(
                f"UPDATE runs SET {', '.join(updates)} WHERE run_id = ?",
                values,
            )
            rows_affected = cursor.rowcount

            # Log status updates in forked child processes for debugging
            if self._forked_child:
                logger.debug(
                    f"Database update: run_id={run_id}, status={status.value}, "
                    f"rows_affected={rows_affected}, pid={os.getpid()}"
                )

    def verify_run_status(self, run_id: str, expected_status: RunStatus) -> bool:
        """Verify that a run has the expected status.

        This method is useful for verifying that database writes succeeded,
        especially in forked child processes where persistence issues have
        been observed. Uses a fresh connection to ensure we're reading the
        actual committed state.

        Returns True if the run exists and has the expected status.
        """
        # Force a fresh connection for verification to ensure we're not
        # reading from any cached state
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            conn.close()

            if row and row["status"] == expected_status.value:
                return True

            if self._forked_child:
                actual_status = row["status"] if row else "NOT_FOUND"
                logger.warning(
                    f"Status verification failed for {run_id}: "
                    f"expected={expected_status.value}, actual={actual_status}"
                )
            return False
        except Exception as e:
            if self._forked_child:
                logger.error(f"Status verification error for {run_id}: {e}")
            return False

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
            stderr_output=row["stderr_output"] if "stderr_output" in row.keys() else None,
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

    # Session operations

    def get_session(self, repo: str) -> Optional[str]:
        """Get the stored session ID for a repo.

        Returns None if no session exists for this repo.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE repo = ?", (repo,)
            ).fetchone()
            if row and row["session_id"]:
                return row["session_id"]
        return None

    def update_session(self, repo: str, session_id: str):
        """Update the session ID for a repo.

        Uses SQLite's ON CONFLICT clause for atomic upsert behavior.
        """
        with self._connect() as conn:
            now = datetime.utcnow().isoformat()
            conn.execute(
                """
                INSERT INTO sessions (repo, session_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(repo) DO UPDATE SET session_id = ?, updated_at = ?
                """,
                (repo, session_id, now, session_id, now),
            )

    def clear_session(self, repo: str):
        """Clear the session ID for a repo.

        Call this if a session becomes invalid (e.g., auth error, corruption).
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE repo = ?", (repo,))

    # Maintenance operations

    def get_stale_runs(self, stale_minutes: int) -> list[Run]:
        """Get runs that have been running longer than stale_minutes.

        Used by the reaper to identify potentially stuck runs that may
        need to be marked as failed.
        """
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

    def get_timed_out_runs(self, timeout_minutes: int) -> list[Run]:
        """Get active runs that have exceeded the timeout duration.

        Used by the timeout reaper to identify runs stuck in any active state
        (queued, claimed, running) for longer than the configured timeout.

        For 'running' status, uses started_at timestamp.
        For 'queued' and 'claimed' status, parses the timestamp from run_id
        (format: YYYYMMDD-HHMMSS-<random>).
        """
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
        timed_out = []

        with self._connect() as conn:
            # Get all active runs
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('queued', 'claimed', 'running')
                """
            ).fetchall()

            for row in rows:
                run = self._row_to_run(row)

                # For running status, check started_at
                if run.status == RunStatus.RUNNING and run.started_at:
                    if run.started_at < cutoff:
                        timed_out.append(run)
                # For queued/claimed, parse timestamp from run_id
                elif run.status in (RunStatus.QUEUED, RunStatus.CLAIMED):
                    try:
                        # run_id format: YYYYMMDD-HHMMSS-<random>
                        timestamp_part = run.run_id[:15]  # YYYYMMDD-HHMMSS
                        created_at = datetime.strptime(timestamp_part, "%Y%m%d-%H%M%S")
                        if created_at < cutoff:
                            timed_out.append(run)
                    except (ValueError, IndexError):
                        # If we can't parse the timestamp, skip this run
                        pass

            return timed_out

    def get_run_by_branch(self, repo: str, branch: str) -> Optional[Run]:
        """Get a run by repo and branch name, useful for idempotency checks."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE repo = ? AND branch = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (repo, branch),
            ).fetchone()
            if row:
                return self._row_to_run(row)
        return None

    def get_run_with_pr_for_target(self, repo: str, target_number: int) -> Optional[Run]:
        """Get the most recent run that created a PR for a target.

        Used for idempotency: if a previous run already created a PR,
        we can detect this and avoid creating duplicates.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE repo = ? AND target_number = ? AND pr_url IS NOT NULL
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (repo, target_number),
            ).fetchone()
            if row:
                return self._row_to_run(row)
        return None
