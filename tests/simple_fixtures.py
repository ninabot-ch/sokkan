"""Builders shared by the simple-profile / document-drop tests: tiny but REAL Office files
(zip packages with the parts Word, PowerPoint and Excel write), and the test world."""
import io
import zipfile

import pytest

CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


def _zip(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


def docx(paragraphs=("Brief client", "Douze assistantes, deux jours."), table=None, extra=None) -> bytes:
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{p[2:]}</w:t></w:r></w:p>'
        if p.startswith("# ") else f"<w:p><w:r><w:t xml:space=\"preserve\">{p}</w:t></w:r></w:p>"
        for p in paragraphs)
    if table:
        body += "<w:tbl>" + "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row)
            + "</w:tr>" for row in table) + "</w:tbl>"
    parts = {"[Content_Types].xml": CT,
             "word/document.xml": f'<?xml version="1.0"?><w:document><w:body>{body}</w:body></w:document>'}
    parts.update(extra or {})
    return _zip(parts)


def pptx(slides=(("Module 3", "Tableaux croisés"),)) -> bytes:
    parts = {"[Content_Types].xml": CT}
    for i, texts in enumerate(slides, 1):
        parts[f"ppt/slides/slide{i}.xml"] = "<p:sld>" + "".join(
            f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in texts) + "</p:sld>"
    return _zip(parts)


def xlsx(rows=(("Module", "Durée"), ("Formules", "3"))) -> bytes:
    shared, cells = [], []
    for r, row in enumerate(rows, 1):
        cs = []
        for c, val in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if str(val).isdigit():
                cs.append(f'<c r="{ref}"><v>{val}</v></c>')
            else:
                shared.append(val)
                cs.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        cells.append(f'<row r="{r}">{"".join(cs)}</row>')
    return _zip({
        "[Content_Types].xml": CT,
        "xl/workbook.xml": '<workbook><sheets><sheet name="Programme" sheetId="1"/></sheets></workbook>',
        "xl/sharedStrings.xml": "<sst>" + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>",
        "xl/worksheets/sheet1.xml": f'<worksheet><sheetData>{"".join(cells)}</sheetData></worksheet>',
    })


PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
       b"trailer<</Root 1 0 R>>\n%%EOF\n")

DEFAULT_SID = "d" * 32
RADIO_SID = "a" * 32


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """The real request path (middleware → route) with a chosen identity.

    People: bob@x (instance dev, opened the default session) · dave@x (instance dev) ·
    carol@x (instance viewer) · admin@x (instance admin) · alice@x and eve@x (unknown to the
    instance, dev of project `radio` through the SSO group radio-devs)."""
    from fastapi.testclient import TestClient

    import agentchat
    import app as a
    import audit
    import auth
    import board
    import documents
    import iam
    import llm
    import projects
    import simple
    import vault

    monkeypatch.setenv("SOKKAN_DATA_DIR", str(tmp_path))
    for f in ("MULTI_PROJECT", "DOCUMENTS", "SIMPLE_PROFILE"):
        monkeypatch.setenv(f"SOKKAN_FEATURE_{f}", "1")
    for v in ("SOKKAN_EDITION", "SOKKAN_PROFILE_DEFAULT", "SOKKAN_PROFILE_LOCKED",
              "SOKKAN_DOCUMENTS_MAX_MB", "SOKKAN_DOCUMENTS_MAX_PER_PROJECT",
              "SOKKAN_FEATURE_CLASSIFICATION", "SOKKAN_UIPREFS_STORE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(iam, "DB", tmp_path / "iam.db")
    iam.init(force=True)
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    for e, r, n in (("bob@x", "dev", "Bob Martin"), ("dave@x", "dev", ""), ("carol@x", "viewer", ""),
                    ("admin@x", "admin", "")):
        iam.upsert_user(e, r, n)
    monkeypatch.setattr(board, "DB", tmp_path / "board.db")
    board.init(force=True)
    monkeypatch.setattr(audit, "DB", tmp_path / "audit.db")
    monkeypatch.setattr(documents, "DB", tmp_path / "documents.db")
    monkeypatch.setattr(simple, "DB", tmp_path / "simple.db")
    monkeypatch.setattr(vault, "KEY_PATH", str(tmp_path / "vault.key"))
    monkeypatch.setattr(vault, "STORE", str(tmp_path / "vault.json"))
    monkeypatch.setattr(llm, "CONFIG", tmp_path / "llm.json")
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(agentchat, "CWD", str(ws))
    monkeypatch.setattr(agentchat, "_registry", {})
    projects.create("radio", "Radio player", created_by="admin@x")
    projects.sync_sso_groups("alice@x", ["radio-devs"])
    projects.sync_sso_groups("eve@x", ["radio-devs"])
    projects.grant("radio", "team", "sso:radio-devs", "dev")
    board.add_sdk_session(DEFAULT_SID, "docs", title="default work", project="default", owner="bob@x")
    board.add_sdk_session(RADIO_SID, "docs", title="radio work", project="radio", owner="alice@x")

    # no model in these tests: a turn is recorded, never run
    turns: list[tuple[str, str]] = []

    async def fake_turn(self, text):
        turns.append((self.sid, text))

    monkeypatch.setattr(agentchat.AgentSession, "handle_user", fake_turn)

    who = {"email": "bob@x"}
    monkeypatch.setattr(auth, "resolve_email", lambda request: who["email"])
    c = TestClient(a.app)

    def as_(email, project=None):
        who["email"] = email
        c.headers.pop("x-sokkan-project", None)
        if project:
            c.headers["x-sokkan-project"] = project
        return c

    def events(action):
        return [e for e in audit.recent(500) if e["action"] == action]

    return {"as": as_, "tmp": tmp_path, "workspace": ws, "turns": turns, "audit": events}


def put(c, name, data, **params):
    return c.put("/api/documents", params={"name": name, **params}, content=data,
                 headers={"content-type": "application/octet-stream"})
