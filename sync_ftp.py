#!/usr/bin/env python3
"""Mirror selected microdata directories from the public MTPS FTP server.

Source:  ftp://ftp.mtps.gov.br/pdet/microdados

Scope (configurable via flags):
  * "NOVO CAGED" -- every reference month from MIN_YEAR onward (monthly data).
  * "RAIS"        -- only the latest reference year (yearly data), or a
                    specific year via --rais-year.

Anything older than MIN_YEAR, and the duplicate "Legado"/"Parcial" folders,
is ignored. Files are written into this repository preserving the remote
folder structure, so:

    NOVO CAGED/<year>/<yearmonth>/CAGEDMOV<yearmonth>.7z
    RAIS/<year>/RAIS_VINC_PUB_*.7z

Incremental sync is driven by the committed "sync-manifest.json": a file is
re-downloaded only when its size on the server differs from the last known
size (or it is new). This keeps each CI run cheap -- the workflow does not
need to pull existing large files out of Git LFS to decide what changed.

Uses only the Python standard library (no third-party dependencies).

Usage:
    python sync_ftp.py                 # mirror both datasets (default)
    python sync_ftp.py --dry-run       # list what would be downloaded
    python sync_ftp.py --datasets "NOVO CAGED"
    python sync_ftp.py --datasets RAIS --rais-year 2025
"""

from __future__ import annotations

import argparse
import ftplib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

HOST = "ftp.mtps.gov.br"
BASE = "pdet/microdados"
MIN_YEAR = 2022

# Folder names that hold duplicate/archived copies rather than canonical data.
SKIP_DIR_NAMES = {"legado"}

YEAR_RE = re.compile(r"^\d{4}$")

# Matches a line of the LIST output such as:
#   "06-08-26  03:55PM               109749 CAGEDEXC202201.7z"
#   "08-08-25  10:00AM       <DIR>          estabelecimento"
LIST_LINE_RE = re.compile(r"^\S+\s+\S+\s+(?P<size><DIR>|\d+)\s+(?P<name>.+)$")


class SyncError(Exception):
    pass


def connect() -> ftplib.FTP:
    ftp = ftplib.FTP()
    # The server reports filenames in Latin-1/CP1252 (e.g. "vínculos").
    ftp.encoding = "latin-1"
    ftp.connect(HOST, 21, timeout=60)
    ftp.login()  # anonymous
    ftp.set_pasv(True)
    ftp.voidcmd("TYPE I")  # binary transfers
    return ftp


def parse_list_line(line: str):
    m = LIST_LINE_RE.match(line)
    if not m:
        return None
    size, name = m.group("size"), m.group("name")
    if size == "<DIR>":
        return (name, True, 0)
    return (name, False, int(size))


def list_entries(ftp: ftplib.FTP, path: str):
    """Return a list of (name, is_dir, size) for a remote directory."""
    # Prefer MLSD (machine readable); fall back to parsing LIST output.
    # This server does not implement MLSD, so the LIST path is the one used.
    try:
        entries = []
        for name, facts in ftp.mlsd(path):
            if name in (".", ".."):
                continue
            is_dir = facts.get("type") == "dir"
            size = int(facts.get("size", "0") or 0)
            entries.append((name, is_dir, size))
        if entries:
            return entries
    except (ftplib.error_perm, ftplib.error_temp):
        pass

    lines: list[str] = []
    ftp.retrlines("LIST " + path, lines.append)
    entries = []
    for line in lines:
        entry = parse_list_line(line)
        if entry:
            entries.append(entry)
    return entries


def normalize_utf8(name: str) -> str:
    """Canonicalize a decoded name to NFC Unicode (written to disk as UTF-8)."""
    return unicodedata.normalize("NFC", name)


def make_entry(remote_dir: str, name: str, size: int) -> dict:
    # `remote` keeps the server's exact name (used verbatim for RETR);
    # `rel` is the local path, normalized to canonical UTF-8 for the repo.
    remote_path = f"{remote_dir}/{name}"
    rel = normalize_utf8(remote_path[len(BASE):].lstrip("/"))
    return {"remote": remote_path, "rel": rel, "size": size}


def collect_files_recursive(ftp, remote_path: str, files: list):
    for name, is_dir, size in list_entries(ftp, remote_path):
        if is_dir:
            if name.lower() in SKIP_DIR_NAMES:
                continue
            collect_files_recursive(ftp, f"{remote_path}/{name}", files)
        else:
            files.append(make_entry(remote_path, name, size))


