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
import shlex
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mhc_config as C

q = shlex.quote  # quote every path — run/output folders may contain spaces or [brackets]


def sh(c):
    return subprocess.run(c, shell=True, env=C.tool_env(), text=True, capture_output=True)


def main():
    if len(sys.argv) < 5:
        sys.exit("usage: drb3_type.py <barcode> <sample> <reads_source> <outdir>")
    bc, sample, reads_source, outdir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    wd = os.path.join(outdir, bc)
    os.makedirs(wd, exist_ok=True)
    # reads_source is either a barcode DIRECTORY (linked ONT run: cat its chunks)
    # or a single FASTQ FILE (uploaded per-sample reads).
    if os.path.isdir(reads_source):
        sh(f"cat {q(reads_source)}/*.fastq.gz > {q(wd)}/m.fq.gz 2>/dev/null")
    elif os.path.isfile(reads_source):
        sh(f"cat {q(reads_source)} > {q(wd)}/m.fq.gz 2>/dev/null")
    else:
        print(f"{bc}\t{sample}\t0\t\t\tMISSING")
        return
    sh(f"nanoq -i {q(wd)}/m.fq.gz -q 10 -l 400 --max-len 850 -o {q(wd)}/f.fq.gz 2>/dev/null")
    sh(f"zcat {q(wd)}/f.fq.gz 2>/dev/null | head -40000 | "
       f"awk 'NR%4==1{{print \">\"substr($1,2)}} NR%4==2{{print}}' > {q(wd)}/reads.fa")
    reads_fa = os.path.join(wd, "reads.fa")
    n = sum(1 for l in open(reads_fa) if l.startswith(">")) if os.path.exists(reads_fa) else 0
    if n == 0:
        print(f"{bc}\t{sample}\t0\t\t\tNO_READS")
        return
    db = C.REFS / "blast_db" / "BoLA_nuc"
    sh(f"{q(C.BLASTN)} -db {q(str(db))} -query {q(wd)}/reads.fa "
       f"-outfmt '6 qseqid pident qcovs stitle' -max_target_seqs 1 -word_size 7 "
       f"-num_threads 4 2>/dev/null | sort -k1,1 -k2,2nr | awk '!s[$1]++' > {q(wd)}/hits.tsv")
    cnt = collections.Counter()
    for line in open(os.path.join(wd, "hits.tsv")):
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
