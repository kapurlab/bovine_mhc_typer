#!/usr/bin/env bash
# install.sh — idempotent, no-sudo deployment of the Bovine MHC Typer GUI.
#
# Mirrors the GenoFLU/Kraken/vSNP sandbox pattern. Every heavy step is skippable
# and clearly logged. Safe to re-run. Portable across macOS (incl. Apple Silicon
# under Rosetta), Linux, Windows (WSL2), and Open OnDemand: it takes no hard-coded
# user paths and prefers a shared env at <repo>/env.
#
# What it does:
#   1. Locate/create the tool's OWN conda env (shared at <repo>/env, else the
#      personal env `mhc_gui`) from conda_setup/environment.yml. This tool does
#      NOT borrow amr_plus — it has its own env like every other bdtools tool.
#   2. pip install backend/requirements.txt into that env.
#   3. Verify the MHC toolchain (nanoq, minimap2, samtools, bcftools, vsearch,
#      spoa, medaka, blastn) and the in-repo BoLA reference bundle (refs/).
#   4. Build the React frontend (frontend/dist/).
#
# Usage:
#   deploy/install.sh [--personal] [--conda-base DIR]
#                     [--skip-verify] [--skip-frontend] [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- defaults ----
SHARED_ENV="${REPO_DIR}/env"
PERSONAL_ENV_NAME="mhc_gui"
CONDA_BASE="${HOME}/miniforge3"
USE_PERSONAL=0
SKIP_VERIFY=0
SKIP_FRONTEND=0
DRY_RUN=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [[ ${DRY_RUN} -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --personal)       USE_PERSONAL=1; shift;;
    --conda-base)     CONDA_BASE="$2"; shift 2;;
    --skip-verify)    SKIP_VERIFY=1; shift;;
    --skip-frontend)  SKIP_FRONTEND=1; shift;;
    --dry-run)        DRY_RUN=1; shift;;
    -h|--help)        sed -n '2,24p' "$0"; exit 0;;
    *) die "unknown arg: $1";;
  esac
done

log "Bovine MHC Typer GUI install"
echo "  repo:  ${REPO_DIR}"
[[ ${DRY_RUN} -eq 1 ]] && warn "DRY RUN — no changes will be made"

# ---------------------------------------------------------------------------
# 1. conda env (the tool's own env)
# ---------------------------------------------------------------------------
CONDA="${CONDA_BASE}/bin/conda"
[[ -x "${CONDA}" ]] || CONDA="$(command -v conda 2>/dev/null || true)"
[[ -n "${CONDA}" && -x "${CONDA}" ]] || die "conda not found. Install miniforge to ${CONDA_BASE} or pass --conda-base."
ok "conda: ${CONDA}"

# Prefer mamba — conda's classic solver hangs on big bioconda envs.
CONDA_FRONTEND="${CONDA_FRONTEND:-}"
if [[ -z "${CONDA_FRONTEND}" ]]; then
  if [[ -x "${CONDA_BASE}/bin/mamba" ]]; then CONDA_FRONTEND="${CONDA_BASE}/bin/mamba"
  elif command -v mamba >/dev/null 2>&1; then CONDA_FRONTEND="$(command -v mamba)"
  else CONDA_FRONTEND="${CONDA}"; fi
fi
ok "env builder: ${CONDA_FRONTEND}"

ENV_FILE="${REPO_DIR}/conda_setup/environment.yml"
if [[ ${USE_PERSONAL} -eq 1 ]]; then
  ENV_BIN="$("${CONDA}" run -n "${PERSONAL_ENV_NAME}" sh -c 'echo $CONDA_PREFIX/bin' 2>/dev/null || true)"
  ENV_DESC="personal env ${PERSONAL_ENV_NAME}"
  ENV_EXISTS=$("${CONDA}" env list | awk '{print $1}' | grep -qx "${PERSONAL_ENV_NAME}" && echo 1 || echo 0)
  CREATE_FLAG=("-n" "${PERSONAL_ENV_NAME}")
else
  ENV_BIN="${SHARED_ENV}/bin"
  ENV_DESC="shared env ${SHARED_ENV}"
  ENV_EXISTS=$([[ -x "${SHARED_ENV}/bin/python" ]] && echo 1 || echo 0)
  CREATE_FLAG=("-p" "${SHARED_ENV}")
fi

if [[ "${ENV_EXISTS}" -eq 1 ]]; then
  ok "${ENV_DESC} already exists — skipping create"
