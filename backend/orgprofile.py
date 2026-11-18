#!/usr/bin/env python3
"""orgprofile.py — 4.0: the organisation's profile, given to every work (« the coworker knows
the house »).

The audit of 17.11.2026 (nine personas, one fictional SME) found the same root cause behind
« to complete: company name, address », a certificate signed by « {signatory} », « T. Bovet »
turned into « Théo Bovet », a register « under article 30 of the GDPR » for a Swiss company and
« 8 120,50 CHF »: the agent did not know whom it worked for. The administrator fills this
profile once (Settings › Organisation, or the « organisation » step of the first launch); it
is turned into ONE stable block of context put in front of every work of the Atelier:

* the legacy engine: in its system prompt (cached with the rest of the prefix, never part of
  the conversation);
* the kernel: the `project` block of the host contract (`docs/…/v4-host-contract`), sent today
  in front of the first turn of a thread (the kernel's host API has no head block yet).

What is sensitive (the IBAN) is only in the block of a work that needs it: a payment, an
invoice, a reminder, an offer (`needs_bank`). The logo is kept as a file for the documents
(`logo_path`), never put in a prompt.

Storage: ``<data dir>/orgprofile.json`` (0600) and ``<data dir>/orgprofile-logo.<ext>``. The
organisation's NAME stays where the instance keeps it (`instance.org_name`, the onboarding
step writes it there): one place, read here.
"""
from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from pathlib import Path

_lock = threading.Lock()

LANGS = ("fr", "en", "de", "it")
ADDRESS = ("vous", "tu")
FORMATS = ("ch", "fr", "de", "en")
MAX_PEOPLE = 40
MAX_SIGNATORIES = 6
LOGO_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml"}
MAX_LOGO = 512 * 1024

# the fields of the profile and their bound (characters); a list field has its own shape
TEXT_FIELDS = {
    "legal_name": 120, "legal_form": 40, "street": 120, "postcode": 12, "city": 60,
    "country": 2, "uid": 20, "vat": 30, "iban": 42, "bank": 80, "phone": 30, "email": 120,
    "website": 120, "lang": 2, "address_form": 4, "formats": 2, "tone": 300, "sector": 120,
}
# what a block never holds unless the work needs it
SENSITIVE = ("iban", "bank")

COUNTRIES = {"CH": ("Suisse", "Switzerland"), "FR": ("France", "France"),
             "DE": ("Allemagne", "Germany"), "AT": ("Autriche", "Austria"),
             "IT": ("Italie", "Italy"), "LI": ("Liechtenstein", "Liechtenstein"),
             "BE": ("Belgique", "Belgium"), "LU": ("Luxembourg", "Luxembourg")}


def _data_dir() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def store_path() -> Path:
    return _data_dir() / "orgprofile.json"


