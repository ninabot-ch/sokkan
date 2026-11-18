"""4.0 (audit coworker, 18.11.2026) — readable approval cards, no promise of an absent capability,
Nina on the local model.

The audit of 21 real tasks on the 27B saw:
* « L'agent veut effectuer une action » with nothing else — it hid a kanban card tagged `seo`
  in the developers' board (marketing) and an automatic Crew agent (project manager);
* « I created an automatic task that e-mails it to you every Friday » with no mail channel;
* Nina: 79 s, then « … ask the admin to raise SOKKAN_ASSISTANT_MAX_TOKENS ».
"""
import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import approvalcard  # noqa: E402
import plain  # noqa: E402

RAW = ("mcp__", "sokkan-board", "sokkan-agents", "create_card", "create_agent", "_usd",
       "SOKKAN_", "0 16 * * 5")


def _visible(ev: dict) -> str:
    """What a person reads on the card: title, where, facts, preview (not the kind's word, not the
    folded technical detail)."""
    return json.dumps({k: v for k, v in ev.items() if k not in ("detail", "kind")},
                      ensure_ascii=False)


def _card(tool, inp, lang="fr", project="Gestion de projets", view=None):
    v = view or plain.View(lang=lang, project=project)
    out = v.feed({"type": "permission", "id": "p1", "tool": tool, "input": inp})
    assert len(out) == 1 and out[0]["type"] == "approval"
    return out[0]


# ---- 1. every card says what, where, and with what ---------------------------------------------
def test_the_audits_kanban_card_is_named_with_its_board_and_label():
    ev = _card("mcp__sokkan-board__create_card",
               {"title": "Optimiser la newsletter", "tag": "seo", "description": "Mots-clés"})
    assert ev["kind"] == "board_card" and ev["title"] == "Optimiser la newsletter"
    assert "tableau des tâches" in ev["where"]
    assert {"label": "Étiquette", "value": "seo"} in ev["facts"]
    assert ev["stays_inside"] is True
    assert not any(r in _visible(ev) for r in ("mcp__", "sokkan-board", "create_card"))


def test_the_audits_automatic_agent_shows_its_mission_and_schedule_in_words():
    ev = _card("mcp__sokkan-agents__create_agent",
               {"name": "point-avancement-vendredi", "purpose": "Préparer le point d'avancement",
                "deliverable": "Un document Word", "trigger": "cron", "schedule": "0 16 * * 5",
                "budget_usd": 2})
    assert ev["kind"] == "create_agent" and ev["title"] == "point-avancement-vendredi"
    facts = {f["label"]: f["value"] for f in ev["facts"]}
    assert facts["Quand"] == "chaque vendredi à 16:00"
    assert facts["Mission"] == "Préparer le point d'avancement"
    assert facts["Budget par exécution"] == "2.00 USD"
    assert "projet « Gestion de projets »" in ev["where"]
    assert "administre SOKKAN" in ev["where"]          # it does nothing before the admin approves
    assert not any(r in _visible(ev) for r in RAW)


def test_the_kernel_naming_reads_the_same():
    a = _card("mcp__sokkan-board__create_card", {"title": "T", "tag": "seo"})
    b = _card("sokkan-board__create_card", {"title": "T", "tag": "seo"})
    assert (a["kind"], a["title"], a["facts"]) == (b["kind"], b["title"], b["facts"])


def test_a_change_to_a_document_is_shown_as_before_and_after(tmp_path):
    deliv = tmp_path / "deliv"
    deliv.mkdir()
    (deliv / "Newsletter.md").write_text("# Newsletter décembre\n\nnos collaborateurs vous conseilleraient de gracieux…\n")
    v = plain.View(deliv_dir=deliv, lang="fr", project="Marketing")
    ev = _card("Edit", {"file_path": str(deliv / "Newsletter.md"),
                        "old_string": "vous conseilleraient de gracieux",
                        "new_string": "vous conseillent gracieusement"}, view=v)
    assert ev["kind"] == "write_draft" and ev["title"] == "Newsletter décembre"
    assert ev["diff"] == [{"before": "vous conseilleraient de gracieux",
                           "after": "vous conseillent gracieusement"}]
    assert ev["where"] == "Les livrables de ce travail — projet « Marketing »"


