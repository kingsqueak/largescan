"""Mac-only Textual interface for largescan."""

from __future__ import annotations

import os
import sys
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
)

import largescan

HELP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HELP.txt")

_TIER_LABELS = {
    "abandoned": "Abandoned",
    "review": "Possible leftover",
    "helper": "Missing helper",
}


def macos_message(platform_name: str | None = None) -> str | None:
    """Return an error when this is not macOS."""
    name = sys.platform if platform_name is None else platform_name
    if name != "darwin":
        return "largescan runs on macOS only."
    return None


def read_help() -> str:
    try:
        with open(HELP_PATH, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        reason = exc.strerror or str(exc)
        return f"Could not read {HELP_PATH}: {reason}"


def paint_size(size: int) -> Text:
    label = largescan.human_size(size)
    if size >= 10 * largescan.ONE_GB:
        style = "bold red"
    elif size >= largescan.ONE_GB:
        style = "red"
    elif size >= 100 * largescan.ONE_MB:
        style = "yellow"
    elif size >= largescan.ONE_MB:
        style = "cyan"
    else:
        style = ""
    return Text(label, style=style, justify="right", no_wrap=True)


class PathInput(Input):
    """Path box that leaves ? free for the help binding."""

    def check_consume_key(self, key: str, character: str | None) -> bool:
        if key == "question_mark":
            return False
        return super().check_consume_key(key, character)


class HelpScreen(ModalScreen[None]):
    """Scrollable view of HELP.txt."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 80%;
        height: 80%;
        padding: 1 2;
        background: $surface;
        border: tall $accent;
    }
    #help-title {
        text-style: bold;
        height: 1;
    }
    #help-body {
        height: 1fr;
        margin: 1 0;
    }
    #close-help {
        dock: bottom;
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        text = read_help().rstrip() + f"\n\nFile: {HELP_PATH}\n"
        with Vertical(id="help-dialog"):
            yield Static("Help", id="help-title")
            with VerticalScroll(id="help-body"):
                yield Static(text, id="help-text", markup=False)
            yield Button("Close", id="close-help")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-help":
            self.dismiss()

    def action_dismiss(self) -> None:
        self.dismiss()


class LargescanApp(App[None]):
    """Browse folder, file, and orphan results in scrollable tabs."""

    TITLE = "largescan"
    SUB_TITLE = "Enter a path"

    CSS = """
    Screen {
        layout: vertical;
    }
    #controls {
        height: 3;
        padding: 0 1;
    }
    #path {
        width: 1fr;
    }
    #status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #status.error {
        color: $warning;
    }
    #tabs {
        height: 1fr;
    }
    #tabs > ContentSwitcher {
        height: 1fr;
    }
    TabPane {
        layout: vertical;
        height: 1fr;
    }
    .summary {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("question_mark", "show_help", "Help", priority=True),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        initial_path: str = "",
        library_path: str | None = None,
        catalog: largescan.AppCatalog | None = None,
    ) -> None:
        super().__init__()
        self.initial_path = initial_path or "~"
        self.library_path = library_path
        self._catalog = catalog
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="controls"):
            yield PathInput(
                value=self.initial_path,
                placeholder="Folder to scan",
                id="path",
            )
            yield Button("Scan", id="scan", variant="primary")
            yield Button("Help", id="help")
        yield Static("Enter a path and press Scan.", id="status")
        with TabbedContent(id="tabs"):
            with TabPane("Folders", id="folders"):
                yield Static("", id="folders-summary", classes="summary")
                yield DataTable(id="folders-table", fixed_columns=2)
            with TabPane("Files", id="files"):
                yield Static("", id="files-summary", classes="summary")
                yield DataTable(id="files-table", fixed_columns=2)
            with TabPane("Orphans", id="orphans"):
                yield Static("", id="orphans-summary", classes="summary")
                yield DataTable(id="orphans-table", fixed_columns=2)
        yield Footer()

    def on_mount(self) -> None:
        self._prepare_table("#folders-table", ("Rank", "Size", "Path"), (6, 10, None))
        self._prepare_table("#files-table", ("Rank", "Size", "Path"), (6, 10, None))
        self._prepare_table(
            "#orphans-table",
            ("Tier", "Size", "Path", "Detail"),
            (18, 10, None, None),
        )

    def _prepare_table(
        self,
        selector: str,
        labels: tuple[str, ...],
        widths: tuple[int | None, ...],
    ) -> None:
        table = self.query_one(selector, DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        for label, width in zip(labels, widths):
            table.add_column(label, width=width)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan":
            self.action_scan()
        elif event.button.id == "help":
            self.action_show_help()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "path":
            self.action_scan()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_scan(self) -> None:
        if self._busy:
            return
        raw = self.query_one("#path", Input).value.strip()
        if not raw:
            self._set_status("Enter a path to scan.", error=True)
            return
        root = os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
        if not os.path.isdir(root):
            self._set_status(f"Not a directory: {root}", error=True)
            return
        self._busy = True
        self.query_one("#scan", Button).disabled = True
        self._set_status(f"Scanning {root}...")
        self.run_worker(
            lambda: self._scan(root),
            thread=True,
            exclusive=True,
            name="scan",
            exit_on_error=False,
        )

    def _scan(self, root: str) -> None:
        try:
            last_update = 0.0

            def on_progress(dirs: int, files: int) -> None:
                nonlocal last_update
                now = time.monotonic()
                if now - last_update < 0.25:
                    return
                last_update = now
                self.call_from_thread(
                    self._set_status,
                    f"Scanning {root} ... {dirs:,} folders, {files:,} files",
                )

            result = largescan.scan(root, on_progress)
            library = self.library_path or os.path.expanduser("~/Library")
            report: largescan.OrphanReport | None = None
            note = ""
            if not os.path.isdir(library):
                note = f"No Library folder at {library}."
            else:
                self.call_from_thread(self._set_status, "Checking installed apps...")
                catalog, note = self._catalog_for_orphans()
                if catalog is not None:
                    report = largescan.collect_orphans(library, catalog)
            self.call_from_thread(
                self._apply_results,
                root,
                result,
                library,
                report,
                note,
            )
        except Exception as exc:
            self.call_from_thread(self._set_status, f"Scan failed: {exc}", True)
        finally:
            self.call_from_thread(self._finish_scan)

    def _catalog_for_orphans(self) -> tuple[largescan.AppCatalog | None, str]:
        if self._catalog is not None:
            return self._catalog, ""
        try:
            catalog = largescan.load_catalog()
        except Exception as exc:
            return None, f"Could not read installed apps: {exc}"
        if len(catalog.bundle_ids) < 5:
            return None, "Found too few installed apps to judge orphans."
        return catalog, ""

    def _apply_results(
        self,
        root: str,
        result: largescan.ScanResult,
        library: str,
        report: largescan.OrphanReport | None,
        note: str,
    ) -> None:
        self.title = root
        total = result.folder_sizes.get(root, 0)
        self.sub_title = largescan.human_size(total)
        folders = largescan.folders_over_one_gb(result)
        files = largescan.files_over_one_mb(result)
        self._fill_size_table("#folders-table", folders, root)
        self._fill_size_table("#files-table", files, root)
        self.query_one("#folders-summary", Static).update(
            f"{largescan.plural(len(folders), 'folder')} over 1 GB · "
            f"{largescan.human_size(total)} in this folder · "
            f"{largescan.plural(result.dir_count, 'folder')} scanned · "
            f"{largescan.plural(result.file_count, 'file')} scanned"
        )
        self.query_one("#files-summary", Static).update(
            f"{largescan.plural(len(files), 'file')} over 1 MB"
        )
        self._fill_orphans(library, report, note)
        messages: list[str] = []
        if result.dir_count == 0 and result.error_count:
            messages.append("Could not read that folder.")
        elif result.error_count:
            messages.append(_unreadable(result.error_count, result.error_samples))
        if report is not None and report.error_count:
            messages.append(_unreadable(report.error_count, report.error_samples))
        if note:
            messages.append(note)
        if messages:
            self._set_status(" ".join(messages), error=True)
        else:
            self._set_status("Scan finished.")

    def _fill_size_table(
        self,
        selector: str,
        rows: list[tuple[int, str]],
        root: str,
    ) -> None:
        table = self.query_one(selector, DataTable)
        table.clear()
        for index, (size, path) in enumerate(rows, start=1):
            table.add_row(
                Text(str(index), justify="right"),
                paint_size(size),
                Text(largescan.safe_display(largescan.relative(root, path)), no_wrap=True),
            )

    def _fill_orphans(
        self,
        library: str,
        report: largescan.OrphanReport | None,
        note: str,
    ) -> None:
        table = self.query_one("#orphans-table", DataTable)
        table.clear()
        summary = self.query_one("#orphans-summary", Static)
        if report is None:
            summary.update(note or "Orphans were not scanned.")
            return
        abandoned = sum(1 for hit in report.hits if hit.tier == "abandoned")
        review = sum(1 for hit in report.hits if hit.tier == "review")
        helpers = sum(1 for hit in report.hits if hit.tier == "helper")
        summary.update(
            f"{largescan.plural(abandoned, 'abandoned item')} · "
            f"{largescan.plural(review, 'possible leftover')} · "
            f"{largescan.plural(helpers, 'missing login helper')} · "
            f"{library}"
        )
        for hit in report.hits:
            detail = "" if hit.tier == "review" else hit.detail
            table.add_row(
                _TIER_LABELS.get(hit.tier, hit.tier),
                paint_size(hit.size),
                Text(
                    largescan.safe_display(largescan.relative(library, hit.path)),
                    no_wrap=True,
                ),
                Text(largescan.safe_display(detail), no_wrap=True),
            )

    def _set_status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#status", Static)
        status.update(message)
        status.set_class(error, "error")

    def _finish_scan(self) -> None:
        self._busy = False
        self.query_one("#scan", Button).disabled = False


def _unreadable(count: int, samples: list[str]) -> str:
    noun = "path" if count == 1 else "paths"
    sample = f" ({samples[0]})" if samples else ""
    return f"{count:,} unreadable {noun}{sample}"