def _load() -> dict:
    try:
        d = json.loads(store_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save(d: dict) -> None:
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


# ---- checks -------------------------------------------------------------------------------
def _clean(s, n: int) -> str:
    s = unicodedata.normalize("NFC", str(s or ""))
    s = "".join(ch for ch in s if ch == "\n" or ch >= " ")
    return " ".join(s.split())[:n]


def iban_ok(iban: str) -> bool:
    """ISO 13616 check (mod 97); a Swiss/Liechtenstein IBAN is 21 characters."""
    s = re.sub(r"\s+", "", iban or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s):
        return False
    if s[:2] in ("CH", "LI") and len(s) != 21:
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def iban_display(iban: str) -> str:
    s = re.sub(r"\s+", "", iban or "").upper()
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


def iban_masked(iban: str) -> str:
    s = re.sub(r"\s+", "", iban or "").upper()
    return f"{s[:4]} •••• {s[-4:]}" if len(s) > 8 else ""


def uid_ok(uid: str) -> bool:
    """Swiss company number CHE-123.456.789 (format and mod-11 check digit)."""
    m = re.fullmatch(r"CHE-?(\d{3})\.?(\d{3})\.?(\d{3})", (uid or "").strip().upper())
    if not m:
        return False
    d = "".join(m.groups())
    s = sum(int(x) * w for x, w in zip(d[:8], (5, 4, 3, 2, 7, 6, 5, 4)))
    c = 11 - s % 11
    c = 0 if c == 11 else c
    return c != 10 and c == int(d[8])


def uid_display(uid: str) -> str:
    m = re.fullmatch(r"CHE-?(\d{3})\.?(\d{3})\.?(\d{3})", (uid or "").strip().upper())
    return f"CHE-{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else (uid or "").strip()


class ProfileError(ValueError):
    def __init__(self, field: str, code: str):
        super().__init__(f"{field}: {code}")
        self.field, self.code = field, code


def _person(x: dict, with_initials: bool) -> dict | None:
    if not isinstance(x, dict):
        return None
    name = _clean(x.get("name"), 80)
    if not name:
        return None
    out = {"name": name, "function": _clean(x.get("function"), 100)}
    if with_initials:
        ini = _clean(x.get("initials"), 6).replace(" ", "")
        if not ini:
            ini = "".join(w[0] for w in re.split(r"[\s-]+", name) if w)[:4].upper()
        out["initials"] = ini
        out["email"] = _clean(x.get("email"), 120).lower()
        out["team"] = _clean(x.get("team"), 60)
    return out


def normalize(body: dict) -> dict:
    """What the administrator sent → the stored profile (bounded, checked). Raises
    ProfileError(field, code) on a value that would end up wrong in a document."""
    out: dict = {}
    for k, n in TEXT_FIELDS.items():
        if k in body and body[k] is not None:
            out[k] = _clean(body[k], n)
    if out.get("country"):
        out["country"] = out["country"].upper()
        if not re.fullmatch(r"[A-Z]{2}", out["country"]):
            raise ProfileError("country", "iso2")
    if out.get("lang") and out["lang"] not in LANGS:
        raise ProfileError("lang", "unknown")
    if out.get("address_form") and out["address_form"] not in ADDRESS:
        raise ProfileError("address_form", "vous|tu")
    if out.get("formats") and out["formats"] not in FORMATS:
        raise ProfileError("formats", "unknown")
    if out.get("iban"):
        if not iban_ok(out["iban"]):
            raise ProfileError("iban", "invalid")
        out["iban"] = iban_display(out["iban"])
    if out.get("uid"):
        if not uid_ok(out["uid"]):
            raise ProfileError("uid", "invalid")
        out["uid"] = uid_display(out["uid"])
    if "signatories" in body:
        out["signatories"] = [p for p in (_person(x, False) for x in (body["signatories"] or []))
                              if p][:MAX_SIGNATORIES]
    if "people" in body:
        seen, people = set(), []
        for p in (_person(x, True) for x in (body["people"] or [])):
            if p and p["name"].lower() not in seen:
                seen.add(p["name"].lower())
                people.append(p)
        out["people"] = people[:MAX_PEOPLE]
    return out


# ---- read / write -------------------------------------------------------------------------
def _org_name() -> str:
    try:
        import instance
        raw = (instance._load().get("org_name") or os.environ.get("SOKKAN_ORG_NAME") or "").strip()
        return raw
    except Exception:  # noqa: BLE001
        return os.environ.get("SOKKAN_ORG_NAME", "").strip()


def get(reveal: bool = False) -> dict:
    """The profile as stored, the organisation's name included. ``reveal=False`` masks the
    IBAN (only an administrator sees it whole)."""
    d = _load()
    p = {k: d.get(k, "") for k in TEXT_FIELDS}
    p["signatories"] = list(d.get("signatories") or [])
    p["people"] = list(d.get("people") or [])
    p["name"] = _org_name()
    p["country"] = p["country"] or "CH"
    p["formats"] = p["formats"] or ("ch" if p["country"] == "CH" else "")
    p["address_form"] = p["address_form"] or "vous"
    if not p["lang"]:
        try:
            import onboarding  # the first launch's language, when that lot is in
            p["lang"] = onboarding.org_lang() or "fr"
        except Exception:  # noqa: BLE001
            p["lang"] = "fr"
    lp = logo_path()
    p["logo"] = lp is not None
    p["logo_v"] = int(lp.stat().st_mtime) if lp else 0   # 4.0.1: the screens' cache key
    p["updated_at"] = d.get("updated_at")
    p["updated_by"] = d.get("updated_by", "")
    if not reveal and p.get("iban"):
        p["iban"] = iban_masked(p["iban"])
        p["iban_masked"] = True
    return p


def update(body: dict, by: str) -> dict:
    """Merge what was sent (a field absent stays as it is; « "" » empties it). The name goes
    to `instance` (the one place it is kept)."""
    import time
    body = dict(body or {})
    if body.get("iban") and "•" in str(body["iban"]):
        body.pop("iban")                   # the masked value sent back unchanged: keep it
    clean = normalize(body)
    with _lock:
        d = _load()
        d.update(clean)
        d["updated_at"] = time.time()
        d["updated_by"] = (by or "").lower().strip()
        _save(d)
    name = _clean(body.get("name"), 80)
    if name:
        set_name(name)
    return get(reveal=True)


def set_name(name: str) -> str:
    """THE way to name the organisation (Settings › Organisation, the first-launch step, the
    cockpit's /api/instance): `instance.json` holds it; the common project, named after the
    organisation (or the owner) at its creation, follows unless somebody renamed it since."""
    import auth
    import instance
    import projects
    name = (name or "").strip()[:80]
    if not name:
        return instance.info()["org_name"]
    before = instance.info()["org_name"]
    instance.set_org_name(name)
    d = projects.get(projects.DEFAULT_PROJECT)
    if d and d.get("name") in (before, "Default project", auth.OWNER_NAME):
        projects.update(projects.DEFAULT_PROJECT, name=name)
    return instance.info()["org_name"]


def completeness() -> dict:
    """What a work will know and what it will not (the onboarding step and Settings show it)."""
    p = get(reveal=True)
    want = {"name": p["name"], "legal_name": p["legal_name"] or p["name"],
            "address": p["street"] and p["city"], "uid": p["uid"], "iban": p["iban"],
            "signatories": p["signatories"], "people": p["people"]}
    filled = [k for k, v in want.items() if v]
    return {"filled": filled, "missing": [k for k in want if k not in filled],
            "ready": bool(p["name"] and p["street"] and p["city"] and p["people"])}


# ---- logo ---------------------------------------------------------------------------------
def document_logo() -> bytes | None:
    """The logo as Word and PDF can carry it (PNG / JPG; an SVG stays on screen only)."""
    p = logo_path()
    if p is None or p.suffix.lower().lstrip(".") not in ("png", "jpg", "jpeg"):
        return None
    return p.read_bytes()


def logo_path() -> Path | None:
    for ext in LOGO_EXT:
        p = _data_dir() / f"orgprofile-logo.{ext}"
        if p.is_file():
            return p
    return None


def save_logo(data: bytes, filename: str) -> Path:
    ext = (filename.rsplit(".", 1)[-1] if "." in (filename or "") else "").lower()
    if ext not in LOGO_EXT:
        raise ProfileError("logo", "type")
    if not data or len(data) > MAX_LOGO:
        raise ProfileError("logo", "size")
    magic = {"png": data[:8] == b"\x89PNG\r\n\x1a\n", "jpg": data[:3] == b"\xff\xd8\xff",
             "jpeg": data[:3] == b"\xff\xd8\xff",
             "svg": b"<svg" in data[:2048] and b"<script" not in data.lower()}
    if not magic[ext]:
        raise ProfileError("logo", "content")
    drop_logo()
    p = _data_dir() / f"orgprofile-logo.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def drop_logo() -> None:
    for ext in LOGO_EXT:
        try:
            (_data_dir() / f"orgprofile-logo.{ext}").unlink()
        except FileNotFoundError:
            pass


# ---- the block of context -----------------------------------------------------------------
_BANK_RX = re.compile(
    r"factur|relanc|rappel|paiement|payer|virement|iban|bancaire|banque|offre|devis|"
    r"commande|acompte|créance|encaiss|décompte|invoice|remind|payment|transfer|bank|quote|"
    r"offer|order|dunning|rechnung|mahnung|zahlung", re.I)
BANK_PLAYBOOK_PREFIXES = ("finance.", "sales.")


def needs_bank(ask: str = "", playbook: str = "") -> bool:
    """Does this work put the bank details in a document? (an invoice, a reminder, an offer)"""
    return bool(playbook and playbook.startswith(BANK_PLAYBOOK_PREFIXES)) or \
        bool(_BANK_RX.search(ask or ""))


def _fmt_rules(p: dict) -> list[str]:
    f = p.get("formats")
    if f == "ch":
        return ["- Formats: amounts « 1'234.50 CHF » (apostrophe for thousands, point for decimals), "
                "dates « 14.01.2027 », times « 14:30 »."]
    if f == "fr":
        return ["- Formats: amounts « 1 234,50 € », dates « 14/01/2027 »."]
    if f == "de":
        return ["- Formats: amounts « 1.234,50 € », dates « 14.01.2027 »."]
    if f == "en":
        return ["- Formats: amounts « 1,234.50 », dates « 14 January 2027 »."]
    return []


def _law_rules(country: str) -> list[str]:
    if country == "CH":
        return ["- Swiss organisation: data protection is the Swiss FADP (« nLPD », in force since "
                "1 September 2023), NOT the GDPR; employment and contracts follow the Swiss Code of "
                "Obligations (CO). Never cite an article number you have not read in a source: write "
                "« article to verify » instead."]
    return []


def _who(p: dict, email: str, display: str) -> str:
    e = (email or "").lower().strip()
    hit = next((x for x in p["people"] if x.get("email") and x["email"] == e), None)
    if hit is None and display:
        hit = next((x for x in p["people"] if x["name"].lower() == display.lower()), None)
    if hit is not None:
        return hit["name"] + (f", {hit['function']}" if hit.get("function") else "")
    return display


def block(*, email: str = "", display_name: str = "", ask: str = "", playbook: str = "",
          bank: bool | None = None) -> str:
    """The block of context of one work ('' when the profile is empty). Stable for a given
    profile, person and need of bank details: it sits in a cached prefix."""
    p = get(reveal=True)
    name = p["name"]
    if not (name or p["legal_name"] or p["people"]):
        return ""
    lines = ["<sokkan-house>",
             "THE ORGANISATION YOU WORK FOR — set by its administrator. Use these facts exactly "
             "as written: never ask for them, never leave a « to complete » for them, never "
             "change a name."]
    legal = p["legal_name"] or name
    form = f" ({p['legal_form']})" if p["legal_form"] and p["legal_form"] not in legal else ""
    lines.append(f"- Name: {legal}{form}" + (f" — known as « {name} »"
                                               if name and name != legal else ""))
    if p["sector"]:
        lines.append(f"- Activity: {p['sector']}")
    ctry = COUNTRIES.get(p["country"], (p["country"], p["country"]))
    addr = ", ".join(x for x in (p["street"], " ".join(x for x in (p["postcode"], p["city"]) if x),
                                 ctry[0] if p["lang"] == "fr" else ctry[1]) if x)
    if p["street"] or p["city"]:
        lines.append(f"- Address: {addr}")
    contact = " · ".join(x for x in (p["phone"], p["email"], p["website"]) if x)
    if contact:
        lines.append(f"- Contact: {contact}")
    ids = " · ".join(x for x in ((f"UID/IDE {p['uid']}" if p["uid"] else ""),
                                 (f"VAT/TVA {p['vat']}" if p["vat"] else "")) if x)
    if ids:
        lines.append(f"- Company numbers: {ids}")
    if bank is None:
        bank = needs_bank(ask, playbook)
    if p["iban"]:
        if bank:
            lines.append(f"- Bank details (for a document that asks for a payment): IBAN {p['iban']}"
                         + (f", {p['bank']}" if p["bank"] else ""))
        else:
            lines.append("- Bank details: kept by the organisation, not needed for this work "
                         "(do not invent any).")
    if p["signatories"]:
        lines.append("- Who signs for the organisation: " + "; ".join(
            s["name"] + (f" ({s['function']})" if s.get("function") else "") for s in p["signatories"]))
    if p["people"]:
        lines.append("- Who does what — EXACT names. Initials or a first name in a document mean "
                     "these people; never complete or change a first name (for example "
                     + _example(p["people"]) + "):")
        for x in p["people"]:
            extra = ", ".join(v for v in (x.get("function"), x.get("team")) if v)
            lines.append(f"  - {x['initials']} = {x['name']}" + (f" — {extra}" if extra else ""))
    lines += _law_rules(p["country"])
    lang_name = {"fr": "French", "en": "English", "de": "German", "it": "Italian"}.get(p["lang"], "")
    writing = []
    if lang_name:
        writing.append(f"documents in {lang_name} unless asked otherwise")
    if p["address_form"] == "vous" and p["lang"] in ("fr", ""):
        writing.append("always « vous » to the person and in documents (never « tu »)")
    elif p["address_form"] == "tu":
        writing.append("« tu » inside the organisation")
    if writing:
        lines.append("- Writing: " + "; ".join(writing) + ".")
    if p["tone"]:
        lines.append(f"- House tone: {p['tone']}")
    lines += _fmt_rules(p)
    me = _who(p, email, display_name) if email or display_name else ""
    if me:
        lines.append(f"- You are working with: {me}. Do not ask for their own name.")
    lines.append("</sokkan-house>")
    return "\n".join(lines)


def person_line(email: str, display_name: str = "") -> str:
    """« You are working with: Sophie Chevalley, Assistante de direction » — for the FIRST
    message of a work, not for the head: the head is then the same for everyone of a project,
    and the model's cached prefix with it (measured: a head that names the person costs the
    whole prefix — tools included — uncached at each person's first work)."""
    p = get(reveal=False)
    me = _who(p, email, display_name)
    if not me:
        return ""
    return f"<sokkan-brief>You are working with: {me}. Do not ask for their own name.</sokkan-brief>"


def _example(people: list[dict]) -> str:
    """« T. Bovet » is « Thomas Bovet », with the profile's own first person whose name has two
    words (the rule reads better with a real case); a neutral sentence otherwise."""
    for x in people:
        parts = x["name"].split()
        if len(parts) >= 2:
            return f"« {parts[0][0]}. {' '.join(parts[1:])} » is « {x['name']} »"
    return "the person listed, never another first name"
