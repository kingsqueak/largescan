"""Mac-only browser for large folders, large files, and leftover Library data.

Usage:
    largescan
    largescan PATH
    largescan --help

Opens a terminal window with three scrollable tabs: Folders, Files, and
Orphans. PATH, when given, fills the path box. The header shows the path
after a scan. The orphan tab always reads ~/Library. Nothing is deleted.
Symbolic links are not followed. Sizes use 1024-based units.

--help prints HELP.txt and exits 0. A missing or extra argument, or an
unknown option, exits 2. On any system other than macOS the program
prints an error and exits 1.
"""

from __future__ import annotations

import argparse
import errno
import os
import plistlib
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, TextIO

ONE_KB = 1024
ONE_MB = 1024 * 1024
ONE_GB = 1024 * 1024 * 1024
MAX_ERROR_SAMPLES = 5

Progress = Callable[[int, int], None]


def human_size(size: int) -> str:
    """Format a byte count with 1024-based units."""
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    value = float(size)
    unit_index = 0
    # 1023.95 rounds to 1024.0 at one decimal, so carry into the next unit.
    while unit_index < len(units) - 1 and value >= 1023.95:
        value /= ONE_KB
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} B"
    return f"{value:.1f} {units[unit_index]}"


def plural(count: int, singular: str) -> str:
    word = singular if count == 1 else singular + "s"
    return f"{count:,} {word}"


def safe_display(text: str) -> str:
    """Keep one filesystem name on one terminal line."""
    cleaned = text.replace("\r", "\\r").replace("\n", "\\n")
    return cleaned.encode("utf-8", "backslashreplace").decode("utf-8")


