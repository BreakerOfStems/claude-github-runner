#!/bin/bash
# ---- Claude GitHub Runner install (run as root) ----
# Places code in /opt, config in /etc, runs as user "claude" via systemd timers.
# Uses a virtual environment at /opt/claude-github-runner-venv for PEP 668 compliance.
#
# Safe to re-run for updates - preserves user config.

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

echo "=== Claude GitHub Runner Bootstrap ==="

# create claude user if missing
if ! id -u "$CLAUDE_USER" &>/dev/null; then
  echo "Creating user: $CLAUDE_USER"
  useradd -m -s /bin/bash "$CLAUDE_USER"
fi

# prereqs
echo "Installing system dependencies..."
apt update
apt install -y git python3 python3-venv python3-pip

# clone/update into /opt
echo "Fetching latest code..."
if [ -d "$RUNNER_DIR/.git" ]; then
  git -C "$RUNNER_DIR" fetch --all
  git -C "$RUNNER_DIR" reset --hard origin/main
else
  # clean up non-git directory if exists
  [ -d "$RUNNER_DIR" ] && rm -rf "$RUNNER_DIR"
  git clone "$RUNNER_REPO" "$RUNNER_DIR"
fi

# create/recreate virtual environment if missing or broken
echo "Setting up Python virtual environment..."
if [ ! -f "$VENV_DIR/bin/python" ]; then
  [ -d "$VENV_DIR" ] && rm -rf "$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

# install/upgrade runner into venv
echo "Installing claude-github-runner..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install --upgrade "$RUNNER_DIR"

# create symlinks in /usr/local/bin for easy access
ln -sf "$VENV_DIR/bin/claude-github-runner" /usr/local/bin/claude-github-runner
ln -sf "$VENV_DIR/bin/cgr" /usr/local/bin/cgr

# config directory
mkdir -p "$CFG_DIR"

# install default config only if none exists
if [ ! -f "$CFG_FILE" ]; then
  echo "Installing default config (edit to add your repos)..."
  cp "$RUNNER_DIR/config.example.yml" "$CFG_FILE"
  chmod 644 "$CFG_FILE"
  FRESH_CONFIG=1
else
  echo "Preserving existing config: $CFG_FILE"
  FRESH_CONFIG=0
fi

# workspace directories
mkdir -p "$RUNS_DIR"
chown -R "$CLAUDE_USER:$CLAUDE_USER" "$WORKSPACE_ROOT"

# patch paths in config only - preserves all other user settings
echo "Ensuring paths are configured..."
"$VENV_DIR/bin/python" - <<PY
import yaml, pathlib, sys

p = pathlib.Path("$CFG_FILE")
try:
    cfg = yaml.safe_load(p.read_text()) or {}
except Exception as e:
    print(f"Warning: Could not parse config, recreating: {e}", file=sys.stderr)
    cfg = {}

# only set paths, preserve everything else
cfg.setdefault("paths", {})
cfg["paths"]["workspace_root"] = "$RUNS_DIR"
cfg["paths"]["db_path"] = "$DB_PATH"

p.write_text(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False))
PY

# stop timers before updating services (ignore errors if not running)
echo "Updating systemd units..."
systemctl stop claude-github-runner.timer 2>/dev/null || true
systemctl stop claude-github-runner-reap.timer 2>/dev/null || true

# install/update systemd units
cp "$RUNNER_DIR/systemd/"*.service /etc/systemd/system/
cp "$RUNNER_DIR/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload

# enable and start timers
systemctl enable --now claude-github-runner.timer
systemctl enable --now claude-github-runner-reap.timer

# quick sanity check
echo "Verifying installation..."
su - "$CLAUDE_USER" -c "claude-github-runner --help >/dev/null"

echo ""
echo "=== Installation Complete ==="
echo "Venv:   $VENV_DIR"
echo "Config: $CFG_FILE"
echo "Runs:   $RUNS_DIR"
echo "Logs:   journalctl -u claude-github-runner -f"
echo ""
systemctl list-timers | grep -E 'claude-github-runner' || true
echo ""

if [ "$FRESH_CONFIG" -eq 1 ]; then
  echo "NEXT STEPS:"
  echo "  1. Edit config to add your repos: sudo vim $CFG_FILE"
  echo "  2. Authenticate gh as claude user: sudo -u $CLAUDE_USER gh auth login"
  echo "  3. Test: sudo -u $CLAUDE_USER cgr status"
  echo "  4. Open UI: sudo -u $CLAUDE_USER cgr ui"
else
  echo "Updated successfully. Config preserved."
  echo "Open UI: sudo -u $CLAUDE_USER cgr ui"
fi
