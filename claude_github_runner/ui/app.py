"""Main TUI application."""

import subprocess
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Log,
    Static,
)

from .db import RunnerDB
from .logs import FileLogReader, JournalLogStream
from .models import RunInfo
from .systemd import SystemdController


class StatusBar(Static):
    """Status bar showing service state."""

    def __init__(self, controller: SystemdController):
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Label("", id="status-text")

    def update_status(self, active_runs: int, db_available: bool):
        """Update status bar."""
        timer_status = self.controller.get_timer_status()
        timer_state = "●" if timer_status.active else "○"
        timer_color = "green" if timer_status.active else "red"

        db_state = "DB:OK" if db_available else "DB:ERR"

        now = datetime.now().strftime("%H:%M:%S")

        text = f"[{timer_color}]{timer_state}[/] Timer  |  Active: {active_runs}  |  {db_state}  |  {now}"
        self.query_one("#status-text", Label).update(text)


class RunsTableScreen(Screen):
    """Main screen showing runs table."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "logs", "Service Logs"),
        Binding("r", "restart_service", "Restart Service"),
        Binding("t", "restart_timer", "Restart Timer"),
        Binding("s", "show_status", "Status"),
        Binding("c", "launch_claude", "Claude"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("enter", "select_run", "Details"),
        Binding("d", "delete_workspace", "Delete"),
        Binding("?", "help", "Help"),
    ]

    filter_mode = reactive("all")

    def __init__(
        self,
        db: RunnerDB,
        logs: FileLogReader,
        controller: SystemdController,
        workspace_root: str,
    ):
        super().__init__()
        self.db = db
        self.logs = logs
        self.controller = controller
        self.workspace_root = workspace_root

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusBar(self.controller)
        yield Container(
            DataTable(id="runs-table"),
            id="table-container",
        )
        yield Static("", id="toast")
        yield Footer()

    def on_mount(self) -> None:
        """Set up the table and start refresh."""
        table = self.query_one("#runs-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Run ID", "Repo", "Target", "Type", "Status", "PID", "Started", "PR"
        )
        self._refresh_runs()
        # Use Textual's set_interval for periodic refresh
        self.set_interval(1.0, self._refresh_runs)

    def _refresh_runs(self) -> None:
        """Refresh the runs table."""
        table = self.query_one("#runs-table", DataTable)

        # Save current selection
        current_row = table.cursor_row

        # Get filter
        filter_map = {
            "all": None,
            "active": "active",
            "failed": "failed",
            "needs-human": "needs-human",
        }

        runs = self.db.get_runs(status_filter=filter_map.get(self.filter_mode))

        # Clear and repopulate
        table.clear()
        for run in runs:
            status_style = ""
            if run.status == "running":
                status_style = "[bold green]"
            elif run.status == "failed":
                status_style = "[red]"
            elif run.status == "needs-human":
                status_style = "[yellow]"

            table.add_row(
                run.short_id,
                run.repo,
                f"#{run.target_number}",
                run.job_type,
                f"{status_style}{run.status}[/]" if status_style else run.status,
                str(run.pid) if run.pid else "—",
                run.started_ago,
                run.short_pr_url,
                key=run.run_id,
            )

        # Restore selection if possible
        if current_row is not None and current_row < table.row_count:
            table.move_cursor(row=current_row)

        # Update status bar
        status_bar = self.query_one(StatusBar)
        status_bar.update_status(self.db.get_active_count(), self.db.is_available())

    def _show_toast(self, message: str, error: bool = False) -> None:
        """Show a toast message."""
        toast = self.query_one("#toast", Static)
        style = "[red]" if error else "[green]"
        toast.update(f"{style}{message}[/]")
        self.set_timer(3, lambda: toast.update(""))

    def _get_selected_run(self) -> Optional[RunInfo]:
        """Get the currently selected run."""
        table = self.query_one("#runs-table", DataTable)
        if table.cursor_row is not None:
            row_key = table.get_row_at(table.cursor_row)
            if row_key:
                # The key is stored in row_key, need to get run_id
                run_id = list(table.rows.keys())[table.cursor_row].value
                return self.db.get_run(run_id)
        return None

    def action_quit(self) -> None:
        """Quit the app."""
        self.app.exit()

    def action_logs(self) -> None:
        """Switch to logs view."""
        self.app.push_screen(ServiceLogsScreen(self.controller))

    @work(exclusive=True)
    async def action_restart_service(self) -> None:
        """Restart the service."""
        success, msg = await self.controller.restart_service()
        self._show_toast(msg, error=not success)

    @work(exclusive=True)
    async def action_restart_timer(self) -> None:
        """Restart the timer."""
        success, msg = await self.controller.restart_timer()
        self._show_toast(msg, error=not success)

    def action_show_status(self) -> None:
        """Show full status."""
        status = self.controller.get_full_status()
        self.app.push_screen(StatusScreen(status))

    def action_launch_claude(self) -> None:
        """Launch Claude in the selected run's workspace."""
        run = self._get_selected_run()

        if run:
            workspace = self.logs.get_workspace_path(run.run_id)
            if workspace:
                self._launch_claude_session(str(workspace))
            else:
                self._show_toast("Workspace not found", error=True)
        else:
            # Launch in default workspace
            self._launch_claude_session(self.workspace_root)

    def _launch_claude_session(self, cwd: str) -> None:
        """Launch Claude and suspend the TUI."""
        with self.app.suspend():
            result = subprocess.run(["claude"], cwd=cwd)

        self._show_toast(f"Claude exited (code {result.returncode})")
        self._refresh_runs()

    def action_cycle_filter(self) -> None:
        """Cycle through filter modes."""
        filters = ["all", "active", "failed", "needs-human"]
        current_idx = filters.index(self.filter_mode)
        self.filter_mode = filters[(current_idx + 1) % len(filters)]
        self._show_toast(f"Filter: {self.filter_mode}")
        self._refresh_runs()

    def action_select_run(self) -> None:
        """Open run detail view."""
        run = self._get_selected_run()
        if run:
            self.app.push_screen(
                RunDetailScreen(run, self.logs, self.workspace_root)
            )
        else:
            self._show_toast("No run selected", error=True)

    def action_delete_workspace(self) -> None:
        """Delete the selected run's workspace."""
        run = self._get_selected_run()
        if not run:
            self._show_toast("No run selected", error=True)
            return

        if run.status == "running":
            self._show_toast("Cannot delete running workspace", error=True)
            return

        workspace = Path(self.workspace_root) / run.run_id
        if workspace.exists():
            shutil.rmtree(workspace)
            self._show_toast(f"Deleted {run.short_id}")
        else:
            self._show_toast("Workspace already deleted", error=True)

    def action_help(self) -> None:
        """Show help screen."""
        self.app.push_screen(HelpScreen())