def test_a_diff_shows_only_what_changes():
    """The 27B replaced a whole section to add one sentence (replay of 18.11): the card shows the
    sentence, not the section twice."""
    section = "## Rénovation connectée\n\nVous habitez déjà votre villa ?\n\nDès 4'900 CHF."
    added = section.replace("\n\nDès", "\n\nNotre équipe s'occupe de tout.\n\nDès")
    assert approvalcard.diff_of("Edit", {"old_string": section, "new_string": added}) == [
        {"before": "", "after": "Notre équipe s'occupe de tout."}]
    assert approvalcard.diff_of("Edit", {"old_string": "a b", "new_string": "a c"}) == [
        {"before": "a b", "after": "a c"}]


def test_a_note_to_retain_can_be_read_before_deciding():
    ev = _card("mcp__sokkan-memory__memory_write",
               {"name": "societe-suisse", "description": "Nous sommes une société suisse",
                "body": "# Société suisse\n\nDroit applicable : nLPD, CO."})
    assert ev["kind"] == "save_memory"
    assert ev["draft"]["text"].startswith("# Société suisse")
    assert ev["where"].startswith("La mémoire du projet")


def test_a_mail_draft_says_to_whom_and_that_nothing_is_sent():
    ev = _card("mcp__sokkan-connectors__mail_draft",
               {"to": ["compta@example.ch"], "subject": "Relance facture 2026-118", "body": "Bonjour"})
    assert ev["kind"] == "mail_draft" and ev["title"] == "Relance facture 2026-118"
    assert {"label": "À", "value": "compta@example.ch"} in ev["facts"]
    assert ev["draft"]["text"] == "Bonjour"
    assert ev["stays_inside"] is False


def test_an_unknown_tool_is_never_a_blind_action():
    ev = _card("mcp__acme-crm__update_deal", {"deal": "Offre Client X", "amount": 93960})
    assert ev["kind"] == "tool" and ev["title"] == "Update deal"
    assert ev["where"] == "Le service « acme-crm »"
    assert {"label": "Amount", "value": "93960"} in ev["facts"]


def test_cards_in_english():
    ev = _card("mcp__sokkan-agents__create_agent",
               {"name": "weekly-report", "purpose": "p", "deliverable": "d", "trigger": "cron",
                "schedule": "30 8 * * 1-5"}, lang="en", project="PM")
    facts = {f["label"]: f["value"] for f in ev["facts"]}
    assert facts["When"] == "Monday to Friday at 08:30"
    assert ev["where"].startswith("The team of automatic agents — project “PM”")


@pytest.mark.parametrize("trigger,cron,fr", [
    ("manual", "", "seulement quand quelqu'un le lance"),
    ("cron", "0 7 * * *", "chaque jour à 07:00"),
    ("cron", "15 9 1 * *", "le 1 de chaque mois à 09:15"),
    ("cron", "0 16 * * 0", "chaque dimanche à 16:00"),
    ("cron", "*/5 * * * *", "planification « */5 * * * * »"),
])
def test_schedules_in_words(trigger, cron, fr):
    assert approvalcard.human_schedule(trigger, cron, "fr") == fr


