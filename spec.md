Claude Runner Service Spec
0. Goal

A headless service on the Claude VPS that:

Polls GitHub on a schedule.

Starts work on ready issues (not assigned / not in progress).

Starts work on new comments that @mention the authenticated gh user (on issues or PRs).

Executes work in isolated ephemeral workspaces.

Pushes a branch and opens/updates a PR.

Cleans up local workspace after completion.

Enforces a configurable concurrency cap.

Non-goals:

No web UI.

No background daemon required beyond a scheduler (cron/systemd timer).

1. Identity model (no bot username config)

At the beginning of each tick:

Resolve the current authenticated GitHub identity:

login = gh api user -q .login (API is invoked through gh api)

This login is used for:

Discovering mention candidates via mentions:<login>

Matching @<login> in comment bodies

Ignoring comments authored by login to prevent loops

2. Triggers and job types
2.1 Issue queue trigger (standard work)

A job is created for an issue when:

It is open

It has label ready (configurable)

It has no assignee

It does not have label in-progress (configurable)

It does not have any blocked labels (configurable list)

2.2 Mention trigger (feedback / work requests)

A job is created when a new comment contains @<login>:

If comment is on an issue → job targets that issue

If comment is on a PR → job targets that PR
PRs are treated as issues for issue comments and share the same comment endpoints

Mention jobs behave like standard jobs:

new run_id

isolated workspace

concurrency applies

pushes branch and PR updates

3. “Processed comment” tracking (no repeated pickup)
3.1 What to store

Use a persistent SQLite database on the VPS. Store:

repo (string: owner/name)

issue_number (int)

comment_id (int, unique)

comment_url (string)

comment_author (string)

created_at (timestamp)

run_id (string)

3.2 De-dupe rule

A mention-comment is actionable if and only if:

comment body contains @<login>

comment author != <login>

comment_id is not in processed_comments

3.3 Transactional insert-before-run

To avoid double runs (e.g. crash between detect and start):

Insert the processed_comments row inside a transaction before starting the worker.

If insert fails due to uniqueness, skip (already handled).

4. Claiming / locking model
4.1 Local lock (single VPS MVP)

Unique constraint on (repo, issue_number, status in {queued, claimed, running}) or equivalent DB logic to prevent multiple concurrent runs for same target.

4.2 GitHub-side claim (recommended)

On job start (claim):

Add label in-progress to the target issue/PR.

Optionally assign the authenticated user to the issue/PR.

Reason: prevents future scale-out runners from racing and makes status visible in GitHub.

(Labels are managed via Issues endpoints; PRs share these because PRs are issues .)

5. Concurrency
5.1 Rule

available_slots = max_concurrency - active_runs

Where active_runs are DB runs with:

status = running

and pid still alive

If available_slots <= 0: exit tick early.

5.2 Limits

Default max_concurrency = 1

Configurable up to 3 during testing

Future: allow higher if resource validated

6. Workspace isolation model
6.1 Root path

/home/claude/workspace/_runs/<run_id>/

6.2 Contents
<run_id>/
  repo/              # git clone here
  artifacts/
    runner.log
    claude.log
    git.diff
    summary.json

6.3 Cleanup

On success: delete <run_id> directory

On failure: configurable:

either keep for N hours

or archive (tar.gz) then delete

7. Discovery algorithms
7.1 Discover ready issues

Use GitHub issue search syntax (via gh search issues or gh api graphql) .

Query shape per repo:

repo:OWNER/REPO is:issue is:open label:ready no:assignee -label:in-progress -label:blocked

Return ordered by:

oldest first or newest first (configurable)

7.2 Discover mention candidates (fast path)

Use search qualifier:

repo:OWNER/REPO mentions:<login> updated:>last_poll_time

This yields candidate issues/PRs that might contain new mention comments.

7.3 Resolve actionable mention comments (truth source)

For each candidate issue/PR:

List issue comments using Issues comments API endpoint (works for issues and PR issue-comments)

Filter comments whose body contains @<login>

Exclude author == <login>

