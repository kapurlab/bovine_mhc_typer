"""Runtime paths for the MHC Typer pipeline, resolved from environment.

The OOD launcher / FastAPI backend sets these from the per-user GUI config
(`~/.config/mhc_gui/config.json`) so the pipeline is relocatable. The fallbacks
keep the scripts runnable standalone on wgs3 during bring-up.
"""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


# Conda env bin dirs. ONT tools (minimap2, samtools, medaka, spoa, nanoq) and
# the phasing tools (bcftools, vsearch, HAPCUT2). One merged env sets both the
# same; two envs set them differently.
ONT_BIN = _env("MHC_ONT_BIN", "/home/vxk1/miniforge3/envs/ont_mhc/bin")
PHASE_BIN = _env("MHC_PHASE_BIN", "/home/vxk1/miniforge3/envs/mhc_phase/bin")

# BoLA reference bundle (blast_db/BoLA_{nuc,gen}, the ARS-UCD2.0 chr23 contig,
# haplotypes.json). Defaults to the bundle committed in the repo (../refs), so
# the tool is self-contained; MHC_REFS overrides it (e.g. a shared/staged copy).
_REPO_REFS = Path(__file__).resolve().parent.parent / "refs"
REFS = Path(_env("MHC_REFS", str(_REPO_REFS)))

# medaka consensus model — MUST match the basecaller (R10.4.1 SUP).
MEDAKA_MODEL = _env("MHC_MEDAKA_MODEL", "r1041_e82_400bps_sup_v5.2.0")

# BLAST binary (per handover: system blastn).
BLASTN = _env("MHC_BLASTN", "/usr/bin/blastn")

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
