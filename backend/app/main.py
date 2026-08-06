"""
AMRFinderPlus GUI — FastAPI backend.

Serves the React SPA from frontend/dist/ and provides:
  /api/projects        — list shared + personal projects (FASTQ browser)
  /api/projects/{n}/samples — list FASTQ pairs in project/download/
  /api/config          — get/set user config (DB paths)
  /api/organism-options — valid AMRFinderPlus --organism tokens (cached)
  /api/run             — start an amr_pipeline.py run
  /api/jobs            — list running/completed jobs
  /api/jobs/{id}       — job detail
  /api/jobs/{id}/log   — SSE stream of the job log
  /api/projects/{n}/samples/{s}/amr-results — per-sample result files
  /api/projects/{n}/samples/{s}/amr-table   — parsed AMRFinderPlus TSV

This backend is a sibling of vsnp_gui and kraken_id_parse_gui and shares their
project layout. All URLs are served from / (uvicorn is behind the OOD rnode
proxy — relative paths only).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_config, save_config
from .jobs import JobManager
from .sra import (
    SRAExpansionError,
    build_download_script,
    expand_accessions_with_mapping,
    write_crosswalk_tsv,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent          # /srv/kapurlab/tools/mhc_gui
_BIN_DIR = _REPO_ROOT / "bin"
_CONFIG_DIR = _REPO_ROOT / "config"
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

# Shared project root
_SHARED_PROJECTS = Path("/srv/kapurlab/projects")

# Jobs log directory (inside repo so it survives across sessions)
_JOBS_DIR = _REPO_ROOT / "backend" / "jobs"

# Fallback list of valid AMRFinderPlus --organism tokens. The live list from
# `amrfinder -l` is DB-version dependent and authoritative; this file is only
# consulted when that command is unavailable.
_ORGANISMS_FALLBACK = _CONFIG_DIR / "amrfinder_organisms.txt"

# ---------------------------------------------------------------------------
# App & job manager
# ---------------------------------------------------------------------------
app = FastAPI(title="AMRFinderPlus GUI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

job_manager = JobManager(_JOBS_DIR)


# ---------------------------------------------------------------------------
# Helpers — project listing
# ---------------------------------------------------------------------------
_SCOPE_SHARED = "shared"
_SCOPE_PERSONAL = "personal"


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime if p.is_dir() else 0
    except PermissionError:
        return 0


def _count_project_reads(download_dir: Path, step1_dir: Path) -> int:
    """Count input read files (*.fastq.gz) across download/ and step1/.

    Native projects keep reads in download/; vSNP/Roar-imported projects keep
    them in step1/<sample>/ (and may symlink them into download/). Count the
    union, deduped by resolved path, skipping *_unmapped_* (the unmapped-read
    subset vSNP3 emits — not an input read set). step1 is globbed one level deep.
    """
    seen: set = set()
    candidates = []
    if download_dir.is_dir():
        candidates += download_dir.rglob("*.fastq.gz")
    if step1_dir.is_dir():
        candidates += step1_dir.glob("*/*.fastq.gz")
    for f in candidates:
        if "_unmapped_" in f.name:
            continue
        try:
            key = f.resolve()
        except OSError:
            key = f
        seen.add(key)
    return len(seen)


def _list_projects_from_root(root: Path, scope: str) -> List[Dict]:
    if not root.is_dir():
        return []
    projects = []
    try:
        entries = sorted(root.iterdir(), key=_safe_mtime, reverse=True)
    except PermissionError:
        return []
    for p in entries:
        try:
            if not p.is_dir() or p.name.startswith("."):
                continue
        except PermissionError:
            continue
        download_dir = p / "download"
        try:
            fastq_count = _count_project_reads(download_dir, p / "step1")
        except PermissionError:
            fastq_count = -1  # signals "no access" to frontend
        amr_runs = []
        amr_dir = p / "amr"
        try:
            if amr_dir.is_dir():
                amr_runs = [d.name for d in sorted(amr_dir.iterdir()) if d.is_dir()]
        except PermissionError:
            pass
        projects.append({
            "name": p.name,
            "path": str(p),
            "scope": scope,
            "fastq_count": fastq_count,
            "amr_runs": amr_runs,
        })
    return projects


def _get_project_dir(name: str) -> Optional[Path]:
    """Find a project dir in shared then personal roots."""
    if "/" in name or name.startswith("."):
        return None
    cfg = load_config()
    for root in [_SHARED_PROJECTS, Path(cfg.get("projects_root", ""))]:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Project creation.
#
# A project created here uses the SAME on-disk skeleton vSNP/Kraken GUIs
# create, so a project made in AMRFinderPlus GUI is immediately usable in the
# siblings (and vice versa) — all tools share /srv/kapurlab/projects and
# per-user ~/projects and list whatever is on disk. We add the amr/ subdir up
# front so the sample browser and results endpoints have a stable layout.
# ---------------------------------------------------------------------------
_PROJECT_NAME_OK_CHARSET = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_project_name(name: str) -> str:
    """Filesystem-safe project dir name. Mirrors the siblings' rules so a name
    accepted in one tool is accepted in the others: spaces auto-convert to
    underscores, other unsafe chars are rejected with a clear message."""
    if not isinstance(name, str):
        raise ValueError("Project name must be a string")
    cleaned = re.sub(r"\s+", "_", name.strip())
    if not cleaned:
        raise ValueError("Project name is empty")
    if cleaned.startswith("."):
        raise ValueError("Project name cannot start with '.'")
    if len(cleaned) > 100:
        raise ValueError("Project name too long (max 100 characters)")
    if not _PROJECT_NAME_OK_CHARSET.match(cleaned):
        bad = sorted(set(ch for ch in cleaned if not re.match(r"[A-Za-z0-9._-]", ch)))
        raise ValueError(
            f"Project name contains unsupported characters: {''.join(bad)!r}. "
            "Only letters, digits, _ - . are allowed (spaces become underscores)."
        )
    return cleaned


def _ensure_project_dirs(project_dir: Path) -> None:
    # MHC Typer layout: linked/uploaded runs, loose uploads, and typing outputs.
    (project_dir / "download").mkdir(parents=True, exist_ok=True)
    (project_dir / "runs").mkdir(parents=True, exist_ok=True)
    (project_dir / "mhc").mkdir(parents=True, exist_ok=True)


def _create_project(name: str, scope: str) -> Path:
    """Create a project under the requested scope ('personal' or 'shared')."""
    name = _normalize_project_name(name)
    cfg = load_config()
    if scope == _SCOPE_SHARED:
        root = _SHARED_PROJECTS
    else:
        root = Path(cfg.get("projects_root", "") or (Path.home() / "projects"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create projects root {root}: {exc}")
    project_dir = root / name
    if project_dir.exists():
        raise ValueError(f"Project already exists: {name}")
    try:
        _ensure_project_dirs(project_dir)
    except PermissionError:
        raise ValueError(
            f"No permission to create a project under {root}. "
            "Shared projects require lab write access; create it as a personal "
            "project instead."
        )
    meta = {"name": name, "created_at": _now_iso(), "status": "created"}
    try:
        with open(project_dir / "project.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, sort_keys=True)
    except OSError:
        pass
    return project_dir


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# Matches _R1/_R2 (with optional _001 etc.) or _1/_2 immediately before .fastq.gz
_READ_TAG_RE = re.compile(r'(?:_R([12])(?:_\d+)?|_([12]))\.fastq\.gz$', re.IGNORECASE)


def _strip_read_tag(filename: str):
    """Return (base, read_num) where read_num is '1', '2', or None."""
    m = _READ_TAG_RE.search(filename)
    if m:
        tag = m.group(1) or m.group(2)
        return filename[:m.start()], tag
    return filename[:-len(".fastq.gz")], None


def _list_fastq_pairs(download_dir: Path) -> List[Dict]:
    """Return samples as {sample, paired, r1, r1_name, r2, r2_name} dicts.

    Handles both Illumina (_R1/_R2) and SRA (_1/_2) naming conventions.
    Files with no read suffix are treated as single-end.
    """
    try:
        all_fq = sorted(download_dir.glob("*.fastq.gz"))
    except PermissionError:
        return []

    groups: Dict[str, Dict] = {}
    for fq in all_fq:
        base, tag = _strip_read_tag(fq.name)
        if base not in groups:
            groups[base] = {"r1": None, "r2": None, "extras": []}
        g = groups[base]
        if tag == "1":
            g["r1"] = fq
        elif tag == "2":
            g["r2"] = fq
        else:
            g["extras"].append(fq)

    pairs = []
    for base, g in groups.items():
        r1, r2 = g["r1"], g["r2"]
        if r1 or r2:
            eff_r1 = r1 or r2
            eff_r2 = r2 if r1 else None
            pairs.append({
                "sample": base,
                "paired": bool(r1 and r2),
                "r1": str(eff_r1), "r1_name": eff_r1.name,
                "r1_size": eff_r1.stat().st_size,
                "r2": str(eff_r2) if eff_r2 else None,
                "r2_name": eff_r2.name if eff_r2 else None,
                "r2_size": eff_r2.stat().st_size if eff_r2 else None,
            })
        for fq in g["extras"]:
            pairs.append({
                "sample": fq.name[:-len(".fastq.gz")],
                "paired": False,
                "r1": str(fq), "r1_name": fq.name,
                "r1_size": fq.stat().st_size,
                "r2": None, "r2_name": None,
                "r2_size": None,
            })

    return pairs


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/projects")
def api_list_projects():
    cfg = load_config()
    projects = _list_projects_from_root(_SHARED_PROJECTS, _SCOPE_SHARED)
    personal_root = Path(cfg.get("projects_root", ""))
    if personal_root != _SHARED_PROJECTS:
        personal = _list_projects_from_root(personal_root, _SCOPE_PERSONAL)
        seen = {p["name"] for p in projects}
        projects += [p for p in personal if p["name"] not in seen]
    return JSONResponse(projects)


class ProjectCreate(BaseModel):
    name: str
    scope: Optional[str] = None   # "personal" (default) | "shared"


@app.post("/api/projects")
def api_create_project(payload: ProjectCreate):
    scope = (payload.scope or _SCOPE_PERSONAL).strip() or _SCOPE_PERSONAL
    if scope not in (_SCOPE_PERSONAL, _SCOPE_SHARED):
        raise HTTPException(400, f"Invalid scope: {scope!r}")
    try:
        project_dir = _create_project(payload.name, scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"name": project_dir.name, "path": str(project_dir), "scope": scope})


# ---------------------------------------------------------------------------
# Loading samples into a project — import (link), upload (drag & drop), and
# SRA download. Mirrors the siblings so a project can be populated from within
# any tool. All three land FASTQs in <project>/download/.
# ---------------------------------------------------------------------------
def _writable_project_dir(name: str) -> Path:
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    (project_dir / "download").mkdir(parents=True, exist_ok=True)
    return project_dir


@app.get("/api/projects/{name}/inputs")
def api_project_inputs(name: str):
    """List files currently in <project>/download/ (name + size + mtime)."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    download_dir = project_dir / "download"
    files: List[Dict] = []
    total = 0
    if download_dir.is_dir():
        for p in sorted(download_dir.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            total += st.st_size
    return JSONResponse({"files": files, "total_bytes": total, "count": len(files)})


@app.delete("/api/projects/{name}/inputs/{filename}")
def api_project_input_delete(name: str, filename: str):
    if not filename or "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    target = project_dir / "download" / filename
    if not target.is_file() and not target.is_symlink():
        raise HTTPException(404, f"File not found: {filename}")
    target.unlink()
    return JSONResponse({"deleted": filename})


@app.post("/api/projects/{name}/upload")
async def api_project_upload(name: str, files: List[UploadFile] = File(...)):
    """Save drag-and-dropped / chosen FASTQ files into <project>/download/."""
    project_dir = _writable_project_dir(name)
    download_dir = project_dir / "download"
    saved = 0
    for f in files:
        if not f.filename:
            continue
        target = download_dir / Path(f.filename).name
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)
        saved += 1
    return JSONResponse({"uploaded": saved})


