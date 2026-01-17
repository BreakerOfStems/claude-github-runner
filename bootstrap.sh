#!/bin/bash
# ---- Claude GitHub Runner install (run as root) ----
# Places code in /opt, config in /etc, runs as user "claude" via systemd timers.
# Uses a virtual environment at /opt/claude-github-runner-venv for PEP 668 compliance.

set -euo pipefail

RUNNER_REPO="https://github.com/BreakerOfStems/claude-github-runner"
RUNNER_DIR="/opt/claude-github-runner"
VENV_DIR="/opt/claude-github-runner-venv"
CFG_DIR="/etc/claude-github-runner"
CFG_FILE="$CFG_DIR/config.yml"
CLAUDE_USER="claude"
WORKSPACE_ROOT="/home/$CLAUDE_USER/workspace"
RUNS_DIR="$WORKSPACE_ROOT/_runs"
DB_PATH="$WORKSPACE_ROOT/runner.sqlite"

# prereqs
apt update
apt install -y git python3 python3-venv python3-pip

# clone/update into /opt (not in $HOME)
if [ -d "$RUNNER_DIR/.git" ]; then
  git -C "$RUNNER_DIR" fetch --all
  git -C "$RUNNER_DIR" reset --hard origin/main
else
  git clone "$RUNNER_REPO" "$RUNNER_DIR"
fi

# create/update virtual environment
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

# install runner into venv
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install "$RUNNER_DIR"

# create symlink in /usr/local/bin for easy access
ln -sf "$VENV_DIR/bin/claude-github-runner" /usr/local/bin/claude-github-runner

# config + workspace
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_FILE" ]; then
  cp "$RUNNER_DIR/config.example.yml" "$CFG_FILE"
  chmod 644 "$CFG_FILE"
fi

mkdir -p "$RUNS_DIR"
chown -R "$CLAUDE_USER:$CLAUDE_USER" "$WORKSPACE_ROOT"

# patch paths in config to match VPS layout (safe idempotent-ish)
"$VENV_DIR/bin/python" - <<PY
import yaml, pathlib
p = pathlib.Path("$CFG_FILE")
cfg = yaml.safe_load(p.read_text())
cfg.setdefault("paths", {})
cfg["paths"]["workspace_root"] = "$RUNS_DIR"
cfg["paths"]["db_path"] = "$DB_PATH"
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
PY

# systemd timers (recommended by the repo)
cp "$RUNNER_DIR/systemd/"*.service /etc/systemd/system/
cp "$RUNNER_DIR/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-github-runner.timer
systemctl enable --now claude-github-runner-reap.timer

# quick sanity
su - "$CLAUDE_USER" -c "claude-github-runner --help >/dev/null"
systemctl list-timers | grep -E 'claude-github-runner' || true

echo ""
echo "=== Installation Complete ==="
echo "Venv:   $VENV_DIR"
echo "Config: $CFG_FILE"
echo "Runs:   $RUNS_DIR"
echo "Logs:   journalctl -u claude-github-runner -f"
