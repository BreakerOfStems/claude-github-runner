"""SQLite database layer for Claude GitHub Runner."""

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class RunStatus(Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_HUMAN = "needs-human"


class JobType(Enum):
    ISSUE_READY = "issue_ready"
    MENTION_ISSUE = "mention_issue"
    MENTION_PR = "mention_pr"


@dataclass
class Run:
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
    repo: str
    target_number: int
    comment_id: int
    comment_url: str
    comment_author: str
    created_at: datetime
    run_id: str


@dataclass
class Cursor:
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
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._pid = os.getpid()

    def get_connection(self) -> sqlite3.Connection:
        """Get or create a connection for the current thread.

        Returns an existing connection if one exists for this thread,
        otherwise creates a new one. Also handles process fork by
        creating new connections in child processes.
        """
        # Check if we've forked - child processes need fresh connections
        current_pid = os.getpid()
        if current_pid != self._pid:
            # We're in a forked child process - reset the thread local
            self._local = threading.local()
            self._pid = current_pid

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
    """

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

    CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_target
    ON runs (repo, target_number)
    WHERE status IN ('queued', 'claimed', 'running');

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

    CREATE TABLE IF NOT EXISTS cursors (
        repo TEXT PRIMARY KEY,
        last_poll_at TEXT
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._pool = ConnectionPool(db_path)
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
        """
        conn = self._pool.get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # Note: We don't close the connection here - it stays in the pool
        # for reuse by subsequent operations in this thread

    def close(self):
        """Close the pooled connection for the current thread.

        Call this when done with database operations in the current thread,
        especially in forked child processes before exiting.
        """
        self._pool.close_connection()

    # Run operations
    def create_run(self, run: Run) -> bool:
        """Create a new run. Returns False if active run exists for target."""
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
        """Update run status and optional fields."""
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
        """Get the last poll time for a repo."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_poll_at FROM cursors WHERE repo = ?", (repo,)
            ).fetchone()
            if row and row["last_poll_at"]:
                return datetime.fromisoformat(row["last_poll_at"])
        return None

    def update_cursor(self, repo: str, last_poll_at: datetime):
        """Update the last poll time for a repo."""
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
        """Get runs that have been running longer than stale_minutes."""
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
