"""Runtime paths for the MHC Typer pipeline, resolved from environment.

The OOD launcher / FastAPI backend sets these from the per-user GUI config
(`~/.config/mhc_gui/config.json`) so the pipeline is relocatable. The fallbacks
keep the scripts runnable standalone on wgs3 during bring-up.
"""
import os
import sys
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


# Bin dir of the env we're running under (the tool's dedicated conda env, whose
# python launched this script). All pipeline tools — minimap2, samtools, bcftools,
# nanoq, vsearch, spoa, medaka, blastn — live in this single env, so it is the
# right default on every platform (macOS/Windows-WSL2/Linux/OOD). The backend may
# still override via MHC_ONT_BIN/MHC_PHASE_BIN (e.g. a legacy two-env layout).
_ENV_BIN = str(Path(sys.executable).resolve().parent)
ONT_BIN = _env("MHC_ONT_BIN", _ENV_BIN)
PHASE_BIN = _env("MHC_PHASE_BIN", _ENV_BIN)

# BoLA reference bundle (blast_db/BoLA_{nuc,gen}, the ARS-UCD2.0 chr23 contig,
# haplotypes.json). Defaults to the bundle committed in the repo (../refs), so
# the tool is self-contained; MHC_REFS overrides it (e.g. a shared/staged copy).
_REPO_REFS = Path(__file__).resolve().parent.parent / "refs"
REFS = Path(_env("MHC_REFS", str(_REPO_REFS)))

# medaka consensus model — MUST match the basecaller (R10.4.1 SUP).
MEDAKA_MODEL = _env("MHC_MEDAKA_MODEL", "r1041_e82_400bps_sup_v5.2.0")

# BLAST binary — from the tool's own env (the `blast` conda package), so the tool
# is self-contained. Override with MHC_BLASTN (e.g. a system blastn).
BLASTN = _env("MHC_BLASTN", str(Path(_ENV_BIN) / "blastn"))

# Class I reference files, derived from the REFS bundle:
#   MHCREF     — clean chr23 MHC contig (NC_037350.1) for on-target mapping
#   BLAST_NUC  — IPD-MHC CDS db;  BLAST_GEN — IPD-MHC genomic db (gDNA amplicons)
#   HAPLOTYPES — Tim's workbook parsed to JSON (MHCI/MHCII haplotype slots)
MHCREF = REFS / "ARS-UCD2.0_chr23_MHC_renamed.fa"
BLAST_NUC = REFS / "blast_db" / "BoLA_nuc"
BLAST_GEN = REFS / "blast_db" / "BoLA_gen"
HAPLOTYPES = REFS / "haplotypes.json"

# Class I compute knobs. Class I is multi-copy + medaka-heavy, so cap the reads
# fed to consensus (deep barcodes choke medaka) and bound its thread use.
CLASSI_READ_CAP = int(_env("MHC_CLASSI_READ_CAP", "25000"))
CLASSI_MEDAKA_THREADS = int(_env("MHC_MEDAKA_THREADS", "8"))


def tool_env() -> dict:
    """os.environ with the ONT + phase env bins prepended to PATH, so bioconda
    tools resolve their own interpreters/libs (see BUILDING_A_SIBLING_TOOL §11.1)."""
    return {**os.environ, "PATH": f"{ONT_BIN}:{PHASE_BIN}:/usr/bin:/bin"}
