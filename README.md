# Bovine MHC Typer (`mhc_gui`)

A web app for **bovine MHC (BoLA) genotyping from Oxford Nanopore amplicon reads** —
**DRB3 (Class II)** and the **Class I** loci (Bov7/11, BosEx, 5′UTR). It wraps the
validated `BoLA_MHC` research pipeline in a **FastAPI backend + React (Vite) SPA**,
deployed as an **Open OnDemand (OOD) batch_connect interactive app** on the kapurlab
WGS server. It's the 9th tool in the kapurlab `bdtools` family (sibling of
`vsnp_gui` / `amr_plus_gui`).

> **Class I calls are PROVISIONAL — leads, not genotypes.** The short amplicons don't
> reproduce and 5′UTR is depth-limited; final calls need the SUP re-basecall + deeper
> data. Keep the amber gate in the UI. See `docs/HANDOFF.md`.

---

## Status

| Area | State |
|---|---|
| **DRB3 (Class II)** | Production-ready — validated, 23/24 clean genotypes |
| **Inputs** | Import (OneDrive) · Link · Upload; per-run sample sheets (view/download/override) |
| **Class I pipeline** | Wired + validated **byte-for-byte** vs research (`classI_type.py` + `reconcile.py`); **gated off** in config pending the ONT/phase env + SUP data |
| **Results** | vSNP-style pass/review/fail QC table; tabbed per-animal view is next |
| **Deploy** | Live on wgs3 (`/srv/kapurlab/tools/mhc_gui`); OOD card `id: mhc` |

Full state + next steps: **[`docs/HANDOFF.md`](docs/HANDOFF.md)**. Family conventions +
traps: **[`docs/BUILDING_A_SIBLING_TOOL.md`](docs/BUILDING_A_SIBLING_TOOL.md)**.

---

## Repo layout

```
backend/app/        FastAPI app — main.py (all routes), config.py, jobs.py
bin/                pipeline scripts run by the backend:
                      drb3_type.py         Class II (DRB3) typing — one barcode
                      classI_type.py       Class I typing — one barcode + amplicon
                      reconcile.py         cross-amplicon -> per-animal + MHCI haplotypes
                      run_typing.py        entry point: manifest -> routes each barcode
                      mhc_config.py        runtime paths/refs/env (from GUI config)
frontend/src/       React SPA (App.jsx = all UI); build output frontend/dist/ (gitignored)
docs/               HANDOFF.md, BUILDING_A_SIBLING_TOOL.md, and the project deck
deploy/ conda_setup/ ood/   install + OOD app definitions
```

## Dev + deploy loop

Source of truth is this repo. The deployed app on wgs3 is a **file-copy** (no git).

```bash
# 1. edit here, commit
# 2. rsync code to the deployed app
D=vxk1@kapurlab-wgs3:/srv/kapurlab/tools/mhc_gui
rsync -a --exclude __pycache__ backend/app/ $D/backend/app/
rsync -a --exclude __pycache__ bin/        $D/bin/
rsync -a frontend/src/ frontend/package*.json $D/frontend/
# 3. rebuild the frontend (uvicorn serves frontend/dist/)
ssh vxk1@kapurlab-wgs3 'cd /srv/kapurlab/tools/mhc_gui/frontend && npm run build'
# 4. start a FRESH OOD session — the backend does NOT hot-reload
```

Quick backend test without a full session:
```bash
PYTHONPATH=backend /srv/kapurlab/tools/mhc_gui/env/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8791   # then curl the /api routes
```

## Constraints that will bite you (read before editing)

- **All frontend URLs must be relative** (`./api/…`). OOD proxies at
  `/rnode/<host>/<port>/…`; any hardcoded host/port 404s under the proxy.
- **Log streaming is POLLING** (`/api/jobs/{id}/logtext`), **not SSE** — the OOD Apache
  proxy breaks SSE.
- **Class I needs the ONT/phase toolchain** (`minimap2`, `samtools`, `medaka`,
  `spoa`, `nanoq`, `vsearch`, `bcftools`) — read via CLI, no `pysam` (dropped so the
  env stays `samtools`-only; pysam pins its own htslib and clashes with the env's
  samtools under conda). The deployed app currently **borrows `amr_plus_gui/env`**,
  which lacks those tools — so `enable_class_i` is **off**. The dedicated env
  (`conda_setup/`) must add the ONT/phase tools before enabling.
- Pipeline tools resolve via `mhc_config` env-bin paths (`ont_mhc`, `mhc_phase`) —
  every shell path is `shlex.quote`d (run/sample names contain spaces & brackets).

## Science source

The typing logic is ported verbatim from **`/home/vxk1/BoLA_MHC`** on wgs3 (see its
`HANDOVER.md`). Chemistry is pinned: **R10.4.1 · SQK-NBD114-96 · dorado SUP ·
medaka `r1041_e82_400bps_sup_v5.2.0`**. The medaka model assumes SUP basecalling —
runs basecalled FAST/HAC must be re-basecalled SUP before Class I is trustworthy.