class RunDetailScreen(Screen):
    """Detail view for a single run."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "back", "Back"),
        Binding("c", "launch_claude", "Claude"),
        Binding("p", "show_pr", "PR Link"),
        Binding("i", "show_issue", "Issue Link"),
    ]

    def __init__(self, run: RunInfo, logs: FileLogReader, workspace_root: str):
        super().__init__()
        self.run = run
        self.logs = logs
        self.workspace_root = workspace_root
        self.selected_artifact: Optional[str] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static(self._build_header(), id="run-header"),
            Horizontal(
                ListView(id="artifacts-list"),
                Log(id="log-viewer", highlight=True),
                id="detail-content",
            ),
            id="detail-container",
        )
        yield Static("", id="toast")
        yield Footer()

    def _build_header(self) -> str:
        """Build the header text."""
        lines = [
            f"[bold]Run:[/] {self.run.run_id}",
            f"[bold]Repo:[/] {self.run.repo}  [bold]Target:[/] #{self.run.target_number}",
            f"[bold]Status:[/] {self.run.status}  [bold]Type:[/] {self.run.job_type}",
            f"[bold]Branch:[/] {self.run.branch or '—'}",
            f"[bold]PR:[/] {self.run.pr_url or '—'}",
            f"[bold]Started:[/] {self.run.started_at or '—'}  [bold]Ended:[/] {self.run.ended_at or '—'}",
        ]
        if self.run.error:
            lines.append(f"[bold red]Error:[/] {self.run.error}")
        return "\n".join(lines)

    def on_mount(self) -> None:
        """Load artifacts list."""
        artifacts = self.logs.get_artifacts(self.run.run_id)
        list_view = self.query_one("#artifacts-list", ListView)

        for artifact in artifacts:
            list_view.append(ListItem(Label(artifact), id=f"artifact-{artifact}"))

        # Select default artifact
        default_order = ["summary.json", "claude.log", "runner.log"]
        for default in default_order:
            if default in artifacts:
                self._load_artifact(default)
                break

    @on(ListView.Selected)
    def on_artifact_selected(self, event: ListView.Selected) -> None:
        """Handle artifact selection."""
        if event.item and event.item.id:
            artifact_name = event.item.id.replace("artifact-", "")
            self._load_artifact(artifact_name)

    def _load_artifact(self, filename: str) -> None:
        """Load an artifact into the log viewer."""
        self.selected_artifact = filename
        content = self.logs.read_artifact(self.run.run_id, filename)
        log_viewer = self.query_one("#log-viewer", Log)
        log_viewer.clear()
        log_viewer.write(content)

    def _show_toast(self, message: str, error: bool = False) -> None:
        """Show a toast message."""
        toast = self.query_one("#toast", Static)
        style = "[red]" if error else "[green]"
        toast.update(f"{style}{message}[/]")
        self.set_timer(3, lambda: toast.update(""))

    def action_back(self) -> None:
        """Go back."""
        self.app.pop_screen()

    def action_launch_claude(self) -> None:
        """Launch Claude in this workspace."""
        workspace = self.logs.get_workspace_path(self.run.run_id)
        if workspace:
            with self.app.suspend():
                result = subprocess.run(["claude"], cwd=str(workspace))
            self._show_toast(f"Claude exited (code {result.returncode})")
        else:
            self._show_toast("Workspace not found", error=True)

    def action_show_pr(self) -> None:
        """Show PR link."""
        if self.run.pr_url:
            self._show_toast(f"PR: {self.run.pr_url}")
        else:
            self._show_toast("No PR URL", error=True)

    def action_show_issue(self) -> None:
        """Show issue link."""
        url = self.run.issue_url
        if url:
            self._show_toast(f"Issue: {url}")
        else:
            self._show_toast("No issue URL", error=True)


class ServiceLogsScreen(Screen):
    """Live service logs view."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "back", "Back"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("r", "restart", "Restart Service"),
    ]

    def __init__(self, controller: SystemdController):
        super().__init__()
        self.controller = controller
        self._stream: Optional[JournalLogStream] = None
        self._paused = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("[bold]Service Logs[/] (journalctl)", id="logs-header"),
            Log(id="service-log", highlight=True),
            id="logs-container",
        )
        yield Static("", id="toast")
        yield Footer()

    def on_mount(self) -> None:
        """Start streaming logs."""
        self._stream = JournalLogStream(self.controller.service_name)
        self._start_streaming()

    @work(exclusive=True)
    async def _start_streaming(self) -> None:
        """Stream logs to the viewer."""
        log_viewer = self.query_one("#service-log", Log)

        async for line in self._stream.start():
            if not self._paused:
                log_viewer.write_line(line)

    async def on_unmount(self) -> None:
        """Stop streaming."""
        if self._stream:
            await self._stream.stop()

    def _show_toast(self, message: str, error: bool = False) -> None:
        """Show a toast message."""
        toast = self.query_one("#toast", Static)
        style = "[red]" if error else "[green]"
        toast.update(f"{style}{message}[/]")
        self.set_timer(3, lambda: toast.update(""))

    def action_back(self) -> None:
        """Go back."""
        self.app.pop_screen()

    def action_toggle_pause(self) -> None:
        """Toggle pause/resume."""
        self._paused = not self._paused
        state = "paused" if self._paused else "resumed"
        self._show_toast(f"Log streaming {state}")

    @work(exclusive=True)
    async def action_restart(self) -> None:
        """Restart service."""
        success, msg = await self.controller.restart_service()
        self._show_toast(msg, error=not success)