# ---- 2. the Atelier has no board, no agent team, no monitoring ----------------------------------
@pytest.fixture()
def world(tmp_path, monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    import agentchat
    import audit
    import board
    import hitl_journal
    import notify
    import simple

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(simple, "DB", tmp_path / "simple.db")
    monkeypatch.setattr(simple, "enabled", lambda: True)
    monkeypatch.setattr(notify, "send", lambda *a, **k: {})
    rows = []
    monkeypatch.setattr(hitl_journal, "automatic", lambda *a, **k: rows.append(k))
    monkeypatch.setattr(hitl_journal, "requested", lambda *a, **k: None)
    monkeypatch.setattr(hitl_journal, "decided", lambda *a, **k: None)
    work = tmp_path / "work"
    work.mkdir()
    yield {"tmp": tmp_path, "work": work, "journal": rows, "agentchat": agentchat}


def _session(world, simple_work: bool):
    import board
    import simple
    agentchat = world["agentchat"]
    sid = agentchat.new_sid()
    board.add_sdk_session(sid, "docs", title="t", project="default", owner="ana@x")
    if simple_work:
        simple.add_work(sid, "default", "ana@x", "Prépare la newsletter.", "", "fr")
    return agentchat.AgentSession(sid, cwd=str(world["work"]), user="ana@x")


def test_an_atelier_work_has_no_cockpit_server(world):
    s = _session(world, simple_work=True)
    spec = asyncio.new_event_loop().run_until_complete(s._run_spec())
    assert "sokkan-memory" in spec.mcp_servers
    assert not {"sokkan-board", "sokkan-agents", "sokkan-observability"} & set(spec.mcp_servers)


def test_the_cockpit_keeps_board_and_agents(world):
    s = _session(world, simple_work=False)
    spec = asyncio.new_event_loop().run_until_complete(s._run_spec())
    assert {"sokkan-board", "sokkan-agents"} <= set(spec.mcp_servers)


@pytest.mark.parametrize("tool", ["mcp__sokkan-board__create_card", "sokkan-board__create_card",
                                  "mcp__sokkan-agents__create_agent", "sokkan-agents__create_agent",
                                  "mcp__sokkan-observability__create_dashboard"])
def test_the_gate_refuses_a_cockpit_tool_in_the_atelier_whatever_the_mode(world, tool):
    from harness_helpers import ask
    agentchat = world["agentchat"]
    s = _session(world, simple_work=True)
    s.mode = "bypassPermissions"
    res = asyncio.new_event_loop().run_until_complete(ask(s, tool, {"title": "x"}))
    assert res.denied and res.message == agentchat.ATELIER_NO_COCKPIT
    assert not [e for e in s.events if e.get("type") == "permission"]     # no card at all
    assert world["journal"][-1]["reason"] == "policy:simple_profile_no_cockpit"
    # the thread does not show it as a failure: a rule of the space, the agent goes on
    v = plain.View()
    v.feed({"type": "tool_use", "id": "t1", "tool": tool, "input": {}})
    done = v.feed({"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                   "text": agentchat.ATELIER_NO_COCKPIT})
    assert done[0]["reason"] == "policy"


def test_the_refusal_tells_the_agent_not_to_claim_it():
    from harness.refusals import ATELIER_NO_COCKPIT
    assert "never say you did" in ATELIER_NO_COCKPIT
    assert "mcp__" not in ATELIER_NO_COCKPIT


# ---- 3. the brief lists what the workspace really does ------------------------------------------
def _brief(tmp_path, monkeypatch, **kw):
    import documents
    import simple
    monkeypatch.setattr(documents, "project_root", lambda p: tmp_path)
    return simple.brief(ask="Chaque vendredi, envoie-le-moi par e-mail.", lang="fr",
                        project="default", sid="e" * 32, user="david@x", **kw)


def test_without_connectors_the_brief_forbids_mail_and_schedules(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "connectors", None)          # no connector module at all
    b = _brief(tmp_path, monkeypatch)
    assert "You CANNOT send or receive e-mail" in b
    assert "schedule or repeat a work automatically" in b
    assert "create tasks, cards, reminders or agents" in b
    assert "Never say you did something you did not do" in b


def test_with_a_connected_mailbox_the_brief_offers_a_draft_never_a_send(tmp_path, monkeypatch):
    status = {"families": {"mail": [{"provider": "imap", "enabled": True, "connected": True}],
                           "calendar": [], "files": []},
              "content_allowed": True, "allow_send": True}
    service = types.SimpleNamespace(status=lambda ctx: status,
                                    Ctx=lambda email, project="default": (email, project))
    mod = types.ModuleType("connectors")
    mod.enabled = lambda: True
    mod.service = service
    monkeypatch.setitem(sys.modules, "connectors", mod)
    monkeypatch.setitem(sys.modules, "connectors.service", service)
    b = _brief(tmp_path, monkeypatch)
    assert "mailbox is connected" in b and "approve the sending card" in b
    assert "send or receive e-mail" not in b
    assert "schedule or repeat a work automatically" in b
    status["allow_send"] = False
    assert "they send it themselves" in _brief(tmp_path, monkeypatch)
    status["content_allowed"] = False                          # a confidential project
    assert "send or receive e-mail" in _brief(tmp_path, monkeypatch)


def test_a_reply_draft_says_to_whom_about_what_and_how_it_begins():
    """4.0.1 (vidéo de lancement): « L'agent veut préparer un brouillon de mail : « » » with no
    recipient and no subject — a reply names only the message it answers."""
    v = plain.View(lang="fr", project="Clients")
    listing = {"provider": "demo", "messages": [
        {"id": "m1", "thread_id": "conv-1", "subject": "Offre de formation 2027", "from": "client@example.ch",
         "to": ["moi@example.ch"], "cc": [], "date": "2026-11-18T08:00:00Z", "is_draft": False,
         "has_attachments": False, "snippet": "Bonjour"}]}
    v.feed({"type": "tool_use", "id": "t1", "tool": "mcp__sokkan-connectors__mail_list", "input": {}})
    v.feed({"type": "tool_result", "tool_use_id": "t1", "text": json.dumps(listing)})
    body = ("Bonjour Madame,\n\nMerci pour votre message. Voici notre offre de formation 2027 au tarif de "
            "notre modèle d'offre : 2 jours, 12 personnes au plus, 6'600.00 CHF HT. " + "Détail. " * 40)
    ev = _card("mcp__sokkan-connectors__mail_draft", {"reply_to": "m1", "body": body}, view=v)
    assert ev["title"] == "Re: Offre de formation 2027"
    assert {"label": "À", "value": "client@example.ch"} in ev["facts"]
    assert {"label": "Objet", "value": "Re: Offre de formation 2027"} in ev["facts"]
    opening = next(f["value"] for f in ev["facts"] if f["label"] == "Début du texte")
    assert opening.startswith("Bonjour Madame, Merci pour votre message.") and opening.endswith("…")
    assert len(opening) < 260


def test_a_reply_to_an_unread_message_never_shows_an_empty_recipient():
    ev = _card("sokkan-connectors__mail_draft", {"reply_to": "zz", "body": "Bonjour"})
    assert ev["title"] == ""          # the front says « un brouillon de réponse »
    assert {"label": "À", "value": "l'expéditeur du message auquel l'agent répond"} in ev["facts"]
    assert {"label": "Début du texte", "value": "Bonjour"} in ev["facts"]


def test_a_cut_listing_still_feeds_the_reply_card():
    v = plain.View(lang="en")
    v.feed({"type": "tool_use", "id": "t1", "tool": "sokkan-connectors__mail_read_thread", "input": {}})
    v.feed({"type": "tool_result", "tool_use_id": "t1",
            "text": '{"provider": "imap", "messages": [{"id": "<a@b>", "subject": "Re: Devis", "from": "x@example.org", "body": "tr'})
    ev = _card("sokkan-connectors__mail_draft", {"reply_to": "<a@b>", "body": "Hello"}, lang="en", view=v)
    assert ev["title"] == "Re: Devis"
    assert {"label": "To", "value": "x@example.org"} in ev["facts"]
