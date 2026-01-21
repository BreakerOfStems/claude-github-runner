# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) documenting key design decisions made in Claude GitHub Runner.

## What are ADRs?

ADRs are short documents that capture important architectural decisions along with their context and consequences. They help future maintainers understand why the system was built the way it was.

## ADR Index

| Number | Title | Status |
|--------|-------|--------|
| [001](001-fork-based-concurrency.md) | Fork-Based Concurrency | Accepted |
| [002](002-github-cli-integration.md) | GitHub CLI Integration | Accepted |
| [003](003-sqlite-storage.md) | SQLite Storage | Accepted |
| [004](004-polling-vs-webhooks.md) | Polling vs Webhooks | Accepted |

## ADR Format

Each ADR follows this structure:

- **Status**: Current state (Proposed, Accepted, Deprecated, Superseded)
- **Context**: The situation and problem being addressed
- **Decision**: What was decided and how it works
- **Consequences**: Positive and negative outcomes of the decision
- **Alternatives Considered**: Other options that were evaluated and why they were rejected

## Contributing

When making significant architectural changes, please:
1. Create a new ADR with the next available number
2. Follow the established format
3. Reference the ADR in your PR description