class StatusScreen(Screen):
    """Full status display."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "back", "Back"),
    ]

    def __init__(self, status_text: str):
        super().__init__()
        self.status_text = status_text

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static("[bold]Service Status[/]", id="status-header"),
            Log(id="status-log"),
            id="status-container",
        )
        yield Footer()

    def on_mount(self) -> None:
        """Display status."""
        log = self.query_one("#status-log", Log)
        log.write(self.status_text)

    def action_back(self) -> None:
        """Go back."""
        self.app.pop_screen()


class HelpScreen(Screen):
    """Help screen with keybindings."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("b", "back", "Back"),
    ]

    HELP_TEXT = """
[bold]Claude GitHub Runner UI[/]

[bold underline]Runs Table[/]
  Enter    View run details
  L        Service logs (journald)
  R        Restart service
  T        Restart timer
  S        Show systemctl status
  C        Launch Claude in workspace
  F        Cycle filter (all/active/failed/needs-human)
  D        Delete workspace (not running)
  ?        This help
  Q        Quit

[bold underline]Run Detail[/]
  B/Esc    Back
  C        Launch Claude in workspace
  P        Show PR link
  I        Show issue link

[bold underline]Service Logs[/]
  B/Esc    Back
  P        Pause/resume
  R        Restart service

[bold underline]Tips[/]
  - Claude sessions suspend the TUI and return on exit
  - Filter cycles: all → active → failed → needs-human
  - Status bar shows timer state and active run count
"""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            Static(self.HELP_TEXT, id="help-text"),
            id="help-container",
        )
        yield Footer()

    def action_back(self) -> None:
        """Go back."""
        self.app.pop_screen()


