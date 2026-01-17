"""Discovery algorithms for finding ready issues and mentions."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import Config
from .database import Database, JobType, ProcessedComment
from .github import GitHub, Issue, Comment

logger = logging.getLogger(__name__)


@dataclass
class Job:
    """A job to be processed."""
    repo: str
    target_number: int
    job_type: JobType
    issue: Issue
    comment: Optional[Comment] = None  # For mention jobs

    @property
    def priority_key(self) -> tuple:
        """Key for sorting jobs. Lower = higher priority."""
        # Mention jobs come first (0), then issue jobs (1)
        type_priority = 0 if self.job_type in (JobType.MENTION_ISSUE, JobType.MENTION_PR) else 1
        return (type_priority, self.issue.created_at)


class Discovery:
    """Discovers work items from GitHub."""

    def __init__(self, config: Config, db: Database, github: GitHub):
        self.config = config
        self.db = db
        self.github = github
        self._login: Optional[str] = None

    @property
    def login(self) -> str:
        """Get the authenticated user's login."""
        if self._login is None:
            self._login = self.github.get_authenticated_user()
        return self._login

    def discover_all(self) -> list[Job]:
        """Discover all jobs across configured repos."""
        jobs = []

        for repo in self.config.repos:
            logger.info(f"Discovering jobs in {repo}")

            # Get mention jobs first (higher priority)
            mention_jobs = self._discover_mention_jobs(repo)
            jobs.extend(mention_jobs)

            # Get ready issue jobs
            issue_jobs = self._discover_ready_issues(repo)
            jobs.extend(issue_jobs)

        # Sort by priority
        if self.config.priorities.mention_first:
            jobs.sort(key=lambda j: j.priority_key)

        return jobs

    def _discover_ready_issues(self, repo: str) -> list[Job]:
        """Discover ready issues in a repository."""
        jobs = []

        issues = self.github.search_ready_issues(
            repo=repo,
            ready_label=self.config.labels.ready,
            in_progress_label=self.config.labels.in_progress,
            blocked_labels=self.config.labels.blocked,
            oldest_first=self.config.priorities.oldest_first,
        )

        for issue in issues:
            # Skip if there's an active run for this target
            if self.db.get_active_run_for_target(repo, issue.number):
                logger.debug(f"Skipping {repo}#{issue.number}: active run exists")
                continue

            jobs.append(Job(
                repo=repo,
                target_number=issue.number,
                job_type=JobType.ISSUE_READY,
                issue=issue,
            ))

        logger.info(f"Found {len(jobs)} ready issues in {repo}")
        return jobs

    def _discover_mention_jobs(self, repo: str) -> list[Job]:
        """Discover mention jobs in a repository."""
        jobs = []

        # Get last poll time for this repo
        last_poll = self.db.get_cursor(repo)

        # Search for candidates
        candidates = self.github.search_mention_candidates(
            repo=repo,
            login=self.login,
            since=last_poll,
        )

        logger.debug(f"Found {len(candidates)} mention candidates in {repo}")

        # Process each candidate
        for number in candidates:
            mention_jobs = self._resolve_actionable_mentions(repo, number)
            jobs.extend(mention_jobs)

        # Update cursor
        self.db.update_cursor(repo, datetime.utcnow())

        logger.info(f"Found {len(jobs)} mention jobs in {repo}")
        return jobs

    def _resolve_actionable_mentions(self, repo: str, number: int) -> list[Job]:
        """Resolve actionable mention comments for an issue/PR."""
        jobs = []

        # Get issue details
        try:
            issue = self.github.get_issue(repo, number)
        except Exception as e:
            logger.error(f"Failed to get issue {repo}#{number}: {e}")
            return []

        # Get comments
        comments = self.github.get_issue_comments(repo, number)

        for comment in comments:
            # Check if this comment mentions us
            if f"@{self.login}" not in comment.body:
                continue

            # Skip our own comments
            if comment.author == self.login:
                continue

            # Skip already processed
            if self.db.is_comment_processed(comment.id):
                continue

            # Skip if there's an active run for this target
            if self.db.get_active_run_for_target(repo, number):
                logger.debug(f"Skipping mention on {repo}#{number}: active run exists")
                continue

            job_type = JobType.MENTION_PR if issue.is_pr else JobType.MENTION_ISSUE

            jobs.append(Job(
                repo=repo,
                target_number=number,
                job_type=job_type,
                issue=issue,
                comment=comment,
            ))

        return jobs

    def mark_comment_processed(self, job: Job, run_id: str) -> bool:
        """Mark a mention comment as processed. Returns False if already processed."""
        if job.comment is None:
            return True  # Not a mention job

        processed = ProcessedComment(
            repo=job.repo,
            target_number=job.target_number,
            comment_id=job.comment.id,
            comment_url=job.comment.url,
            comment_author=job.comment.author,
            created_at=datetime.utcnow(),
            run_id=run_id,
        )

        return self.db.mark_comment_processed(processed)
