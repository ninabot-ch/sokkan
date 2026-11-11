"""The one substitution applied to the gateway's text before it is frozen or compared.

The gateway's page (sokkan-cloud infer/pilote-etude.md) is an internal document: its `GET /usage`
example carries the identifier of a real study, and its § 2 the name of a real tenant. Neither
belongs in this repository, and neither is part of the contract — an instance reads `study_id`
as an opaque string. So, on BOTH sides (regenerate.py when it writes contract.json, the test when
it compares the frozen copy with a checkout):

* `usage.study.study_id` — whatever non-empty string the page holds — becomes STUDY_ID;
* the tenant regenerate.py creates to play the refusals is named TENANT and carries STUDY_ID.

The substitution is by FIELD, not by value: the real identifier is written nowhere here. Every
other field of the example (dates, flags, limits, counters) and every word of the refusals is
compared as it stands.
"""
from __future__ import annotations

import copy

STUDY_ID = "etude-pilote-2027"
TENANT = "atelier"


def usage(example: dict) -> dict:
    """The `GET /usage` example with the study identifier neutralised — and nothing else."""
    out = copy.deepcopy(example)
    sid = out["study"]["study_id"]
    if not (isinstance(sid, str) and sid.strip()):
        raise ValueError("usage.study.study_id must be a non-empty string")
    out["study"]["study_id"] = STUDY_ID
    return out
