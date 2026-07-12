# MHC Typer (mhc_gui) — Session Handoff

**Date:** 2026-07-12 · **Branch:** `feature/initial-build` (local only, not on GitHub) · **HEAD:** `226f158`

An OOD GUI for **bovine MHC (BoLA) genotyping from Oxford Nanopore amplicon reads** —
the 9th app in the kapurlab bdtools family (sibling of vsnp_gui / amr_plus_gui).
Built this session from scratch. **DRB3 (Class II) works end-to-end; Class I is the
next big piece.**

---

## 1. Where everything lives

| | Path |
|---|---|
| Local dev repo (edit here) | `/Users/vivekkapur/mhc_gui` (Mac) |
| Deployed app (test/run here) | `/srv/kapurlab/tools/mhc_gui` (wgs3) |
| Shared runs library | `/srv/kapurlab/databases/mhc/runs/` (setgid, kapurlab-admins) |
| OOD app | `/var/www/ood/apps/sys/mhc_gui` · launch card `id: mhc` in `/etc/ood/config/wgs_pipelines.yml` (status: available) |
| Research pipeline (source of the science) | `/home/vxk1/BoLA_MHC/` — see its `HANDOVER.md` |
| Conda env (uvicorn) | `mhc_gui/env` → **symlink to `amr_plus_gui/env`** (borrowed! no dedicated env yet) |
| Pipeline tools | via config: `MHC_ONT_BIN=/home/vxk1/miniforge3/envs/ont_mhc/bin`, `MHC_PHASE_BIN=…/mhc_phase/bin` |
| Refs | `/home/vxk1/BoLA_MHC/refs` (blast_db/BoLA_{nuc,gen}, chr23, haplotypes.json) |

**Deploy loop:** edit on Mac → `rsync backend/app + bin + frontend/src → wgs3` →
`npm run build` (in frontend) → **start a fresh OOD session** (prod app doesn't
hot-reload the backend). Test with a throwaway uvicorn: `PYTHONPATH=$APP/bin
$APP/env/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 87NN` + curl.

**Log model: POLLING `/api/jobs/{id}/logtext`, NOT SSE** (OOD Apache proxy breaks SSE).

---

## 2. What's built + validated

- **Scaffold** from amr_plus_gui; shared family layout (header/status-strip/panels/dark log). Slug `mhc_gui`, title "MHC Typer".
- **DRB3 typing** — `bin/drb3_type.py` (validated: reproduces `results/drb3_typed.tsv` byte-for-byte). All shell paths `shlex.quote`d (runs have spaces/`[brackets]`).
- **Manifest pipeline** — `bin/run_typing.py` reads a 4-col manifest (`barcode, sample, reads_source, amplicon`); no path ever hits the command line. `reads_source` = a barcode dir (linked run) OR a fastq file (uploaded).
- **Inputs (complete):**
  - **Import (OneDrive)** — `/api/onedrive-runs` + `/api/import-run` → `bin/import_run.sh` (rclone copy inbox→library, then move→`Uploaded_Archive/<month>/`). Background job. Nightly cron installed (`bin/sync_onedrive.sh`, 2 AM, vxk1 crontab).
  - **Link** — from the shared library into `project/runs/` (symlink).
  - **Upload** — a whole run folder (`/api/.../upload-run`, webkitdirectory) OR loose FASTQs (`/upload` → "Uploaded reads").
