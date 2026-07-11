#!/usr/bin/env python3
"""MHC Typer pipeline entry point — the command the FastAPI backend launches.

Types a set of barcodes from one run folder for one amplicon and writes the
per-amplicon TSV + a provenance manifest into <outdir>. Prints unbuffered
progress so the GUI's polling log shows live status.

Usage:
  run_typing.py --run-dir <dir> --outdir <dir> --amplicon drb3 \
      --barcodes barcode01:animal1 barcode02:animal2 ...

v1 wires the reliable DRB3 (Class II) path; Class I (provisional) plugs in here.
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


def _pair_label(row: str) -> str:
    parts = row.split("\t")
    return f"{parts[0]}  {parts[3] if len(parts) > 3 else '?'}/{parts[4] if len(parts) > 4 else '?'}"


def run_drb3(run_dir, outdir, pairs, threads):
    drb3_run = Path(outdir) / "drb3_run"
    drb3_run.mkdir(parents=True, exist_ok=True)
    out = Path(outdir) / "drb3_typed.tsv"

    def one(bc, sample):
        r = subprocess.run(
            [sys.executable, str(HERE / "drb3_type.py"), bc, sample, run_dir, str(drb3_run)],
            text=True, capture_output=True,
        )
        line = (r.stdout or "").strip().splitlines()
        if line:
            return line[0]
        return f"{bc}\t{sample}\t0\t\t\tERROR"

    print(f"[drb3] typing {len(pairs)} barcodes (threads={threads})", flush=True)
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(one, bc, s): bc for bc, s in pairs}
        for fut in concurrent.futures.as_completed(futs):
            row = fut.result()
            rows.append(row)
            print(f"  done {_pair_label(row)}", flush=True)
    rows.sort()
    out.write_text(DRB3_HEADER + "\n" + "\n".join(rows) + "\n")
    called = sum(1 for r in rows if r.split("\t")[-1] in ("het", "hom"))
    print(f"[drb3] wrote {out.name} — {called}/{len(rows)} animals genotyped", flush=True)
    return str(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, help="folder with per-barcode subdirs of *.fastq.gz")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--amplicon", default="drb3", choices=["drb3"])
    ap.add_argument("--barcodes", nargs="+", required=True, help="barcodeNN:sample_id (sample optional)")
    ap.add_argument("--threads", type=int, default=int(os.environ.get("MHC_THREADS", "12")))
    a = ap.parse_args()

    if not Path(a.run_dir).is_dir():
        sys.exit(f"run-dir not found: {a.run_dir}")
    os.makedirs(a.outdir, exist_ok=True)

    pairs = []
    for b in a.barcodes:
        bc, _, s = b.partition(":")
        pairs.append((bc, s or bc))

    print(f"=== MHC Typer: {a.amplicon} · {len(pairs)} barcodes ===", flush=True)
    print(f"    run_dir = {a.run_dir}", flush=True)
    print(f"    refs    = {C.REFS}", flush=True)
    print(f"    ont_bin = {C.ONT_BIN}", flush=True)

    manifest = {
        "amplicon": a.amplicon,
        "run_dir": a.run_dir,
        "n_barcodes": len(pairs),
        "refs": str(C.REFS),
        "medaka_model": C.MEDAKA_MODEL,
        "outputs": {},
    }
    if a.amplicon == "drb3":
        manifest["outputs"]["drb3_typed"] = run_drb3(a.run_dir, a.outdir, pairs, a.threads)

    (Path(a.outdir) / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
    print("=== done ===", flush=True)


if __name__ == "__main__":
    main()
