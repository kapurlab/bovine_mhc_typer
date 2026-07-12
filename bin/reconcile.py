#!/usr/bin/env python3
"""Cross-amplicon reconciliation for Class I.

Combine the per-amplicon typed TSVs (Bov7/11 + BosEx + 5'UTR) into per-animal
reconciled calls with concordance-based confidence, then call MHCI haplotypes from
the confident set and merge in DRB3. Ported from BoLA_MHC/scripts/reconcile.py —
only the hard-coded project paths were lifted out to <outdir> + mhc_config; the
reconciliation + haplotype-calling logic is unchanged.

Usage: reconcile.py <outdir>
  reads  <outdir>/classI_{bov711,bosex,utr5}_typed.tsv (whichever exist) + drb3_typed.tsv
  writes <outdir>/reconciled_alleles.tsv + <outdir>/per_animal_reconciled.tsv

Class I stays PROVISIONAL (HANDOVER §8): the short amplicons don't reproduce, so the
concordance tiers below are leads, not final genotypes — keep the amber gate in the UI.
"""
import csv
import json
import collections
import itertools
import re
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mhc_config as C

CLASSICAL = {"BOLA-1", "BOLA-2", "BOLA-3", "BOLA-4", "BOLA-6"}
NCTAG = re.compile(r"NC\d|JSP|LOC")
# amplicon token -> per-amplicon typed TSV written by run_classI (display name kept
# identical to the research pipeline so support strings match).
AMP_FILES = [("Bov7/11", "classI_bov711_typed.tsv"),
             ("BosEx",   "classI_bosex_typed.tsv"),
             ("5UTR",    "classI_utr5_typed.tsv")]


def norm(a):
    if "*" not in a:
        return a
    g, rest = a.split("*", 1)
    f = rest.split(":")
    return f"{g}*{f[0]}" + (f":{f[1]}" if len(f) > 1 else "")


def is_null(a):
    return a.rstrip().endswith("N")


def locus(a):
    g = a.split("*")[0].upper() if "*" in a else a.upper()
    if g in CLASSICAL:
        return "classical_I"
    if NCTAG.search(a.upper()):
        return "non_classical"
    if "DRB" in g or "DQ" in g:
        return "class_II"
    return "other"


def gene(a):
    return a.split("*")[0]


