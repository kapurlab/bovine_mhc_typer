#!/usr/bin/env python3
"""Class-I (Bov7/11, BosEx, 5'UTR) typing for ONE barcode + amplicon.

Ports the validated research pipeline (BoLA_MHC/scripts/classI_typed_pipeline.py):

  stage reads -> nanoq length/Q filter (per amplicon) -> minimap2 on-target ->
  vsearch cluster (separates the gene copies) -> spoa + medaka backbone ->
  diploid split (bcftools ploidy-2: allele-1 = majority, allele-2 = het-ALT) ->
  BLAST IPD nuc + gen -> confidence tier.

Prints TSV rows (one per cluster x allele) to stdout, columns:

  barcode  sample  ontarget_reads  qc  tag  hap  reads  status  allele  pident  note

Usage: classI_type.py <barcode> <sample> <reads_source> <amplicon> <outdir>
  reads_source = a barcode DIRECTORY (linked run: cat its chunks) or a FASTQ FILE (upload)
  amplicon     = bov711 | bosex | utr5

Only the hard-coded paths/refs/env and the per-amplicon filter+cluster params were
lifted out of the research script into mhc_config + the AMPLICON table below; the
typing logic is unchanged. Two additive safety knobs vs the research script:
a per-barcode read-depth GATE (skip clustering+medaka on undersampled barcodes,
cheaply flagging them SHALLOW) and a read CAP (deep barcodes choke medaka).

Class I calls are PROVISIONAL — leads, not genotypes (HANDOVER §8): the two short
amplicons do not reproduce; 5'UTR is depth-limited. Keep the amber gate in the UI.
"""
import sys
import os
import re
import glob
import shutil
import shlex
import subprocess
from pathlib import Path

import pysam

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mhc_config as C

q = shlex.quote  # quote every path — run/output folders may contain spaces or [brackets]

# --- per-amplicon parameters, recovered from the validated research runs ---
# (bov711 filtered range 400-900; bosex 450-950/q10; utr5 2500-6000/q8, cluster 0.85)
AMPLICON = {
    "bov711": dict(minlen=400,  maxlen=900,  minq=10, cluster_id=0.88, min_reads=200),
    "bosex":  dict(minlen=450,  maxlen=950,  minq=10, cluster_id=0.88, min_reads=200),
    "utr5":   dict(minlen=2500, maxlen=6000, minq=8,  cluster_id=0.85, min_reads=30),
}

TOP_CLUSTERS = 8      # process at most this many clusters per barcode (by size)
POLISH_READS = 250    # reads fed to spoa/medaka per cluster
MIN_DP = 50           # bcftools depth floor for the diploid split
LOW_DEPTH = 5000      # on-target reads below this -> qc="LOW_DEPTH" (review)

COLS = ["barcode", "sample", "ontarget_reads", "qc", "tag", "hap",
        "reads", "status", "allele", "pident", "note"]

CLASSICAL = {"BOLA-1", "BOLA-2", "BOLA-3", "BOLA-4", "BOLA-6"}
NCTAG = re.compile(r"NC\d|JSP|LOC")

MHCREF = str(C.MHCREF)
NUC = str(C.BLAST_NUC)
GEN = str(C.BLAST_GEN)
MODEL = C.MEDAKA_MODEL
MED_T = C.CLASSI_MEDAKA_THREADS
MAP_T = 8


def sh(c):
    return subprocess.run(c, shell=True, env=C.tool_env(), text=True, capture_output=True)


def locus_class(st):
    g = st.split("*")[0].upper() if "*" in st else st.upper()
    if g in CLASSICAL:
        return "classical_I"
    if NCTAG.search(st.upper()):
        return "non_classical"
    if "DRB" in g or "DQ" in g:
        return "class_II"
    return "other"


def gene_of(al):
    return al.split("*")[0].upper()


def nreads(fa):
    try:
        return sum(1 for l in open(fa) if l.startswith(">"))
    except OSError:
        return 0


def backbone(reads_fa, wd, tag):
    """spoa draft -> medaka consensus of the cluster's top reads. Returns a fasta."""
    sub = f"{wd}/{tag}.sub.fa"
    sh(f"head -n {POLISH_READS * 2} {q(reads_fa)} > {q(sub)}")
    d = f"{wd}/{tag}.spoa.fa"
    sh(f"spoa {q(sub)} 2>/dev/null | sed '1s/.*/>c/' > {q(d)}")
    if not (os.path.exists(d) and os.path.getsize(d)):
        return None
    m = f"{wd}/{tag}_med"
    shutil.rmtree(m, ignore_errors=True)
    log = f"{wd}/{tag}.med.log"
    sh(f"medaka_consensus -i {q(sub)} -d {q(d)} -o {q(m)} -m {q(MODEL)} -t {MED_T} -f > {q(log)} 2>&1")
    c = f"{m}/consensus.fasta"
    return c if os.path.exists(c) and os.path.getsize(c) else d


