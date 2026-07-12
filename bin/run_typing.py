#!/usr/bin/env python3
"""MHC Typer pipeline entry point — the command the FastAPI backend launches.

Reads a manifest of samples, each carrying its own amplicon, and routes each
barcode to the pipeline for its amplicon (auto-run): DRB3 barcodes -> DRB3
typing, Class-I barcodes -> the Class-I pipeline. Writes per-amplicon outputs +
a provenance manifest into <outdir>. Unbuffered output for the polling log.

  run_typing.py --manifest <tsv> --outdir <dir> [--run-folder <name>]

Manifest is a headerless TSV: barcode <tab> sample <tab> reads_source <tab>
amplicon. reads_source is a barcode DIRECTORY (linked run) or a FASTQ FILE
(uploaded). Building it in the backend keeps every path off the command line.
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
CLASS_I = ("bov711", "bosex", "utr5")


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
        rows.append((f[0], f[1], f[2], f[3] if len(f) > 3 else "drb3"))
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
        futs = [ex.submit(one, bc, s, reads) for bc, s, reads, _amp in entries]
        for fut in concurrent.futures.as_completed(futs):
            row = fut.result()
            rows.append(row)
            print(f"  done {_label(row)}", flush=True)
    rows.sort()
    out.write_text(DRB3_HEADER + "\n" + "\n".join(rows) + "\n")
    called = sum(1 for r in rows if r.split("\t")[-1] in ("het", "hom"))
    print(f"[drb3] wrote {out.name} — {called}/{len(rows)} samples genotyped", flush=True)
    return str(out)


def run_classI(amp, entries, outdir, threads):
    # Placeholder until #3 wires classI_typed_pipeline -> reconcile -> haplotype.
    print(f"[classI:{amp}] {len(entries)} samples — Class I typing is not yet wired "
          f"(provisional). Skipped for now.", flush=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="TSV: barcode, sample, reads_source, amplicon")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--run-folder", default="", help="source run folder name (for sheet lookups)")
    ap.add_argument("--threads", type=int, default=int(os.environ.get("MHC_THREADS", "12")))
    a = ap.parse_args()

    entries = read_manifest(a.manifest)
    if not entries:
        sys.exit("manifest is empty")
    os.makedirs(a.outdir, exist_ok=True)

    by_amp = {}
    for e in entries:
        by_amp.setdefault(e[3], []).append(e)

    print(f"=== MHC Typer: {len(entries)} samples · amplicons {'+'.join(sorted(by_amp))} ===", flush=True)
    print(f"    refs = {C.REFS}  ont_bin = {C.ONT_BIN}", flush=True)

    manifest = {
        "run_folder": a.run_folder,
        "n_samples": len(entries),
        "amplicons": sorted(by_amp),
        "refs": str(C.REFS),
        "medaka_model": C.MEDAKA_MODEL,
        "outputs": {},
    }
    for amp in sorted(by_amp):
        group = by_amp[amp]
        print(f"--- amplicon {amp}: {len(group)} samples ---", flush=True)
        if amp == "drb3":
            manifest["outputs"]["drb3_typed"] = run_drb3(group, a.outdir, a.threads)
        elif amp in CLASS_I:
            manifest["outputs"][f"classI_{amp}"] = run_classI(amp, group, a.outdir, a.threads)
        else:
            print(f"[skip] unknown amplicon '{amp}' ({len(group)} samples)", flush=True)

    (Path(a.outdir) / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
    print("=== done ===", flush=True)


if __name__ == "__main__":
    main()
