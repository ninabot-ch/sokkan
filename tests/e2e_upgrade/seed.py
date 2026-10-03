#!/usr/bin/env python3
"""Runs INSIDE the api container of the SOURCE version: a session with its transcript and
a few board cards, written through the version's own board module (any 0.1-3.x)."""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, "/app/backend")
import board  # noqa: E402

SID = "upg-session-0001"
CSID = str(uuid.UUID(int=0x5E55107))
CARDS = [("Order the spring flour", "Ask Moulin des Trois Ponts for the T80 quota."),
         ("Descale the espresso machine", "Monthly, see the maintenance note."),
         ("Print the 2026 price list", "")]

for title, desc in CARDS:
    board.add_card(title, desc, tag="backend")
con = board._con()
con.execute("INSERT OR REPLACE INTO sessions(session_id, tag, window, title, prompt, created_at,"
            " kind, claude_session_id) VALUES(?,?,?,?,?,?,?,?)",
            (SID, "ops", "", "Flour order follow-up", "When does the flour arrive?",
             time.time() - 86400, "sdk", CSID))
con.commit()
con.close()
proj = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", "/data/claude"), "projects", "-workspace")
os.makedirs(proj, exist_ok=True)
with open(os.path.join(proj, CSID + ".jsonl"), "w") as fh:
    for i, (role, text) in enumerate([("user", "When does the flour arrive?"),
                                      ("assistant", "Every Tuesday, from Moulin des Trois Ponts.")]):
        fh.write(json.dumps({"type": role, "sessionId": CSID, "uuid": str(uuid.uuid4()),
                             "timestamp": "2026-09-29T08:0%d:00Z" % i,
                             "message": {"role": role, "content": text}}) + "\n")
print(json.dumps({"session": SID, "cards": [t for t, _ in CARDS]}))