def relative(root: str, path: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


class Style:
    """ANSI colors, disabled when output is piped or the user asks."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _paint(self, code: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self._paint("1", text)

    def dim(self, text: str) -> str:
        return self._paint("2", text)

    def red(self, text: str) -> str:
        return self._paint("31", text)

    def yellow(self, text: str) -> str:
        return self._paint("33", text)

    def bold_cyan(self, text: str) -> str:
        return self._paint("1;36", text)

    def size(self, size: int, text: str) -> str:
        if size >= 10 * ONE_GB:
            return self._paint("1;31", text)
        if size >= ONE_GB:
            return self._paint("31", text)
        if size >= 100 * ONE_MB:
            return self._paint("33", text)
        if size >= ONE_MB:
            return self._paint("36", text)
        return text


@dataclass
class _Frame:
    path: str
    iterator: object
    total: int


@dataclass
class ScanResult:
    root: str
    folder_sizes: dict[str, int]
    large_files: list[tuple[int, str]]
    error_count: int
    error_samples: list[str]
    file_count: int
    dir_count: int


def scan(root: str, on_progress: Progress | None = None) -> ScanResult:
    """Walk root. Folder totals include every file in that subtree.

    Directory inodes are remembered so a bind mount cannot recurse forever.
    """
    folder_sizes: dict[str, int] = {}
    large_files: list[tuple[int, str]] = []
    error_samples: list[str] = []
    error_count = 0
    file_count = 0
    dir_count = 0
    seen_dirs: set[tuple[int, int]] = set()
    frames: list[_Frame] = []

    def note_error(path: str, exc: OSError) -> None:
        nonlocal error_count
        error_count += 1
        if len(error_samples) < MAX_ERROR_SAMPLES:
            reason = exc.strerror or str(exc)
            error_samples.append(safe_display(f"{path}: {reason}"))

    def finish_result() -> ScanResult:
        return ScanResult(
            root=root,
            folder_sizes=folder_sizes,
            large_files=large_files,
            error_count=error_count,
            error_samples=error_samples,
            file_count=file_count,
            dir_count=dir_count,
        )

    def finish_dir() -> None:
        frame = frames.pop()
        try:
            frame.iterator.close()
        finally:
            folder_sizes[frame.path] = frame.total
            if frames:
                frames[-1].total += frame.total
        if on_progress is not None:
            on_progress(dir_count, file_count)

    try:
        root_stat = os.stat(root)
    except OSError as exc:
        note_error(root, exc)
        return finish_result()

    if not stat.S_ISDIR(root_stat.st_mode):
        note_error(
            root,
            OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)),
        )
        return finish_result()

    try:
        root_it = os.scandir(root)
    except OSError as exc:
        note_error(root, exc)
        return finish_result()

    seen_dirs.add((root_stat.st_dev, root_stat.st_ino))
    frames.append(_Frame(root, root_it, 0))
    dir_count = 1

    try:
        while frames:
            frame = frames[-1]
            try:
                entry = next(frame.iterator)
            except StopIteration:
                finish_dir()
                continue
            except OSError as exc:
                note_error(frame.path, exc)
                finish_dir()
                continue

            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                note_error(entry.path, exc)
                continue

            mode = info.st_mode
            if stat.S_ISDIR(mode):
                ident = (info.st_dev, info.st_ino)
                if ident in seen_dirs:
                    continue
                seen_dirs.add(ident)
                try:
                    child_it = os.scandir(entry.path)
                except OSError as exc:
                    note_error(entry.path, exc)
                    continue
                frames.append(_Frame(entry.path, child_it, 0))
                dir_count += 1
                continue

            if not stat.S_ISREG(mode):
                continue

            file_count += 1
            frame.total += info.st_size
            if info.st_size > ONE_MB:
                large_files.append((info.st_size, entry.path))
            if on_progress is not None and (file_count & 255) == 0:
                on_progress(dir_count, file_count)

        if on_progress is not None:
            on_progress(dir_count, file_count)
    finally:
        while frames:
            leftover = frames.pop()
            try:
                leftover.iterator.close()
            except OSError:
                pass

    return finish_result()


def folders_over_one_gb(result: ScanResult) -> list[tuple[int, str]]:
    """Every subfolder whose files total more than 1 GB, largest first."""
    rows = [
        (size, path)
        for path, size in result.folder_sizes.items()
        if path != result.root and size > ONE_GB
    ]
    rows.sort(key=lambda item: (-item[0], item[1]))
    return rows


def files_over_one_mb(result: ScanResult) -> list[tuple[int, str]]:
    rows = [item for item in result.large_files if item[0] > ONE_MB]
    rows.sort(key=lambda item: (-item[0], item[1]))
    return rows


def use_color(mode: str, stream: TextIO) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return bool(stream.isatty())


def _write_section(
    out: TextIO,
    style: Style,
    title: str,
    rows: list[tuple[int, str]],
    root: str,
    empty_message: str,
) -> None:
    print(style.bold(title), file=out)
    if not rows:
        print(style.dim(f"  {empty_message}"), file=out)
        return

    labels = [human_size(size) for size, _path in rows]
    size_width = max(len(label) for label in labels)
    rank_width = len(str(len(rows)))
    for index, ((size, path), label) in enumerate(zip(rows, labels), start=1):
        rank = f"{index:>{rank_width}}"
        sized = f"{label:>{size_width}}"
        shown = safe_display(relative(root, path))
        print(
            f"  {style.dim(rank)}  {style.size(size, sized)}  {shown}",
            file=out,
        )


def print_report(result: ScanResult, style: Style, out: TextIO) -> None:
    total = result.folder_sizes.get(result.root, 0)
    large = files_over_one_mb(result)
    over = len(large)
    over_text = "1 over 1 MB" if over == 1 else f"{over:,} over 1 MB"
    summary = [
        style.size(total, human_size(total)) + " total",
        f"{plural(result.dir_count, 'folder')} scanned",
        f"{plural(result.file_count, 'file')} scanned",
        over_text,
    ]
    print(style.bold_cyan(safe_display(result.root)), file=out)
    print("  " + style.dim(" · ").join(summary), file=out)
    print(file=out)
    _write_section(
        out,
        style,
        "Folders over 1 GB",
        folders_over_one_gb(result),
        result.root,
        "No folders larger than 1 GB.",
    )
    print(file=out)
    _write_section(
        out,
        style,
        "Files over 1 MB",
        large,
        result.root,
        "No files larger than 1 MB.",
    )


def print_errors(result: ScanResult, style: Style, err: TextIO) -> None:
    count = result.error_count
    if count == 0:
        return
    label = "1 unreadable path" if count == 1 else f"{count:,} unreadable paths"
    print(style.yellow(label), file=err)
    for sample in result.error_samples:
        print(style.dim(f"  {sample}"), file=err)
    extra = count - len(result.error_samples)
    if extra:
        print(style.dim(f"  and {extra:,} more"), file=err)


# Reverse-DNS labels used by macOS and developer tools, not removable apps.
_SKIP_ID_PREFIXES = (
    "com.apple.",
    "org.cups.",
    "org.swift.",
    "org.freedesktop.",
    "com.juce.",
    "is.workflow.",
)

# Folder names created by macOS or toolchains. Matched after punctuation is removed.
_SKIP_NAMES = {
    "addressbook",
    "animoji",
    "appstore",
    "arfilecache",
    "askpermissiond",
    "assistant",
    "baseband",
    "cache",
    "caches",
    "callhistorydb",
    "callhistorytransactions",
    "cef",
    "clouddocs",
    "cloudkit",
    "contactsd",
    "controlcenter",
    "coresimulator",
    "crashpad",
    "crashpadhandler",
    "crashreporter",
    "diagnosticreports",
    "differentialprivacy",
    "diskimages",
    "electron",
    "energykit",
    "facetime",
    "familycircle",
    "familycircled",
    "fileprovider",
    "gamekit",
    "geoservices",
    "gmpmpfrsys",
    "homebrew",
    "homeenergyd",
    "icdd",
    "icloud",
    "identityservicesd",
    "jetpackcache",
    "jna",
    "knowledge",
    "locationaccessstored",
    "lsmimagecache",
    "mobilesync",
    "networkserviceproxy",
    "numba",
    "passkit",
    "pbs",
    "privacypreservingmeasurement",
    "sharedfilelistd",
    "sharedimagecache",
    "sirittsservice",
    "spotlight",
    "stickersd",
    "swiftfrontend",
    "syncservices",
    "temp",
    "tipsd",
    "appplaceholdersyncd",
    "diagnosticsagent",
    "featuresconfig",
    "intelligenceflow",
    "localizationswitcherd",
    "loginwindow",
    "nobackup",
    "sirittss",
    "trickplay",
    "tvappbag",
}

_UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")
_BYHOST_SUFFIX = re.compile(
    r"\.[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
_NAME_SUFFIX = re.compile(r"\.(app|com|org|net|io|us)$", re.IGNORECASE)

LIBRARY_AREAS = (
    "Application Support",
    "Caches",
    "Containers",
    "Group Containers",
    "Preferences",
    "Saved Application State",
    "Logs",
    "HTTPStorages",
    "Application Scripts",
    "LaunchAgents",
)
# Name-shaped leftovers are only reported in folders that hold app data.
_NAME_REVIEW_AREAS = {
    "Application Support",
    "Caches",
    "Logs",
    "Preferences",
    "HTTPStorages",
}


@dataclass
class AppCatalog:
    bundle_ids: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)


@dataclass
class OrphanHit:
    tier: str
    path: str
    size: int
    detail: str


@dataclass
class OrphanReport:
    library: str
    app_count: int
    hits: list[OrphanHit]
    error_count: int
    error_samples: list[str]


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def name_keys(value: str) -> set[str]:
    keys = set()
    full = normalize_name(value)
    if full:
        keys.add(full)
    trimmed = _NAME_SUFFIX.sub("", value)
    short = normalize_name(trimmed)
    if short:
        keys.add(short)
    return keys


def looks_like_bundle_id(value: str) -> bool:
    if value.count(".") < 2 or _UUID.fullmatch(value):
        return False
    labels = value.split(".")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", labels[0]):
        return False
    for label in labels[1:]:
        if not label or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _+-]*", label):
            return False
    return True


def is_ignored_identifier(identifier: str) -> bool:
    lowered = identifier.lower()
    if lowered == "com.apple" or ".com.apple." in f".{lowered}.":
        return True
    return any(lowered.startswith(prefix) for prefix in _SKIP_ID_PREFIXES)


def bundle_id_from_name(name: str) -> str | None:
    """Pull a bundle identifier out of a Library file or folder name."""
    if _UUID.fullmatch(name):
        return None
    base = name
    for suffix in (".binarycookies", ".savedState", ".plist"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    base = _BYHOST_SUFFIX.sub("", base)
    team, separator, rest = base.partition(".")
    if separator and _TEAM_ID.fullmatch(team):
        base = rest
    if base.startswith("group."):
        base = base[len("group.") :]
    if looks_like_bundle_id(base):
        return base
    return None


def _without_version_suffix(identifier: str) -> str | None:
    """org.gimp.gimp-3.0 belongs to the installed app org.gimp.gimp.

    The version's dots are bundle-id separators, so this cannot split on
    dots first.
    """
    match = re.fullmatch(r"(.+)-\d+(?:\.\d+)*", identifier)
    if not match or match.group(1).count(".") < 1:
        return None
    return match.group(1)


def _identifier_is_live(identifier: str, catalog: AppCatalog) -> bool:
    if identifier in catalog.bundle_ids:
        return True
    labels = identifier.split(".")
    for end in range(len(labels) - 1, 1, -1):
        parent = ".".join(labels[:end])
        if parent in catalog.bundle_ids:
            return True
    prefix = identifier + "."
    return any(app_id.startswith(prefix) for app_id in catalog.bundle_ids)


def is_live_identifier(identifier: str, catalog: AppCatalog) -> bool:
    if _identifier_is_live(identifier, catalog):
        return True
    plain = _without_version_suffix(identifier)
    return bool(plain) and _identifier_is_live(plain, catalog)


def is_ignored_name(name: str) -> bool:
    return bool(name_keys(name) & _SKIP_NAMES) or normalize_name(name) in _SKIP_NAMES


def is_live_name(name: str, catalog: AppCatalog) -> bool:
    keys = name_keys(name)
    if keys & catalog.names:
        return True
    for key in keys:
        if len(key) < 4:
            continue
        if key in catalog.components:
            return True
        for label in catalog.names:
            if len(label) < 4:
                continue
            if len(key) >= 5 and len(label) >= 5 and (
                label.startswith(key) or key.startswith(label)
            ):
                return True
        # "Zeus" belongs with com.zeussdr.link; require 4 characters so
        # short tokens such as "pro" do not match everything.
        if len(key) >= 4:
            for component in catalog.components:
                if len(component) < 4:
                    continue
                if component.startswith(key) or key.startswith(component):
                    return True
    return False


def classify_entry(
    area: str,
    name: str,
    is_dir: bool,
    catalog: AppCatalog,
) -> tuple[str, str] | None:
    """Return ('abandoned' or 'review', detail), or None to keep the entry."""
    if name.startswith((".", "_")) or name == "ByHost":
        return None
    if area == "Logs" and (not is_dir or name.endswith((".log", ".txt", ".aapbz"))):
        return None
    if area == "Preferences" and not is_dir and not name.endswith(".plist"):
        return None

    identifier = bundle_id_from_name(name)
    if identifier is not None:
        if is_ignored_identifier(identifier) or is_live_identifier(identifier, catalog):
            return None
        return "abandoned", identifier

    if area not in _NAME_REVIEW_AREAS:
        return None
    if area == "Preferences" and not is_dir:
        return None
    if is_ignored_name(name) or is_live_name(name, catalog):
        return None
    return "review", name


def launch_agent_program(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = plistlib.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    program = data.get("Program")
    if isinstance(program, str) and program.strip():
        return os.path.expanduser(program)
    arguments = data.get("ProgramArguments")
    if (
        isinstance(arguments, list)
        and arguments
        and isinstance(arguments[0], str)
        and arguments[0].strip()
    ):
        return os.path.expanduser(arguments[0])
    return None


def total_file_size(path: str) -> tuple[int, int, list[str]]:
    """Return (byte total, error count, up to five error messages)."""
    if os.path.islink(path):
        return 0, 0, []
    try:
        info = os.lstat(path)
    except OSError as exc:
        reason = exc.strerror or str(exc)
        return 0, 1, [safe_display(f"{path}: {reason}")]
    if stat.S_ISREG(info.st_mode):
        return info.st_size, 0, []
    if not stat.S_ISDIR(info.st_mode):
        return 0, 0, []

    total = 0
    error_count = 0
    samples: list[str] = []
    seen: set[tuple[int, int]] = {(info.st_dev, info.st_ino)}
    stack: list[tuple[object, str]] = []
    try:
        stack.append((os.scandir(path), path))
    except OSError as exc:
        reason = exc.strerror or str(exc)
        return 0, 1, [safe_display(f"{path}: {reason}")]

    def note(item_path: str, exc: OSError) -> None:
        nonlocal error_count
        error_count += 1
        if len(samples) < MAX_ERROR_SAMPLES:
            reason = exc.strerror or str(exc)
            samples.append(safe_display(f"{item_path}: {reason}"))

    try:
        while stack:
            iterator, current = stack[-1]
            try:
                entry = next(iterator)
            except StopIteration:
                iterator.close()
                stack.pop()
                continue
            except OSError as exc:
                note(current, exc)
                iterator.close()
                stack.pop()
                continue
            try:
                item = entry.stat(follow_symlinks=False)
            except OSError as exc:
                note(entry.path, exc)
                continue
            if stat.S_ISLNK(item.st_mode):
                continue
            if stat.S_ISDIR(item.st_mode):
                ident = (item.st_dev, item.st_ino)
                if ident in seen:
                    continue
                seen.add(ident)
                try:
                    stack.append((os.scandir(entry.path), entry.path))
                except OSError as exc:
                    note(entry.path, exc)
                continue
            if stat.S_ISREG(item.st_mode):
                total += item.st_size
    finally:
        while stack:
            leftover, _current = stack.pop()
            try:
                leftover.close()
            except OSError:
                pass
    return total, error_count, samples


def default_app_roots() -> list[str]:
    return [
        "/Applications",
        os.path.expanduser("~/Applications"),
        "/System/Applications",
        "/System/Applications/Utilities",
    ]


def _apps_under(root: str, max_depth: int = 3) -> list[str]:
    found: list[str] = []
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return found
    base_depth = root.count(os.sep)
    for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
        depth = dirpath.count(os.sep) - base_depth
        for name in dirnames:
            if name.endswith(".app"):
                found.append(os.path.join(dirpath, name))
        dirnames[:] = [
            name
            for name in dirnames
            if not name.endswith(".app") and depth < max_depth
        ]
    return found


def _spotlight_apps() -> list[str]:
    try:
        completed = subprocess.run(
            ["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [
        line
        for line in completed.stdout.splitlines()
        if line.endswith(".app") and os.path.isdir(line)
    ]


def _nested_bundles(app_path: str) -> list[str]:
    """Extensions and helpers ship inside the app and have their own ids."""
    found: list[str] = []
    contents = os.path.join(app_path, "Contents")
    relatives = (
        "PlugIns",
        "Extensions",
        "XPCServices",
        "Library/LoginItems",
        "Library/SystemExtensions",
    )
    for relative in relatives:
        folder = os.path.join(contents, relative)
        if not os.path.isdir(folder):
            continue
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.name.endswith((".appex", ".xpc", ".app", ".bundle")):
                found.append(entry.path)
    return found


def _add_app(catalog: AppCatalog, app_path: str) -> None:
    stem = os.path.splitext(os.path.basename(app_path))[0]
    labels = [stem]
    info_path = os.path.join(app_path, "Contents", "Info.plist")
    try:
        with open(info_path, "rb") as handle:
            info = plistlib.load(handle)
    except Exception:
        # A damaged Info.plist should not abort the whole catalog.
        info = None
    if isinstance(info, dict):
        identifier = info.get("CFBundleIdentifier")
        if isinstance(identifier, str) and "." in identifier.strip():
            catalog.bundle_ids.add(identifier.strip())
        for key in ("CFBundleDisplayName", "CFBundleName"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
                break
    for label in labels:
        normalized = normalize_name(label)
        if len(normalized) >= 2:
            catalog.names.add(normalized)


def load_catalog(
    app_roots: list[str] | None = None,
    query_spotlight: bool = True,
) -> AppCatalog:
    """Collect bundle identifiers and names of apps installed on this Mac."""
    roots = default_app_roots() if app_roots is None else app_roots
    paths: list[str] = []
    for root in roots:
        paths.extend(_apps_under(root))
    if query_spotlight:
        paths.extend(_spotlight_apps())

    catalog = AppCatalog()
    seen: set[str] = set()
    for path in paths:
        normalized = os.path.normpath(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        _add_app(catalog, normalized)
        if normalized.endswith(".app"):
            for nested in _nested_bundles(normalized):
                nested_key = os.path.normpath(nested)
                if nested_key in seen:
                    continue
                seen.add(nested_key)
                _add_app(catalog, nested_key)
    for identifier in catalog.bundle_ids:
        for part in identifier.split("."):
            component = normalize_name(part)
            if len(component) >= 4:
                catalog.components.add(component)
    return catalog


def _record_error(
    error_count: int,
    samples: list[str],
    new_count: int,
    new_samples: list[str],
) -> int:
    for sample in new_samples:
        if len(samples) < MAX_ERROR_SAMPLES:
            samples.append(sample)
    return error_count + new_count


def collect_orphans(library: str, catalog: AppCatalog) -> OrphanReport:
    hits: list[OrphanHit] = []
    error_count = 0
    samples: list[str] = []

    for area in LIBRARY_AREAS:
        area_path = os.path.join(library, area)
        if os.path.islink(area_path) or not os.path.isdir(area_path):
            continue
        try:
            children = list(os.scandir(area_path))
        except OSError as exc:
            reason = exc.strerror or str(exc)
            error_count += 1
            if len(samples) < MAX_ERROR_SAMPLES:
                samples.append(safe_display(f"{area_path}: {reason}"))
            continue

        entries: list[tuple[str, str, bool]] = []
        for entry in children:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                reason = exc.strerror or str(exc)
                error_count += 1
                if len(samples) < MAX_ERROR_SAMPLES:
                    samples.append(safe_display(f"{entry.path}: {reason}"))
                continue
            entries.append((entry.path, entry.name, is_dir))
        if area == "Preferences":
            byhost = os.path.join(area_path, "ByHost")
            if os.path.isdir(byhost) and not os.path.islink(byhost):
                try:
                    host_children = list(os.scandir(byhost))
                except OSError as exc:
                    reason = exc.strerror or str(exc)
                    error_count += 1
                    if len(samples) < MAX_ERROR_SAMPLES:
                        samples.append(safe_display(f"{byhost}: {reason}"))
                    host_children = []
                entries.extend(
                    (entry.path, entry.name, False) for entry in host_children
                )

        for path, name, is_dir in entries:
            if area == "LaunchAgents":
                if not name.endswith(".plist"):
                    continue
                program = launch_agent_program(path)
                if not program or os.path.exists(program):
                    continue
                size, problems, messages = total_file_size(path)
                error_count = _record_error(error_count, samples, problems, messages)
                hits.append(OrphanHit("helper", path, size, program))
                continue

            classified = classify_entry(area, name, is_dir, catalog)
            if classified is None:
                continue
            tier, detail = classified
            size, problems, messages = total_file_size(path)
            error_count = _record_error(error_count, samples, problems, messages)
            if size <= 0:
                continue
            hits.append(OrphanHit(tier, path, size, detail))

    hits.sort(key=lambda item: (-item.size, item.path))
    return OrphanReport(library, len(catalog.bundle_ids), hits, error_count, samples)


def _print_hit_rows(
    out: TextIO,
    style: Style,
    library: str,
    rows: list[OrphanHit],
) -> None:
    labels = [human_size(item.size) for item in rows]
    size_width = max(len(label) for label in labels)
    rank_width = len(str(len(rows)))
    for index, (item, label) in enumerate(zip(rows, labels), start=1):
        rank = f"{index:>{rank_width}}"
        sized = f"{label:>{size_width}}"
        shown = safe_display(relative(library, item.path))
        print(
            f"  {style.dim(rank)}  {style.size(item.size, sized)}  {shown}",
            file=out,
        )
        if item.tier == "helper":
            print(style.dim(f"     missing {safe_display(item.detail)}"), file=out)


def print_orphan_report(report: OrphanReport, style: Style, out: TextIO) -> None:
    abandoned = [item for item in report.hits if item.tier == "abandoned"]
    review = [item for item in report.hits if item.tier == "review"]
    helpers = [item for item in report.hits if item.tier == "helper"]
    summary = [
        f"{plural(report.app_count, 'installed app')}",
        f"{plural(len(abandoned), 'abandoned item')}",
        f"{plural(len(review), 'possible leftover')}",
        f"{plural(len(helpers), 'missing login helper')}",
    ]
    print(style.bold_cyan(safe_display(report.library)), file=out)
    print(style.dim("  Read-only. Nothing is deleted."), file=out)
    print("  " + style.dim(" · ").join(summary), file=out)

    sections = (
        (
            "Abandoned",
            style.bold,
            "Bundle identifiers with no installed app.",
            abandoned,
            "No abandoned bundle identifiers.",
        ),
        (
            "Possible leftovers",
            style.yellow,
            "Folder names that match no installed app. A shared vendor folder can outlive one app.",
            review,
            "No unmatched folder names.",
        ),
        (
            "Missing login helpers",
            style.yellow,
            "Launch agents whose program is gone.",
            helpers,
            "No missing login helpers.",
        ),
    )
    for title, paint, blurb, rows, empty in sections:
        print(file=out)
        print(paint(title), file=out)
        print(style.dim(f"  {blurb}"), file=out)
        if rows:
            _print_hit_rows(out, style, report.library, rows)
        else:
            print(style.dim(f"  {empty}"), file=out)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively scan a directory and list every folder larger "
            "than 1 GB and every file larger than 1 MB (1,048,576 bytes). "
            "With --orphans, list ~/Library data whose app is no longer installed."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s ~/Downloads\n"
            "  %(prog)s --orphans\n"
            "  %(prog)s --orphans ~/Library"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Directory to scan. With --orphans, the Library folder (default: ~/Library)",
    )
    parser.add_argument(
        "--orphans",
        action="store_true",
        help="List Library data abandoned by apps that are no longer installed",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="When to colorize output (default: auto)",
    )
    return parser.parse_args(argv)


def _silence_broken_pipe() -> None:
    try:
        sys.stdout.close()
    except OSError:
        pass
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass
    finally:
        os.close(devnull)


def _run_orphans(
    library_arg: str,
    output: TextIO,
    error: TextIO,
    err_style: Style,
    out_style: Style,
) -> int:
    library = os.path.normpath(os.path.abspath(os.path.expanduser(library_arg)))
    if not os.path.exists(library):
        print(err_style.red(f"No such path: {library}"), file=error)
        return 2
    if not os.path.isdir(library):
        print(err_style.red(f"Not a directory: {library}"), file=error)
        return 2

    progress_on = error.isatty()
    if progress_on:
        error.write("Reading installed apps...\n")
        error.flush()
    catalog = load_catalog()
    if len(catalog.bundle_ids) < 5:
        print(
            err_style.red(
                "Found too few installed apps to decide what is abandoned."
            ),
            file=error,
        )
        return 1
    if progress_on:
        error.write("Checking Library for abandoned app data...\n")
        error.flush()
    try:
        report = collect_orphans(library, catalog)
    except KeyboardInterrupt:
        print("Scan interrupted.", file=error)
        return 130
    try:
        print_orphan_report(report, out_style, output)
    except BrokenPipeError:
        _silence_broken_pipe()
        return 0
    print_errors(
        ScanResult(
            root=library,
            folder_sizes={},
            large_files=[],
            error_count=report.error_count,
            error_samples=report.error_samples,
            file_count=0,
            dir_count=0,
        ),
        err_style,
        error,
    )
    return 1 if report.error_count else 0


def _read_help_file() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HELP.txt")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(
    argv: list[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output = sys.stdout if out is None else out
    error = sys.stderr if err is None else err

    if sys.platform != "darwin":
        print("largescan runs on macOS only.", file=error)
        return 1

    if "-h" in args or "--help" in args:
        try:
            text = _read_help_file()
        except OSError as exc:
            reason = exc.strerror or str(exc)
            help_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "HELP.txt"
            )
            print(f"Could not read {help_path}: {reason}", file=error)
            return 1
        output.write(text)
        if not text.endswith("\n"):
            output.write("\n")
        return 0

    unknown = next((arg for arg in args if arg.startswith("-")), None)
    if unknown is not None:
        print(f"Unknown option: {unknown}", file=error)
        return 2
    if len(args) > 1:
        print("Give one path, or none.", file=error)
        return 2

    try:
        from .ui import LargescanApp
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        print(
            "Textual is not installed. From the largescan folder, run:\n"
            "    pip3 install -r requirements.txt\n"
            "Or install the largescan command with:\n"
            "    pipx install --editable .",
            file=error,
        )
        return 1

    LargescanApp(initial_path=args[0] if args else "").run()
    return 0


def cli() -> None:
    """Entry point for the largescan command."""
    try:
        sys.exit(main())
    except BrokenPipeError:
        _silence_broken_pipe()
        sys.exit(0)
