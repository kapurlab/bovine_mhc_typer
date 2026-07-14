# Conda Setup — Bovine MHC Typer GUI

The canonical, idempotent setup is **`deploy/install.sh`** (creates the tool's
own conda env, pip-installs the backend, verifies the toolchain + in-repo BoLA
refs, builds the frontend). See **`deploy/INSTALL.md`** for the full guide.

Manual env creation:

```bash
# shared env at <repo>/env
mamba env create -p /srv/kapurlab/tools/mhc_gui/env -f conda_setup/environment.yml
# or a personal env named mhc_gui
mamba env create -f conda_setup/environment.yml
```

The env (`environment.yml`) is the tool's **own** env — it does not borrow
amr_plus. It provides the ONT amplicon typing toolchain (nanoq, minimap2,
samtools, bcftools, vsearch, spoa, medaka, blast) plus the FastAPI web layer.
The BoLA reference bundle ships in-repo under `../refs` (see `refs/README.md`),
so no database download is needed. bdtools builds this env as osx-64 under
Rosetta on Apple Silicon; Linux/OOD build native.
