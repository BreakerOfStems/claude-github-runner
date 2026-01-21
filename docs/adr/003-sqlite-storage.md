# ADR 003: SQLite Storage

## Status

Accepted

## Context

Claude GitHub Runner needs persistent storage for:
- **Run records**: Track job execution state (queued, running, succeeded, failed)
- **Processed comments**: Remember which mention comments have been handled to avoid duplicate processing
- **Polling cursors**: Track last poll time per repository for incremental mention discovery

The storage solution needed to support:
- Concurrent access from multiple forked processes
- Simple deployment without external services
- Reliable state tracking across restarts
- Easy inspection and debugging

## Decision

We chose **SQLite** as the storage backend with the following schema:

```sql
-- Track job executions
CREATE TABLE runs (
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
    error TEXT
);

-- Prevent duplicate runs for same target
CREATE UNIQUE INDEX idx_runs_active_target
ON runs (repo, target_number)
WHERE status IN ('queued', 'claimed', 'running');

-- Track processed mention comments
CREATE TABLE processed_comments (
    comment_id INTEGER NOT NULL UNIQUE,
    ...
);

-- Track polling cursor per repo
CREATE TABLE cursors (
    repo TEXT PRIMARY KEY,
    last_poll_at TEXT
);
```

Key implementation details:
- Database file stored at configurable path (default: `/home/claude/workspace/runner.sqlite`)
- Each database operation uses a fresh connection via context manager
- Partial unique index prevents multiple active runs for the same issue/PR
- ISO 8601 timestamps stored as TEXT for simplicity

## Consequences

### Positive

- **Zero infrastructure**: No database server to install, configure, or maintain
- **Single file**: Easy backup, migration, and inspection
- **ACID compliance**: SQLite provides full transaction support
- **Cross-process safe**: WAL mode and proper connection handling support concurrent access
- **Debuggable**: Can query directly with `sqlite3` CLI
- **Portable**: Database file can be copied between systems

### Negative

- **Single-node only**: Cannot distribute across multiple machines (acceptable for our use case)
- **Connection per operation**: Cannot benefit from connection pooling (fork-safety requires fresh connections anyway)
- **Limited concurrent writes**: Write operations are serialized (acceptable at our scale)
- **No built-in replication**: Would need external backup strategy for high availability

### Scale Considerations

SQLite is appropriate because:
- Maximum 3 concurrent jobs by design
- Write volume is low (a few writes per job)
- Read volume is low (discovery queries every few minutes)
- Single-server deployment model

## Alternatives Considered

### PostgreSQL
- **Rejected because**:
  - Requires running a database server
  - Overkill for our data volume and query patterns
  - Adds operational complexity (backups, upgrades, monitoring)
  - Would need connection string configuration and management

### Redis
- **Rejected because**:
  - Primarily a cache/queue, not ideal for relational data
  - Requires running Redis server
  - Data persistence configuration is complex
  - Would still need SQLite or files for some data (like run history)

### JSON Files
- **Rejected because**:
  - No built-in concurrency handling
  - Would need to implement locking manually
  - No indexing for efficient queries
  - Harder to query historical data

### In-Memory Only
- **Rejected because**:
  - State lost on restart
  - Cannot track processed comments across restarts
  - Cannot detect orphaned runs from previous instances

## Implementation Notes

### Fork Safety

After `os.fork()`, the child process creates a fresh `Database` instance to avoid sharing SQLite connections across process boundaries:

```python
# In child process after fork
self.db = Database(self.config.paths.db_path)
```

### Partial Unique Index

The `idx_runs_active_target` index is crucial for preventing race conditions:

```sql
CREATE UNIQUE INDEX idx_runs_active_target
ON runs (repo, target_number)
WHERE status IN ('queued', 'claimed', 'running');
```

This allows multiple completed runs for the same target while preventing duplicate active runs.

### Connection Management

Every operation uses a context manager that commits/rolls back and closes:

```python
@contextmanager
def _connect(self):
    conn = sqlite3.connect(self.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

## References

- `claude_github_runner/database.py`: Full implementation
- SQLite documentation: https://sqlite.org/docs.html
- SQLite WAL mode: https://sqlite.org/wal.html
