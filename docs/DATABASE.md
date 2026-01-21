# Database Schema Documentation

Claude GitHub Runner uses SQLite for persisting run state, tracking processed comments, and managing polling cursors. The database is located at the path configured in `paths.db_path` (default: `/home/claude/workspace/runner.sqlite`).

## Schema Overview

The database consists of three tables:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              runs                                       │
│ (Primary table tracking all run executions)                             │
├─────────────────────────────────────────────────────────────────────────┤
│ run_id (PK)  │ repo │ target_number │ job_type │ status │ pid │ ...    │
└─────────────────────────────────────────────────────────────────────────┘
        │
        │ run_id (FK reference)
        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        processed_comments                               │
│ (Tracks which GitHub comments have been handled)                        │
├─────────────────────────────────────────────────────────────────────────┤
│ repo │ target_number │ comment_id (UNIQUE) │ run_id │ ...              │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                            cursors                                      │
│ (Polling timestamps per repository)                                     │
├─────────────────────────────────────────────────────────────────────────┤
│ repo (PK) │ last_poll_at                                                │
└─────────────────────────────────────────────────────────────────────────┘
```

## Tables

### `runs`

The primary table that tracks all execution runs. Each row represents a single invocation of Claude Code to process an issue or respond to a mention.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `run_id` | TEXT | PRIMARY KEY | Unique identifier in format `YYYYMMDD-HHMMSS-<random>` |
| `repo` | TEXT | NOT NULL | Repository in `owner/repo` format |
| `target_number` | INTEGER | NOT NULL | Issue or PR number being processed |
| `job_type` | TEXT | NOT NULL | Type of job (see Job Types below) |
| `status` | TEXT | NOT NULL | Current run status (see Run Statuses below) |
| `pid` | INTEGER | | Process ID of the forked worker (if running) |
| `branch` | TEXT | | Git branch created for this run (e.g., `claude/42-fix-bug`) |
| `pr_url` | TEXT | | URL of created/updated pull request |
| `started_at` | TEXT | | ISO 8601 timestamp when run started executing |
| `ended_at` | TEXT | | ISO 8601 timestamp when run completed |
| `error` | TEXT | | Error message if run failed |

#### Indexes

- **`idx_runs_active_target`**: Partial unique index on `(repo, target_number)` where status is active.
  - Ensures only one active run per issue/PR at a time
  - Covers statuses: `queued`, `claimed`, `running`
  - Allows completed/failed runs for the same target

#### Job Types

| Value | Description |
|-------|-------------|
| `issue_ready` | Issue with `ready` label picked up from queue |
| `mention_issue` | Response to @mention in issue comment |
| `mention_pr` | Response to @mention in PR comment |

#### Run Statuses

| Value | Description |
|-------|-------------|
| `queued` | Job created, waiting for available slot |
| `claimed` | Slot acquired, about to start execution |
| `running` | Claude Code is actively processing |
| `succeeded` | Run completed successfully |
| `failed` | Run failed with an error |
| `needs-human` | Run requires human intervention |

### `processed_comments`

Tracks GitHub comments that have been processed to prevent duplicate handling. When a mention is detected, the comment ID is recorded before processing begins.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `repo` | TEXT | NOT NULL | Repository in `owner/repo` format |
| `target_number` | INTEGER | NOT NULL | Issue or PR number containing the comment |
| `comment_id` | INTEGER | NOT NULL, UNIQUE | GitHub's unique comment ID |
| `comment_url` | TEXT | | Full URL to the comment |
| `comment_author` | TEXT | | GitHub username of comment author |
| `created_at` | TEXT | | ISO 8601 timestamp when comment was created |
| `run_id` | TEXT | NOT NULL | ID of the run that processed this comment |

#### Indexes

- **`idx_processed_comments_repo`**: Index on `repo` for efficient filtering by repository.

### `cursors`

Stores the last poll timestamp for each monitored repository. Used to fetch only new comments since the last check, reducing API calls and preventing reprocessing.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `repo` | TEXT | PRIMARY KEY | Repository in `owner/repo` format |
| `last_poll_at` | TEXT | | ISO 8601 timestamp of last successful poll |

## Schema Initialization

The schema is automatically created when the `Database` class is instantiated. The `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` statements ensure idempotent initialization.

```python
# From database.py
def _init_schema(self):
    """Initialize database schema."""
    with self._connect() as conn:
        conn.executescript(self.SCHEMA)
