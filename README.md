# MTPS microdata mirror

A GitHub Actions pipeline that mirrors selected microdata from Brazil's public
MTPS FTP server into this repository, preserving the server's folder structure.

Source: <ftp://ftp.mtps.gov.br/pdet/microdados>

## What it mirrors

| Dataset | Scope | Cadence |
|---|---|---|
| `NOVO CAGED/` | every month from **2022 onward** | monthly |
| `RAIS/` | the **latest reference year** only | yearly (auto-detected) |

Anything older than 2022, and the duplicate `Legado` / `Parcial` folders, are
ignored. The result looks like the FTP layout:

```
NOVO CAGED/
  2022/202201/CAGEDMOV202201.7z   (…plus CAGEDFOR/CAGEDEXC)
  …
  2026/202607/…
  Leia-me.txt, Sobre o Novo Caged.pdf, …
RAIS/
  2025/RAIS_ESTAB_PUB.7z, RAIS_VINC_PUB_*.7z
sync-manifest.json
```

## How it works

- `sync_ftp.py` (stdlib-only Python) lists the FTP tree, filters to the scope
  above, and downloads files that are new or whose size changed since the last
  run. It records every mirrored file in `sync-manifest.json`.
- `.github/workflows/sync-ftp.yml` runs it on a monthly schedule and on manual
  dispatch, then commits and pushes any changes.
- Large `.7z` files are stored with [Git LFS](https://git-lfs.com)
  (`.gitattributes`).

Incremental sync is driven by the committed `sync-manifest.json`, so the CI job
never needs to pull the existing multi-GB dataset out of LFS to decide what
changed.

## Setup

1. Create a GitHub repository and push this folder to it:
   ```bash
   git init
   git add .
   git commit -m "Add FTP microdata mirror pipeline"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. Enable Git LFS if the files were not uploaded by the first workflow run.
   The workflow commits and pushes the data itself on its first run.

## Running

- **Manual:** *Actions → Sync FTP microdata → Run workflow*. You can pass
  `datasets` (e.g. `NOVO CAGED` only) and `rais_year`.
- **Schedule:** monthly (6th at 06:17 UTC). A newly published RAIS year is
  picked up automatically.
- **Local dry run:** `python sync_ftp.py --dry-run`

Useful flags:

```bash
python sync_ftp.py --datasets "NOVO CAGED"          # CAGED only
python sync_ftp.py --datasets RAIS --rais-year 2024 # specific RAIS year
python sync_ftp.py --min-year 2023                  # raise the cutoff
python sync_ftp.py --force                          # re-download everything
```

## ⚠️ Storage / quota warning

The current scope totals **~6.7 GB** (NOVO CAGED 2022–2026 ≈ 2.8 GB + RAIS
latest year ≈ 3.9 GB), and individual RAIS files exceed 1 GB.

- GitHub's **free Git LFS quota is 1 GB storage + 1 GB transfer/month**, so the
  **first run will exceed it**. Options:
  - purchase a [Git LFS data pack](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-storage-and-bandwidth-usage)
    (~$5/mo per 50 GB), or
  - reduce scope (drop RAIS: `--datasets "NOVO CAGED"` → ~2.8 GB), or
  - store the data in cloud object storage (Azure Blob / S3) and keep only the
    manifest in this repo — ask to add that variant if preferred.
- A GitHub repository's hard size limit is ~5 GB and files must go through LFS
  to exceed 100 MB; this pipeline uses LFS for that reason.

## Notes

- The server stores filenames in Latin-1/CP1252; `sync_ftp.py` decodes them to
  Unicode and normalizes them (NFC) to clean UTF-8 names in the repo.
  `NOVO CAGED` keeps its space, matching the server layout.
- The mirror is additive: changing the scope does not delete files that were
  previously committed.
