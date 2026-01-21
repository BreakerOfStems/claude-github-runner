# ADR 001: Fork-Based Concurrency

## Status

Accepted (with Daemon Mode alternative added in PR #18)

## Context

Claude GitHub Runner needs to process multiple jobs concurrently while maintaining:
- Process isolation between jobs
- Proper resource management
- Simple deployment without external dependencies
- Ability to run Claude Code CLI as a subprocess

We considered several approaches for handling concurrent job execution.

## Decision

We chose **fork-based concurrency using `os.fork()`** as the primary mechanism, with a **subprocess-based daemon mode** as an alternative.

### Fork Mode (tick command)
- Parent process handles discovery and scheduling
- Child processes are created via `os.fork()` for each job
- Each child reconnects to SQLite and GitHub to avoid sharing connections across fork
- Parent returns immediately after forking; child handles full job lifecycle
- PID tracking in database allows reaping orphaned processes

### Daemon Mode (daemon command)
- Single long-running process manages scheduling
- Jobs spawned as separate `cgr run` subprocess calls
- In-memory tracking of running processes
- Better environment inheritance for Claude Code subprocesses

## Consequences

### Positive
- **No external dependencies**: No need for Celery, Redis, RabbitMQ, or other job queues
- **Process isolation**: Each job runs in its own process with separate memory space
- **Simple deployment**: Single Python package, no distributed system complexity
- **Native Unix semantics**: Uses well-understood fork/exec patterns
- **Graceful failure handling**: Dead children can be detected via PID checks

### Negative
- **Linux-only**: `os.fork()` works best on Unix systems; Windows compatibility would require significant changes
- **Database reconnection overhead**: SQLite connection must be recreated after fork
- **Memory duplication**: Forking copies parent memory (though copy-on-write mitigates this)
- **Complexity in logging**: Child processes need separate log file setup

### Daemon Mode Trade-offs
- **Pro**: Better environment inheritance for Claude Code CLI (important for TTY contexts)
- **Pro**: Single process simplifies monitoring
- **Con**: Requires long-running process management (systemd service vs timer)

## Alternatives Considered

### Celery with Redis/RabbitMQ
- **Rejected because**: Adds significant infrastructure complexity. Requires running and maintaining Redis/RabbitMQ. Overkill for max 3 concurrent jobs.

### Python RQ (Redis Queue)
- **Rejected because**: Still requires Redis. Simpler than Celery but unnecessary dependency for our scale.

### asyncio with async/await
- **Rejected because**: Claude Code CLI is invoked via `subprocess.run()`, which is blocking. True async would require complex subprocess handling. Also, asyncio doesn't provide process isolation.

### ThreadPoolExecutor
- **Rejected because**: Threads share memory space, risking state corruption. GIL limits true parallelism for CPU-bound work. No process isolation between jobs.

### multiprocessing.Pool
- **Rejected because**: Pool semantics don't fit our model well. We need fine-grained control over process lifecycle, PID tracking, and graceful handling of long-running jobs.

## References

- `claude_github_runner/worker.py`: `execute_async()` method implements fork-based execution
- `claude_github_runner/cli.py`: `Daemon` class implements subprocess-based alternative
- PR #18: Added daemon mode as alternative to fork-based tick