def collect_novo_caged(ftp, files: list, min_year: int):
    top = f"{BASE}/NOVO CAGED"
    for name, is_dir, size in list_entries(ftp, top):
        if is_dir:
            if name.lower() in SKIP_DIR_NAMES:
                continue
            # Only recurse into reference-year directories we care about.
            if YEAR_RE.match(name) and int(name) >= min_year:
                collect_files_recursive(ftp, f"{top}/{name}", files)
            # Ignore any other non-year directory.
        else:
            # Keep the small root-level docs (readme, layouts, PDFs).
            files.append(make_entry(top, name, size))


def latest_year(ftp, top: str):
    years = [
        int(name)
        for name, is_dir, _ in list_entries(ftp, top)
        if is_dir and YEAR_RE.match(name)
    ]
    return max(years) if years else None


def collect_rais(ftp, files: list, rais_year):
    top = f"{BASE}/RAIS"
    year = rais_year or latest_year(ftp, top)
    if year is None:
        raise SyncError("No RAIS reference-year folder found on the server")
    print(f"[RAIS] mirroring year {year}")
    collect_files_recursive(ftp, f"{top}/{year}", files)


def load_manifest(path: Path) -> dict:
    """Return {rel_path: size} from a previously committed manifest."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, list):
        return {e["path"]: e["size"] for e in data}
    return {}


def needs_download(entry: dict, root: Path, known: dict, force: bool) -> bool:
    if force:
        return True
    rel = entry["rel"]
    if known.get(rel) == entry["size"]:
        return False  # unchanged since last successful run
    dest = root / rel
    if dest.exists() and dest.stat().st_size == entry["size"]:
        return False  # already present on disk at the right size
    return True


def download(ftp, entry: dict, root: Path):
    dest = root / entry["rel"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(tmp, "wb") as fh:
            ftp.retrbinary("RETR " + entry["remote"], fh.write)
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--datasets", default="NOVO CAGED,RAIS",
                   help="comma-separated datasets to mirror (default: both)")
    p.add_argument("--rais-year", type=int, default=None,
                   help="RAIS year to mirror (default: latest available)")
    p.add_argument("--min-year", type=int, default=MIN_YEAR,
                   help="ignore reference years older than this")
    p.add_argument("--root", default=".",
                   help="local output directory (default: repo root)")
    p.add_argument("--dry-run", action="store_true",
                   help="list only; download nothing")
    p.add_argument("--force", action="store_true",
                   help="re-download every file")
    p.add_argument("--manifest", default="sync-manifest.json",
                   help="JSON manifest path (read for skip decisions, "
                        "rewritten unless --dry-run)")
    args = p.parse_args(argv)

    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    if not datasets:
        raise SyncError("No datasets specified")

    root = Path(args.root)
    known = load_manifest(root / args.manifest)

    ftp = connect()
    try:
        files: list[dict] = []
        for ds in datasets:
            key = ds.upper().replace("_", " ").strip()
            if key == "NOVO CAGED":
                collect_novo_caged(ftp, files, args.min_year)
            elif key == "RAIS":
                collect_rais(ftp, files, args.rais_year)
            else:
                raise SyncError(f"Unknown dataset: {ds!r}")

        files.sort(key=lambda e: e["rel"])

        counts = {"downloaded": 0, "skip": 0, "would-download": 0}
        bytes_done = 0
        bytes_pending = 0
        for entry in files:
            if not needs_download(entry, root, known, args.force):
                counts["skip"] += 1
                print(f"  skip {entry['rel']}")
                continue
            if args.dry_run:
                counts["would-download"] += 1
                bytes_pending += entry["size"]
                print(f"  NEW  {entry['rel']}  ({entry['size']:,} bytes)")
            else:
                download(ftp, entry, root)
                counts["downloaded"] += 1
                bytes_done += entry["size"]
                print(f"  ok   {entry['rel']}  ({entry['size']:,} bytes)")

        total = sum(e["size"] for e in files)
        print(f"\n{len(files)} files, {total / 1e6:,.1f} MB total on the server")

        if args.dry_run:
            print(f"dry-run: {counts['would-download']} to download "
                  f"({bytes_pending / 1e6:,.1f} MB), {counts['skip']} up to date")
        else:
            manifest = [
                {
                    "path": e["rel"],
                    "size": e["size"],
                    "url": f"ftp://{HOST}/{e['remote']}",
                }
                for e in files
            ]
            (root / args.manifest).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"downloaded {counts['downloaded']} new/changed "
                  f"({bytes_done / 1e6:,.1f} MB), skipped {counts['skip']}")
            print(f"wrote {args.manifest}")
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


if __name__ == "__main__":
    try:
        main()
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