- **Sample sheets** — in-run `sample_sheet.csv` (or `sample_sheet_YYYYMMDD.csv`, latest wins). Delimiter-sniffed (CSV/TSV). Columns `barcode, sample_id, amplicon` (req) + `animal_id, tissue` (rec). Case-insensitive barcodes; `run_folder` col ignored. Precedence: in-run → project → site map → barcode#.
  - `bin/export_sheets_from_master.py` — standalone: master multisheet Excel → per-run `sample_sheet.csv` (matched by date; normalizes barcode#→barcodeNN, `(Bov 7/11)`→Bov7/11; emits animal_id=sample_id).
- **#2 auto-run per amplicon** — `/api/run` builds the 4-col manifest, gating Class-I; `run_typing` groups by amplicon and routes: DRB3→typing, Class-I→`run_classI()` **stub**. Frontend: `Auto (per sheet)` / `Force all → X`. Validated on mixed 20260709.
- **Results** — vSNP-style **QC table** (`/api/jobs/{id}/table`): Sample · Barcode · alleles · zygosity · reads · **pass/review/fail** (`_drb3_qc`). Per-animal via `animal_id` (fallback sample_id).

---

## 3. What's NEXT (in order)

1. **#3 — Class I pipeline** (the heavy piece). The seam is **`run_classI(amp, entries, outdir, threads)` in `bin/run_typing.py`** (currently a stub). Wire, per `BoLA_MHC/HANDOVER.md`:
   `classI_typed_pipeline.py` (cluster id 0.88 short / 0.85 long → spoa+medaka consensus → diploid split → BLAST IPD nuc+gen → tier) → `reconcile.py` → `haplotype_call.py`. Copy those scripts into `bin/`, parameterize (like drb3_type: config paths, quote everything), add a **per-barcode read cap** (config knob, ~20–30k) so medaka doesn't choke on deep barcodes.
2. **Results tabs + consolidated** — the agreed design (mockup shown, not built): **Per-animal (default) · Class II · Class I (provisional, amber gate)**. Consolidated groups by `animal_id`. Add `make_report.py` for the per-animal HTML/PDF.
3. **Dedicated conda env + install.sh** — the app borrows amr's env. Needs `conda_setup/environment.yml` (merge ont_mhc + mhc_phase, or keep two) + `deploy/install.sh`. Also **push repo to GitHub** so the `_dev` app (hot-reload, no relaunch) works.
4. Minor: multi-user rclone (import uses vxk1's config; cron covers others). Vestigial dirs already trimmed to `download/ runs/ mhc/`.

---

## 4. Class I — scientific caveats (READ before wiring #3)

Class I is **provisional — leads, not genotypes** (per HANDOVER §8). The UI must keep the amber gate. Specifics to handle:

- **5′UTR was undersampled** (20260618 run: long-range library sequenced mostly short fragments, raw median ~805 bp) → **21/24 too shallow to call**. So 5′UTR barcodes will often lack depth. **Add a per-barcode read-depth QC gate** (min reads before spending medaka) so undersampled barcodes flag `fail`/`review` cheaply instead of burning compute.
- **BosEx** — systematic **~2-SNP offset vs IPD** (classical hits land ~99.2%, not 100%). **Check this** — is it a basecaller artifact or a real cohort-vs-IPD diff? Don't report BosEx alleles as exact until resolved.
- **Bov7/11 + BosEx (short amplicons) don't reproduce** — same animals share only ~3 of ~40 class-I alleles. Treat as leads.
- **The real class-I resolver is deeper 5′UTR / Arm B** (orthogonal confirmation) — noted as the path to promoting any candidate.

**Practical for the app:** before running Class I on a barcode, check read depth; surface undersampled/offset warnings in the provisional tab; cap reads for compute.

---

## 5. Conventions (locked in with Vivek)

- **Run folder:** `YYYYMMDD_<Tissue>_<descriptor>` — underscores only, **no spaces/`[]`/`&`/`'`/`/`**. Amplicon NOT in the folder name (it's per-barcode in the sheet).
- **Sample sheet:** per-run `sample_sheet.csv` inside the run folder is **canonical**. Master Excel is optional (→ export helper). Fixed name or `sample_sheet_YYYYMMDD.csv`.
- **Amplicon tokens** (`_amplicon_token`): `DRB3→drb3`, `Bov7/11→bov711`, `BosEx→bosex`, `5'UTR→utr5` (path-safe).
- **OneDrive** (`rxk104_mhc:`): drop **basecalled run folders** (NOT the 10–15 GB `Raw_Data_*` siblings) into `For_WGS3_Upload/` with `sample_sheet.csv` inside → import → moved to `Uploaded_Archive/<month>/`. The 6 existing top-level runs should be moved to Archive + renamed to the convention.

---

## 6. Verify-first checklist for the next session

- [ ] `cd /Users/vivekkapur/mhc_gui && git log --oneline -5` (branch `feature/initial-build`).
- [ ] Deployed app matches: `rsync -n` or diff `backend/app` vs wgs3.
- [ ] Launch MHC Typer OOD session; confirm Import/Link/Upload + Auto routing + QC table.
- [ ] Read `/home/vxk1/BoLA_MHC/HANDOVER.md` + `scripts/classI_typed_pipeline.py` before wiring `run_classI`.
- [ ] Don't `sudo cp` over the box; don't use SSE. See memory `ood-sse-polling-and-deploy-safety`.
