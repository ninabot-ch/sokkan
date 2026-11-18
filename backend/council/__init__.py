"""Launch council (experimental, feature `council`): nine « Nina » roles debate a project the
CEO puts in a room, the finance lead arbitrates and hands back ONE agreement card.

    roles.py     the nine roles (roles/*.yaml, FR + EN)
    engine.py    the interface that makes a role speak + the fake engine (tests)
    models.py    the real engine: direct model calls through SOKKAN's inference tiers
    schema.py    the strict JSON of the arbitration, validated
    store.py     SQLite (council.db): councils, documents, journal of turns
    service.py   the three bounded rounds, the budget, accept / refuse
    card.py      the agreement card (conventions of approvalcard.py)
    export.py    JSON and Markdown export of a council
    api.py       routes /api/council/*

Doc: docs/enterprise/COUNCIL.md
"""