class LinkLocalRequest(BaseModel):
    path: str


@app.post("/api/projects/{name}/link-local")
def api_project_link_local(name: str, payload: LinkLocalRequest):
    """Symlink every *.fastq.gz (or *.fasta/*.fa/*.fna assembly) under a
    server-side directory into download/.

    Lets users 'import' reads/assemblies that already live on the shared
    filesystem without copying gigabytes around.
    """
    project_dir = _writable_project_dir(name)
    src = Path((payload.path or "").strip()).expanduser()
    if not src.exists():
        raise HTTPException(400, f"Input path not found: {src}")
    download_dir = project_dir / "download"
    _accept = (".fastq.gz", ".fasta", ".fa", ".fna")
    if src.is_file():
        candidates = [src]
    else:
        candidates = sorted(
            f for f in src.iterdir()
            if f.is_file() and f.name.lower().endswith(_accept)
        )
    count = 0
    for f in candidates:
        if not f.name.lower().endswith(_accept):
            continue
        target = download_dir / f.name
        if not target.exists():
            target.symlink_to(f.resolve())
            count += 1
    return JSONResponse({"linked": count})


class SraRequest(BaseModel):
    accessions: List[str]
    folder: Optional[str] = None


@app.post("/api/projects/{name}/sra/download")
def api_project_sra_download(name: str, payload: SraRequest):
    """Resolve SRA accessions and kick off a background download into
    download/. Uses curl/ENA + (if present) fasterq-dump."""
    project_dir = _writable_project_dir(name)
    try:
        expanded, mapping = expand_accessions_with_mapping(payload.accessions, strict=True)
    except SRAExpansionError as e:
        raise HTTPException(
            502,
            f"Could not resolve SRA accessions via NCBI eutils: {e}. "
            "This is usually NCBI rate-limiting; wait ~30 s and retry.",
        )
    download_root = project_dir / "download"
    if payload.folder:
        download_root = download_root / Path(payload.folder).name
    download_root.mkdir(parents=True, exist_ok=True)
    try:
        write_crosswalk_tsv(download_root, mapping)
    except OSError as e:
        logger.warning("Failed to write sra_crosswalk.tsv: %s", e)
    script = build_download_script(download_root, expanded, allow_insecure_https=False)
    script_path = download_root / "download_sra.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", "")}
    job_id = job_manager.start_job(
        name=f"sra_download — {name}",
        command=["bash", str(script_path)],
        cwd=download_root,
        env=env,
    )
    return JSONResponse({"job_id": job_id})


