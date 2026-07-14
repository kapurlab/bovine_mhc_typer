# BoLA MHC reference bundle

Runtime references for the typing pipeline (`bin/mhc_config.py` resolves `MHC_REFS`
to this directory by default). **Only the files the pipeline actually reads are
committed here** — the FASTAs used to *build* the BLAST databases are intentionally
excluded (a BLAST DB is self-contained; the source FASTA is not needed to query it).

## What's here (and where the code uses it)

| File | Used by | Purpose |
|---|---|---|
| `ARS-UCD2.0_chr23_MHC_renamed.fa` | `classI_type.py` (`MHCREF`) | minimap2 on-target target — ARS-UCD2.0 chr23 MHC contig, header renamed to `>NC_037350.1` |
| `blast_db/BoLA_nuc.*` | `drb3_type.py`, `classI_type.py` (`BLAST_NUC`) | IPD-MHC BoLA **CDS/nucleotide** BLAST DB |
| `blast_db/BoLA_gen.*` | `classI_type.py` (`BLAST_GEN`) | IPD-MHC BoLA **genomic** BLAST DB |
| `haplotypes.json` | `reconcile.py` (`HAPLOTYPES`) | MHCI/MHCII haplotype slot definitions |

## How the BLAST databases were built (record of source material)

Both DBs are BLAST v5 nucleotide databases built on 2026-07-10 with:

```bash
makeblastdb -in IPD_BoLA_nuc.fasta -dbtype nucl -parse_seqids -out blast_db/BoLA_nuc
makeblastdb -in IPD_BoLA_gen.fasta -dbtype nucl -parse_seqids -out blast_db/BoLA_gen
```

| BLAST DB | Built from | Sequences | Total bases |
|---|---|---|---|
| `BoLA_nuc` | `IPD_BoLA_nuc.fasta` | 856 | 394,042 |
| `BoLA_gen` | `IPD_BoLA_gen.fasta` | 167 | 468,404 |

`IPD_BoLA_{nuc,gen}.fasta` are the **BoLA-only subsets** filtered from the full
IPD-MHC releases `MHC_nuc.fasta` (11,525 seqs, all species) and `MHC_gen.fasta`
(3,008 seqs). Deflines look like `>IPD-MHC:BoLA02982 BoLA-2*012:01 1077 bp`.
The renamed contig derives from NCBI `NC_037350.1:27800000-28700000` (ARS-UCD2.0
chr23), header shortened to `>NC_037350.1`.

## Deliberately NOT committed (not needed at runtime)

- `MHC_nuc.fasta`, `MHC_gen.fasta` — raw full IPD-MHC downloads (all species); only
  their BoLA subsets were used, and only to build the DBs above.
- `IPD_BoLA_nuc.fasta`, `IPD_BoLA_gen.fasta` — the DB build inputs (self-contained
  in `blast_db/` once built).
- `IPD_BoLA_alleles.fasta` — a saved HTTP 404 error page (a failed download), not data.
- `ARS-UCD2.0_chr23_MHC.fa` (+ `.fai`) — the pre-rename contig; the code uses the
  `_renamed` version.
- `*.mmi` (minimap2 indexes) and `*.fa.fai` — regenerated on the fly by minimap2 /
  samtools; not required.
- `blast_db/Tim_BoLA_MHCI.*` and `tim_mhci/` — not referenced by any pipeline script.

To rebuild the excluded/source layer, re-fetch the IPD-MHC FASTAs, filter to BoLA,
and re-run the `makeblastdb` commands above.
