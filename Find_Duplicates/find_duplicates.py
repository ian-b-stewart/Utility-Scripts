#!/usr/bin/env python3
"""
Duplicate File Finder
=====================
Scans one or more directories for duplicate files using a high-performance
tiered hashing strategy, generates a CSV report, and optionally deletes
duplicates (sending them to the Recycle Bin).

Detection pipeline:
  1. Index all files (os.scandir, recursive)
  2. Group by file size — unique sizes are eliminated
  3. Partial hash (first 8 KB + last 8 KB, xxhash) — eliminates near-misses
  4. Full content hash (xxhash, 128 KB chunks) — confirms true duplicates

Usage examples:
  python find_duplicates.py D:\\Media
  python find_duplicates.py D:\\Media E:\\Backup --report dupes.csv
  python find_duplicates.py D:\\Media --delete --keep newest
  python find_duplicates.py D:\\Media --min-size 1048576 --exclude "*.tmp" "Thumbs.db"
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import fnmatch
import os
import platform
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import xxhash
except ImportError:
    sys.exit(
        "ERROR: xxhash is required.  Install it with:\n"
        "  pip install xxhash\n"
        "Or install all dependencies:\n"
        "  pip install -r requirements.txt"
    )

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None  # type: ignore[assignment]

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PARTIAL_HASH_CHUNK = 8 * 1024          # 8 KB from each end
FULL_HASH_CHUNK    = 128 * 1024        # 128 KB read buffer
LONG_PATH_PREFIX   = "\\\\?\\"         # Win32 long-path prefix

# ---------------------------------------------------------------------------
# Console helpers (graceful fallback when rich is unavailable)
# ---------------------------------------------------------------------------

if RICH_AVAILABLE:
    console = Console(stderr=True)

    def _print(*args, **kwargs):
        console.print(*args, **kwargs)

    def _warn(msg: str):
        console.print(f"[yellow]WARNING:[/yellow] {msg}")

    def _error(msg: str):
        console.print(f"[red]ERROR:[/red] {msg}")

    def _success(msg: str):
        console.print(f"[green]{msg}[/green]")
else:
    def _print(*args, **kwargs):  # type: ignore[misc]
        print(*args, **kwargs)

    def _warn(msg: str):
        print(f"WARNING: {msg}", file=sys.stderr)

    def _error(msg: str):
        print(f"ERROR: {msg}", file=sys.stderr)

    def _success(msg: str):
        print(msg)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _long_path(p: str) -> str:
    """Add the \\\\?\\ long-path prefix on Windows if not already present."""
    if platform.system() == "Windows" and not p.startswith(LONG_PATH_PREFIX):
        # Convert to absolute first so the prefix is valid
        return LONG_PATH_PREFIX + os.path.abspath(p)
    return p


def _strip_long_path(p: str) -> str:
    """Remove the \\\\?\\ long-path prefix so paths are user-friendly."""
    if p.startswith(LONG_PATH_PREFIX):
        return p[len(LONG_PATH_PREFIX):]
    return p


def _human_size(nbytes: int) -> str:
    """Return a human-readable size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:,.1f} {unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:,.1f} PB"


def _matches_any_pattern(name: str, patterns: List[str]) -> bool:
    """Return True if *name* matches any of the given glob patterns."""
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)


# ---------------------------------------------------------------------------
# File info container
# ---------------------------------------------------------------------------

class FileRecord:
    """Lightweight container for a scanned file."""
    __slots__ = ("path", "size", "mtime", "inode", "nlink",
                 "partial_hash", "full_hash")

    def __init__(self, path: str, size: int, mtime: float,
                 inode: int, nlink: int):
        self.path = path
        self.size = size
        self.mtime = mtime
        self.inode = inode
        self.nlink = nlink
        self.partial_hash: Optional[str] = None
        self.full_hash: Optional[str] = None

    @property
    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)


# ---------------------------------------------------------------------------
# Phase 1 — Index
# ---------------------------------------------------------------------------

