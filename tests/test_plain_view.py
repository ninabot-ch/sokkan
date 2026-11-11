"""plain.View — the simple profile's thread never shows a tool name, a path, a command or the
internal brief outside the folded technical detail."""
import json

import plain
from simple_fixtures import docx, pptx, xlsx  # noqa: F401

RAW_TOOLS = ("mcp__", "memory_search", "memory_get", "Bash", "Glob", "Grep", "TodoWrite",
             "WebFetch", "MultiEdit", "tool_use")


def _view(tmp_path, attached=3):
    docs = tmp_path / "documents"
    deliv = tmp_path / "deliverables" / ("c" * 32)
    for d in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
        (docs / d).mkdir(parents=True)
    deliv.mkdir(parents=True)
    store = {
        "aaaaaaaaaaaaaaaa": {"id": "aaaaaaaaaaaaaaaa", "name": "Brief client.docx", "ext": "docx",
                             "modified_at": 1_700_000_000.0},
        "bbbbbbbbbbbbbbbb": {"id": "bbbbbbbbbbbbbbbb", "name": "Module 3.pptx", "ext": "pptx",
                             "modified_at": 1_650_000_000.0},
    }
    versions: dict[str, set] = {}

    def on_write(name, tool_id):
        versions.setdefault(name, set()).add(tool_id)
        return len(versions[name])

    return plain.View(docs_dir=docs, deliv_dir=deliv, doc_lookup=store.get, on_write=on_write,
                      attached=attached), docs, deliv


def _visible(ev: dict) -> str:
    """Everything a person can see without unfolding the technical detail."""
    return json.dumps({k: v for k, v in ev.items() if k != "detail"}, ensure_ascii=False)


def test_a_whole_turn_reads_in_plain_words(tmp_path):
    v, docs, deliv = _view(tmp_path)
    plan = "# Plan de formation Excel\n\n## Objectifs\n- Savoir filtrer\n"
    raw = [
        {"type": "status", "state": "working"},
        {"type": "model", "model": "qwen3.8-27b"},
        {"type": "thinking", "text": "I should call memory_search first"},
        {"type": "tool_use", "id": "t1", "tool": "mcp__sokkan-memory__memory_search",
         "title": "plan de formation", "input": {"query": "plan de formation"}},
        {"type": "tool_result", "tool_use_id": "t1", "text": "[]", "is_error": False, "truncated": False},
        {"type": "tool_use", "id": "t2", "tool": "Read", "title": str(docs / "aaaaaaaaaaaaaaaa" / "Brief client.docx.txt"),
         "input": {"file_path": str(docs / "aaaaaaaaaaaaaaaa" / "Brief client.docx.txt")}},
        {"type": "tool_result", "tool_use_id": "t2", "text": "…", "is_error": False, "truncated": False},
        {"type": "tool_use", "id": "t3", "tool": "Glob", "title": "*", "input": {"pattern": "*", "path": str(docs)}},
        {"type": "tool_use", "id": "t4", "tool": "Bash", "title": "ls -la /root", "input": {"command": "ls -la /root"}},
        {"type": "tool_use", "id": "t5", "tool": "TodoWrite", "title": "TodoWrite", "input": {"todos": []}},
        {"type": "tool_use", "id": "t6", "tool": "WebFetch", "title": "https://example.org/x?y=1",
         "input": {"url": "https://example.org/x?y=1"}},
        {"type": "permission", "id": "p1", "tool": "Write", "title": str(deliv / "plan-de-formation-excel.md"),
         "input": {"file_path": str(deliv / "plan-de-formation-excel.md"), "content": plan}},
        {"type": "permission_resolved", "id": "p1"},
        {"type": "tool_use", "id": "t7", "tool": "Write", "title": str(deliv / "plan-de-formation-excel.md"),
         "input": {"file_path": str(deliv / "plan-de-formation-excel.md"), "content": plan}},
        {"type": "tool_result", "tool_use_id": "t7", "text": "File created", "is_error": False, "truncated": False},
        {"type": "text", "text": "Le plan est prêt. Il reste la salle à confirmer."},
        {"type": "result", "text": "", "is_error": False},
        {"type": "status", "state": "idle"},
    ]
    out = [o for ev in raw for o in v.feed(ev)]
    kinds = [(o["type"], o.get("kind")) for o in out]
    assert ("activity", "search_memory") in kinds and ("activity", "read_document") in kinds
    assert ("activity", "search_documents") in kinds and ("activity", "run_command") in kinds
    assert ("activity", "plan_steps") in kinds and ("activity", "web_read") in kinds
    assert not any(o["type"] in ("model", "thinking", "result", "tool_use", "permission") for o in out)
    shown = "\n".join(_visible(o) for o in out)
    for word in RAW_TOOLS:
        assert word not in shown, word
    assert str(tmp_path) not in shown and "ls -la" not in shown and "/root" not in shown
    # the raw material is still there for whoever unfolds « technical detail »
    assert any(o.get("detail", {}).get("tool") == "Bash" for o in out)
    read = next(o for o in out if o.get("kind") == "read_document")
    assert read["subject"] == "Brief client.docx"
    assert next(o for o in out if o.get("kind") == "web_read")["subject"] == "example.org"

    card = next(o for o in out if o["type"] == "approval")
    assert card["kind"] == "write_draft" and card["title"] == "Plan de formation Excel"
    assert card["version"] == 1 and card["stays_inside"] is True and card["attached"] == 3
    assert [s["name"] for s in card["sources"]] == ["Brief client.docx"]       # only what was opened
    assert card["sources"][0]["modified_at"] == 1_700_000_000.0
    assert card["draft"]["text"] == plan and card["draft"]["partial"] is False
    assert {"type": "approval_resolved", "id": "p1"} in out
    deliverable = next(o for o in out if o["type"] == "deliverable")
    assert deliverable == {"type": "deliverable", "name": "plan-de-formation-excel.md",
                           "title": "Plan de formation excel", "version": 1}


