#!/bin/bash
# Installation script for claude-github-runner
# Run as root or with sudo

set -e

echo "=== Claude GitHub Runner Installation ==="

# Check prerequisites
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required but not installed."; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "GitHub CLI (gh) is required but not installed."; exit 1; }
command -v git >/dev/null 2>&1 || { echo "Git is required but not installed."; exit 1; }
command -v claude >/dev/null 2>&1 || { echo "Warning: Claude CLI not found in PATH"; }

# Check gh authentication
if ! gh api user -q .login >/dev/null 2>&1; then
    echo "Error: GitHub CLI is not authenticated. Run 'gh auth login' first."
    exit 1
fi

GITHUB_USER=$(gh api user -q .login)
echo "Authenticated as: $GITHUB_USER"

# Create claude user if it doesn't exist
if ! id -u claude >/dev/null 2>&1; then
    echo "Creating claude user..."
    useradd -m -s /bin/bash claude
fi

# Install Python package
echo "Installing claude-github-runner..."
pip install .

# Create directories
echo "Creating directories..."
mkdir -p /etc/claude-github-runner
mkdir -p /home/claude/workspace/_runs
chown -R claude:claude /home/claude/workspace

# Copy config if it doesn't exist
if [ ! -f /etc/claude-github-runner/config.yml ]; then
    echo "Installing example configuration..."
    cp config.example.yml /etc/claude-github-runner/config.yml
    chmod 644 /etc/claude-github-runner/config.yml
    echo "IMPORTANT: Edit /etc/claude-github-runner/config.yml to add your repositories"
fi

# Install systemd units
echo "Installing systemd units..."
cp systemd/*.service /etc/systemd/system/
cp systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit /etc/claude-github-runner/config.yml to add your repositories"
echo "2. Ensure the claude user has gh authentication:"
echo "   sudo -u claude gh auth login"
echo "3. Start the timers:"
echo "   sudo systemctl enable --now claude-github-runner.timer"
echo "   sudo systemctl enable --now claude-github-runner-reap.timer"
echo "4. Check status:"
echo "   systemctl list-timers | grep claude"
echo "   journalctl -u claude-github-runner -f"
