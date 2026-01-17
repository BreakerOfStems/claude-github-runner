"""Systemd service control."""

import asyncio
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class ServiceStatus:
    """Service status information."""
    active: bool
    state: str  # active, inactive, failed, etc.
    sub_state: str  # running, dead, etc.
    description: str


class SystemdController:
    """Control systemd services."""

    def __init__(self, service_name: str = "claude-github-runner", timer_name: str = "claude-github-runner.timer"):
        self.service_name = service_name
        self.timer_name = timer_name

    def _run_systemctl(self, *args: str, use_sudo: bool = False) -> subprocess.CompletedProcess:
        """Run a systemctl command."""
        cmd = ["sudo", "systemctl"] if use_sudo else ["systemctl"]
        cmd.extend(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def get_service_status(self) -> ServiceStatus:
        """Get service status."""
        result = self._run_systemctl("show", self.service_name,
                                      "--property=ActiveState,SubState,Description")

        props = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value

        return ServiceStatus(
            active=props.get("ActiveState") == "active",
            state=props.get("ActiveState", "unknown"),
            sub_state=props.get("SubState", "unknown"),
            description=props.get("Description", ""),
        )

    def get_timer_status(self) -> ServiceStatus:
        """Get timer status."""
        result = self._run_systemctl("show", self.timer_name,
                                      "--property=ActiveState,SubState,Description")

        props = {}
        for line in result.stdout.strip().split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                props[key] = value

        return ServiceStatus(
            active=props.get("ActiveState") == "active",
            state=props.get("ActiveState", "unknown"),
            sub_state=props.get("SubState", "unknown"),
            description=props.get("Description", ""),
        )

    async def restart_service(self) -> tuple[bool, str]:
        """Restart the service. Returns (success, message)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "systemctl", "restart", self.service_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode == 0:
                return True, f"Restarted {self.service_name}"
            else:
                return False, f"Failed: {stderr.decode().strip()}"
        except Exception as e:
            return False, f"Error: {e}"

    async def restart_timer(self) -> tuple[bool, str]:
        """Restart the timer. Returns (success, message)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "systemctl", "restart", self.timer_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode == 0:
                return True, f"Restarted {self.timer_name}"
            else:
                return False, f"Failed: {stderr.decode().strip()}"
        except Exception as e:
            return False, f"Error: {e}"

    async def trigger_run(self) -> tuple[bool, str]:
        """Trigger an immediate run. Returns (success, message)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo", "systemctl", "start", self.service_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode == 0:
                return True, f"Triggered {self.service_name}"
            else:
                return False, f"Failed: {stderr.decode().strip()}"
        except Exception as e:
            return False, f"Error: {e}"

    def get_full_status(self) -> str:
        """Get full status output."""
        result = self._run_systemctl("status", self.service_name, "--no-pager")
        return result.stdout + result.stderr
