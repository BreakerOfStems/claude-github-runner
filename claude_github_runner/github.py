"""GitHub API interactions via gh CLI."""

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    assignees: list[str]
    created_at: datetime
    url: str
    is_pr: bool = False


@dataclass
class Comment:
    id: int
    body: str
    author: str
    url: str
    created_at: datetime


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    url: str
    head_branch: str
    base_branch: str


class GitHubError(Exception):
    """Error interacting with GitHub."""
    pass


class GitHub:
    """GitHub API wrapper using gh CLI."""

    def __init__(self):
        self._login: Optional[str] = None

    def _run_gh(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        """Run a gh command."""
        cmd = ["gh"] + args
        logger.debug(f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.error(f"gh command failed: {e.stderr}")
            raise GitHubError(f"gh command failed: {e.stderr}") from e

    def get_authenticated_user(self) -> str:
        """Get the authenticated user's login."""
        if self._login is None:
            result = self._run_gh(["api", "user", "-q", ".login"])
            self._login = result.stdout.strip()
        return self._login

    def search_ready_issues(
        self,
        repo: str,
        ready_label: str,
        in_progress_label: str,
        blocked_labels: list[str],
        oldest_first: bool = True,
    ) -> list[Issue]:
        """Search for ready issues in a repository."""
        # Build search query
        blocked_parts = " ".join(f"-label:{label}" for label in blocked_labels)
        query = f"repo:{repo} is:issue is:open label:{ready_label} no:assignee -label:{in_progress_label} {blocked_parts}"

        sort_order = "created-asc" if oldest_first else "created-desc"

        result = self._run_gh([
            "search", "issues",
            "--json", "number,title,body,labels,assignees,createdAt,url",
            "--sort", "created",
            "--order", "asc" if oldest_first else "desc",
            "--limit", "100",
            "--", query
        ])

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse search results: {result.stdout}")
            return []

        issues = []
        for item in data:
            issues.append(Issue(
                number=item["number"],
                title=item["title"],
                body=item.get("body") or "",
                labels=[l["name"] for l in item.get("labels", [])],
                assignees=[a["login"] for a in item.get("assignees", [])],
                created_at=datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")),
                url=item["url"],
            ))

        return issues

    def search_mention_candidates(
        self,
        repo: str,
        login: str,
        since: Optional[datetime] = None,
    ) -> list[int]:
        """Search for issues/PRs that mention the user since last poll."""
        # Build search query
        query = f"repo:{repo} mentions:{login}"
        if since:
            # GitHub search uses YYYY-MM-DD format
            query += f" updated:>{since.strftime('%Y-%m-%dT%H:%M:%S')}"

        result = self._run_gh([
            "search", "issues",
            "--json", "number",
            "--limit", "100",
            "--", query
        ])

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
            return [item["number"] for item in data]
        except json.JSONDecodeError:
            logger.error(f"Failed to parse mention search results: {result.stdout}")
            return []

    def get_issue(self, repo: str, number: int) -> Issue:
        """Get a single issue or PR."""
        result = self._run_gh([
            "api",
            f"repos/{repo}/issues/{number}",
        ])

        data = json.loads(result.stdout)

        return Issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            labels=[l["name"] for l in data.get("labels", [])],
            assignees=[a["login"] for a in data.get("assignees", [])],
            created_at=datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")),
            url=data["html_url"],
            is_pr="pull_request" in data,
        )

    def get_issue_comments(self, repo: str, number: int) -> list[Comment]:
        """Get comments on an issue or PR."""
        result = self._run_gh([
            "api",
            f"repos/{repo}/issues/{number}/comments",
            "--paginate",
        ])

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse comments: {result.stdout}")
            return []

        comments = []
        for item in data:
            comments.append(Comment(
                id=item["id"],
                body=item.get("body") or "",
                author=item["user"]["login"],
                url=item["html_url"],
                created_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
            ))

        return comments

    def add_label(self, repo: str, number: int, label: str):
        """Add a label to an issue or PR."""
        self._run_gh([
            "api",
            "-X", "POST",
            f"repos/{repo}/issues/{number}/labels",
            "-f", f"labels[]={label}",
        ])

    def remove_label(self, repo: str, number: int, label: str):
        """Remove a label from an issue or PR."""
        # URL encode the label name for the path
        encoded_label = label.replace(" ", "%20")
        self._run_gh([
            "api",
            "-X", "DELETE",
            f"repos/{repo}/issues/{number}/labels/{encoded_label}",
        ], check=False)  # Don't fail if label doesn't exist

    def assign_issue(self, repo: str, number: int, assignee: str):
        """Assign a user to an issue or PR."""
        self._run_gh([
            "api",
            "-X", "POST",
            f"repos/{repo}/issues/{number}/assignees",
            "-f", f"assignees[]={assignee}",
        ])

    def unassign_issue(self, repo: str, number: int, assignee: str):
        """Remove a user from an issue or PR."""
        self._run_gh([
            "api",
            "-X", "DELETE",
            f"repos/{repo}/issues/{number}/assignees",
            "-f", f"assignees[]={assignee}",
        ], check=False)

    def create_comment(self, repo: str, number: int, body: str):
        """Create a comment on an issue or PR."""
        self._run_gh([
            "api",
            "-X", "POST",
            f"repos/{repo}/issues/{number}/comments",
            "-f", f"body={body}",
        ])

    def clone_repo(self, repo: str, target_dir: str):
        """Clone a repository."""
        self._run_gh([
            "repo", "clone",
            repo,
            target_dir,
        ])

    def create_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
        working_dir: str,
    ) -> str:
        """Create a pull request. Returns the PR URL."""
        result = subprocess.run(
            [
                "gh", "pr", "create",
                "--repo", repo,
                "--title", title,
                "--body", body,
                "--head", head_branch,
                "--base", base_branch,
            ],
            capture_output=True,
            text=True,
            cwd=working_dir,
        )

        if result.returncode != 0:
            raise GitHubError(f"Failed to create PR: {result.stderr}")

        return result.stdout.strip()

    def get_pr_for_branch(self, repo: str, branch: str) -> Optional[PullRequest]:
        """Get PR for a branch if it exists."""
        result = self._run_gh([
            "pr", "list",
            "--repo", repo,
            "--head", branch,
            "--json", "number,title,body,url,headRefName,baseRefName",
            "--limit", "1",
        ], check=False)

        if result.returncode != 0 or not result.stdout.strip():
            return None

        try:
            data = json.loads(result.stdout)
            if not data:
                return None

            item = data[0]
            return PullRequest(
                number=item["number"],
                title=item["title"],
                body=item.get("body") or "",
                url=item["url"],
                head_branch=item["headRefName"],
                base_branch=item["baseRefName"],
            )
        except (json.JSONDecodeError, KeyError, IndexError):
            return None

    def update_pr(self, repo: str, number: int, title: Optional[str] = None, body: Optional[str] = None):
        """Update a pull request."""
        args = ["pr", "edit", str(number), "--repo", repo]

        if title:
            args.extend(["--title", title])
        if body:
            args.extend(["--body", body])

        self._run_gh(args)

    def add_pr_comment(self, repo: str, number: int, body: str):
        """Add a comment to a PR."""
        self.create_comment(repo, number, body)