class RunnerUI(App):
    """Main application."""

    CSS = """
    #table-container {
        height: 100%;
    }

    #runs-table {
        height: 100%;
    }

    #toast {
        dock: bottom;
        height: 1;
        padding: 0 1;
    }

    StatusBar {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $surface;
    }

    #detail-content {
        height: 100%;
    }

    #artifacts-list {
        width: 25%;
        border-right: solid $primary;
    }

    #log-viewer {
        width: 75%;
    }

    #run-header {
        padding: 1;
        background: $surface;
    }

    #logs-header, #status-header {
        padding: 1;
        background: $surface;
    }

    #service-log, #status-log {
        height: 100%;
    }

    #help-text {
        padding: 1 2;
    }
    """

    TITLE = "Claude GitHub Runner"

    def __init__(
        self,
        db_path: str = "/home/claude/workspace/runner.sqlite",
        workspace_root: str = "/home/claude/workspace/_runs",
        service_name: str = "claude-github-runner",
        timer_name: str = "claude-github-runner.timer",
    ):
        super().__init__()
        self.db = RunnerDB(db_path)
        self.logs = FileLogReader(workspace_root)
        self.controller = SystemdController(service_name, timer_name)
        self.workspace_root = workspace_root

    def on_mount(self) -> None:
        """Push the main screen."""
        self.push_screen(
            RunsTableScreen(self.db, self.logs, self.controller, self.workspace_root)
        )


def main(
    db_path: str = "/home/claude/workspace/runner.sqlite",
    workspace_root: str = "/home/claude/workspace/_runs",
    service_name: str = "claude-github-runner",
    timer_name: str = "claude-github-runner.timer",
):
    """Run the UI."""
    app = RunnerUI(
        db_path=db_path,
        workspace_root=workspace_root,
        service_name=service_name,
        timer_name=timer_name,
    )
    app.run()