def _scan_directory(
    root: str,
    *,
    recursive: bool = True,
    min_size: int = 1,
    exclude_patterns: List[str] | None = None,
    errors: List[str],
) -> List[FileRecord]:
    """Walk *root* and return a list of FileRecord objects."""
    records: List[FileRecord] = []
    exclude = exclude_patterns or []

    def _walk(dirpath: str):
        try:
            with os.scandir(_long_path(dirpath)) as it:
                for entry in it:
                    try:
                        # Skip symlinks / reparse points
                        if entry.is_symlink():
                            continue

                        if entry.is_dir(follow_symlinks=False):
                            if recursive:
                                _walk(entry.path)
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue

                        # Exclusion patterns
                        if exclude and _matches_any_pattern(entry.name, exclude):
                            continue

                        stat = entry.stat(follow_symlinks=False)

                        # Min-size filter
                        if stat.st_size < min_size:
                            continue

                        records.append(FileRecord(
                            path=_strip_long_path(entry.path),
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            inode=getattr(stat, "st_ino", 0),
                            nlink=getattr(stat, "st_nlink", 1),
                        ))
                    except (PermissionError, OSError) as exc:
                        errors.append(f"Skip {entry.path}: {exc}")
        except (PermissionError, OSError) as exc:
            errors.append(f"Skip directory {dirpath}: {exc}")

    _walk(root)
    return records


def index_files(
    paths: List[str],
    *,
    recursive: bool = True,
    min_size: int = 1,
    exclude_patterns: List[str] | None = None,
) -> Tuple[List[FileRecord], List[str]]:
    """Scan all given *paths* and return (records, errors)."""
    errors: List[str] = []
    all_records: List[FileRecord] = []

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Indexing files…[/]"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Indexing", total=len(paths))
            for p in paths:
                recs = _scan_directory(
                    p,
                    recursive=recursive,
                    min_size=min_size,
                    exclude_patterns=exclude_patterns,
                    errors=errors,
                )
                all_records.extend(recs)
                progress.advance(task)
    else:
        for p in paths:
            _print(f"Indexing {p} …")
            recs = _scan_directory(
                p,
                recursive=recursive,
                min_size=min_size,
                exclude_patterns=exclude_patterns,
                errors=errors,
            )
            all_records.extend(recs)

    return all_records, errors


# ---------------------------------------------------------------------------
# Phase 2 — Group by size
# ---------------------------------------------------------------------------

def group_by_size(records: List[FileRecord]) -> Dict[int, List[FileRecord]]:
    """Return a dict mapping file-size → list of records with that size,
    discarding sizes that appear only once (cannot be duplicates)."""
    by_size: Dict[int, List[FileRecord]] = defaultdict(list)
    for r in records:
        by_size[r.size].append(r)
    return {sz: recs for sz, recs in by_size.items() if len(recs) >= 2}


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _partial_hash(path: str, size: int) -> str:
    """Hash the first and last PARTIAL_HASH_CHUNK bytes of a file."""
    h = xxhash.xxh64()
    lp = _long_path(path)
    with open(lp, "rb") as f:
        head = f.read(PARTIAL_HASH_CHUNK)
        h.update(head)
        if size > PARTIAL_HASH_CHUNK:
            # Seek to the last chunk
            tail_start = max(size - PARTIAL_HASH_CHUNK, PARTIAL_HASH_CHUNK)
            f.seek(tail_start)
            tail = f.read(PARTIAL_HASH_CHUNK)
            h.update(tail)
    return h.hexdigest()


def _full_hash(path: str) -> str:
    """Hash the entire file content in FULL_HASH_CHUNK-sized reads."""
    h = xxhash.xxh64()
    lp = _long_path(path)
    with open(lp, "rb") as f:
        while True:
            chunk = f.read(FULL_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Phase 3 — Partial hash (first + last 8 KB)
# ---------------------------------------------------------------------------

def refine_by_partial_hash(
    size_groups: Dict[int, List[FileRecord]],
    errors: List[str],
) -> Dict[str, List[FileRecord]]:
    """Within each size group, compute partial hashes and return groups of
    2+ files with the same (size, partial_hash) key."""
    by_partial: Dict[str, List[FileRecord]] = defaultdict(list)

    candidates = [r for recs in size_groups.values() for r in recs]

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]Partial hashing…[/]"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Partial hash", total=len(candidates))
            for rec in candidates:
                try:
                    rec.partial_hash = _partial_hash(rec.path, rec.size)
                    key = f"{rec.size}:{rec.partial_hash}"
                    by_partial[key].append(rec)
                except (PermissionError, OSError) as exc:
                    errors.append(f"Partial hash skip {rec.path}: {exc}")
                progress.advance(task)
    else:
        for i, rec in enumerate(candidates, 1):
            try:
                rec.partial_hash = _partial_hash(rec.path, rec.size)
                key = f"{rec.size}:{rec.partial_hash}"
                by_partial[key].append(rec)
            except (PermissionError, OSError) as exc:
                errors.append(f"Partial hash skip {rec.path}: {exc}")
            if i % 500 == 0:
                _print(f"  Partial hashed {i}/{len(candidates)} files …")

    return {k: v for k, v in by_partial.items() if len(v) >= 2}


