# Bovine MHC Typer GUI — Environment Setup

Setup is automated by **`deploy/install.sh`** and documented in
**`deploy/INSTALL.md`**. This file is a short pointer to avoid drift.

## Quick start

```bash
cd /srv/kapurlab/tools/mhc_gui
deploy/install.sh            # shared env at ./env  (use --personal for ~/env)
```

That script:
1. creates the tool's own conda env from `conda_setup/environment.yml`,
2. `pip install -r backend/requirements.txt`,
3. verifies the MHC toolchain (nanoq, minimap2, samtools, bcftools, vsearch,
   spoa, medaka, blastn) and the in-repo BoLA reference bundle (`refs/`),
4. builds the React frontend (`frontend/dist/`).

It is idempotent — re-run any time. See `deploy/INSTALL.md` for flags and OOD app
registration. The BoLA reference bundle ships in-repo (`refs/`), so there is no
database download step.
