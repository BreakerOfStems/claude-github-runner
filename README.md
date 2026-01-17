# Claude GitHub Runner

A headless service that automatically processes GitHub issues using Claude Code. It polls your repositories, picks up issues marked as "ready", and creates pull requests with implementations.

## Features

- **Automatic issue processing**: Monitors repos for issues labeled `ready` and starts work
- **Mention triggers**: Responds to `@username` mentions in issue/PR comments
- **Isolated workspaces**: Each run gets a fresh clone in an ephemeral directory
- **Concurrency control**: Configurable limit on parallel runs (default: 1, max: 3)
- **GitHub-side claiming**: Adds labels and assigns issues to prevent races
- **PR creation**: Automatically creates/updates PRs with the implementation
- **Conflict handling**: Detects merge conflicts and requests human intervention

## Prerequisites

- **Linux** (designed for VPS/server deployment)
- **Python 3.10+**
- **GitHub CLI (`gh`)** - authenticated with access to target repositories
- **Claude Code CLI (`claude`)** - installed and configured
- **Git** - for repository operations

### Installing Prerequisites

```bash
# Install GitHub CLI
# See: https://github.com/cli/cli#installation

# Authenticate GitHub CLI
gh auth login

# Verify authentication
gh api user -q .login

# Install Claude Code
# See: https://docs.anthropic.com/claude-code
```

## Installation

### Quick Start (VPS Bootstrap)

On a fresh Linux VPS, run as root:

```bash
# Create the claude user first (if it doesn't exist)
useradd -m -s /bin/bash claude

# Download and run the bootstrap script
curl -fsSL https://raw.githubusercontent.com/BreakerOfStems/claude-github-runner/main/bootstrap.sh -o bootstrap.sh
sudo bash bootstrap.sh
```

The bootstrap script will:
- Install Python and dependencies
- Clone the repo to `/opt/claude-github-runner`
- Install the CLI to `/usr/local/bin`
- Create config at `/etc/claude-github-runner/config.yml`
- Set up workspace at `/home/claude/workspace`
- Enable systemd timers

After bootstrap, configure your repos and authenticate:

```bash
# Edit config to add your repositories
sudo vim /etc/claude-github-runner/config.yml

# Authenticate gh CLI as the claude user
sudo -u claude gh auth login

# Verify
sudo -u claude claude-github-runner status
```

### Manual Installation

```bash
# Clone the repository
git clone https://github.com/BreakerOfStems/claude-github-runner.git
cd claude-github-runner

# Install with pip
pip install .

# Or install in development mode
pip install -e ".[dev]"
```

### Verify Installation

```bash
claude-github-runner --help
```

## Configuration

### Create Configuration Directory

```bash
sudo mkdir -p /etc/claude-github-runner
sudo cp config.example.yml /etc/claude-github-runner/config.yml
sudo chmod 644 /etc/claude-github-runner/config.yml
```

### Edit Configuration

```yaml
# /etc/claude-github-runner/config.yml

# Required: repositories to monitor
repos:
  - "your-org/your-repo"
  - "your-org/another-repo"

# Label configuration (adjust to match your workflow)
labels:
  ready: "ready"              # Issues ready for Claude to work on
  in_progress: "in-progress"  # Added when work starts
  blocked:                    # Skip issues with these labels
    - "blocked"
    - "needs-info"
  needs_human: "needs-human"  # Added when human help needed

# Adjust paths if needed
paths:
  workspace_root: "/home/claude/workspace/_runs"
  db_path: "/home/claude/workspace/runner.sqlite"
```

### Create Workspace Directory

```bash
sudo mkdir -p /home/claude/workspace/_runs
sudo chown -R claude:claude /home/claude/workspace
```

## Usage

### Manual Commands

