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
# haplotypes.json).
REFS = Path(_env("MHC_REFS", "/home/vxk1/BoLA_MHC/refs"))

# medaka consensus model — MUST match the basecaller (R10.4.1 SUP).
MEDAKA_MODEL = _env("MHC_MEDAKA_MODEL", "r1041_e82_400bps_sup_v5.2.0")

# BLAST binary (per handover: system blastn).
BLASTN = _env("MHC_BLASTN", "/usr/bin/blastn")


def tool_env() -> dict:
    """os.environ with the ONT + phase env bins prepended to PATH, so bioconda
    tools resolve their own interpreters/libs (see BUILDING_A_SIBLING_TOOL §11.1)."""
    return {**os.environ, "PATH": f"{ONT_BIN}:{PHASE_BIN}:/usr/bin:/bin"}