def two_alleles(reads_fa, cons, wd, tag):
    """[(fa,label)]: allele-1 = majority backbone; allele-2 = het-ALT consensus if biallelic."""
    ref = f"{wd}/{tag}.ref.fa"
    bam = f"{wd}/{tag}.bam"
    sh(f"cp {q(cons)} {q(ref)} && sed -i '1s/.*/>c/' {q(ref)} && samtools faidx {q(ref)} && "
       f"minimap2 -ax map-ont --secondary=no -t {MAP_T} {q(ref)} {q(reads_fa)} 2>/dev/null "
       f"| samtools sort -o {q(bam)} - && samtools index {q(bam)}")
    vcf = f"{wd}/{tag}.vcf.gz"
    sh(f"bcftools mpileup -B -Q5 -I --max-BQ 30 -d 100000 -a FORMAT/AD -f {q(ref)} {q(bam)} 2>/dev/null "
       f"| bcftools call --ploidy 2 -mv -Ou 2>/dev/null | bcftools norm -f {q(ref)} -d all -Ou 2>/dev/null "
       f"| bcftools filter -i 'INFO/DP>={MIN_DP}' -Oz -o {q(vcf)} 2>/dev/null; bcftools index -f {q(vcf)}")
    rows = [l for l in sh(f"bcftools view -H {q(vcf)} 2>/dev/null").stdout.splitlines() if l]
    het = [r for r in rows
           if re.match(r"[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t0[/|]1", r)]
    multi = [r for r in rows if "," in r.split("\t")[4]]
    a1 = f"{wd}/{tag}.a1.fa"
    sh(f"cp {q(cons)} {q(a1)}")   # dominant = backbone
    out = [(a1, "dominant")]
    if het and not multi:         # clean 2-allele model
        a2 = f"{wd}/{tag}.a2.fa"
        sh(f"samtools faidx {q(ref)} c | bcftools consensus -H 2 {q(vcf)} 2>/dev/null "
           f"| sed '1s/.*/>a2/' > {q(a2)}")
        if os.path.exists(a2) and os.path.getsize(a2):
            out.append((a2, f"second({len(het)}het)"))
    elif multi:
        out = [(a1, f"dominant(WARN {len(multi)} multiallelic->paralog-mix)")]
    return out


def classify(fa, wd, tag):
    query = f"{wd}/{tag}.q.fa"
    sh(f"awk 'NR==1{{print \">{tag}\"}} NR>1{{print}}' {q(fa)} > {q(query)}")

    def bl(db):
        r = sh(f"{q(C.BLASTN)} -db {q(db)} -query {q(query)} "
               f"-outfmt '6 pident length mismatch gaps qcovs stitle' "
               f"-max_target_seqs 1 -word_size 7 2>/dev/null | sort -k1,1nr | head -1")
        if not r.stdout.strip():
            return None
        f = r.stdout.strip().split("\t")
        return dict(pident=float(f[0]), mism=int(f[2]), gaps=int(f[3]), allele=f[5].split()[0])

    return bl(NUC), bl(GEN)


def tier(nuc, gen):
    b = nuc or gen
    if b is None:
        return "CHIMERA", "no BLAST hit", "", 0
    lc = locus_class(b["allele"])
    if lc == "non_classical":
        return "NON_CLASSICAL", "diagnostic", b["allele"], b["pident"]
    if lc != "classical_I":
        return "REVIEW", f"locus={lc}", b["allele"], b["pident"]
    if b["pident"] >= 100 and b["mism"] == 0 and b["gaps"] == 0:
        return "CONFIRMED", "exact IPD match", b["allele"], b["pident"]
    if b["gaps"] == 0:
        return "CANDIDATE_NOVEL", f"{b['mism']}SNP -> ArmB", b["allele"], b["pident"]
    return "REVIEW", f"gaps={b['gaps']} (chimera/error?)", b["allele"], b["pident"]


def process(reads_fa, wd, tag, min_reads):
    n = nreads(reads_fa)
    if n < min_reads:
        return []
    cons = backbone(reads_fa, wd, tag)
    if not cons:
        return [dict(tag=tag, reads=n, status="CHIMERA", note="backbone failed",
                     allele="", pident=0, hap="")]
    alleles = two_alleles(reads_fa, cons, wd, tag)
    rows, genes = [], []
    for i, (afa, lab) in enumerate(alleles, 1):
        nuc, gen = classify(afa, wd, f"{tag}_h{i}")
        st, note, al, pid = tier(nuc, gen)
        if al and locus_class(al) == "classical_I":
            genes.append(gene_of(al))
        rows.append(dict(tag=tag, hap=lab, reads=n, status=st, note=note, allele=al, pident=pid))
    if len(genes) == 2 and len(set(genes)) == 2:
        for r in rows:
            r["note"] += " | WARN a1/a2 different genes"
    return rows