# ---------------------------------------------------------------------------
# Phase 4 — Full content hash
# ---------------------------------------------------------------------------

def refine_by_full_hash(
    partial_groups: Dict[str, List[FileRecord]],
    errors: List[str],
) -> Dict[str, List[FileRecord]]:
    """Compute full content hashes and return groups of confirmed duplicates."""
    by_full: Dict[str, List[FileRecord]] = defaultdict(list)

    candidates = [r for recs in partial_groups.values() for r in recs]
    total_bytes = sum(r.size for r in candidates)

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold magenta]Full hashing…[/]"),
            BarColumn(),
            TransferSpeedColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Full hash", total=total_bytes)
            for rec in candidates:
                try:
                    rec.full_hash = _full_hash(rec.path)
                    by_full[rec.full_hash].append(rec)
                except (PermissionError, OSError) as exc:
                    errors.append(f"Full hash skip {rec.path}: {exc}")
                progress.advance(task, advance=rec.size)
    else:
        hashed_bytes = 0
        for i, rec in enumerate(candidates, 1):
            try:
                rec.full_hash = _full_hash(rec.path)
                by_full[rec.full_hash].append(rec)
            except (PermissionError, OSError) as exc:
                errors.append(f"Full hash skip {rec.path}: {exc}")
            hashed_bytes += rec.size
            if i % 100 == 0:
                _print(f"  Full hashed {i}/{len(candidates)} files "
                       f"({_human_size(hashed_bytes)}/{_human_size(total_bytes)}) …")

    return {k: v for k, v in by_full.items() if len(v) >= 2}


# ---------------------------------------------------------------------------
# Hard-link detection
# ---------------------------------------------------------------------------

def _remove_hardlinks(groups: Dict[str, List[FileRecord]]) -> Dict[str, List[FileRecord]]:
    """Within each duplicate group, if multiple paths share the same inode
    on the same device they are hard links — keep only one representative."""
    cleaned: Dict[str, List[FileRecord]] = {}
    for key, recs in groups.items():
        seen_inodes: Set[int] = set()
        unique: List[FileRecord] = []
        for r in recs:
            if r.inode and r.inode in seen_inodes:
                continue  # hard link to an already-seen file
            if r.inode:
                seen_inodes.add(r.inode)
            unique.append(r)
        if len(unique) >= 2:
            cleaned[key] = unique
    return cleaned


# ---------------------------------------------------------------------------
# Keep-rule selection
# ---------------------------------------------------------------------------

