"""docingest — feature `doc_ingest`: the memory agent that reads the company's documents.

A read-only, allow-listed pipeline that turns documents (shared folders, SharePoint /
OneDrive / Google Drive mirrored by rclone) into CortHeXis notes:

    sources.py   what may be read (allow-list, read-only, incremental listing)
    extract.py   file -> text + headings + metadata (MarkItDown, Docling fallback for PDFs)
    agent.py     text -> typed notes in strict JSON -> CortHeXis markdown (the document is
                 DATA: the model has no tool, every field is re-validated here)
    batches.py   the produced notes wait in quarantine BY BATCH (source x pass); review,
                 sample, approve / reject, trust policy per source, decision journal
    runner.py    one pass: resumable, budgeted, journaled
    scheduler.py nightly incremental pass, ticked by the agents scheduler
    api.py       /api/doc-ingest/*

Nothing here writes into the memory directory except `batches.approve*`. Nothing leaves the
machine: the model endpoint must be local (llmclient.check_local) and sources are only read.
See docs/enterprise/DOC-INGEST.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

# memory/ (quarantine.py, core/, store_backend.py) is shared with the API the same way
# app.py does it; needed when this package runs on its own (`python -m docingest`)
_MEM = Path(__file__).resolve().parents[2] / "memory"
if _MEM.is_dir() and str(_MEM) not in sys.path:
    sys.path.insert(0, str(_MEM))

FEATURE = "doc_ingest"


def enabled() -> bool:
    import features
    return features.enabled(FEATURE)
