"""Log streaming utilities."""

import asyncio
from pathlib import Path
from typing import AsyncIterator, Optional


class JournalLogStream:
    """Stream logs from journald."""

    def __init__(self, service_name: str, lines: int = 200):
        self.service_name = service_name
        self.lines = lines
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False

    async def start(self) -> AsyncIterator[str]:
        """Start streaming logs."""
        self._running = True

        # Use compact output format: just timestamp and message
        # -o short-iso gives "YYYY-MM-DDTHH:MM:SS+TZ hostname service[pid]: message"
        # We'll parse and reformat for even more compact output
        self._process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-u", self.service_name,
            "-n", str(self.lines),
            "-f",
            "--no-pager",
            "-o", "short-iso",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        if self._process.stdout:
            while self._running:
                try:
                    line = await asyncio.wait_for(
                        self._process.stdout.readline(),
                        timeout=0.5
                    )
                    if line:
                        yield self._format_line(line.decode().rstrip())
                    elif self._process.returncode is not None:
                        break
                except asyncio.TimeoutError:
                    continue

    def _format_line(self, line: str) -> str:
        """Reformat journald line to be more compact.

        Input:  2026-01-17T04:12:13+0000 hostname service[pid]: actual message
        Output: 04:12:13 actual message
        """
        # Skip empty lines
        if not line.strip():
            return ""

        # Try to parse the ISO timestamp format
        # Format: YYYY-MM-DDTHH:MM:SS+ZZZZ hostname service[pid]: message
        parts = line.split(" ", 3)  # Split into max 4 parts

        if len(parts) >= 4:
            timestamp = parts[0]  # 2026-01-17T04:12:13+0000
            # hostname = parts[1]  # skip
            # service = parts[2]   # skip (e.g., claude-github-runner[12345]:)
            message = parts[3]    # the actual log message

            # Extract just HH:MM:SS from timestamp
            if "T" in timestamp:
                time_part = timestamp.split("T")[1][:8]  # Get HH:MM:SS
                return f"{time_part} {message}"

        # Fallback: return original line if parsing fails
        return line

    async def stop(self):
        """Stop streaming."""
        self._running = False
        if self._process:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None


class FileLogReader:
    """Read and tail log files."""

    def __init__(self, workspace_root: str):
        self.workspace_root = Path(workspace_root)

    def get_artifacts(self, run_id: str) -> list[str]:
        """Get list of artifact files for a run."""
        artifacts_dir = self.workspace_root / run_id / "artifacts"
        if not artifacts_dir.exists():
            return []

        return sorted([f.name for f in artifacts_dir.iterdir() if f.is_file()])

    def read_artifact(self, run_id: str, filename: str, tail_lines: int = 100) -> str:
        """Read an artifact file."""
        artifact_path = self.workspace_root / run_id / "artifacts" / filename

        if not artifact_path.exists():
            return f"File not found: {artifact_path}"

        try:
            lines = artifact_path.read_text().splitlines()
            if len(lines) > tail_lines:
                return f"... ({len(lines) - tail_lines} lines omitted)\n" + "\n".join(lines[-tail_lines:])
            return "\n".join(lines)
        except Exception as e:
            return f"Error reading file: {e}"

    def get_workspace_path(self, run_id: str) -> Optional[Path]:
        """Get the repo path for a run."""
        repo_path = self.workspace_root / run_id / "repo"
        if repo_path.exists():
            return repo_path

        workspace_path = self.workspace_root / run_id
        if workspace_path.exists():
            return workspace_path

        return None

    def workspace_exists(self, run_id: str) -> bool:
        """Check if workspace exists."""
        return (self.workspace_root / run_id).exists()
