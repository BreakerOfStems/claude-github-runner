# ADR 002: GitHub CLI Integration

## Status

Accepted

## Context

Claude GitHub Runner needs to interact with GitHub to:
- Search for issues and PRs
- Read issue/PR details and comments
- Add/remove labels and assignees
- Create comments
- Clone repositories
- Create and update pull requests

We needed to choose an approach for GitHub API integration that would be reliable, maintainable, and work well with our deployment model.

## Decision

We chose to use the **GitHub CLI (`gh`)** as our primary interface to GitHub, wrapping it in a Python class that invokes `gh` commands via `subprocess`.

All GitHub operations go through the `GitHub` class in `github.py`, which:
- Executes `gh` commands with appropriate flags
- Parses JSON output from `gh` commands
- Provides a typed Python interface with dataclasses for Issues, Comments, PullRequests

## Consequences

### Positive

- **Authentication handled externally**: `gh auth login` manages tokens, including OAuth refresh. No need to handle token storage, refresh logic, or credential management in our code.
- **Battle-tested implementation**: The `gh` CLI is maintained by GitHub and handles edge cases, rate limiting, and API versioning.
- **Consistent with deployment model**: Users must have `gh` installed anyway for Claude Code to create PRs. No additional dependencies.
- **Simple debugging**: Failed commands can be reproduced manually by running the same `gh` command.
- **Rich functionality**: `gh` provides high-level commands like `gh pr create` that handle branch detection, remote setup, etc.
- **JSON output**: `gh` supports `--json` flag for structured output, making parsing reliable.

### Negative

- **Subprocess overhead**: Each API call spawns a new process. Acceptable for our low-volume use case but wouldn't scale to thousands of requests.
- **Error handling complexity**: Must parse stderr and return codes rather than catching typed exceptions.
- **Version dependency**: Behavior may vary between `gh` versions, though the JSON output format is stable.
- **No connection pooling**: Can't reuse HTTP connections across requests.

## Alternatives Considered

### PyGithub Library
- **Rejected because**:
  - Requires managing GitHub tokens directly (storage, refresh, etc.)
  - Adds a Python dependency that must be kept updated
  - Authentication would duplicate what `gh` already provides
  - Users already need `gh` for Claude Code, so it's guaranteed to be present

### Direct REST API Calls (requests library)
- **Rejected because**:
  - Would need to implement authentication, pagination, rate limiting
  - Same token management issues as PyGithub
  - More code to maintain with no benefit over `gh`

### GraphQL API
- **Rejected because**:
  - More complex query construction
  - `gh api` can execute GraphQL if needed, so this remains an option
  - REST API via `gh` meets all current needs

### GitHub Actions
- **Rejected because**:
  - Runner is designed to work outside of GitHub Actions environment
  - Designed for self-hosted deployment on VPS

## Implementation Notes

The `GitHub` class uses two patterns for API calls:

1. **High-level commands** for complex operations:
   ```python
   gh pr create --repo owner/repo --title "..." --body "..."
   gh repo clone owner/repo /path/to/dir
   ```

2. **Low-level API calls** for data fetching:
   ```python
   gh api repos/owner/repo/issues/123
   gh api repos/owner/repo/issues/123/comments --paginate
   ```

Error handling uses `check=False` for operations that may legitimately fail (e.g., removing a label that doesn't exist) and `check=True` for operations that should always succeed.

## References

- `claude_github_runner/github.py`: Full implementation of GitHub CLI wrapper
- GitHub CLI documentation: https://cli.github.com/manual/