else
  # A prior run cancelled mid-solve leaves a partial env dir with no python;
  # env create would then abort with "prefix already exists". Clear it first.
  if [[ ${USE_PERSONAL} -eq 0 && -d "${SHARED_ENV}" ]]; then
    warn "removing incomplete env at ${SHARED_ENV} (no python found)"
    run rm -rf "${SHARED_ENV}"
  fi
  log "creating ${ENV_DESC} from ${ENV_FILE} (solve can take 2-5 min)"
  run "${CONDA_FRONTEND}" env create "${CREATE_FLAG[@]}" -f "${ENV_FILE}"
fi

# A --personal env may have just been created above; if so, the ENV_BIN probed
# earlier (via `conda run` before the env existed) is empty, which would make
# PYTHON="/python". Re-resolve now that the env exists — prefer the live prefix,
# fall back to <conda base>/envs/<name> (where `conda env create -n` puts it).
if [[ ${USE_PERSONAL} -eq 1 && ! -x "${ENV_BIN}/python" ]]; then
  ENV_BIN="$("${CONDA}" run -n "${PERSONAL_ENV_NAME}" sh -c 'echo $CONDA_PREFIX/bin' 2>/dev/null || true)"
  [[ -x "${ENV_BIN}/python" ]] || ENV_BIN="$("${CONDA}" info --base 2>/dev/null)/envs/${PERSONAL_ENV_NAME}/bin"
fi

PYTHON="${ENV_BIN}/python"
[[ ${DRY_RUN} -eq 1 || -x "${PYTHON}" ]] || die "env python not found at '${PYTHON}' — ${ENV_DESC} did not build correctly."
# Put the env's bin on PATH for every tool call below (and so the backend, which
# runs under this env, finds the whole toolchain).
if [[ -d "${ENV_BIN}" ]]; then export PATH="${ENV_BIN}:${PATH}"; fi

log "pip install backend requirements into ${ENV_DESC}"
run "${PYTHON}" -m pip install -r "${REPO_DIR}/backend/requirements.txt"

# ---------------------------------------------------------------------------
# 2. Verify the MHC toolchain + in-repo reference bundle
# ---------------------------------------------------------------------------
if [[ ${SKIP_VERIFY} -eq 1 ]]; then
  warn "skipping toolchain/refs verification (--skip-verify)"
else
  # DRB3 (Class II) needs only nanoq + blastn + the BoLA_nuc DB and runs
  # everywhere. The rest (minimap2/samtools/bcftools/vsearch/spoa/medaka) drive
  # the provisional Class I path.
  for t in nanoq blastn minimap2 samtools bcftools vsearch spoa medaka_consensus; do
    if [[ -x "${ENV_BIN}/${t}" ]] || command -v "${t}" >/dev/null 2>&1; then
      ok "${t} present"
    else
      warn "${t} not found in env — re-run after the env build completes."
    fi
  done

  # BoLA reference bundle ships in-repo (refs/); see refs/README.md.
  REFS="${REPO_DIR}/refs"
  if [[ -f "${REFS}/blast_db/BoLA_nuc.ndb" && -f "${REFS}/ARS-UCD2.0_chr23_MHC_renamed.fa" \
        && -f "${REFS}/haplotypes.json" ]]; then
    ok "BoLA reference bundle present: ${REFS} (BoLA_nuc/gen DBs, chr23 MHC contig, haplotypes.json)"
  else
    warn "BoLA reference bundle incomplete under ${REFS} — typing will fail until present."
  fi
fi

# ---------------------------------------------------------------------------
# 3. Frontend build
# ---------------------------------------------------------------------------
if [[ ${SKIP_FRONTEND} -eq 1 ]]; then
  warn "skipping frontend build (--skip-frontend)"
else
  log "building React frontend"
  pushd "${REPO_DIR}/frontend" >/dev/null
  if command -v npm >/dev/null 2>&1; then
    run npm ci || run npm install
    run npm run build
  elif [[ -x node_modules/.bin/vite ]]; then
    run node_modules/.bin/vite build
  else
    # Reuse a sibling GUI's node_modules if ours is missing (no network / no npm).
    SIB="/srv/kapurlab/tools/kraken_id_parse_gui/frontend/node_modules"
    if [[ -d "${SIB}" && ! -e node_modules ]]; then
      run ln -s "${SIB}" node_modules
      run node_modules/.bin/vite build
    else
      warn "no npm and no node_modules — frontend not built. Install Node and re-run."
    fi
  fi
  popd >/dev/null
  [[ -f "${REPO_DIR}/frontend/dist/index.html" ]] && ok "frontend built: ${REPO_DIR}/frontend/dist/"
fi

log "Done. Register the OOD app (sudo deploy/register_ood_apps.sh) and launch a session."
echo "  Backend entry:  ${REPO_DIR}/backend/app/main.py (uvicorn app.main:app)"
echo "  Env python:     ${PYTHON}"