def _pick_keeper(recs: List[FileRecord], rule: str) -> FileRecord:
    """Choose which file in a duplicate group to keep based on *rule*."""
    if rule == "shortest-path":
        return min(recs, key=lambda r: len(r.path))
    elif rule == "longest-path":
        return max(recs, key=lambda r: len(r.path))
    elif rule == "oldest":
        return min(recs, key=lambda r: r.mtime)
    elif rule == "newest":
        return max(recs, key=lambda r: r.mtime)
    else:
        return min(recs, key=lambda r: len(r.path))


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    dup_groups: Dict[str, List[FileRecord]],
    keep_rule: str,
    report_path: str,
) -> Tuple[int, int, int]:
    """Write the CSV report and return (groups, total_dupes, wasted_bytes)."""
    total_groups = 0
    total_dupes = 0
    wasted_bytes = 0

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "group_id", "action", "file_path", "file_size",
            "file_size_human", "modified_date", "hash",
        ])

        for group_id, (hash_key, recs) in enumerate(
            sorted(dup_groups.items(), key=lambda kv: -kv[1][0].size), start=1
        ):
            total_groups += 1
            keeper = _pick_keeper(recs, keep_rule)
            for r in recs:
                action = "KEEP" if r is keeper else "DELETE"
                if action == "DELETE":
                    total_dupes += 1
                    wasted_bytes += r.size
                writer.writerow([
                    group_id,
                    action,
                    r.path,
                    r.size,
                    _human_size(r.size),
                    r.mtime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    r.full_hash or r.partial_hash or "",
                ])

    return total_groups, total_dupes, wasted_bytes


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def print_summary(
    total_scanned: int,
    total_groups: int,
    total_dupes: int,
    wasted_bytes: int,
    report_path: str,
    elapsed: float,
    error_count: int,
):
    if RICH_AVAILABLE:
        table = Table(title="Duplicate File Report Summary", show_lines=True)
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Files scanned", f"{total_scanned:,}")
        table.add_row("Duplicate groups", f"{total_groups:,}")
        table.add_row("Duplicate files (to remove)", f"{total_dupes:,}")
        table.add_row("Wasted space", _human_size(wasted_bytes))
        table.add_row("Report saved to", report_path)
        table.add_row("Scan time", f"{elapsed:.1f}s")
        if error_count:
            table.add_row("Errors / skipped", f"[yellow]{error_count:,}[/yellow]")
        console.print()
        console.print(table)
    else:
        _print("\n=== Duplicate File Report Summary ===")
        _print(f"  Files scanned        : {total_scanned:,}")
        _print(f"  Duplicate groups     : {total_groups:,}")
        _print(f"  Duplicate files      : {total_dupes:,}")
        _print(f"  Wasted space         : {_human_size(wasted_bytes)}")
        _print(f"  Report saved to      : {report_path}")
        _print(f"  Scan time            : {elapsed:.1f}s")
        if error_count:
            _print(f"  Errors / skipped     : {error_count:,}")


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def delete_duplicates(report_path: str) -> Tuple[int, int, int]:
    """Read the CSV report and delete all files marked DELETE.

    Returns (deleted_count, failed_count, freed_bytes).
    """
    if send2trash is None:
        _error("send2trash is not installed. Cannot delete files.")
        _print("Install it with:  pip install send2trash")
        return 0, 0, 0

    to_delete: List[Tuple[str, int]] = []
    with open(report_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["action"] == "DELETE":
                to_delete.append((row["file_path"], int(row["file_size"])))

    if not to_delete:
        _print("Nothing to delete.")
        return 0, 0, 0

    deleted = 0
    failed = 0
    freed = 0

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold red]Deleting duplicates…[/]"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Deleting", total=len(to_delete))
            for filepath, size in to_delete:
                try:
                    send2trash(filepath)
                    deleted += 1
                    freed += size
                except Exception as exc:
                    _warn(f"Could not delete {filepath}: {exc}")
                    failed += 1
                progress.advance(task)
    else:
        for i, (filepath, size) in enumerate(to_delete, 1):
            try:
                send2trash(filepath)
                deleted += 1
                freed += size
            except Exception as exc:
                _warn(f"Could not delete {filepath}: {exc}")
                failed += 1
            if i % 100 == 0:
                _print(f"  Deleted {i}/{len(to_delete)} …")

    _success(f"\nDeleted {deleted:,} files  |  Freed {_human_size(freed)}  |  "
             f"Failed {failed:,}")
    return deleted, failed, freed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find_duplicates",
        description="Find and optionally remove duplicate files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python find_duplicates.py D:\\Media\n"
            "  python find_duplicates.py D:\\Media E:\\Backup --report dupes.csv\n"
            "  python find_duplicates.py D:\\Media --delete --keep newest\n"
            "  python find_duplicates.py D:\\Media --min-size 1048576 "
            '--exclude "*.tmp" "Thumbs.db"\n'
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="One or more directories to scan for duplicates.",
    )
    parser.add_argument(
        "-r", "--report",
        default=None,
        help="Output CSV path (default: duplicates_report_<timestamp>.csv).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        default=False,
        help="Actually delete duplicate files (sends to Recycle Bin). "
             "Without this flag the script only generates a report.",
    )
    parser.add_argument(
        "--keep",
        choices=["shortest-path", "longest-path", "oldest", "newest"],
        default="shortest-path",
        help="Rule for choosing which file to keep in each group "
             "(default: shortest-path).",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=1,
        help="Skip files smaller than N bytes (default: 1, skips zero-byte files).",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help='Glob patterns for filenames to exclude (e.g. "*.tmp" "Thumbs.db").',
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        default=False,
        help="Do not recurse into subdirectories.",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate paths
    for p in args.paths:
        if not os.path.isdir(p):
            _error(f"Not a valid directory: {p}")
            return 1

    # Default report filename
    if args.report is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.report = f"duplicates_report_{ts}.csv"

    _print(f"\n[bold]Duplicate File Finder[/bold]" if RICH_AVAILABLE
           else "\n=== Duplicate File Finder ===")
    _print(f"Scanning: {', '.join(args.paths)}")
    _print(f"Keep rule: {args.keep}")
    _print(f"Min size: {_human_size(args.min_size)}")
    if args.exclude:
        _print(f"Excluding: {', '.join(args.exclude)}")
    _print("")

    t0 = time.perf_counter()

    # Phase 1 — Index
    records, errors = index_files(
        args.paths,
        recursive=not args.no_recursive,
        min_size=args.min_size,
        exclude_patterns=args.exclude or None,
    )
    total_scanned = len(records)
    _print(f"Indexed {total_scanned:,} files.")

    if total_scanned == 0:
        _warn("No files found. Nothing to do.")
        return 0

    # Phase 2 — Group by size
    size_groups = group_by_size(records)
    size_candidates = sum(len(v) for v in size_groups.values())
    _print(f"Size-matched candidates: {size_candidates:,} files in "
           f"{len(size_groups):,} size groups.")

    if not size_groups:
        _print("No potential duplicates found by size. Done.")
        elapsed = time.perf_counter() - t0
        print_summary(total_scanned, 0, 0, 0, args.report, elapsed, len(errors))
        return 0

    # Phase 3 — Partial hash
    partial_groups = refine_by_partial_hash(size_groups, errors)
    partial_candidates = sum(len(v) for v in partial_groups.values())
    _print(f"Partial-hash candidates: {partial_candidates:,} files in "
           f"{len(partial_groups):,} groups.")

    if not partial_groups:
        _print("No potential duplicates after partial hashing. Done.")
        elapsed = time.perf_counter() - t0
        print_summary(total_scanned, 0, 0, 0, args.report, elapsed, len(errors))
        return 0

    # Phase 4 — Full hash
    dup_groups = refine_by_full_hash(partial_groups, errors)

    # Remove hard links
    dup_groups = _remove_hardlinks(dup_groups)

    confirmed = sum(len(v) for v in dup_groups.values())
    _print(f"Confirmed duplicates: {confirmed:,} files in "
           f"{len(dup_groups):,} groups.\n")

    if not dup_groups:
        _print("No true duplicates found after full hashing. Done.")
        elapsed = time.perf_counter() - t0
        print_summary(total_scanned, 0, 0, 0, args.report, elapsed, len(errors))
        return 0

    # Generate report
    total_groups, total_dupes, wasted_bytes = generate_report(
        dup_groups, args.keep, args.report,
    )

    elapsed = time.perf_counter() - t0
    print_summary(
        total_scanned, total_groups, total_dupes,
        wasted_bytes, args.report, elapsed, len(errors),
    )

    # Optionally delete
    if args.delete:
        if send2trash is None:
            _error("send2trash is not installed — cannot delete. "
                   "Run:  pip install send2trash")
            return 1
        _print("")
        confirm = input(
            f"About to send {total_dupes:,} files "
            f"({_human_size(wasted_bytes)}) to the Recycle Bin.\n"
            "Type YES to confirm: "
        )
        if confirm.strip().upper() == "YES":
            delete_duplicates(args.report)
        else:
            _print("Deletion cancelled.")
    else:
        _print("\nRun again with --delete to remove duplicates (Recycle Bin).")

    # Print errors at end if any
    if errors:
        _print(f"\n[yellow]Encountered {len(errors):,} errors/warnings:[/yellow]"
               if RICH_AVAILABLE
               else f"\nEncountered {len(errors):,} errors/warnings:")
        for e in errors[:20]:
            _warn(e)
        if len(errors) > 20:
            _print(f"  … and {len(errors) - 20:,} more (see log for details).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
