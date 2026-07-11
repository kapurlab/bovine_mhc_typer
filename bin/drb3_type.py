#!/usr/bin/env python3
"""DRB3 (Class II) typing for ONE barcode.

cat run's *.fastq.gz -> nanoq length/Q filter -> BLAST vs BoLA_nuc ->
top-2 distinct DRB3 allele groups -> zygosity. Prints one TSV row:

    barcode  sample  n_reads  allele1:count  allele2:count  zygosity

Usage: drb3_type.py <barcode> <sample> <run_dir> <outdir>
  run_dir = a folder containing per-barcode subdirs (barcodeNN/) of *.fastq.gz

Algorithm is byte-for-byte the validated research pipeline; only the run folder,
refs, and env were lifted out of hard-coded constants into mhc_config.
"""
import sys
import os
import subprocess
import collections
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mhc_config as C


def sh(c):
    return subprocess.run(c, shell=True, env=C.tool_env(), text=True, capture_output=True)


def main():
    if len(sys.argv) < 5:
        sys.exit("usage: drb3_type.py <barcode> <sample> <run_dir> <outdir>")
    bc, sample, run_dir, outdir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    raw = os.path.join(run_dir, bc)
    wd = os.path.join(outdir, bc)
    os.makedirs(wd, exist_ok=True)
    if not os.path.isdir(raw):
        print(f"{bc}\t{sample}\t0\t\t\tMISSING")
        return
    sh(f"cat {raw}/*.fastq.gz > {wd}/m.fq.gz 2>/dev/null")
    sh(f"nanoq -i {wd}/m.fq.gz -q 10 -l 400 --max-len 850 -o {wd}/f.fq.gz 2>/dev/null")
    sh(f"zcat {wd}/f.fq.gz 2>/dev/null | head -40000 | "
       f"awk 'NR%4==1{{print \">\"substr($1,2)}} NR%4==2{{print}}' > {wd}/reads.fa")
    n = sum(1 for l in open(f"{wd}/reads.fa") if l.startswith(">")) if os.path.exists(f"{wd}/reads.fa") else 0
    if n == 0:
        print(f"{bc}\t{sample}\t0\t\t\tNO_READS")
        return
    db = C.REFS / "blast_db" / "BoLA_nuc"
    sh(f"{C.BLASTN} -db {db} -query {wd}/reads.fa "
       f"-outfmt '6 qseqid pident qcovs stitle' -max_target_seqs 1 -word_size 7 "
       f"-num_threads 4 2>/dev/null | sort -k1,1 -k2,2nr | awk '!s[$1]++' > {wd}/hits.tsv")
    cnt = collections.Counter()
    for line in open(f"{wd}/hits.tsv"):
        f = line.rstrip().split("\t")
        if len(f) < 4:
            continue
        if "DRB3" not in f[3] or float(f[1]) < 90:
            continue
        cnt[f[3].split()[0]] += 1

    def grp(a):
        m = re.match(r"(BoLA-DRB3\*\d+)", a)
        return m.group(1) if m else a

    top = cnt.most_common()
    a1 = top[0] if top else ("", 0)
    a2 = ("", 0)
    for a, c in top[1:]:
        if grp(a) != grp(a1[0]):
            a2 = (a, c)
            break
    zyg = "het" if a2[1] >= 0.2 * max(a1[1], 1) and a2[1] > 0 else ("hom" if a1[1] else "none")
    print(f"{bc}\t{sample}\t{n}\t{a1[0]}:{a1[1]}\t{a2[0]}:{a2[1]}\t{zyg}")


if __name__ == "__main__":
    main()
