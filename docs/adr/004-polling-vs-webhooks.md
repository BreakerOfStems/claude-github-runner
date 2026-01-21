# ADR 004: Polling vs Webhooks

## Status

Accepted

## Context

Claude GitHub Runner needs to discover work items from GitHub:
- Issues marked with the "ready" label
- Comments that mention the bot user

There are two primary approaches for discovering these events:
1. **Polling**: Periodically query GitHub API for current state
2. **Webhooks**: Receive push notifications from GitHub when events occur

## Decision

We chose **polling** as the discovery mechanism, implemented via:
- Periodic `tick` command execution (systemd timer or cron)
- Or continuous polling in `daemon` mode with configurable interval

The discovery process:
1. Search for issues with "ready" label, no assignee, not "in-progress"
2. Search for issues/PRs updated recently that mention the bot user
3. Check each mention comment against processed_comments table
4. Filter out items with active runs in the database

Default polling interval is 5 minutes (300 seconds), configurable via `polling.interval_seconds`.

## Consequences

### Positive

- **No public endpoint required**: Works behind NAT, firewalls, and VPNs without exposing any network services
- **Simple deployment**: No web server, no SSL certificates, no domain configuration
- **Self-healing**: If the runner crashes and restarts, it simply resumes polling. No missed webhooks.
- **Stateless discovery**: Each poll is independent; no need to maintain webhook state or handle delivery failures
- **Works with any GitHub plan**: No GitHub App or webhook configuration required
- **Debuggable**: Can manually run `tick` to see exactly what would be discovered
- **Cursor-based efficiency**: Mention search uses `updated:>timestamp` to avoid re-processing old items

### Negative

- **Latency**: Up to `interval_seconds` delay between event and processing (default 5 minutes)
- **API usage**: Consumes GitHub API quota even when nothing has changed
- **Scaling limits**: Frequent polling of many repos could hit rate limits
- **Not real-time**: Cannot support use cases requiring immediate response

### Latency Mitigation

For users needing lower latency:
- Reduce `polling.interval_seconds` (e.g., 60 seconds)
- Use daemon mode for continuous polling without systemd timer overhead
- Accept slightly higher API usage

## Alternatives Considered

### GitHub Webhooks (Push-based)

**Architecture would require:**
- Web server to receive POST requests
- Public HTTPS endpoint with valid SSL
- GitHub App or repository webhook configuration
- Webhook secret validation
- Delivery failure handling and replay

**Rejected because:**
- Significantly increases deployment complexity
- Requires public network exposure (security concern for some users)
- Must handle webhook delivery failures (GitHub retries, but gaps possible)
- Requires SSL certificate management
- Would need to maintain webhook subscription state
- Many target users are on VPSes behind NAT or on private networks

### GitHub App with Webhooks

**Same issues as webhooks, plus:**
- Requires creating and managing a GitHub App
- More complex authentication (JWT + installation tokens)
- App installation management per repository

**Rejected because:**
- Even higher complexity than basic webhooks
- Not necessary for the single-user deployment model

### Hybrid: Polling + Webhooks

**Could support both:**
- Polling as baseline
- Optional webhook endpoint for lower latency

**Deferred because:**
- Adds complexity without clear demand
- Polling meets current needs
- Can be added later if needed

### GitHub Actions Workflow Triggers

**Could trigger runner via repository_dispatch:**
- Workflow listens for label events
- Sends repository_dispatch to trigger runner

**Rejected because:**
- Requires workflow in each monitored repo
- Still needs mechanism for runner to receive dispatch
- Adds indirection without clear benefit

## Implementation Details

### Discovery Algorithm

```python
def discover_all():
    jobs = []
    for repo in config.repos:
        # 1. Find ready issues
        issues = github.search_ready_issues(repo, ...)
        for issue in issues:
            if not db.get_active_run_for_target(repo, issue.number):
                jobs.append(Job(...))

        # 2. Find mention comments
        last_poll = db.get_cursor(repo)
        candidates = github.search_mention_candidates(repo, since=last_poll)
        for number in candidates:
            comments = github.get_issue_comments(repo, number)
            for comment in comments:
                if mentions_bot(comment) and not db.is_comment_processed(comment.id):
                    jobs.append(Job(...))

        db.update_cursor(repo, now())

    return sorted(jobs, key=priority)
```

### Rate Limit Considerations

With default settings (5-minute interval, few repos):
- ~12 search requests per repo per hour
- ~12 cursor updates per repo per hour
- Additional requests only when candidates found

GitHub's rate limit is 5000 requests/hour for authenticated users, so this is well within limits even for dozens of repositories.

## References

- `claude_github_runner/discovery.py`: Discovery implementation
- `claude_github_runner/cli.py`: Tick and daemon implementations
- GitHub REST API rate limits: https://docs.github.com/en/rest/rate-limit
