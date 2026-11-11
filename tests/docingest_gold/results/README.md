# Archived runs of the document-ingestion bench

Raw outputs of `scripts/doc-ingest-bench.py` on the gold set (`tests/docingest_gold/gold.py`):
figures, scores and every note the model produced. Model, date and settings:
`docs/enterprise/DOC-INGEST.md` § 12. No test reads these files.

**Edited after the fact**: these are model outputs, kept as written, with ONE exception. The
fictional school of the gold set was abbreviated by the model with an acronym that is also
that of real institutions; on 11.11.2026 the acronym was replaced in the three files — in the
note names and tags (`…-ecole-chablais…`) and in one sentence per file of runs 5 and 6
(« la Haute école du Chablais ») — and the matching decision key of `gold.py` was renamed
`partenariat-ecole`. Nothing else was touched: scores and figures are those of the runs (the
scorer matches on « chablais » / « haute ecole », which the replacement keeps).