```

## Key Constraints and Behaviors

### Active Run Uniqueness

The partial unique index `idx_runs_active_target` enforces that only one active run can exist for a given `(repo, target_number)` combination. This prevents race conditions where multiple workers might try to process the same issue simultaneously.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_active_target
ON runs (repo, target_number)
WHERE status IN ('queued', 'claimed', 'running');
```

When `create_run()` is called, it returns `False` if an active run already exists for the target, allowing the caller to handle the conflict gracefully.

### Comment Deduplication

The `UNIQUE` constraint on `processed_comments.comment_id` ensures each comment is processed exactly once, even if the same mention appears in multiple poll cycles.

### Cursor Upsert

The `update_cursor()` method uses SQLite's `ON CONFLICT` clause for atomic upsert:

```sql
INSERT INTO cursors (repo, last_poll_at) VALUES (?, ?)
ON CONFLICT(repo) DO UPDATE SET last_poll_at = ?
```

## Migration Strategy

Currently, the schema uses a **create-if-not-exists** pattern which works for additive changes:

- New tables can be added to the `SCHEMA` constant
- New indexes can be added with `CREATE INDEX IF NOT EXISTS`
- New columns require manual `ALTER TABLE` statements

### Adding New Columns

To add a new column to an existing table:

1. Add the column to the `SCHEMA` constant for new installations
2. Add a migration check in `_init_schema()`:

```python
def _init_schema(self):
    with self._connect() as conn:
        conn.executescript(self.SCHEMA)

        # Migration: add new_column if it doesn't exist
        cursor = conn.execute("PRAGMA table_info(runs)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'new_column' not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN new_column TEXT")
```

### Future Migration Framework

For more complex migrations, consider implementing:

1. A `schema_version` table to track the current version
2. Numbered migration files (e.g., `migrations/001_initial.sql`)
3. A migration runner that applies pending migrations in order

## Common Queries

### View Active Runs

```sql
SELECT run_id, repo, target_number, job_type, status, started_at
FROM runs
WHERE status IN ('queued', 'claimed', 'running');
```

### View Recent Completed Runs

```sql
SELECT run_id, repo, target_number, status, ended_at, error
FROM runs
WHERE status IN ('succeeded', 'failed', 'needs-human')
ORDER BY ended_at DESC
LIMIT 10;
```

### Find Stale Runs

```sql
SELECT * FROM runs
WHERE status = 'running'
AND started_at < datetime('now', '-60 minutes');
```

### View Processed Comments for a Repository

```sql
SELECT comment_id, target_number, comment_author, created_at
FROM processed_comments
WHERE repo = 'owner/repo'
ORDER BY created_at DESC;
```

### Check Polling Status

```sql
SELECT repo, last_poll_at,
       (julianday('now') - julianday(last_poll_at)) * 24 * 60 AS minutes_ago
FROM cursors;
```

## Data Types and Formats

- **Timestamps**: Stored as ISO 8601 strings (e.g., `2024-01-15T14:30:22.123456`)
- **Enums**: Stored as lowercase strings matching the enum values
- **Booleans**: Not used directly; use INTEGER 0/1 if needed
- **Repository format**: Always `owner/repo` (e.g., `BreakerOfStems/claude-github-runner`)

## Database Location

The database file location is configured in `config.yml`:

```yaml
paths:
  db_path: "/home/claude/workspace/runner.sqlite"
```

For development, you can use a local path:

```yaml
paths:
  db_path: "./runner.sqlite"
```

## Backup and Recovery

SQLite databases can be backed up while the application is running:

```bash
# Create a backup
sqlite3 /home/claude/workspace/runner.sqlite ".backup /path/to/backup.sqlite"

# Or use the online backup API via Python
```

To reset the database completely, simply delete the file. A fresh schema will be created on next startup.