def load(path):
    d = collections.defaultdict(list)
    try:
        rows = list(csv.DictReader(open(path), delimiter="\t"))
    except FileNotFoundError:
        return d
    for r in rows:
        al = r.get("allele", "")
        if al and r.get("status") in ("CONFIRMED", "CANDIDATE_NOVEL", "NON_CLASSICAL"):
            d[r["sample"]].append((al, r["status"], float(r.get("pident") or 0)))
    return d


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: reconcile.py <outdir>")
    outdir = sys.argv[1]

    AMPS = [(name, load(os.path.join(outdir, fn)))
            for name, fn in AMP_FILES if os.path.exists(os.path.join(outdir, fn))]
    if not AMPS:
        print("[reconcile] no Class I typed TSVs in outdir — nothing to reconcile", flush=True)
        return
    HAPS = json.load(open(C.HAPLOTYPES))
    samples = sorted(set().union(*[set(d) for _, d in AMPS]) - {""})

    # --- reconcile per sample ---
    recon_rows = []
    conf_alleles = {}   # sample -> set of confident classical (normalized)
    for s in samples:
        perallele = collections.defaultdict(lambda: {"amps": set(), "confirmed": False, "best": 0.0})
        for amp, d in AMPS:
            for al, st, pid in d.get(s, []):
                na = norm(al)
                e = perallele[na]
                e["amps"].add(amp)
                if st == "CONFIRMED":
                    e["confirmed"] = True
                e["best"] = max(e["best"], pid)
        confset = set()
        for na, e in sorted(perallele.items()):
            lc = locus(na)
            namps = len(e["amps"])
            if namps >= 2:
                conf = "HIGH (concordant %d-amplicon)" % namps
            elif e["confirmed"]:
                conf = "CONFIRMED (single-amplicon 100%)"
            else:
                conf = "PROVISIONAL (single candidate)"
            recon_rows.append(dict(sample=s, locus=lc, allele=na, confidence=conf,
                                   support="+".join(sorted(e["amps"])), best_pident=round(e["best"], 2)))
            if lc == "classical_I" and (namps >= 2 or e["confirmed"]) and not is_null(na):
                confset.add(na)
        conf_alleles[s] = confset

    # --- MHCI haplotype from reconciled confident alleles (specificity-weighted) ---
    MHCI = {h: [[norm(x) for x in slot] for slot in slots] for h, slots in HAPS["MHCI"].items()}
    hapcount = collections.Counter()
    for h, slots in MHCI.items():
        for a in set(x for sl in slots for x in sl):
            hapcount[a] += 1

    def spec(a):
        return 1.0 / hapcount.get(a, 1)

    def exp_slots(h):
        return [sl for sl in MHCI[h] if any(not is_null(a) for a in sl)]

    def sat(sl, obs):
        return any(a in obs for a in sl)

    def universe(h):
        return set(a for sl in MHCI[h] for a in sl)

    def call_hap(obs):
        obs = set(obs)
        if len(obs) < 2:
            return None
        cand = [h for h in MHCI if any(sat(sl, obs) for sl in exp_slots(h))]
        best = None
        for h1, h2 in itertools.combinations_with_replacement(cand, 2):
            u = universe(h1) | universe(h2)
            expl = obs & u
            if len(expl) < 2:
                continue
            if not any(hapcount.get(a, 99) <= 8 for a in expl):
                continue
            s1 = sum(1 for sl in exp_slots(h1) if sat(sl, obs))
            s2 = sum(1 for sl in exp_slots(h2) if sat(sl, obs))
            if s1 == 0 or (h1 != h2 and s2 == 0):
                continue
            sc = (len(expl), round(sum(spec(a) for a in expl), 4), -len(u))
            if best is None or sc > best[0]:
                best = (sc, (h1, h2), sorted(obs & u))
        return best

    # --- DRB3 (optional) ---
    drb3 = {}
    drb3_path = os.path.join(outdir, "drb3_typed.tsv")
    if os.path.exists(drb3_path):
        for r in csv.DictReader(open(drb3_path), delimiter="\t"):
            a1 = r["allele1:count"].rsplit(":", 1)[0] if ":" in r["allele1:count"] else ""
            a2 = r["allele2:count"].rsplit(":", 1)[0] if ":" in r["allele2:count"] else ""
            z = r["zygosity"]
            drb3[r["sample"]] = (f"{norm(a1)}/{norm(a2)}" if z == "het"
                                 else (f"{norm(a1)} (hom)" if z == "hom" else z))

    # --- write reconciled allele table ---
    with open(os.path.join(outdir, "reconciled_alleles.tsv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["sample", "locus", "allele", "confidence",
                                           "support", "best_pident"], delimiter="\t")
        w.writeheader()
        [w.writerow(r) for r in recon_rows]

    # --- final per-animal table ---
    with open(os.path.join(outdir, "per_animal_reconciled.tsv"), "w", newline="") as out:
        w = csv.writer(out, delimiter="\t")
        w.writerow(["sample", "confident_classI_alleles", "n_conf", "MHCI_haplotype", "DRB3_genotype"])
        n_called = 0
        for s in samples:
            cs = sorted(conf_alleles[s])
            res = call_hap(cs)
            if res:
                hc = f"{res[1][0]} / {res[1][1]}" if res[1][0] != res[1][1] else f"{res[1][0]} (hom?)"
                n_called += 1
            else:
                hc = "(insufficient)"
            w.writerow([s, ";".join(cs) or "-", len(cs), hc, drb3.get(s, "-")])

    allconf = collections.Counter(r["confidence"].split()[0] for r in recon_rows if r["locus"] == "classical_I")
    print(f"[reconcile] amplicons: {'+'.join(n for n, _ in AMPS)} · {len(samples)} animals", flush=True)
    print(f"[reconcile] class-I confidence tiers: {dict(allconf)}", flush=True)
    print(f"[reconcile] MHCI haplotype called: {n_called}/{len(samples)} · "
          f"wrote reconciled_alleles.tsv + per_animal_reconciled.tsv", flush=True)


if __name__ == "__main__":
    main()