Exclude those with comment_id already in processed_comments

Notes:

MVP ignores PR “review comments” (inline code review comments). Those are separate endpoints; add later if needed.

8. Job selection priority

Per tick:

Fill slots with mention jobs first (these are “human requested feedback/action”).

Fill remaining slots with ready issues.

Within each category:

deterministic ordering (e.g. oldest created first) to avoid starvation.

9. Worker execution flow (one job)
9.1 Inputs

repo (owner/name)

target_number (issue or PR number)

job_type = issue_ready | mention_issue | mention_pr

For mention jobs: comment_id, comment_body, comment_url

9.2 Steps

Create run_id

Create workspace directories

Clone repo into <run_id>/repo

Checkout default branch (config: main)

Create working branch:

claude/<target_number>-<slug>

Construct task prompt:

issue/PR title + body

if mention: include the mention comment body and URL

include repo rules: no direct push to main, keep diffs minimal, run checks if present

Invoke Claude Code non-interactively

Validate result:

git status not dirty beyond intended

optional: run ./ci.sh or similar if repo contains it (configurable)

Commit:

include #<issue> in commit message when possible

Push branch

Create/update PR:

gh pr create if none exists for branch

otherwise update PR body/comment

Link PR to issue if applicable (so merge can close issue)

Update GitHub labels:

on success: remove in-progress, add done (optional) or leave to human

on needs-human: add needs-human, keep in-progress or swap to blocked based on policy

Record run outcome in DB

Cleanup workspace per policy

9.3 Merge conflict policy

If conflicts occur during rebase/cherry-pick:

Stop

Summarize files + conflict markers

Comment on issue/PR with “needs human” and instructions

Mark run needs-human

Do not attempt “creative” conflict resolutions unless explicitly requested

10. Configuration
10.1 config.yml

Fields:

repos: [ "owner/repo", ... ]

labels:

ready: "ready"

in_progress: "in-progress"

blocked: ["blocked", "needs-info"]

needs_human: "needs-human"

branching:

base_branch: "main"

branch_prefix: "claude"

polling:

interval_seconds: 300

max_concurrency: 1 (max 3)

timeouts:

run_timeout_minutes: 60

stale_run_minutes: 180

paths:

workspace_root: "/home/claude/workspace/_runs"

db_path: "/home/claude/workspace/runner.sqlite"

claude:

command: "claude" (or full path)

non_interactive_args: [...] (your chosen mode)

priorities:

mention_first: true

No bot username field.

11. Database schema (DDL sketch)

Tables:

runs

run_id TEXT PRIMARY KEY

repo TEXT NOT NULL

target_number INTEGER NOT NULL

job_type TEXT NOT NULL

status TEXT NOT NULL (queued|claimed|running|succeeded|failed|needs-human)

pid INTEGER

branch TEXT

pr_url TEXT

started_at TEXT

ended_at TEXT

error TEXT

Unique index:

(repo, target_number) WHERE status IN ('queued','claimed','running')

processed_comments

repo TEXT NOT NULL

target_number INTEGER NOT NULL

comment_id INTEGER NOT NULL UNIQUE

comment_url TEXT

comment_author TEXT

created_at TEXT

run_id TEXT NOT NULL

cursors

repo TEXT PRIMARY KEY

last_poll_at TEXT

12. Scheduler
Recommended: systemd timer

claude-runner.service runs runner tick

claude-runner.timer triggers every N minutes

Logs go to journald + artifacts

Cron is acceptable for MVP; systemd is more observable.

13. CLI surface (runner commands)

runner tick
One scheduling cycle: discover, enqueue, start up to slots, then exit.

runner run --repo owner/repo --number 123 [--comment-id 999]
Execute one job (used internally and for manual runs).

runner reap
Detect dead PIDs, mark stale runs, optionally release labels.

14. Observability

Each run writes:

artifacts/runner.log (all shell commands)

artifacts/claude.log (Claude output)

artifacts/summary.json (structured outcome)

Runner writes a short tick summary to stdout (journald friendly).