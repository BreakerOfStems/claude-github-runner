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

    def get_default_branch(self, repo: str) -> str:
        """Get the repository's default branch."""
        result = self._run_gh(["api", f"repos/{repo}", "-q", ".default_branch"])
        return result.stdout.strip()

    def search_ready_issues(
        self,
        repo: str,
        ready_label: str,
        in_progress_label: str,
        blocked_labels: list[str],
        oldest_first: bool = True,
    ) -> list[Issue]:
        """Search for ready issues in a repository."""
        # Use gh search with flags instead of query string (more reliable)
        args = [
            "search", "issues",
            "--repo", repo,
            "--state", "open",
            "--label", ready_label,
            "--json", "number,title,body,labels,assignees,createdAt,url",
            "--sort", "created",
            "--order", "asc" if oldest_first else "desc",
            "--limit", "100",
        ]

        result = self._run_gh(args, check=False)

        if result.returncode != 0:
            logger.warning(f"Issue search failed: {result.stderr}")
            return []

        if not result.stdout.strip():
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse search results: {result.stdout}")
            return []

        # Filter results in Python since gh search doesn't support all filters as flags
        # - must not have in_progress label
        # - must not have any blocked labels
        # - must not have assignees
        issues = []
        for item in data:
            labels = [l["name"] for l in item.get("labels", [])]
            assignees = [a["login"] for a in item.get("assignees", [])]

            # Skip if has in-progress label
            if in_progress_label in labels:
                continue

            # Skip if has any blocked label
            if any(bl in labels for bl in blocked_labels):
                continue

            # Skip if has assignees
            if assignees:
                continue

            issues.append(Issue(
                number=item["number"],
                title=item["title"],
                body=item.get("body") or "",
                labels=labels,
                assignees=assignees,
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
        # Use gh api to search directly - avoids CLI quoting issues
        # GitHub search API: https://docs.github.com/en/rest/search#search-issues-and-pull-requests
        query_parts = [f"repo:{repo}", f"mentions:{login}"]
        if since:
            query_parts.append(f"updated:>{since.strftime('%Y-%m-%dT%H:%M:%S')}")

        query = " ".join(query_parts)

        result = self._run_gh([
            "api", "search/issues",
            "-X", "GET",
            "-f", f"q={query}",
            "-f", "per_page=100",
            "--jq", ".items[].number",
        ], check=False)

        if result.returncode != 0:
            logger.warning(f"Mention search failed: {result.stderr}")
            return []

        if not result.stdout.strip():
            return []

        try:
            # Output is one number per line from jq
            return [int(n) for n in result.stdout.strip().split("\n") if n]
        except ValueError:
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

    def add_label(self, repo: str, number: int, label: str) -> bool:
        """Add a label to an issue or PR.

        Returns True if successful, False otherwise.
        """
        try:
            self._run_gh([
                "api",
                "-X", "POST",
                f"repos/{repo}/issues/{number}/labels",
                "-f", f"labels[]={label}",
            ])
            return True
        except GitHubError as e:
            logger.warning(f"Failed to add label '{label}' to {repo}#{number}: {e}")
            return False

    def remove_label(self, repo: str, number: int, label: str) -> bool:
        """Remove a label from an issue or PR.

        Returns True if successful (or label didn't exist), False on error.
        """
        # URL encode the label name for the path
        encoded_label = label.replace(" ", "%20")
        result = self._run_gh([
            "api",
            "-X", "DELETE",
            f"repos/{repo}/issues/{number}/labels/{encoded_label}",
        ], check=False)  # Don't fail if label doesn't exist

        if result.returncode != 0:
            # 404 is expected if label doesn't exist - treat as success
            if "404" in result.stderr or "Not Found" in result.stderr:
                logger.debug(f"Label '{label}' not found on {repo}#{number} (already removed)")
                return True
            logger.warning(f"Failed to remove label '{label}' from {repo}#{number}: {result.stderr}")
            return False
        return True

    def assign_issue(self, repo: str, number: int, assignee: str) -> bool:
        """Assign a user to an issue or PR.

        Returns True if successful, False otherwise.
        """
        try:
            self._run_gh([
                "api",
                "-X", "POST",
                f"repos/{repo}/issues/{number}/assignees",
                "-f", f"assignees[]={assignee}",
            ])
            return True
        except GitHubError as e:
            logger.warning(f"Failed to assign '{assignee}' to {repo}#{number}: {e}")
            return False

    def unassign_issue(self, repo: str, number: int, assignee: str) -> bool:
        """Remove a user from an issue or PR.

        Returns True if successful, False otherwise.
        """
        result = self._run_gh([
            "api",
            "-X", "DELETE",
            f"repos/{repo}/issues/{number}/assignees",
            "-f", f"assignees[]={assignee}",
        ], check=False)

        if result.returncode != 0:
            logger.warning(f"Failed to unassign '{assignee}' from {repo}#{number}: {result.stderr}")
            return False
        return True

    def create_comment(self, repo: str, number: int, body: str) -> bool:
        """Create a comment on an issue or PR.

        Returns True if successful, False otherwise.
        """
        try:
            self._run_gh([
                "api",
                "-X", "POST",
                f"repos/{repo}/issues/{number}/comments",
                "-f", f"body={body}",
            ])
            return True
        except GitHubError as e:
            logger.warning(f"Failed to create comment on {repo}#{number}: {e}")
            return False

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

    def add_pr_comment(self, repo: str, number: int, body: str) -> bool:
        """Add a comment to a PR."""
        return self.create_comment(repo, number, body)

    def get_issue_labels(self, repo: str, number: int) -> list[str]:
        """Get current labels on an issue or PR.

        Returns list of label names, or empty list on error.
        """
        result = self._run_gh([
            "api",
            f"repos/{repo}/issues/{number}/labels",
            "--jq", ".[].name",
        ], check=False)

        if result.returncode != 0:
            logger.warning(f"Failed to get labels for {repo}#{number}: {result.stderr}")
            return []

        if not result.stdout.strip():
            return []

        return [label.strip() for label in result.stdout.strip().split("\n") if label.strip()]

    def reconcile_labels(
        self,
        repo: str,
        number: int,
        expected_labels: list[str],
        unexpected_labels: list[str],
        max_retries: int = 2,
    ) -> bool:
        """Verify and fix label state on an issue/PR.

        Ensures expected_labels are present and unexpected_labels are removed.
        This is useful for critical state transitions where label consistency matters.

        Returns True if labels are in expected state (or were successfully fixed),
        False if reconciliation failed after retries.
        """
        for attempt in range(max_retries + 1):
            current_labels = self.get_issue_labels(repo, number)

            if not current_labels and attempt == 0:
                # Couldn't fetch labels, but don't fail on first attempt
                logger.warning(f"Could not verify labels on {repo}#{number}, will retry")
                continue

            # Check what's missing or extra
            missing_expected = [l for l in expected_labels if l not in current_labels]
            present_unexpected = [l for l in unexpected_labels if l in current_labels]

            if not missing_expected and not present_unexpected:
                if attempt > 0:
                    logger.info(f"Labels reconciled on {repo}#{number} after {attempt} retries")
                return True

            if attempt == max_retries:
                logger.error(
                    f"Failed to reconcile labels on {repo}#{number} after {max_retries} retries. "
                    f"Missing: {missing_expected}, Unexpected: {present_unexpected}"
                )
                return False

            # Try to fix
            logger.warning(
                f"Label mismatch on {repo}#{number} (attempt {attempt + 1}): "
                f"missing={missing_expected}, unexpected={present_unexpected}"
            )

            for label in missing_expected:
                self.add_label(repo, number, label)

            for label in present_unexpected:
                self.remove_label(repo, number, label)

        return False