def test_second_save_is_version_two_and_an_edit_shows_what_changes(tmp_path):
    v, _docs, deliv = _view(tmp_path)
    path = str(deliv / "Plan de formation.md")
    for i in (1, 2):
        v.feed({"type": "tool_use", "id": f"w{i}", "tool": "Write", "title": path,
                "input": {"file_path": path, "content": "# Plan\n"}})
        out = v.feed({"type": "tool_result", "tool_use_id": f"w{i}", "text": "ok", "is_error": False})
        assert out[-1]["type"] == "deliverable" and out[-1]["version"] == i
    card = v.feed({"type": "permission", "id": "p", "tool": "Edit", "title": path,
                   "input": {"file_path": path, "old_string": "a", "new_string": "Durée : 3 jours"}})[0]
    assert card["version"] == 3 and card["title"] == "Plan de formation"
    assert card["draft"] == {"text": "Durée : 3 jours", "partial": True, "truncated": False}
    # a failed save is not a deliverable
    v.feed({"type": "tool_use", "id": "w9", "tool": "Write", "title": path, "input": {"file_path": path, "content": "x"}})
    out = v.feed({"type": "tool_result", "tool_use_id": "w9", "text": "denied", "is_error": True})
    assert [o["type"] for o in out] == ["activity_done"] and out[0]["ok"] is False


def test_a_file_outside_the_session_space_is_not_a_deliverable(tmp_path):
    v, docs, deliv = _view(tmp_path)
    for path in ("/etc/passwd", str(deliv.parent / "other" / "x.md"), str(deliv / "sub" / "x.md"),
                 str(deliv / ".." / ".." / "x.md")):
        out = v.feed({"type": "permission", "id": "p", "tool": "Write", "title": path,
                      "input": {"file_path": path, "content": "x"}})[0]
        assert out["kind"] == "write_file" and out["version"] == 0, path
        assert "/" not in out["title"]
    # a path that only LOOKS like a document is not a source
    v.feed({"type": "tool_use", "id": "r", "tool": "Read", "title": "x",
            "input": {"file_path": str(docs / ".." / "documents-evil" / "aaaaaaaaaaaaaaaa" / "f.txt")}})
    assert v.sources() == []


def test_the_brief_never_reaches_the_thread(tmp_path):
    v, _d, _l = _view(tmp_path)
    seed = ("<sokkan-brief>\nYou are helping a person who is NOT a developer.\n- read: /srv/x/y.txt\n"
            "=== Project memory (auto-recalled) ===\n- [note] secret-ish context\n</sokkan-brief>\n\n"
            "Un plan de formation Excel pour 12 personnes")
    out = v.feed({"type": "user", "text": seed})
    assert out == [{"type": "user", "text": "Un plan de formation Excel pour 12 personnes", "documents": []}]
    follow = "<sokkan-brief>\nThe person just attached more documents.\n</sokkan-brief>\n\nAjoute le module 3"
    assert v.feed({"type": "user", "text": follow})[0]["text"] == "Ajoute le module 3"
    assert v.feed({"type": "user", "text": "<sokkan-brief>only</sokkan-brief>"}) == []
    echo = v.feed({"type": "user_echo", "text": "Bonjour", "documents": [{"id": "a", "name": "Brief.docx"}]})
    assert echo == [{"type": "user", "text": "Bonjour", "documents": [{"id": "a", "name": "Brief.docx"}]}]


