#!/usr/bin/env python3
"""MHC Typer pipeline entry point — the command the FastAPI backend launches.

Types the samples listed in a manifest for one amplicon and writes the
per-amplicon TSV + a provenance manifest into <outdir>. Prints unbuffered
progress so the GUI's polling log shows live status.

  run_typing.py --manifest <tsv> --outdir <dir> --amplicon drb3

The manifest is a headerless TSV of  barcode <tab> sample <tab> reads_source,
where reads_source is a barcode DIRECTORY (linked ONT run) or a single FASTQ
FILE (uploaded reads). Building it in the backend keeps every path off the
command line (run/output folders may contain spaces or [brackets]).

v1 wires the reliable DRB3 (Class II) path; Class I plugs in here.
"""
import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mhc_config as C

HERE = Path(__file__).resolve().parent
DRB3_HEADER = "barcode\tsample\tn_reads\tallele1:count\tallele2:count\tzygosity"


def _label(row: str) -> str:
    p = row.split("\t")
    return f"{p[0]}  {p[3] if len(p) > 3 else '?'}/{p[4] if len(p) > 4 else '?'}"


def read_manifest(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        f = line.split("\t")
        if len(f) < 3:
            continue
        rows.append((f[0], f[1], f[2]))  # barcode, sample, reads_source
    return rows


def run_drb3(entries, outdir, threads):
    drb3_run = Path(outdir) / "drb3_run"
    drb3_run.mkdir(parents=True, exist_ok=True)
    out = Path(outdir) / "drb3_typed.tsv"

    def one(bc, sample, reads):
        r = subprocess.run(
            [sys.executable, str(HERE / "drb3_type.py"), bc, sample, reads, str(drb3_run)],
            text=True, capture_output=True,
        )
        line = (r.stdout or "").strip().splitlines()
        return line[0] if line else f"{bc}\t{sample}\t0\t\t\tERROR"

    print(f"[drb3] typing {len(entries)} samples (threads={threads})", flush=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(one, bc, s, reads) for bc, s, reads in entries]
        for fut in concurrent.futures.as_completed(futs):
            row = fut.result()
            rows.append(row)
            print(f"  done {_label(row)}", flush=True)
    rows.sort()
    out.write_text(DRB3_HEADER + "\n" + "\n".join(rows) + "\n")
    called = sum(1 for r in rows if r.split("\t")[-1] in ("het", "hom"))
    print(f"[drb3] wrote {out.name} — {called}/{len(rows)} samples genotyped", flush=True)
    return str(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="TSV: barcode, sample, reads_source")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--amplicon", default="drb3", choices=["drb3"])
    ap.add_argument("--run-folder", default="", help="source run folder name (for sheet lookups)")
    ap.add_argument("--threads", type=int, default=int(os.environ.get("MHC_THREADS", "12")))
    a = ap.parse_args()

    entries = read_manifest(a.manifest)
    if not entries:
        sys.exit("manifest is empty")
    os.makedirs(a.outdir, exist_ok=True)

    print(f"=== MHC Typer: {a.amplicon} · {len(entries)} samples ===", flush=True)
    print(f"    refs    = {C.REFS}", flush=True)
    print(f"    ont_bin = {C.ONT_BIN}", flush=True)

    manifest = {
        "amplicon": a.amplicon,
        "run_folder": a.run_folder,
        "n_samples": len(entries),
        "refs": str(C.REFS),
        "medaka_model": C.MEDAKA_MODEL,
        "outputs": {},
    }
    if a.amplicon == "drb3":
        manifest["outputs"]["drb3_typed"] = run_drb3(entries, a.outdir, a.threads)

    (Path(a.outdir) / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
    print("=== done ===", flush=True)


if __name__ == "__main__":
    main()
