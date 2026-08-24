import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "mhc_gui"
    return Path.home() / ".config" / "mhc_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

# The multi-user "shared projects" root, if this deployment has one. A laptop
# normally does not — only a lab server or an OOD site. Never assume a path:
# probe in order of authority and fall back to None (no shared root) rather than
# a fictional one, so macOS/WSL users aren't shown a directory that cannot exist.
#   1. BDTOOLS_SHARED_PROJECTS_ROOT — exported by the launcher, which resolved it
#      from the machine's recorded site config. An explicitly empty value is
#      authoritative: it DISABLES the shared root.
#   2. the user's own `shared_projects_root` setting — see shared_projects_root()
# There is no step 3. A site supplies its own value (bdtools records it in
# <BDTOOLS_HOME>/site.conf); this file contains no path of its own, so the same
# release is correct on macOS, WSL, Linux and OOD without editing.
#
# This used to be one lab server's projects path, guarded by is_dir(). The guard
# kept the value out of config.json off that server, but the literal still
# decided what "shared" MEANT: any other site with its own shared root got no
# shared projects at all, silently, because the only path this file would accept
# was one it could never have.
_ENV_SHARED_PROJECTS_ROOT = "BDTOOLS_SHARED_PROJECTS_ROOT"


def _default_shared_projects_root() -> str:
    env = os.environ.get(_ENV_SHARED_PROJECTS_ROOT)
    return env.strip() if env is not None else ""


_DEFAULT_SHARED_PROJECTS_ROOT = _default_shared_projects_root()


