import json
import os
from pathlib import Path
from typing import Any, Dict


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "mhc_gui"
    return Path.home() / ".config" / "mhc_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

_SHARED_PROJECTS_ROOT = Path("/srv/kapurlab/projects")
_DEFAULT_SHARED_PROJECTS_ROOT = (
    str(_SHARED_PROJECTS_ROOT) if _SHARED_PROJECTS_ROOT.is_dir() else ""
)


def _first_existing(*paths: str) -> str:
    """Return the first path that exists, else the first candidate (so the
    default is informative even on a fresh box)."""
    for p in paths:
        if p and Path(p).exists():
            return p
    return paths[0] if paths else ""


# BoLA reference bundle (IPD-MHC BLAST DBs, ARS-UCD2.0 chr23 MHC contig,
# haplotypes.json). Prefer the shared install location; fall back to the
# original hand-built refs under the source project during bring-up.
_BOLA_REFS_DEFAULT = _first_existing(
    "/srv/kapurlab/databases/bola",
    "/home/vxk1/BoLA_MHC/refs",
)

# Conda env bin dirs. The pipeline needs ONT tools (minimap2, samtools, medaka,
# spoa, nanoq) and the phasing tools (bcftools, vsearch, HAPCUT2). Prefer the
# tool's own packaged env; fall back to the two source envs during bring-up.
_TOOL_ENV_BIN = "/srv/kapurlab/tools/mhc_gui/env/bin"
_ONT_ENV_BIN_DEFAULT = _first_existing(
    _TOOL_ENV_BIN,
    "/home/vxk1/miniforge3/envs/ont_mhc/bin",
)
_PHASE_ENV_BIN_DEFAULT = _first_existing(
    _TOOL_ENV_BIN,
    "/home/vxk1/miniforge3/envs/mhc_phase/bin",
)

DEFAULTS: Dict[str, Any] = {
    "projects_root": str(Path.home() / "projects"),
    "shared_projects_root": _DEFAULT_SHARED_PROJECTS_ROOT,
    # Where run folders of barcoded ONT FASTQ live (rclone-synced). Users can
    # also point the app at a project's own download/ dir.
    "runs_root": _first_existing("/srv/kapurlab/databases/mhc/runs", "/home/vxk1/BoLA_MHC/data"),
    # barcode -> animal/sample map (run_date, run_folder, amplicon, barcode,
    # sample_id, lab_id, tissue, ...). Lets the GUI show animal IDs, not barcodes.
    "barcode_map": _first_existing("/home/vxk1/BoLA_MHC/barcode_sample_map.tsv", ""),
    "bola_refs": _BOLA_REFS_DEFAULT,
    "ont_env_bin": _ONT_ENV_BIN_DEFAULT,
    "phase_env_bin": _PHASE_ENV_BIN_DEFAULT,
    # medaka consensus model — MUST match the basecaller (R10.4.1 SUP). Change
    # this only if the sequencing chemistry / basecaller model changes.
    "medaka_model": "r1041_e82_400bps_sup_v5.2.0",
    # Class I (multi-copy) calls are provisional per the pipeline handover —
    # gated off by default so the reliable DRB3 (Class II) path is the primary
    # output. Enabling surfaces Class I with explicit confidence tiers.
    "enable_class_i": False,
}


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
