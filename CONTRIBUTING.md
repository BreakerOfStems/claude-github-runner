# Contributing to Claude GitHub Runner

Thank you for your interest in contributing to Claude GitHub Runner! This document provides guidelines and information for contributors.

## Development Environment Setup

### Prerequisites

- Python 3.10 or higher
- Git
- GitHub CLI (`gh`) - for testing GitHub integrations
- Claude Code CLI (`claude`) - for end-to-end testing

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/BreakerOfStems/claude-github-runner.git
   cd claude-github-runner
   ```

2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install in development mode with dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

4. Verify installation:
   ```bash
   claude-github-runner --help
   ```

## Code Style

This project uses automated tools to maintain consistent code style:

### Formatting with Black

We use [Black](https://black.readthedocs.io/) for code formatting:

```bash
# Format all files
black claude_github_runner/

# Check formatting without making changes
black --check claude_github_runner/
```

Configuration (in `pyproject.toml`):
- Line length: 100 characters
- Target Python version: 3.10

### Linting with Ruff

We use [Ruff](https://docs.astral.sh/ruff/) for fast linting:

```bash
# Run linter
ruff check claude_github_runner/

# Auto-fix issues where possible
ruff check --fix claude_github_runner/
```

Enabled rule sets:
- `E`, `F`: pyflakes and pycodestyle errors
- `I`: isort (import sorting)
- `N`: pep8-naming
- `W`: pycodestyle warnings
- `UP`: pyupgrade (Python version upgrades)

### Type Checking with mypy

We use [mypy](https://mypy.readthedocs.io/) for static type checking:

```bash
mypy claude_github_runner/
```

Configuration requires:
- All functions must have type annotations (`disallow_untyped_defs = true`)
- Warns on `Any` return types

### Pre-commit Workflow

Before committing, run all checks:

```bash
black claude_github_runner/
ruff check claude_github_runner/
mypy claude_github_runner/
```

## Testing

### Running Tests

Tests are located in the `tests/` directory and use pytest:

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=claude_github_runner

# Run specific test file
pytest tests/test_worker.py

# Run with verbose output
pytest -v
```

### Writing Tests

- Place tests in the `tests/` directory
- Name test files with the `test_` prefix
- Use descriptive test function names that explain what's being tested
- Mock external dependencies (GitHub API, Claude CLI, filesystem operations)

## Architecture Overview

The project follows a modular architecture:

```
claude_github_runner/
├── __init__.py       # Package initialization
├── __main__.py       # Entry point for `python -m claude_github_runner`
├── cli.py            # CLI interface, Runner and Daemon classes
├── config.py         # Configuration loading and validation
├── database.py       # SQLite database operations, Run tracking
├── discovery.py      # Job discovery from GitHub (ready labels, mentions)
├── github.py         # GitHub API wrapper using gh CLI
├── worker.py         # Job execution logic
├── workspace.py      # Workspace/directory management
└── ui/               # Terminal UI (Textual-based)
    ├── __init__.py
    ├── app.py        # Main UI application
    ├── db.py         # UI database queries
    ├── logs.py       # Log viewing
    ├── models.py     # UI data models
    └── systemd.py    # Systemd integration
```

### Key Components

**Runner (`cli.py`)**: Orchestrates the tick-based execution model. Discovers jobs, manages concurrency slots, and spawns workers.

**Daemon (`cli.py`)**: Long-running alternative to tick-based execution. Spawns jobs as subprocesses and manages their lifecycle.

**Discovery (`discovery.py`)**: Finds work to do by querying GitHub for:
- Issues with the `ready` label (and no blockers)
- Comments mentioning the bot

**Worker (`worker.py`)**: Executes individual jobs:
1. Claims the issue on GitHub (labels, assignment)
2. Clones the repository
3. Creates a feature branch
4. Invokes Claude Code with a constructed prompt
5. Commits changes and creates/updates a PR
6. Handles success, failure, and edge cases

**Database (`database.py`)**: SQLite-based state tracking for:
- Run records (status, timestamps, PR URLs)
- Processed comments (deduplication)
- Concurrency management

**GitHub (`github.py`)**: Wrapper around the `gh` CLI for all GitHub operations.

### Execution Flow

```
tick/daemon → discovery → worker.execute_async() → fork/subprocess
                                                        ↓
                                                   _execute_job()
                                                        ↓
                                          claim → clone → branch → claude → commit → PR
```

## Pull Request Process

### Creating a PR

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes, following the code style guidelines

3. Run all checks before committing:
   ```bash
   black claude_github_runner/
   ruff check claude_github_runner/
   mypy claude_github_runner/
   pytest
   ```

4. Commit with a descriptive message:
   ```bash
   git commit -m "Add feature: brief description"
   ```

5. Push and create a PR:
   ```bash
   git push -u origin feature/your-feature-name
   gh pr create
   ```

### PR Guidelines

- Keep changes focused and minimal
- Include a clear description of what changed and why
- Reference any related issues (e.g., "Closes #123")
- Ensure all CI checks pass
- Be responsive to review feedback

### Code Review

All PRs require review before merging. Reviewers will check:

- Code correctness and logic
- Adherence to code style
- Test coverage for new functionality
- Documentation updates if needed
- No security vulnerabilities introduced

## Issue Guidelines

### Reporting Bugs

Include:
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version)
- Relevant logs or error messages

### Feature Requests

Include:
- Clear description of the proposed feature
- Use case and motivation
- Any implementation ideas (optional)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
