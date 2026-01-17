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

        self._process = await asyncio.create_subprocess_exec(
            "journalctl",
            "-u", self.service_name,
            "-n", str(self.lines),
            "-f",
            "--no-pager",
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
                        yield line.decode().rstrip()
                    elif self._process.returncode is not None:
                        break
                except asyncio.TimeoutError:
                    continue

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