```bash
# Run one scheduling cycle (discover and process jobs)
claude-github-runner tick

# Manually run a specific issue
claude-github-runner run --repo owner/repo --number 123

# Manually run a mention job
claude-github-runner run --repo owner/repo --number 123 --comment-id 456789

# Clean up dead/stale runs
claude-github-runner reap

# Check current status
claude-github-runner status
```

### Systemd Setup (Recommended)

```bash
# Copy service files
sudo cp systemd/*.service /etc/systemd/system/
sudo cp systemd/*.timer /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable and start timers
sudo systemctl enable --now claude-github-runner.timer
sudo systemctl enable --now claude-github-runner-reap.timer

# Check timer status
systemctl list-timers | grep claude

# View logs
journalctl -u claude-github-runner -f
```

### Cron Alternative

If you prefer cron over systemd:

```bash
# Edit crontab for the claude user
sudo -u claude crontab -e

# Add these lines:
*/5 * * * * /usr/local/bin/claude-github-runner tick >> /var/log/claude-github-runner.log 2>&1
*/15 * * * * /usr/local/bin/claude-github-runner reap >> /var/log/claude-github-runner.log 2>&1
```

## How It Works

### Issue Queue Trigger

An issue is picked up when:
1. It is **open**
2. It has the `ready` label
3. It has **no assignee**
4. It does **not** have the `in-progress` label
5. It does **not** have any blocked labels

### Mention Trigger

A job is created when:
1. A new comment contains `@<your-github-username>`
2. The comment author is not the bot itself
3. The comment hasn't been processed before

### Execution Flow

1. **Claim**: Add `in-progress` label, assign to self
2. **Clone**: Fresh clone of the repository
3. **Branch**: Create `claude/<number>-<slug>` branch
4. **Invoke Claude**: Run Claude Code with the issue context
5. **Commit & Push**: Commit changes and push branch
6. **PR**: Create or update pull request
7. **Cleanup**: Remove workspace, update labels

### Concurrency

- Default: 1 concurrent run
- Maximum: 3 concurrent runs (configurable)
- Jobs queue if all slots are full
- Mentions take priority over ready issues

## Labels

| Label | Purpose |
|-------|---------|
| `ready` | Mark issues for Claude to work on |
| `in-progress` | Auto-added when work starts |
| `needs-human` | Auto-added when Claude needs help |
| `blocked` | Issues to skip |

## Directory Structure

```
/home/claude/workspace/
├── _runs/
│   └── 20240115-143022-a1b2c3d4/
│       ├── repo/           # Git clone
│       ├── artifacts/
│       │   ├── runner.log  # Command log
│       │   ├── claude.log  # Claude output
│       │   ├── git.diff    # Changes made
│       │   └── summary.json
│       └── prompt.md       # Task prompt
└── runner.sqlite           # State database
```

## Troubleshooting

### Check Authentication

```bash
# Verify gh is authenticated
gh api user

# Verify Claude is working
claude --version
```

### View Logs

```bash
# Systemd logs
journalctl -u claude-github-runner -n 100

# Specific run artifacts
cat /home/claude/workspace/_runs/<run-id>/artifacts/claude.log
```

### Database Inspection

```bash
sqlite3 /home/claude/workspace/runner.sqlite

# View active runs
SELECT * FROM runs WHERE status IN ('queued', 'claimed', 'running');

# View processed comments
SELECT * FROM processed_comments ORDER BY created_at DESC LIMIT 10;
```

### Reset a Stuck Run

```bash
# Mark run as failed
sqlite3 /home/claude/workspace/runner.sqlite \
  "UPDATE runs SET status='failed' WHERE run_id='<run-id>'"

# Remove labels manually
gh issue edit <number> --repo owner/repo --remove-label in-progress
```

## Security Considerations

- The service runs with `--dangerously-skip-permissions` flag for Claude
- Runs are isolated in ephemeral workspaces
- Only processes repos explicitly listed in config
- GitHub claims prevent duplicate processing
- Consider network isolation for the VPS

## License

MIT
