#!/usr/bin/env python3
"""Strip job-id / playlist-id prefixes from filenames in the download folder.

Patterns removed (leading prefix only):
  <12-hex>-           single-track jobs
  pl<10-hex>-NNN-     playlist tracks (NNN = 3-digit index)

Usage:
  python scripts/clean_filenames.py            # dry-run
  python scripts/clean_filenames.py --apply    # actually rename
  python scripts/clean_filenames.py --dir PATH # override folder
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PREFIX_RE = re.compile(r"^(?:pl[0-9a-f]{10}-\d{3}-|[0-9a-f]{12}-)")


def unique_path(directory: Path, base: str, ext: str) -> Path:
    base = base.strip() or "track"
    p = directory / f"{base}{ext}"
    i = 2
    while p.exists():
        p = directory / f"{base} ({i}){ext}"
        i += 1
    return p


def resolve_default_dir() -> Path:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo))
    try:
        import app  # type: ignore
        return app.current_download_dir()
    except Exception:
        return repo / "downloads"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually rename files (default: dry-run)")
    ap.add_argument("--dir", type=str, default=None, help="override download folder")
    args = ap.parse_args()

    target = Path(args.dir).expanduser().resolve() if args.dir else resolve_default_dir()
    if not target.is_dir():
        print(f"not a directory: {target}", file=sys.stderr)
        return 2

    print(f"Scanning: {target}")
    planned: list[tuple[Path, Path]] = []
    skipped = 0
    for f in sorted(target.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        m = PREFIX_RE.match(f.name)
        if not m:
            skipped += 1
            continue
        clean = f.name[m.end():]
        if not clean:
            skipped += 1
            continue
        ext = f.suffix
        base = clean[: -len(ext)] if clean.endswith(ext) else Path(clean).stem
        target_path = unique_path(target, base, ext)
        if target_path == f:
            skipped += 1
            continue
        planned.append((f, target_path))

    if not planned:
        print(f"Nothing to rename. ({skipped} files already clean)")
        return 0

    for src, dst in planned:
        print(f"  {src.name}\n  → {dst.name}")

    print(f"\n{len(planned)} rename(s) planned, {skipped} clean.")
    if not args.apply:
        print("Dry-run. Re-run with --apply to perform renames.")
        return 0

    done = 0
    for src, dst in planned:
        try:
            src.rename(dst)
            done += 1
        except Exception as e:
            print(f"  FAILED: {src.name}: {e}", file=sys.stderr)
    print(f"Renamed {done}/{len(planned)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