@app.get("/api/projects/{name}/sra-crosswalk")
def api_project_sra_crosswalk(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    crosswalk = project_dir / "download" / "sra_crosswalk.tsv"
    if not crosswalk.is_file():
        raise HTTPException(404, "No SRA crosswalk for this project")
    return FileResponse(crosswalk, media_type="text/plain")


@app.get("/api/projects/{name}/samples")
def api_project_samples(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    download_dir = project_dir / "download"
    if not download_dir.is_dir():
        return JSONResponse([])
    return JSONResponse(_list_fastq_pairs(download_dir))


# ---------------------------------------------------------------------------
# Per-sample AMR results (decoupled from a single job).
#
# Results are read straight from <project>/amr/<sample>/ on disk so any
# previously-run sample's outputs can be revisited — not just the last job.
# ---------------------------------------------------------------------------
def _collect_result_files(run_dir: Path, include_all: bool) -> List[Dict]:
    """List result files under an amr run dir, categorized + sorted."""
    files: List[Dict] = []
    if not run_dir.is_dir():
        return files
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file() or p.name.endswith(".log"):
            continue
        rel = str(p.relative_to(run_dir))
        category = _result_category(rel)
        if not include_all and category is None:
            continue
        stat = p.stat()
        files.append({
            "name": rel,
            "path": str(p),
            "label": _result_label(rel, category),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "openable": _can_open_inline(rel),
            "category": category,
        })

    def sort_key(f):
        category = f.get("category")
        if category in _CATEGORY_ORDER:
            return (_CATEGORY_ORDER[category], f["name"])
        return (50, f["name"])

    files.sort(key=sort_key)
    for f in files:
        f.pop("mtime", None)
        if include_all and f.get("category") is None:
            f["label"] = f["name"]
    return files


def _sample_run_status(run_dir: Path) -> str:
    """Status for a sample: 'running' if a live job owns its dir, else 'done'
    if the dir holds output, else 'none'."""
    run_dir_str = str(run_dir)
    for job in job_manager.list_jobs():
        if job.get("cwd") == run_dir_str and job.get("status") == "running":
            return "running"
    try:
        if run_dir.is_dir() and any(p.is_file() for p in run_dir.rglob("*")):
            return "done"
    except PermissionError:
        pass
    return "none"


@app.get("/api/projects/{name}/samples/{sample}/amr-results")
def api_sample_amr_results(name: str, sample: str, all: int = Query(0)):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    run_dir = project_dir / "amr" / sample
    return JSONResponse({
        "project": name,
        "sample": sample,
        "present": run_dir.is_dir(),
        "status": _sample_run_status(run_dir),
        "run_dir": str(run_dir),
        "files": _collect_result_files(run_dir, bool(all)),
    })


# ---------------------------------------------------------------------------
# AMRFinderPlus TSV parsing — turn the per-sample amrfinder.tsv into a
# structured summary + rows the results table renders. Tolerant of the leading
# `name` column (--name) and the trailing `Hierarchy node` column
# (--print_node), and of header-label drift across DB versions.
# ---------------------------------------------------------------------------
def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


# Map normalized header tokens -> canonical field names we emit.
_AMR_FIELD_ALIASES = {
    "name": "name",
    "protein_id": "protein_id",
    "protein_identifier": "protein_id",
    "contig_id": "contig_id",
    "contig": "contig_id",
    "start": "start",
    "stop": "stop",
    "strand": "strand",
    "element_symbol": "element_symbol",
    "gene_symbol": "element_symbol",
    "element_name": "element_name",
    "sequence_name": "element_name",
    "scope": "scope",
    "type": "type",
    "element_type": "type",
    "subtype": "subtype",
    "element_subtype": "subtype",
    "class": "class",
    "subclass": "subclass",
    "method": "method",
    "target_length": "target_length",
    "reference_sequence_length": "ref_length",
    "ref_seq_len": "ref_length",
    "coverage_of_reference": "pct_coverage",
    "coverage_of_reference_sequence": "pct_coverage",
    "identity_to_reference": "pct_identity",
    "identity_to_reference_sequence": "pct_identity",
    "alignment_length": "alignment_length",
    "closest_reference_accession": "closest_ref_accession",
    "accession_of_closest_sequence": "closest_ref_accession",
    "closest_reference_name": "closest_ref_name",
    "name_of_closest_sequence": "closest_ref_name",
    "hmm_accession": "hmm_accession",
    "hmm_id": "hmm_accession",
    "hmm_description": "hmm_description",
    "hierarchy_node": "hierarchy_node",
}


def _parse_amrfinder_tsv(tsv_path: Path) -> Dict[str, Any]:
    """Parse an amrfinder.tsv into {rows, summary, columns}. Returns
    {rows: []} if the file is empty or only a header."""
    rows: List[Dict[str, Any]] = []
    try:
        text = tsv_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"rows": [], "summary": {}, "columns": []}
    lines = [ln for ln in text.splitlines() if ln.strip() != ""]
    if not lines:
        return {"rows": [], "summary": {}, "columns": []}
    raw_header = lines[0].split("\t")
    header = [_AMR_FIELD_ALIASES.get(_norm_header(h), _norm_header(h)) for h in raw_header]
    for line in lines[1:]:
        cells = line.split("\t")
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        row = {header[i]: cells[i] for i in range(len(header))}
        rows.append(row)

    # ---- summary aggregation ----
    by_class: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    point_mutations = 0
    plus_count = 0
    for r in rows:
        cls = (r.get("class") or "").strip() or "(unclassified)"
        by_class[cls] = by_class.get(cls, 0) + 1
        typ = (r.get("type") or "").strip().upper() or "UNKNOWN"
        by_type[typ] = by_type.get(typ, 0) + 1
        subtype = (r.get("subtype") or "").strip().upper()
        method = (r.get("method") or "").strip().upper()
        if subtype == "POINT" or method.startswith("POINT"):
            point_mutations += 1
        scope = (r.get("scope") or "").strip().lower()
        if scope == "plus":
            plus_count += 1

    summary = {
        "total": len(rows),
        "by_class": by_class,
        "by_type": by_type,
        "point_mutations": point_mutations,
        "plus_count": plus_count,
    }
    return {"rows": rows, "summary": summary, "columns": header}


@app.get("/api/projects/{name}/samples/{sample}/amr-table")
def api_sample_amr_table(name: str, sample: str):
    """Parse <project>/amr/<sample>/amrfinder.tsv into a structured table plus
    the organism call and provenance (from organism_detection.json /
    run_manifest.json) so the Results pane can render everything in one fetch."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    run_dir = project_dir / "amr" / sample
    tsv = run_dir / "amrfinder.tsv"
    organism = {}
    provenance = {}
    det = run_dir / "organism_detection.json"
    if det.is_file():
        try:
            organism = json.loads(det.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            organism = {}
    man = run_dir / "run_manifest.json"
    if man.is_file():
        try:
            provenance = json.loads(man.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            provenance = {}
    parsed = _parse_amrfinder_tsv(tsv) if tsv.is_file() else {"rows": [], "summary": {}, "columns": []}
    return JSONResponse({
        "project": name,
        "sample": sample,
        "present": tsv.is_file(),
        "organism": organism,
        "provenance": provenance,
        "summary": parsed["summary"],
        "columns": parsed["columns"],
        "rows": parsed["rows"],
    })


# ---------------------------------------------------------------------------
# Cross-tool visibility — surface vSNP results for a sample (read-only).
# ---------------------------------------------------------------------------
def _resolve_vsnp_sample_dir(step1_dir: Path, sample: str) -> Optional[Path]:
    """Resolve a sample name to its vSNP step1 subdirectory."""
    exact = step1_dir / sample
    if exact.is_dir():
        return exact
    try:
        candidates = sorted(
            d for d in step1_dir.iterdir()
            if d.is_dir() and d.name.startswith(f"{sample}_")
        )
    except (OSError, PermissionError):
        return None
    return candidates[0] if candidates else None


@app.get("/api/projects/{name}/vsnp/samples/{sample}/files")
def api_vsnp_sample_files(name: str, sample: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    step1_dir = project_dir / "step1"
    sample_dir = _resolve_vsnp_sample_dir(step1_dir, sample) if step1_dir.is_dir() else None
    files: List[Dict] = []
    sample_dir_str = ""
    if sample_dir:
        base = sample_dir.resolve()
        sample_dir_str = str(base)
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.name.startswith(".~lock"):
                continue
            try:
                rel = p.relative_to(base).as_posix()
                st = p.stat()
            except (OSError, ValueError):
                continue
            files.append({
                "name": p.name,
                "relpath": rel,
                "path": str(p),
                "size": st.st_size,
                "openable": _can_open_inline(p.name),
                "type": p.suffix.lstrip(".").lower() or "file",
            })
    return JSONResponse({
        "project": name,
        "sample": sample,
        "step1_present": bool(sample_dir),
        "step1_dir": sample_dir_str,
        "files": files,
    })


@app.get("/api/projects/{name}/file")
def api_project_file(name: str, path: str = Query(...), inline: int = 0):
    """Serve a file from anywhere inside a project dir (cross-tool downloads)."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    root = project_dir.resolve()
    target = Path(path).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(403, "Path outside project directory")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    media_type = _media_type_for(target.name)
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{target.name}"'}
    return FileResponse(target, media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Organism options — valid AMRFinderPlus --organism tokens.
#
# The authoritative list comes from `amrfinder -l`, which is DB-version
# dependent. We cache it (in-process) and fall back to the shipped
# config/amrfinder_organisms.txt if the binary or DB is unavailable.
# ---------------------------------------------------------------------------
_ORG_CACHE: Dict[str, Any] = {"organisms": None, "db_version": None, "source": None, "ts": 0.0}
_ORG_CACHE_TTL = 600  # seconds


def _read_fallback_organisms() -> List[str]:
    try:
        text = _ORGANISMS_FALLBACK.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _query_amrfinder_organisms() -> Dict[str, Any]:
    """Query `amrfinder -l` for the valid --organism tokens + DB version.

    Returns {organisms, db_version, source}. Falls back to the shipped list
    when amrfinder is not on PATH or fails.
    """
    organisms: List[str] = []
    db_version: Optional[str] = None
    source = "fallback"
    try:
        proc = subprocess.run(
            ["amrfinder", "-l"],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for line in out.splitlines():
            low = line.lower()
            if "database version" in low:
                m = re.search(r"version[:\s]+([0-9][\w.\-]*)", line, re.IGNORECASE)
                if m and db_version is None:
                    db_version = m.group(1)
            if "available --organism" in low or "--organism options" in low or "valid options" in low:
                tail = line.split(":", 1)[-1]
                organisms = [t.strip() for t in re.split(r"[,\s]+", tail) if t.strip()]
        # Some versions print one organism per line; grab plausible tokens too.
        if not organisms:
            for line in out.splitlines():
                s = line.strip()
                if re.fullmatch(r"[A-Z][a-z]+(?:_[a-z]+)*", s):
                    organisms.append(s)
        if organisms:
            source = "amrfinder"
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        logger.info("amrfinder -l unavailable (%s); using fallback organism list", exc)

    if not organisms:
        organisms = _read_fallback_organisms()
        source = "fallback"
    # de-dupe, preserve order
    seen = set()
    uniq = []
    for o in organisms:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return {"organisms": uniq, "db_version": db_version, "source": source}


@app.get("/api/organism-options")
def api_organism_options(refresh: int = Query(0)):
    now = time.time()
    if (not refresh) and _ORG_CACHE["organisms"] is not None and (now - _ORG_CACHE["ts"] < _ORG_CACHE_TTL):
        return JSONResponse({
            "organisms": _ORG_CACHE["organisms"],
            "db_version": _ORG_CACHE["db_version"],
            "source": _ORG_CACHE["source"],
        })
    result = _query_amrfinder_organisms()
    _ORG_CACHE.update({
        "organisms": result["organisms"],
        "db_version": result["db_version"],
        "source": result["source"],
        "ts": now,
    })
    return JSONResponse(result)


@app.get("/api/config")
def api_get_config():
    return JSONResponse(load_config())


class ConfigPayload(BaseModel):
    projects_root: Optional[str] = None
    shared_projects_root: Optional[str] = None
    runs_root: Optional[str] = None
    barcode_map: Optional[str] = None
    bola_refs: Optional[str] = None
    ont_env_bin: Optional[str] = None
    phase_env_bin: Optional[str] = None
    medaka_model: Optional[str] = None
    enable_class_i: Optional[bool] = None


@app.post("/api/config")
def api_save_config(payload: ConfigPayload):
    cfg = load_config()
    updates = payload.model_dump(exclude_none=True)
    cfg.update(updates)
    new_root = (updates.get("projects_root") or "").strip()
    if new_root:
        recent = [r for r in cfg.get("recent_projects_roots", []) if r != new_root]
        recent.insert(0, new_root)
        cfg["recent_projects_roots"] = recent[:10]
    save_config(cfg)
    return JSONResponse({"ok": True})


@app.get("/api/browse-dirs")
def api_browse_dirs(path: str = ""):
    """List sub-directories of `path` for the project-root folder picker."""
    try:
        p = (Path(path).expanduser() if path.strip() else Path.home()).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "Invalid path")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    entries: List[Dict[str, str]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {p}")
    parent = str(p.parent) if p.parent != p else None
    return JSONResponse({"path": str(p), "parent": parent, "entries": entries})


class SampleEntry(BaseModel):
    barcode: str
    sample: str = ""
    amplicon: str = ""           # sheet amplicon; routes the barcode to its pipeline


class RunPayload(BaseModel):
    project: str
    run_dir: str                 # absolute path to the run folder (barcodeNN/ subdirs)
    samples: List[SampleEntry]
    force_amplicon: str = ""     # "" = auto (each sample's own amplicon from the sheet)
    threads: Optional[int] = None


@app.post("/api/run")
def api_run(payload: RunPayload):
    cfg = load_config()
    project_dir = _get_project_dir(payload.project)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {payload.project}")

    run_src = Path(payload.run_dir)
    if not run_src.is_dir():
        raise HTTPException(400, f"Run folder not found: {payload.run_dir}")
    if not payload.samples:
        raise HTTPException(400, "No samples selected")

    force = _amplicon_token(payload.force_amplicon) if payload.force_amplicon.strip() else ""
    class_i_enabled = bool(cfg.get("enable_class_i", False))

    run_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", run_src.name).strip("_")
    out_dir = project_dir / "mhc" / run_tag

    # Refuse to start a second pipeline in the same output directory (race).
    for existing in job_manager.list_jobs():
        if existing.get("status") == "running" and existing.get("cwd") == str(out_dir):
            raise HTTPException(
                409,
                f"A run is already in progress for {run_tag} "
                f"(job {existing['id'][:8]}). Wait for it to finish before re-running.",
            )

    # Build a 4-col manifest (barcode, sample, reads_source, amplicon) — each
    # barcode routed to its own amplicon pipeline (auto), unless force overrides.
    # No path ever hits the command line (spaces/[brackets] safe).
    rows, skipped = [], []
    for s in payload.samples:
        bc = s.barcode.strip()
        if not bc:
            continue
        amp = force or _amplicon_token(s.amplicon)
        if not amp or amp == "amp":
            skipped.append(f"{bc} (no amplicon)")
            continue
        if amp in _CLASS_I and not class_i_enabled:
            skipped.append(f"{bc} (Class I disabled)")
            continue
        src = run_src / bc
        if src.is_dir():
            reads = str(src)
        else:
            m = sorted(run_src.glob(f"{bc}*.fastq.gz"))
            reads = str(m[0]) if m else str(src)
        rows.append(f"{bc}\t{s.sample or bc}\t{reads}\t{amp}")
    if not rows:
        raise HTTPException(400, "No runnable samples — no amplicon in the sheet, "
                                 "or only Class-I barcodes with Class I disabled.")

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.tsv"
    manifest_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    script = _BIN_DIR / "run_typing.py"
    command = [sys.executable, "-u", str(script),
               "--manifest", str(manifest_path),
               "--outdir", str(out_dir),
               "--run-folder", run_src.name]
    if payload.threads:
        command.extend(["--threads", str(int(payload.threads))])

    # Hand the pipeline its runtime paths from the per-user config (mhc_config
    # reads these MHC_* env vars). PATH gets the tool env bins so bioconda tools
    # resolve their own interpreters/libs (see BUILDING_A_SIBLING_TOOL §11.1).
    ont_bin = cfg.get("ont_env_bin", "")
    phase_bin = cfg.get("phase_env_bin", "")
    env = {
        "PYTHONPATH": str(_BIN_DIR),
        "PATH": ":".join(p for p in [ont_bin, phase_bin, os.environ.get("PATH", "")] if p),
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "MHC_ONT_BIN": ont_bin,
        "MHC_PHASE_BIN": phase_bin,
        "MHC_REFS": cfg.get("bola_refs", ""),
        "MHC_MEDAKA_MODEL": cfg.get("medaka_model", ""),
    }

    amps = "+".join(sorted({r.split("\t")[3] for r in rows}))
    job_name = f"{payload.project}/{run_tag} — MHC Typer ({len(rows)} samples · {amps})"
    job_id = job_manager.start_job(name=job_name, command=command, cwd=out_dir, env=env)
    return JSONResponse({"job_id": job_id, "run_dir": str(out_dir), "skipped": skipped})


def _parse_sheet(path: str, default_run: str = "") -> Dict[str, Dict[str, Dict[str, str]]]:
    """Parse a sample sheet (TSV *or* CSV — the delimiter is sniffed) into
    {run_folder: {barcode: {sample, tissue, amplicon}}}. Required columns:
    barcode, sample_id (+ optional run_folder, tissue, amplicon). A per-run
    in-folder sheet omits run_folder — its rows go under `default_run`."""
    out: Dict[str, Dict[str, Dict[str, str]]] = {}
    if not path or not Path(path).is_file():
        return out
    try:
        with open(path, encoding="utf-8-sig") as fh:
            first = fh.readline()
            delim = "\t" if first.count("\t") >= first.count(",") else ","
            header = [h.strip() for h in first.rstrip("\n").split(delim)]
            idx = {name: i for i, name in enumerate(header)}
            bc, sid = idx.get("barcode"), idx.get("sample_id")
            if bc is None or sid is None:
                return out
            rf, tis, amp = idx.get("run_folder"), idx.get("tissue"), idx.get("amplicon")
            aid = idx.get("animal_id")
            for line in fh:
                if not line.strip():
                    continue
                f = [c.strip() for c in line.rstrip("\n").split(delim)]
                if len(f) <= max(bc, sid):
                    continue

                def g(i):
                    return f[i] if i is not None and len(f) > i else ""

                run = f[rf] if rf is not None and len(f) > rf and f[rf] else default_run
                out.setdefault(run, {}).setdefault(f[bc], {
                    "sample": f[sid],
                    "animal": g(aid) or f[sid],   # animal_id, falling back to sample_id
                    "tissue": g(tis), "amplicon": g(amp),
                })
    except OSError:
        pass
    return out


def _barcode_map(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Dict[str, str]]]:
    """The site-wide sample map (config `barcode_map`)."""
    return _parse_sheet(cfg.get("barcode_map", ""))


def _project_sheet(project_dir: Path) -> Dict[str, Dict[str, Dict[str, str]]]:
    """A project's own uploaded sample sheet."""
    return _parse_sheet(str(project_dir / "sample_sheet.tsv"))


def _run_sheet(run_dir: Path) -> Dict[str, Dict[str, str]]:
    """A sample sheet that travels *inside* the run folder — the preferred source.
    Accepts sample_sheet.csv/tsv OR sample_sheet_YYYYMMDD.csv/tsv; the latest date
    in the name wins (plain = baseline), mtime breaks ties. The folder IS the run,
    so any run_folder column is ignored (merge all rows). Keyed lowercase."""
    cands = list(run_dir.glob("sample_sheet*.csv")) + list(run_dir.glob("sample_sheet*.tsv"))
    cands = [p for p in cands if p.is_file()]
    if not cands:
        return {}

    def key(p):
        m = re.search(r"(\d{8})", p.name)
        try:
            return (m.group(1) if m else "00000000", p.stat().st_mtime)
        except OSError:
            return ("00000000", 0)

    chosen = max(cands, key=key)
    parsed = _parse_sheet(str(chosen), default_run=run_dir.name)
    merged: Dict[str, Dict[str, str]] = {}
    for barcodes in parsed.values():
        for bc, meta in barcodes.items():
            merged[bc.lower()] = meta
    merged["__sheet__"] = {"name": chosen.name}  # surfaced so the UI shows which sheet was used
    return merged


_RUN_SHEETS_DIR = "run_sheets"


def _run_override(project_dir: Path, run_name: str) -> Dict[str, Dict[str, str]]:
    """A per-run sheet override kept in the project (project/run_sheets/<run>.tsv), so a
    linked (read-only) run can be given a sheet without touching the run folder. Highest
    precedence — an explicit per-run choice wins over the run's own in-folder sheet."""
    p = project_dir / _RUN_SHEETS_DIR / f"{run_name}.tsv"
    if not p.is_file():
        return {}
    merged: Dict[str, Dict[str, str]] = {}
    for barcodes in _parse_sheet(str(p), default_run=run_name).values():
        for bc, meta in barcodes.items():
            merged[bc.lower()] = meta
    if merged:
        merged["__sheet__"] = {"name": p.name}
    return merged


def _effective_sheet(project_dir: Path, run_dir: Path,
                     project_sheet: Dict, site: Dict):
    """Resolve the sheet a run actually uses, highest precedence first:
    per-run override -> in-run sheet -> project sheet -> site map -> none.
    Returns (rmap, source, name)."""
    ov = _run_override(project_dir, run_dir.name)
    if ov:
        return ov, "override", ov.get("__sheet__", {}).get("name", "")
    inrun = _run_sheet(run_dir)
    if inrun:
        return inrun, "in-run", inrun.get("__sheet__", {}).get("name", "")
    ps = project_sheet.get(run_dir.name)
    if ps:
        return ps, "project", "sample_sheet.tsv"
    st = site.get(run_dir.name)
    if st:
        return st, "site", "(site map)"
    return {}, "none", ""


def _annotate(rmap_raw: Dict[str, Dict[str, str]], names) -> List[Dict[str, str]]:
    """Attach sample/animal/tissue/amplicon to each barcode, matching
    case-insensitively (runs use Barcode01 or barcode01; sheets may differ)."""
    rmap = {k.lower(): v for k, v in rmap_raw.items() if k != "__sheet__"}
    return [{"barcode": b,
             "sample": rmap.get(b.lower(), {}).get("sample", ""),
             "animal": rmap.get(b.lower(), {}).get("animal", ""),
             "tissue": rmap.get(b.lower(), {}).get("tissue", ""),
             "amplicon": rmap.get(b.lower(), {}).get("amplicon", "")}
            for b in names]


def _amplicon_token(amp: str) -> str:
    """Canonical, path-safe pipeline token for a sheet amplicon value.
    DRB3->drb3, Bov7/11->bov711, BosEx->bosex, 5'UTR->utr5."""
    low = re.sub(r"[^a-z0-9]", "", str(amp or "").lower())
    if "drb3" in low:
        return "drb3"
    if "bov" in low:
        return "bov711"
    if "bosex" in low:
        return "bosex"
    if "utr" in low:
        return "utr5"
    return low or "amp"


_CLASS_I = {"bov711", "bosex", "utr5"}


@app.get("/api/runs")
def api_runs():
    """List ONT run folders (barcodeNN/ subdirs) under the configured runs_root,
    each with its barcodes annotated with the animal/sample ID from the map."""
    cfg = load_config()
    bmap = _barcode_map(cfg)
    out: List[Dict[str, Any]] = []
    seen = set()
    for root in [cfg.get("runs_root", ""), cfg.get("shared_projects_root", "")]:
        if not root or not Path(root).is_dir():
            continue
        for d in sorted(Path(root).iterdir()):
            if not d.is_dir() or str(d) in seen:
                continue
            try:
                names = sorted(p.name for p in d.iterdir()
                               if p.is_dir() and p.name.lower().startswith("barcode"))
            except PermissionError:
                continue
            if not names:
                continue
            seen.add(str(d))
            barcodes = _annotate(_run_sheet(d) or bmap.get(d.name, {}), names)
            out.append({"name": d.name, "path": str(d), "barcodes": barcodes})
    return JSONResponse(out)


class LinkRunRequest(BaseModel):
    source: str


@app.post("/api/projects/{name}/link-run")
def api_project_link_run(name: str, payload: LinkRunRequest):
    """Link an existing ONT run folder (barcodeNN/ subdirs) into the project's
    runs/ — a symlink, no copy."""
    project_dir = _writable_project_dir(name)
    src = Path((payload.source or "").strip()).expanduser()
    if not src.is_dir():
        raise HTTPException(400, f"Run folder not found: {src}")
    try:
        has_bc = any(p.is_dir() and p.name.lower().startswith("barcode") for p in src.iterdir())
    except OSError:
        has_bc = False
    if not has_bc:
        raise HTTPException(400, "That folder has no barcodeNN/ subdirs — not a run folder.")
    runs_dir = project_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / src.name
    if target.exists() or target.is_symlink():
        raise HTTPException(409, f"Run already linked: {src.name}")
    target.symlink_to(src.resolve())
    return JSONResponse({"linked": src.name})


@app.post("/api/projects/{name}/upload-run")
async def api_project_upload_run(name: str,
                                 paths: str = Form(...),
                                 files: List[UploadFile] = File(...)):
    """Upload a whole run folder from a local machine (barcodeNN/*.fastq.gz +
    sample_sheet.csv), preserving structure into the project's runs/. `paths` is
    a JSON array of each file's relative path (browser webkitRelativePath)."""
    project_dir = _writable_project_dir(name)
    try:
        rel_paths = json.loads(paths)
    except json.JSONDecodeError:
        raise HTTPException(400, "Bad paths payload")
    runs_dir = project_dir / "runs"
    saved, run_root = 0, ""
    for f, rel in zip(files, rel_paths):
        parts = [p for p in Path(str(rel).replace("\\", "/")).parts if p not in ("..", "", "/")]
        if not parts:
            continue
        if not run_root:
            run_root = parts[0]
        target = runs_dir.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)
        saved += 1
    return JSONResponse({"saved": saved, "run": run_root})


@app.post("/api/projects/{name}/sample-sheet")
async def api_project_sample_sheet(name: str, file: UploadFile = File(...)):
    """Upload the project's sample sheet (barcode -> sample_id -> tissue -> amplicon)."""
    project_dir = _writable_project_dir(name)
    dest = project_dir / "sample_sheet.tsv"
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            await out.write(chunk)
    parsed = _parse_sheet(str(dest))
    return JSONResponse({"saved": True, "run_folders": list(parsed.keys()),
                         "barcodes": sum(len(v) for v in parsed.values())})


def _linked_run_dir(project_dir: Path, run: str) -> Path:
    """Resolve a linked run by name, rejecting traversal / unknown runs."""
    if run in ("", ".", "..") or "/" in run or "\\" in run:
        raise HTTPException(400, "Bad run name")
    d = project_dir / "runs" / run
    if not d.is_dir():
        raise HTTPException(404, f"Run not linked: {run}")
    return d


def _run_barcode_names(run_dir: Path) -> List[str]:
    try:
        return sorted(p.name for p in run_dir.iterdir()
                      if p.is_dir() and p.name.lower().startswith("barcode"))
    except OSError:
        return []


@app.get("/api/projects/{name}/runs/{run}/sheet")
def api_run_sheet(name: str, run: str):
    """Preview the sheet a run actually uses — its effective source + resolved rows."""
    cfg = load_config()
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    d = _linked_run_dir(project_dir, run)
    rmap, source, sname = _effective_sheet(project_dir, d, _project_sheet(project_dir), _barcode_map(cfg))
    rows = _annotate(rmap, _run_barcode_names(d))
    return JSONResponse({"run": run, "source": source, "name": sname,
                         "named": sum(1 for r in rows if r["sample"]), "rows": rows})


@app.get("/api/projects/{name}/runs/{run}/sheet/download")
def api_run_sheet_download(name: str, run: str):
    """Download the effective sheet as TSV — exactly the mapping the pipeline will use
    (works for any source: a real file, the site map, or bare barcode numbers)."""
    cfg = load_config()
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    d = _linked_run_dir(project_dir, run)
    rmap, _source, _name = _effective_sheet(project_dir, d, _project_sheet(project_dir), _barcode_map(cfg))
    rows = _annotate(rmap, _run_barcode_names(d))
    lines = ["run_folder\tamplicon\tbarcode\tsample_id\ttissue"]
    lines += [f"{run}\t{r['amplicon']}\t{r['barcode']}\t{r['sample']}\t{r['tissue']}" for r in rows]
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", run) + "_sample_sheet.tsv"
    return Response("\n".join(lines) + "\n", media_type="text/tab-separated-values",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/projects/{name}/runs/{run}/sheet")
async def api_run_sheet_upload(name: str, run: str, file: UploadFile = File(...)):
    """Upload a per-run sheet override (stored in the project, not the run folder)."""
    project_dir = _writable_project_dir(name)
    _linked_run_dir(project_dir, run)
    ov_dir = project_dir / _RUN_SHEETS_DIR
    ov_dir.mkdir(parents=True, exist_ok=True)
    dest = ov_dir / f"{run}.tsv"
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            await out.write(chunk)
    ov = _run_override(project_dir, run)
    return JSONResponse({"saved": True, "source": "override",
                         "barcodes": sum(1 for k in ov if k != "__sheet__")})


@app.delete("/api/projects/{name}/runs/{run}/sheet")
def api_run_sheet_clear(name: str, run: str):
    """Remove a per-run override, falling back to in-run / project / site."""
    project_dir = _writable_project_dir(name)
    dest = project_dir / _RUN_SHEETS_DIR / f"{run}.tsv"
    if dest.is_file():
        dest.unlink()
    return JSONResponse({"cleared": True})


@app.get("/api/projects/{name}/inputs-status")
def api_project_inputs_status(name: str):
    """What's loaded into a project: linked run folders + sample-sheet status."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    cfg = load_config()
    project_sheet = _project_sheet(project_dir)
    site = _barcode_map(cfg)
    runs_dir = project_dir / "runs"
    runs = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if not d.is_dir():
                continue
            try:
                names = [p.name for p in d.iterdir()
                         if p.is_dir() and p.name.lower().startswith("barcode")]
            except OSError:
                names = []
            rmap, source, sname = _effective_sheet(project_dir, d, project_sheet, site)
            ann = _annotate(rmap, names)
            runs.append({"name": d.name, "barcodes": len(names),
                         "sheet": {"source": source, "name": sname,
                                   "named": sum(1 for b in ann if b["sample"])}})
    parsed = project_sheet
    return JSONResponse({
        "runs": runs,
        "sheet": {"present": (project_dir / "sample_sheet.tsv").is_file(),
                  "run_folders": list(parsed.keys()),
                  "barcodes": sum(len(v) for v in parsed.values())},
    })


@app.get("/api/projects/{name}/runs")
def api_project_runs(name: str):
    """The project's linked runs, barcodes annotated from the project sheet
    (falling back to the site-wide map)."""
    cfg = load_config()
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    sheet = _project_sheet(project_dir)
    site = _barcode_map(cfg)
    runs_dir = project_dir / "runs"
    out = []
    if runs_dir.is_dir():
        for d in sorted(runs_dir.iterdir()):
            if not d.is_dir():
                continue
            try:
                names = sorted(p.name for p in d.iterdir()
                               if p.is_dir() and p.name.lower().startswith("barcode"))
            except OSError:
                continue
            if not names:
                continue
            rmap, source, sname = _effective_sheet(project_dir, d, sheet, site)
            barcodes = _annotate(rmap, names)
            out.append({"name": d.name, "path": str(d.resolve()), "barcodes": barcodes,
                        "sheet": {"source": source, "name": sname,
                                  "named": sum(1 for b in barcodes if b["sample"])}})

    # Uploaded reads: each FASTQ in download/ is treated as one sample.
    dl = project_dir / "download"
    if dl.is_dir():
        stems = sorted({re.sub(r"_R?[12](_\d+)?$", "",
                               re.sub(r"\.(fastq|fq)\.gz$", "", f.name, flags=re.IGNORECASE))
                        for f in dl.glob("*.fastq.gz")})
        if stems:
            out.append({"name": "Uploaded reads", "path": str(dl),
                        "barcodes": [{"barcode": s, "sample": s, "tissue": "", "amplicon": ""}
                                     for s in stems]})
    return JSONResponse(out)


class ImportRunRequest(BaseModel):
    run: str


def _rclone_env(cfg) -> Dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}
    if cfg.get("rclone_config"):
        env["RCLONE_CONFIG"] = cfg["rclone_config"]
    return env


@app.get("/api/onedrive-runs")
def api_onedrive_runs():
    """Runs sitting in the OneDrive inbox (For_WGS3_Upload), flagged for whether
    each is already in the library."""
    cfg = load_config()
    remote = cfg.get("onedrive_remote", "").strip()
    inbox = cfg.get("onedrive_inbox", "For_WGS3_Upload").strip()
    if not remote:
        return JSONResponse([])
    try:
        r = subprocess.run(["rclone", "lsf", "--dirs-only", f"{remote}{inbox}"],
                           text=True, capture_output=True, timeout=45, env={**os.environ, **_rclone_env(cfg)})
        dirs = [d.rstrip("/") for d in r.stdout.splitlines() if d.strip()]
    except (subprocess.SubprocessError, OSError):
        dirs = []
    lib = Path(cfg.get("runs_root", ""))
    return JSONResponse([{"name": d, "in_library": (lib / d).exists()} for d in sorted(dirs)])


@app.post("/api/import-run")
def api_import_run(payload: ImportRunRequest):
    """Import one inbox run into the library (background rclone job; archives on success)."""
    cfg = load_config()
    remote = cfg.get("onedrive_remote", "").strip()
    inbox = cfg.get("onedrive_inbox", "For_WGS3_Upload").strip()
    archive = cfg.get("onedrive_archive", "Uploaded_Archive").strip()
    lib = cfg.get("runs_root", "").strip()
    run = (payload.run or "").strip().strip("/")
    if not remote or not lib:
        raise HTTPException(400, "OneDrive/library not configured")
    if not run or "/" in run or run.startswith("."):
        raise HTTPException(400, "Invalid run name")
    for j in job_manager.list_jobs():
        if j.get("status") == "running" and j.get("name") == f"import {run}":
            raise HTTPException(409, f"{run} is already importing (job {j['id'][:8]}).")
    command = ["bash", str(_BIN_DIR / "import_run.sh"), remote, inbox, archive, lib, run]
    job_id = job_manager.start_job(name=f"import {run}", command=command,
                                   cwd=Path(lib), env=_rclone_env(cfg))
    return JSONResponse({"job_id": job_id})


_EXAMPLE_SHEETS = _REPO_ROOT / "examples" / "sample_sheets"


@app.get("/api/example-sheets")
def api_example_sheets():
    """List the per-run example sample sheets + the blank template."""
    out = []
    if _EXAMPLE_SHEETS.is_dir():
        for f in sorted(_EXAMPLE_SHEETS.glob("*.tsv")):
            out.append({"name": f.name, "template": f.name.startswith("TEMPLATE")})
    return JSONResponse(out)


@app.get("/api/example-sheets/{filename}")
def api_example_sheet(filename: str):
    f = _EXAMPLE_SHEETS / Path(filename).name
    if not f.is_file():
        raise HTTPException(404, "Example sheet not found")
    return FileResponse(str(f), media_type="text/tab-separated-values", filename=f.name)


def _drb3_qc(c1: int, c2: int, zyg: str) -> str:
    """vSNP-style pass / review / fail for a DRB3 call, from allele read support.
    DRB3 is single-copy: a clean het has two well-supported alleles, a clean hom
    one. Thin/absent primary support fails; thin secondary or low depth reviews."""
    if not zyg or zyg in ("none", "MISSING", "NO_READS", "ERROR") or c1 < 20:
        return "fail"
    if c1 < 100 or (zyg == "het" and c2 < 50):
        return "review"
    return "pass"


def _parse_allele(field: str):
    """'BoLA-DRB3*167:01:12' -> ('BoLA-DRB3*167:01', 12). The allele name itself
    contains colons, so split the trailing count off the right."""
    field = (field or "").strip()
    if not field:
        return "", 0
    allele, _, count = field.rpartition(":")
    if not allele:
        return count, 0
    try:
        return allele, int(count)
    except ValueError:
        return field, 0


@app.get("/api/jobs/{job_id}/table")
def api_job_table(job_id: str):
    """Parse the run's drb3_typed.tsv into a per-animal genotype + QC table."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    cwd = Path(job.get("cwd", ""))
    tsv = cwd / "drb3_typed.tsv"
    if not tsv.is_file():
        return JSONResponse({"rows": [], "summary": {"pass": 0, "review": 0, "fail": 0, "total": 0}})

    # animal IDs: resolve the run folder from the manifest, then the map
    cfg = load_config()
    run_folder = ""
    try:
        manifest = json.loads((cwd / "run_manifest.json").read_text(encoding="utf-8"))
        run_folder = manifest.get("run_folder", "")
    except (OSError, json.JSONDecodeError):
        pass
    run_map = _barcode_map(cfg).get(run_folder, {})

    rows = []
    summary = {"pass": 0, "review": 0, "fail": 0, "total": 0}
    for line in tsv.read_text(encoding="utf-8").splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 6:
            continue
        bc, _sample, n_reads = f[0], f[1], f[2]
        a1, c1 = _parse_allele(f[3])
        a2, c2 = _parse_allele(f[4])
        zyg = f[5]
        qc = _drb3_qc(c1, c2, zyg)
        summary[qc] = summary.get(qc, 0) + 1
        summary["total"] += 1
        rows.append({
            "barcode": bc,
            "animal": run_map.get(bc, {}).get("animal", "") or _sample,
            "tissue": run_map.get(bc, {}).get("tissue", ""),
            "allele1": a1, "count1": c1,
            "allele2": a2, "count2": c2,
            "zygosity": zyg,
            "reads": int(n_reads) if n_reads.isdigit() else 0,
            "qc": qc,
        })
    rows.sort(key=lambda r: (r["barcode"]))
    return JSONResponse({"rows": rows, "summary": summary})


@app.get("/api/jobs/{job_id}/classI")
def api_job_classI(job_id: str):
    """Per-animal Class I calls (reconciled) + tier summary. PROVISIONAL — the short
    amplicons don't reproduce, so these are leads, not genotypes; 'confident' = exact
    100% IPD match, and the amber gate stays in the UI."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    cwd = Path(job.get("cwd", ""))
    pa = cwd / "per_animal_reconciled.tsv"
    if not pa.is_file():
        return JSONResponse({"per_animal": [], "tiers": {}, "amplicons": []})

    per_animal = []
    for line in pa.read_text(encoding="utf-8").splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 5:
            continue
        sample, alleles, n_conf, hap, drb3 = f[0], f[1], f[2], f[3], f[4]
        per_animal.append({
            "sample": sample,
            "alleles": [a for a in alleles.split(";") if a and a != "-"],
            "n_conf": int(n_conf) if n_conf.isdigit() else 0,
            "haplotype": hap,
            "drb3": drb3,
        })
    per_animal.sort(key=lambda r: r["sample"])

    # tier summary + which amplicons were typed, from classI_<amp>_typed.tsv
    tiers: Dict[str, int] = {}
    amplicons = []
    for tsv in sorted(cwd.glob("classI_*_typed.tsv")):
        m = re.match(r"classI_(\w+)_typed\.tsv$", tsv.name)
        if m:
            amplicons.append(m.group(1))
        for line in tsv.read_text(encoding="utf-8").splitlines()[1:]:
            f = line.split("\t")
            if len(f) > 7 and f[7]:
                tiers[f[7]] = tiers.get(f[7], 0) + 1
    n_called = sum(1 for r in per_animal if r["n_conf"] > 0)
    return JSONResponse({"per_animal": per_animal, "tiers": tiers,
                         "amplicons": amplicons, "n_called": n_called,
                         "provisional": True})


@app.get("/api/projects/{name}/runs/{run}/last-job")
def api_run_last_job(name: str, run: str):
    """Latest typing job for a linked run's output dir, so the UI can re-load a
    finished run's results (Genotypes / Class I / downloads) on selection — no
    re-run. Scans persisted job state, so it survives session restarts (where the
    in-memory job list is empty)."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    if "/" in run or run in ("", ".", ".."):
        raise HTTPException(400, "Bad run name")
    try:
        target = (project_dir / "mhc" / run).resolve()
    except OSError:
        return JSONResponse({"job_id": None})
    best = None
    for sf in job_manager.jobs_dir.glob("*.json"):
        try:
            j = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        c = j.get("cwd")
        if not c:
            continue
        try:
            if Path(c).resolve() != target:
                continue
        except OSError:
            continue
        if best is None or (j.get("started_at", "") > best.get("started_at", "")):
            best = j
    if best is None:
        return JSONResponse({"job_id": None})
    return JSONResponse({"job_id": best.get("id"), "status": best.get("status")})


@app.get("/api/jobs")
def api_list_jobs():
    return JSONResponse(job_manager.list_jobs())


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str, request: Request):
    """SSE stream of the job's log file. Tails from beginning, closes when job finishes."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    log_path = Path(job["log_path"])
    _ansi_re = re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJsur]')

    async def event_stream():
        position = 0
        while True:
            if await request.is_disconnected():
                break
            current_job = job_manager.get_job(job_id)
            if log_path.exists():
                async with aiofiles.open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    await f.seek(position)
                    chunk = await f.read(4096)
                    if chunk:
                        lines = chunk.splitlines(keepends=True)
                        for line in lines:
                            clean = _ansi_re.sub("", line.rstrip())
                            if clean:
                                yield f"data: {clean}\n\n"
                        position += len(chunk.encode("utf-8"))
            if current_job and current_job["status"] in ("succeeded", "failed"):
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# File extensions a browser can render in a tab (open inline); everything else
# is sent as a download. Maps extension -> MIME type.
_INLINE_MEDIA = {
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".tsv": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".csv": "text/plain",
}
_DOWNLOAD_MEDIA = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".vcf": "text/plain",
    ".fasta": "text/plain",
    ".fa": "text/plain",
    ".fna": "text/plain",
    ".gz": "application/gzip",
}


def _can_open_inline(name: str) -> bool:
    return Path(name).suffix.lower() in _INLINE_MEDIA


def _media_type_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _INLINE_MEDIA.get(ext) or _DOWNLOAD_MEDIA.get(ext) or "application/octet-stream"


def _result_category(rel: str) -> Optional[str]:
    """Return the primary-results category for a relative run output path.

    The run dir keeps intermediates for audit; the GUI surfaces the small set
    users normally open or download.
    """
    path = Path(rel)
    name = path.name
    parts = path.parts

    if any(part.startswith(".") for part in parts):
        return None
    if name.endswith(".fastq.gz") or name.endswith(".fa") or name.endswith(".fq.gz"):
        return None

    if name == "per_animal_report.html":
        return "report_html"
    if name == "report.pdf":
        return "report_pdf"
    if name == "per_animal_report.txt":
        return "report_txt"
    if name == "drb3_typed.tsv":
        return "drb3_typed"
    if name in ("per_animal_reconciled.tsv", "reconciled_alleles.tsv"):
        return "reconciled"
    if name == "per_animal_haplotypes.tsv":
        return "haplotypes"
    if name in ("classI_typed.tsv", "bosex_typed.tsv", "utr_typed.tsv") or name.endswith("_typed.tsv"):
        return "classI_typed"
    if name.endswith("_stats.xlsx"):
        return "stats_xlsx"
    if name == "run_manifest.json":
        return "run_manifest"
    if name == "pipeline.log":
        return "log"
    return None


_CATEGORY_ORDER = {
    "report_html": 0,
    "report_pdf": 1,
    "report_txt": 2,
    "drb3_typed": 3,
    "reconciled": 4,
    "haplotypes": 5,
    "classI_typed": 6,
    "stats_xlsx": 7,
    "run_manifest": 11,
    "log": 99,
}


def _result_label(rel: str, category: Optional[str]) -> str:
    return {
        "report_html": "Per-animal report (HTML)",
        "report_pdf": "Per-animal report (PDF)",
        "report_txt": "Per-animal report (text)",
        "drb3_typed": "DRB3 (Class II) genotypes (TSV)",
        "reconciled": "Reconciled alleles (TSV)",
        "haplotypes": "MHC-I haplotypes (TSV)",
        "classI_typed": "Class I per-amplicon calls — PROVISIONAL (TSV)",
        "stats_xlsx": "Statistics workbook (Excel)",
        "run_manifest": "Run manifest / provenance (JSON)",
        "log": "Pipeline log",
    }.get(category, rel)


@app.get("/api/jobs/{job_id}/logtext")
def api_job_logtext(job_id: str):
    """Plain (non-streaming) log + status — POLLED by the UI instead of SSE.
    OOD's /rnode Apache reverse proxy buffers/garbles SSE, so a single proxy-safe
    GET returning both status and the log file is the reliable pattern here."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    text = ""
    lp = job.get("log_path")
    try:
        if lp and Path(lp).is_file():
            text = Path(lp).read_text(encoding="utf-8", errors="replace")
            if len(text) > 40000:
                text = "...(earlier log truncated)...\n" + text[-40000:]
    except OSError:
        pass
    return JSONResponse({
        "status": job.get("status"),
        "exit_code": job.get("exit_code"),
        "log": text,
    })


@app.get("/api/jobs/{job_id}/results")
def api_job_results(job_id: str, all: int = Query(0)):
    """List output files in the job's run directory, plus the pipeline log."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    files = []
    cwd = job.get("cwd")
    if cwd and Path(cwd).is_dir():
        run_dir = Path(cwd)
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and not p.name.endswith(".log"):
                rel = str(p.relative_to(run_dir))
                category = _result_category(rel)
                if not all and category is None:
                    continue
                files.append({
                    "name": rel,
                    "label": _result_label(rel, category),
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                    "openable": _can_open_inline(rel),
                    "category": category,
                })

    log_path = Path(job.get("log_path", ""))
    if log_path.is_file():
        files.append({
            "name": "pipeline_log.txt",
            "label": "Pipeline log",
            "size": log_path.stat().st_size,
            "mtime": log_path.stat().st_mtime,
            "openable": True,
            "category": "log",
            "is_log": True,
        })

    def sort_key(f):
        if f.get("is_log"):
            return (_CATEGORY_ORDER["log"], f["name"])
        category = f.get("category")
        if category in _CATEGORY_ORDER:
            return (_CATEGORY_ORDER[category], f["name"])
        return (50, f["name"])

    files.sort(key=sort_key)
    for file in files:
        file.pop("mtime", None)
        if all and file.get("category") is None:
            file["label"] = file["name"]
    return JSONResponse(files)


@app.get("/api/jobs/{job_id}/file")
def api_job_file(job_id: str, path: str = Query(...), inline: int = 0):
    """Serve a single result file. `inline=1` renders in the browser."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    if path == "pipeline_log.txt":
        target = Path(job.get("log_path", ""))
        display_name = f"{job_id[:8]}_pipeline_log.txt"
    else:
        cwd = job.get("cwd")
        if not cwd:
            raise HTTPException(404, "No run directory for job")
        run_dir = Path(cwd).resolve()
        target = (run_dir / path).resolve()
        if run_dir != target and run_dir not in target.parents:
            raise HTTPException(403, "Path outside run directory")
        display_name = target.name

    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")

    media_type = _media_type_for(target.name)
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    headers = {"Content-Disposition": f'{disposition}; filename="{display_name}"'}
    return FileResponse(target, media_type=media_type, headers=headers)


# ---------------------------------------------------------------------------
# Static frontend — must be last (catches everything not matched above)
# ---------------------------------------------------------------------------
if _FRONTEND_DIST.is_dir():
    _INDEX_HTML = _FRONTEND_DIST / "index.html"

    @app.get("/")
    def index():
        return FileResponse(
            _INDEX_HTML,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
else:
    @app.get("/")
    def root():
        return JSONResponse(
            {"error": "Frontend not built. Run: cd frontend && npm run build"},
            status_code=503,
        )