def test_errors_become_coded_notices(tmp_path):
    v, _d, _l = _view(tmp_path)
    cases = {
        "A turn is already running — interrupt it first.": "busy",
        "Session budget reached ($2.00 ≥ $2.00). Raise the budget in Setup › Organisation": "budget",
        "CLIConnectionError: process exited": "engine",
        "The approval mode cannot be changed from the simple profile.": "mode_locked",
    }
    for msg, code in cases.items():
        out = v.feed({"type": "error", "message": msg})
        assert out == [{"type": "notice", "code": code, "detail": msg}]


def test_an_engine_failure_is_a_notice_not_a_sentence_of_the_agent(tmp_path):
    v, _d, _l = _view(tmp_path)
    msg = "API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)"
    assert v.feed({"type": "text", "text": msg}) == [{"type": "notice", "code": "engine", "detail": msg}]
    assert plain.last_line([{"type": "text", "text": "Le plan est prêt."}, {"type": "text", "text": msg}]) == "Le plan est prêt."


def test_last_line_is_the_last_sentence_without_markup():
    evs = [{"type": "text", "text": "Je lis vos documents."},
           {"type": "tool_use", "tool": "Read"},
           {"type": "text", "text": "## Résumé\n\nLe **plan** est prêt. Il reste `la salle` à confirmer."}]
    assert plain.last_line(evs) == "Il reste la salle à confirmer."
    assert plain.last_line([]) == ""
    assert len(plain.last_line([{"type": "text", "text": "x" * 500}])) <= 140


def test_human_titles():
    assert plain.human_title("plan-de-formation_excel-v2.md") == "Plan de formation excel"
    assert plain.human_title("Synthèse appel d'offres.md") == "Synthèse appel d'offres"
    assert plain.title_from_markdown("\n# **Plan** de formation `Excel`\n\ntexte") == "Plan de formation Excel"
    assert plain.title_from_markdown("pas de titre") == ""


def test_doctext_reads_office_files(tmp_path):
    import doctext
    p = tmp_path / "b.docx"
    p.write_bytes(docx(("# Brief", "Douze assistantes &amp; deux jours."), table=[("Module", "Durée"), ("Formules", "3 h")]))
    text, state = doctext.extract(p, "docx")
    assert state == "ok" and text.splitlines()[0] == "# Brief"
    assert "Douze assistantes & deux jours." in text and "Module | Durée" in text and "Formules | 3 h" in text
    p = tmp_path / "m.pptx"
    p.write_bytes(pptx((("Module 3", "Tableaux croisés"), ("Exercice", "Filtrer la liste"))))
    text, state = doctext.extract(p, "pptx")
    assert state == "ok" and "## Slide 1" in text and "Tableaux croisés" in text and "## Slide 2" in text
    p = tmp_path / "p.xlsx"
    p.write_bytes(xlsx((("Module", "Durée"), ("Formules", "3"))))
    text, state = doctext.extract(p, "xlsx")
    assert state == "ok" and "## Programme" in text and "Module | Durée" in text and "Formules | 3" in text
    # damaged or foreign content never raises
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"PK\x03\x04 nope")
    assert doctext.extract(bad, "docx") == ("", "failed")
    empty = tmp_path / "e.txt"
    empty.write_bytes(b"   \n")
    assert doctext.extract(empty, "txt") == ("", "empty")
    latin = tmp_path / "l.txt"
    latin.write_bytes("Référentiel été".encode("cp1252"))
    assert doctext.extract(latin, "txt")[0] == "Référentiel été"


def test_word_export_is_a_real_package(tmp_path):
    import zipfile

    import doctext
    import simple
    md = ("# Plan de formation Excel\n\nPublic : **12 assistantes**.\n\n## Modules\n\n"
          "| Module | Durée |\n|---|---|\n| Formules | 3 h |\n\n- Filtrer\n- Trier\n\n1. Ouvrir\n2. Tester <b>&\n")
    data = simple.markdown_to_docx(md)
    p = tmp_path / "out.docx"
    p.write_bytes(data)
    with zipfile.ZipFile(p) as z:
        assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml"} <= set(z.namelist())
        xml = z.read("word/document.xml").decode()
    assert "<w:tbl>" in xml and 'w:val="Title"' in xml and "&lt;b&gt;&amp;" in xml
    # and it reads back through the same reader as an uploaded document
    text, state = doctext.extract(p, "docx")
    assert state == "ok" and "Plan de formation Excel" in text and "Formules | 3 h" in text
    import documents
    documents.check_content(p, "docx")
