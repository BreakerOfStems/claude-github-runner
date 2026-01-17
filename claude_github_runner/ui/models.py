"""Data models for the UI."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class RunInfo:
    """Run information for display."""
    run_id: str
    repo: str
    target_number: int
    job_type: str
    status: str
    pid: Optional[int]
    branch: Optional[str]
    pr_url: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    error: Optional[str]

    @property
    def short_id(self) -> str:
        """Get shortened run ID for display."""
        return self.run_id[:16] if len(self.run_id) > 16 else self.run_id

    @property
    def started_ago(self) -> str:
        """Get relative time since started."""
        if not self.started_at:
            return "—"

        delta = datetime.utcnow() - self.started_at
        seconds = int(delta.total_seconds())

        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            return f"{seconds // 60}m ago"
        elif seconds < 86400:
            return f"{seconds // 3600}h ago"
        else:
            return f"{seconds // 86400}d ago"

    @property
    def issue_url(self) -> str:
        """Compute issue/PR URL."""
        if "/" in self.repo:
            return f"https://github.com/{self.repo}/issues/{self.target_number}"
        return ""

    @property
    def short_pr_url(self) -> str:
        """Get shortened PR URL for display."""
        if not self.pr_url:
            return "—"
        # Extract just the PR number if it's a GitHub URL
        if "/pull/" in self.pr_url:
            return "#" + self.pr_url.split("/pull/")[-1]
        return self.pr_url[:20]