def shared_projects_root() -> Optional[Path]:
    """The resolved shared-projects root, or None when this deployment has none.

    Read through this rather than a module constant, so the Settings value is
    honoured: main.py used to carry its own hard-coded literal, which meant
    setting `shared_projects_root` in the GUI changed what Settings displayed and
    nothing about where projects were discovered.

    Returns None — never Path("") — because Path("") is Path("."), the current
    working directory. An "unset" sentinel that silently means "look in ." would
    turn a missing shared root into project lookups against wherever uvicorn
    happens to have been started."""
    env = os.environ.get(_ENV_SHARED_PROJECTS_ROOT)
    if env is not None:
        return Path(env.strip()) if env.strip() else None
    try:
        configured = str(load_config().get("shared_projects_root", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return Path(configured)
    return Path(_DEFAULT_SHARED_PROJECTS_ROOT) if _DEFAULT_SHARED_PROJECTS_ROOT else None


def _first_existing(*paths: str) -> str:
    """Return the first path that exists, else "".

    Several candidates below are legacy paths under another user's home. Path.exists()
    RAISES PermissionError — rather than returning False — when an ancestor directory
    is unreadable, and these calls run at import time. So on a shared server every
    account except the original owner crashed on `import app.main`, and the tool
    exited the instant the dashboard launched it. A path this account cannot stat
    does not exist for our purposes; treat it as absent."""
    for p in paths:
        if not p:
            continue
        try:
            if Path(p).exists():
                return p
        except OSError:
            continue
    # Nothing exists: return EMPTY, never the first candidate. Returning it
    # "informatively" is how a path from one machine reached config.json on
    # every other one, and once written it is permanent — load_config()
    # setdefaults keys that are MISSING, so no later release takes it back out.
    # Empty is what the GUI already renders as "not set", which is both true and
    # something the user can act on.
    return ""


def _db_root() -> Path:
    """Where reference databases live on THIS machine.

    `bdtools setup-databases` asks once and records the answer in
    <BDTOOLS_HOME>/db-root — home, shared, or an arbitrary directory — and the
    launcher exports it. Read that rather than assuming a site layout, so the
    same code is right on a laptop, a WSL box, the lab server, and another
    institution's cluster."""
    env = os.environ.get("BDTOOLS_DB_ROOT", "").strip()
    if env:
        return Path(env)
    home = os.environ.get("BDTOOLS_HOME", "").strip()
    if not home:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        home = str(Path(xdg) / "bdtools") if xdg else str(Path.home() / ".local/share/bdtools")
    try:
        recorded = (Path(home) / "db-root").read_text(encoding="utf-8").strip()
        if recorded:
            return Path(recorded)
    except OSError:
        pass
    return Path.home() / "databases"


_DB_ROOT = _db_root()


def _tools_root() -> Path:
    """The directory holding the tool checkouts, so this tool is <root>/mhc_gui.
    Exported by the launcher; falls back to the per-user default it also uses."""
    env = os.environ.get("BDTOOLS_TOOLS_ROOT", "").strip()
    if env:
        return Path(env)
    home = os.environ.get("BDTOOLS_HOME", "").strip()
    if not home:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        home = str(Path(xdg) / "bdtools") if xdg else str(Path.home() / ".local/share/bdtools")
    return Path(home) / "checkouts"


# BoLA reference bundle (IPD-MHC BLAST DBs, ARS-UCD2.0 chr23 MHC contig,
# haplotypes.json). A shared install under this machine's database root first,
# then the refs bundled in this repo (refs/, the self-contained default that is
# always present).
#
# The shared candidate used to be a /srv path and the last one was a directory
# under the original developer's home. Neither is a defensible fallback: the
# first is one site's layout, and the second names an account that usually does
# not exist and whose home is unreadable when it does. The bundled refs are what
# actually answered on every machine but one; the resolved database root is what
# answers on a site that installed its own copy.
_REPO_REFS = Path(__file__).resolve().parents[2] / "refs"
_BOLA_REFS_DEFAULT = _first_existing(
    str(_DB_ROOT / "bola"),
    str(_REPO_REFS),
)

# Conda env bin dir. The whole pipeline toolchain (minimap2, samtools, bcftools,
# nanoq, vsearch, spoa, medaka, blastn) lives in the tool's OWN dedicated env.
# The backend runs under that env's python, so its bin dir is the correct default
# on every platform (macOS/Windows-WSL2/Linux/OOD).
#
# The two legacy candidates that followed it were conda envs under the original
# developer's home, and the shared one was a /srv literal. _ENV_BIN has been the
# right answer everywhere since this tool got its own env; the shared-site
# layout is kept, resolved from the tools root the launcher exports rather than
# spelled out.
_ENV_BIN = str(Path(sys.executable).resolve().parent)
_TOOL_ENV_BIN = str(_tools_root() / "mhc_gui" / "env" / "bin")
_ONT_ENV_BIN_DEFAULT = _first_existing(_ENV_BIN, _TOOL_ENV_BIN)
_PHASE_ENV_BIN_DEFAULT = _first_existing(_ENV_BIN, _TOOL_ENV_BIN)

DEFAULTS: Dict[str, Any] = {
    "projects_root": str(Path.home() / "projects"),
    "shared_projects_root": _DEFAULT_SHARED_PROJECTS_ROOT,
    # Where run folders of barcoded ONT FASTQ live (rclone-synced). Users can
    # also point the app at a project's own download/ dir.
    # Under this machine's database root when a site installed one there; empty
    # otherwise, which the GUI shows as "not set". It used to fall back to one
    # site's /srv path and then to a directory under the original developer's
    # home, so on every other machine the app opened Settings already pointing
    # at somewhere that could not be read.
    "runs_root": _first_existing(str(_DB_ROOT / "mhc" / "runs")),
    # OneDrive import (rclone). Inbox runs are copied into runs_root, then moved
    # to the archive. rclone_config lets the app use a shared config for all users.
    "onedrive_remote": "rxk104_mhc:",
    "onedrive_inbox": "For_WGS3_Upload",
    "onedrive_archive": "Uploaded_Archive",
    # rclone's own per-user location, which is where rclone itself looks. The
    # previous default named one person's copy of it.
    "rclone_config": _first_existing(str(Path.home() / ".config" / "rclone" / "rclone.conf")),
    # barcode -> animal/sample map (run_date, run_folder, amplicon, barcode,
    # sample_id, lab_id, tissue, ...). Lets the GUI show animal IDs, not barcodes.
    "barcode_map": _first_existing(str(_DB_ROOT / "mhc" / "barcode_sample_map.tsv")),
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
