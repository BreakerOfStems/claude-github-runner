"""Database access for the UI."""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .models import RunInfo


def is_pid_alive(pid: int) -> bool:
    """Check if a process is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class RunnerDB:
    """Read-only access to the runner database."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        """Create a connection."""
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_runs(
        self,
        status_filter: Optional[str] = None,
        limit: int = 100,
    ) -> list[RunInfo]:
        """Get runs from the database."""
        try:
            conn = self._connect()
        except FileNotFoundError:
            return []

        try:
            query = "SELECT * FROM runs"
            params = []

            if status_filter == "active":
                query += " WHERE status IN ('queued', 'claimed', 'running')"
            elif status_filter == "failed":
                query += " WHERE status = 'failed'"
            elif status_filter == "needs-human":
                query += " WHERE status = 'needs-human'"

            query += " ORDER BY started_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()

            runs = []
            for row in rows:
                runs.append(RunInfo(
                    run_id=row["run_id"],
                    repo=row["repo"],
                    target_number=row["target_number"],
                    job_type=row["job_type"],
                    status=row["status"],
                    pid=row["pid"],
                    branch=row["branch"],
                    pr_url=row["pr_url"],
                    started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
                    ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
                    error=row["error"],
                ))

            return runs
        finally:
            conn.close()

    def get_run(self, run_id: str) -> Optional[RunInfo]:
        """Get a single run by ID."""
        try:
            conn = self._connect()
        except FileNotFoundError:
            return None

        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,)
            ).fetchone()

            if not row:
                return None

            return RunInfo(
                run_id=row["run_id"],
                repo=row["repo"],
                target_number=row["target_number"],
                job_type=row["job_type"],
                status=row["status"],
                pid=row["pid"],
                branch=row["branch"],
                pr_url=row["pr_url"],
                started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
                ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
                error=row["error"],
            )
        finally:
            conn.close()

    def get_active_count(self) -> int:
        """Get count of active runs with alive PIDs."""
        try:
            conn = self._connect()
        except FileNotFoundError:
            return 0

        try:
            rows = conn.execute(
                "SELECT pid FROM runs WHERE status = 'running'"
            ).fetchall()

            return sum(1 for row in rows if row["pid"] and is_pid_alive(row["pid"]))
        finally:
            conn.close()

    def is_available(self) -> bool:
        """Check if database is accessible."""
        try:
            self._connect().close()
            return True
        except Exception:
            return False