def stage_and_filter(reads_source, wd, p):
    """cat reads (dir chunks or single file) -> nanoq length/Q filter. Returns fastq path or None."""
    merged = f"{wd}/m.fq.gz"
    filt = f"{wd}/f.fq.gz"
    if os.path.isdir(reads_source):
        sh(f"cat {q(reads_source)}/*.fastq.gz > {q(merged)} 2>/dev/null")
    elif os.path.isfile(reads_source):
        sh(f"cat {q(reads_source)} > {q(merged)} 2>/dev/null")
    else:
        return None
    sh(f"nanoq -i {q(merged)} -q {p['minq']} -l {p['minlen']} --max-len {p['maxlen']} "
       f"-o {q(filt)} 2>/dev/null")
    return filt


def ontarget(filt, wd, cap):
    """minimap2 to the MHC contig; keep primary aligned (on-target) reads as fasta.

    Returns (fasta, total_ontarget). Writes at most `cap` reads to the fasta so a very
    deep barcode doesn't choke medaka, but `total` is the true depth used for QC/gating.
    """
    bam = f"{wd}/mhc.bam"
    ot = f"{wd}/ontarget.fa"
    sh(f"minimap2 -ax map-ont --secondary=no -t {MAP_T} {q(MHCREF)} {q(filt)} 2>/dev/null "
       f"| samtools sort -o {q(bam)} - && samtools index {q(bam)}")
    af = pysam.AlignmentFile(bam, "rb")
    total = written = 0
    with open(ot, "w") as fh:
        for a in af.fetch():
            if a.is_secondary or a.is_supplementary or a.is_unmapped or not a.query_sequence:
                continue
            total += 1
            if not cap or written < cap:
                fh.write(f">{a.query_name}\n{a.query_sequence}\n")
                written += 1
    af.close()
    return ot, total


def emit(row):
    print("\t".join(str(row.get(c, "")) for c in COLS), flush=True)


def main():
    if len(sys.argv) < 6:
        sys.exit("usage: classI_type.py <barcode> <sample> <reads_source> <amplicon> <outdir>")
    bc, sample, reads_source, amp, outdir = sys.argv[1:6]
    if amp not in AMPLICON:
        emit(dict(barcode=bc, sample=sample, status="ERROR", note=f"unknown amplicon '{amp}'"))
        return
    p = AMPLICON[amp]
    wd = os.path.join(outdir, f"{amp}_{bc}")
    shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd, exist_ok=True)

    filt = stage_and_filter(reads_source, wd, p)
    if filt is None:
        emit(dict(barcode=bc, sample=sample, ontarget_reads=0, qc="",
                  status="MISSING", note="reads_source not found", reads=0, allele="", pident=0))
        return

    ot, k = ontarget(filt, wd, C.CLASSI_READ_CAP)
    qc = "LOW_DEPTH" if k < LOW_DEPTH else "ok"

    # cheap per-barcode read-depth gate: don't spend vsearch+medaka on undersampled
    # barcodes (esp. 5'UTR) — flag them SHALLOW so they show as review/fail cheaply.
    if k < p["min_reads"]:
        emit(dict(barcode=bc, sample=sample, ontarget_reads=k, qc=qc, tag="", hap="", reads=0,
                  status="SHALLOW", allele="", pident=0,
                  note=f"on-target {k} < {p['min_reads']} min reads — skipped (undersampled)"))
        return

    cl = f"{wd}/clusters"
    os.makedirs(cl, exist_ok=True)
    sh(f"vsearch --cluster_size {q(ot)} --id {p['cluster_id']} --strand plus "
       f"--clusters {q(cl + '/c_')} --threads {MAP_T} 2>/dev/null")
    cfs = [c for c in sorted(glob.glob(f"{cl}/c_*"), key=lambda x: -nreads(x))
           if nreads(c) >= p["min_reads"]][:TOP_CLUSTERS]

    rows = []
    for cf in cfs:
        for r in process(cf, wd, os.path.basename(cf), p["min_reads"]):
            r.update(barcode=bc, sample=sample, ontarget_reads=k, qc=qc)
            rows.append(r)
    if not rows:
        rows = [dict(barcode=bc, sample=sample, ontarget_reads=k, qc=qc, tag="", hap="", reads=0,
                     status="NO_CLUSTERS", allele="", pident=0,
                     note=f"no cluster >= {p['min_reads']} reads")]
    for r in rows:
        emit(r)


if __name__ == "__main__":
    main()
