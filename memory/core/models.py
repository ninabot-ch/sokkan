#!/usr/bin/env python3
"""models.py — memory models: registry, licence gate and first-run download.

Pure standard library and self-contained (CortHeXis memory-core; no import from
an application): it runs in the one-shot `corthexis-embed-fetch` container
(plain python image), on a host, and inside an application container
(read-only, to report the state).

The code ships no model weights. The recommended embedding model,
EmbeddingGemma-300m (Google), is distributed under the *Gemma Terms of Use*,
which are not an open-source licence: it is downloaded on first run, only after
the operator has read and accepted those terms. A refusal (or a machine that
cannot fetch it) falls back to a model under a permissive licence, with no loss
of function — recall is a bit lower.

Configuration: `CORTHEXIS_*` variables; the `SOKKAN_*` names of SOKKAN 2.x/3.0
are read as a fallback (see `env()`).

State, in the models volume (`CORTHEXIS_MODELS_DIR`, `/models` in the compose):

  licence.json   decision on the Gemma terms (accepted | declined), version of
                 the terms, date, who, how — plus the full history
  active.json    the models in use (embed, rerank) and why
  embed.env      shell fragment read by run.sh (model path, pooling)
  rerank.env     same for the reranker (absent = no reranker)
  gguf/          the downloaded files, verified against their SHA-256

CLI:
  python3 models.py setup [--interactive] [--accept | --decline] [--profile P]
  python3 models.py status
  python3 models.py terms [--lang en|fr]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- terms
# Bump GEMMA_TERMS_VERSION when Google changes either page: a recorded
# acceptance carries the version it was given for, and an outdated one is
# asked again (interactive) or kept with a warning (unattended).
GEMMA_TERMS_VERSION = "gemma-tou-2026-04-01+pup-2024-02-21"
GEMMA_TERMS_URL = "https://ai.google.dev/gemma/terms"
GEMMA_POLICY_URL = "https://ai.google.dev/gemma/prohibited_use_policy"
GEMMA_NOTICE = ("Gemma is provided under and subject to the Gemma Terms of Use "
                "found at ai.google.dev/gemma/terms")

TERMS_SUMMARY = {
    "en": f"""\
Memory: recommended model — EmbeddingGemma (Google), 330 MB

This is the engine that lets your agents find the right information in your
memory (about one time in three more often than the basic model). It runs
entirely on your machine; no data is sent to Google.

It is free, commercial use included, but it is not open source: Google allows
it under its Gemma Terms of Use, which forbid certain uses (illegal activities,
privacy violations, disinformation…) and which Google may update. Google claims
no rights over the outputs (your vectors are yours). By downloading it, you
accept these terms.

  Terms:          {GEMMA_TERMS_URL}
  Use policy:     {GEMMA_POLICY_URL}

If you decline, a model under the MIT licence is used instead: everything
works, recall is a little lower. You can change your mind later.""",
    "fr": f"""\
Mémoire : modèle recommandé — EmbeddingGemma (Google), 330 Mo

C'est le moteur qui permet à vos agents de retrouver la bonne information dans
votre mémoire (environ 1 fois sur 3 de plus que le modèle de base). Il tourne
entièrement sur votre machine ; aucune donnée n'est envoyée à Google.

Il est gratuit, usage commercial compris, mais il n'est pas open source :
Google l'autorise sous ses Gemma Terms of Use, qui interdisent certains usages
(activités illégales, atteinte à la vie privée, désinformation…) et que Google
peut faire évoluer. Google ne revendique aucun droit sur les sorties (vos
vecteurs vous appartiennent). En le téléchargeant, vous acceptez ces conditions.

  Conditions :          {GEMMA_TERMS_URL}
  Politique d'usage :   {GEMMA_POLICY_URL}

Si vous refusez, un modèle sous licence MIT est utilisé à la place : tout
fonctionne, le rappel est un peu plus faible. Vous pourrez changer d'avis.""",
}

# --------------------------------------------------------------------------- registry
# Files are pinned to a repository commit and checked against their SHA-256.
# Quality figures (MRR, 300-question bench, hybrid search) and the lexical
# weight that suits each model come from the memory bench of 03.10.2026.
HF = "https://huggingface.co"

MODELS: dict[str, dict] = {
    "embeddinggemma-300m-q8": {
        "kind": "embed", "label": "EmbeddingGemma-300m (Q8_0)", "publisher": "Google",
        "licence": "gemma", "licence_url": GEMMA_TERMS_URL,
        "repo": "ggml-org/embeddinggemma-300M-GGUF",
        "rev": "0f741b5a6585bd53aeb15cd1372c56f2a0f65e12",
        "file": "embeddinggemma-300M-Q8_0.gguf",
        "sha256": "b5ce9d77a3fc4b3b39ccb5643c36777911cc4eb46a66962eadfa3f5f60490d63",
        "size": 333590944,
        "dim": 768, "pooling": "mean",
        "query_prefix": "task: search result | query: ",
        "doc_prefix": "title: none | text: ",
        "lexical_weight": 0.3, "mrr": 0.82,
    },
    # MIT on its model card. NB: same architecture and vocabulary as Gemma 3
    # 270m (likely a fine-tune of it) — see the README, "Licences", before
    # relying on it as a way out of the Gemma terms; multilingual-e5-base
    # (XLM-R lineage) is the fallback without that question.
    "harrier-oss-v1-270m-q8": {
        "kind": "embed", "label": "harrier-oss-v1-270m (Q8_0)", "publisher": "Microsoft",
        "licence": "mit", "licence_url": f"{HF}/microsoft/harrier-oss-v1-270m",
        "repo": "mykor/harrier-oss-v1-270m-GGUF",
        "rev": "fe07a2a15994730f1b8f13943927bc8198bd7fa7",
        "file": "harrier-oss-v1-270M-Q8_0.gguf",
        "sha256": "fe12f3583dbbb832def4cffeb46c0d0ab49a3288542d5cdbaeb1741315a01b87",
        "size": 291544768,
        "dim": 640, "pooling": "last",
        "query_prefix": ("Instruct: Given a web search query, retrieve relevant passages "
                         "that answer the query\nQuery: "),
        "doc_prefix": "",
        "lexical_weight": 0.2, "mrr": 0.77,
    },
    "multilingual-e5-base-q8": {
        "kind": "embed", "label": "multilingual-e5-base (Q8_0)", "publisher": "Microsoft",
        "licence": "mit", "licence_url": f"{HF}/intfloat/multilingual-e5-base",
        "repo": "dinab/multilingual-e5-base-Q8_0-GGUF",
        "rev": "b96477725c04afcdc7c8bba980c18e682a80f462",
        "file": "multilingual-e5-base-q8_0.gguf",
        "sha256": "548c31b068947aa26b86c8bbfc1f2fabe5233f6d0e1241319832b20a01e5968a",
        "size": 303138624,
        "dim": 768, "pooling": "mean",
        "query_prefix": "query: ", "doc_prefix": "passage: ",
        "lexical_weight": 0.1, "mrr": 0.74,
    },
    "qwen3-reranker-0.6b-q8": {
        "kind": "rerank", "label": "Qwen3-Reranker-0.6B (Q8_0)", "publisher": "Alibaba Qwen",
        "licence": "apache-2.0", "licence_url": f"{HF}/Qwen/Qwen3-Reranker-0.6B",
        "repo": "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF",
        "rev": "a02f48bb4f057028298c21fa033da2b30d7742d5",
        "file": "qwen3-reranker-0.6b-q8_0.gguf",
        "sha256": "22c9979ce4fbcdc5acdc310c6641c32797eff1aa980b8f7a2db8a8ea23429a48",
        "size": 639153184,
    },
    "bge-reranker-v2-m3-q8": {
        "kind": "rerank", "label": "bge-reranker-v2-m3 (Q8_0)", "publisher": "BAAI",
        "licence": "apache-2.0", "licence_url": f"{HF}/BAAI/bge-reranker-v2-m3",
        "repo": "gpustack/bge-reranker-v2-m3-GGUF",
        "rev": "3093af03b1a635e67b084b1d8c03c5f5e020fd05",
        "file": "bge-reranker-v2-m3-Q8_0.gguf",
        "sha256": "a43c7c9b11a4c1517e5bf95151960e1621d1b72f7a493364b01e386cf1aaa1d3",
        "size": 635676416,
    },
}

DEFAULT_EMBED = "embeddinggemma-300m-q8"
DEFAULT_FALLBACK = "harrier-oss-v1-270m-q8"
# Reranker per memory profile (None = no reranker). Standard: CPU, called only
# from background work (4-5 s for a top 10); GPU: interactive top 10.
PROFILE_RERANKER = {"leger": None, "standard": "bge-reranker-v2-m3-q8",
                    "gpu": "qwen3-reranker-0.6b-q8"}

_TRUE = ("1", "true", "yes", "y", "accept", "accepted")
_FALSE = ("0", "false", "no", "n", "decline", "declined")


def env(name: str, default: str | None = None, *aliases: str) -> str | None:
    """CORTHEXIS_<name>, else SOKKAN_<name>, else the given aliases, else default.
    An empty value counts as unset."""
    for var in (f"CORTHEXIS_{name}", f"SOKKAN_{name}", *aliases):
        v = os.environ.get(var)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def env_name(name: str, *aliases: str) -> str | None:
    """Which variable `env(name)` read (for logs and the audit trail)."""
    for var in (f"CORTHEXIS_{name}", f"SOKKAN_{name}", *aliases):
        if (os.environ.get(var) or "").strip():
            return var
    return None


def models_dir() -> Path:
    d = env("MODELS_DIR")
    if d:
        return Path(d)
    data = env("DATA_DIR")
    if data:
        return Path(data) / "memory-models"
    return Path(os.path.expanduser("~/.local/share/corthexis/models"))


def fallback_key() -> str:
    key = env("EMBED_FALLBACK", DEFAULT_FALLBACK)
    spec = MODELS.get(key)
    if not spec or spec["kind"] != "embed" or spec["licence"] == "gemma":
        raise ValueError(f"CORTHEXIS_EMBED_FALLBACK={key!r}: not a non-Gemma embedding model")
    return key


def url_of(key: str) -> str:
    spec = MODELS[key]
    base = env("MODEL_BASE_URL", HF).rstrip("/")
    return f"{base}/{spec['repo']}/resolve/{spec['rev']}/{spec['file']}"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _write_atomic(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


# --------------------------------------------------------------------------- licence
def licence_state(root: Path | None = None) -> dict:
    """{"decision": accepted|declined|None, "terms_version", "at", "by", "via",
    "current": bool (given for the current terms version), "history": [...]}"""
    st = _read_json((root or models_dir()) / "licence.json").get("gemma") or {}
    out = {"decision": st.get("decision"), "terms_version": st.get("terms_version"),
           "at": st.get("at"), "by": st.get("by"), "via": st.get("via"),
           "history": st.get("history") or []}
    out["current"] = bool(out["decision"]) and out["terms_version"] == GEMMA_TERMS_VERSION
    return out


def record_decision(decision: str, by: str, via: str, root: Path | None = None) -> dict:
    """Journalise une décision sur les Gemma Terms (append-only history)."""
    if decision not in ("accepted", "declined"):
        raise ValueError(decision)
    root = root or models_dir()
    p = root / "licence.json"
    doc = _read_json(p)
    prev = doc.get("gemma") or {}
    entry = {"decision": decision, "terms_version": GEMMA_TERMS_VERSION, "at": _now(),
             "by": by, "via": via}
    doc["schema"] = 1
    doc["gemma"] = {**entry, "terms_url": GEMMA_TERMS_URL, "policy_url": GEMMA_POLICY_URL,
                    "history": [*(prev.get("history") or []), entry]}
    _write_atomic(p, json.dumps(doc, indent=2) + "\n")
    return licence_state(root)


# --------------------------------------------------------------------------- download
def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verified(key: str, root: Path | None = None) -> Path | None:
    """Chemin du fichier s'il est présent ET conforme à son SHA-256. Le hash d'un
    fichier déjà vérifié est mémorisé (taille + mtime) pour ne pas relire 300 Mo
    à chaque démarrage."""
    spec = MODELS[key]
    p = (root or models_dir()) / "gguf" / spec["file"]
    if not p.is_file():
        return None
    st = p.stat()
    if st.st_size != spec["size"]:
        return None
    stamp = p.with_name(p.name + ".sha256")
    sig = f"{spec['sha256']} {st.st_size} {int(st.st_mtime)}"
    try:
        if stamp.read_text().strip() == sig:
            return p
    except OSError:
        pass
    if _sha256_file(p) != spec["sha256"]:
        return None
    try:
        stamp.write_text(sig + "\n")
    except OSError:
        pass
    return p


def download(key: str, root: Path | None = None, log=print, timeout: float = 60.0) -> Path:
    """Télécharge (ou réutilise) le GGUF `key`, vérifie son SHA-256. Un fichier
    déposé à la main dans gguf/ (machine hors ligne) est accepté s'il est conforme."""
    root = root or models_dir()
    spec = MODELS[key]
    ok = verified(key, root)
    if ok:
        return ok
    dest = root / "gguf" / spec["file"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    url = url_of(key)
    log(f"downloading {spec['label']} ({spec['size'] / 1e6:.0f} MB) from {url}")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "corthexis-models"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as out:
        done, step = 0, max(spec["size"] // 10, 1)
        while True:
            block = resp.read(1 << 20)
            if not block:
                break
            out.write(block)
            h.update(block)
            done += len(block)
            if done // step != (done - len(block)) // step:
                log(f"  {min(100, 100 * done // spec['size'])} %")
    if h.hexdigest() != spec["sha256"]:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{spec['file']}: SHA-256 mismatch — file discarded")
    os.replace(part, dest)
    st = dest.stat()
    dest.with_name(dest.name + ".sha256").write_text(
        f"{spec['sha256']} {st.st_size} {int(st.st_mtime)}\n")
    return dest


# --------------------------------------------------------------------------- selection
def active(root: Path | None = None) -> dict:
    return _read_json((root or models_dir()) / "active.json")


def _env_fragment(key: str, path: Path) -> str:
    spec = MODELS[key]
    lines = [f"CORTHEXIS_MODEL_KEY={key}", f"CORTHEXIS_MODEL_FILE=gguf/{path.name}"]
    if spec["kind"] == "embed":
        lines.append(f"CORTHEXIS_MODEL_POOLING={spec['pooling']}")
    return "\n".join(lines) + "\n"


def _write_active(root: Path, embed: str | None, embed_path: Path | None,
                  rerank: str | None, rerank_path: Path | None, reason: str) -> dict:
    doc = {"schema": 1, "embed": embed, "rerank": rerank, "reason": reason, "updated_at": _now(),
           "dim": MODELS[embed]["dim"] if embed else None,
           "licence": MODELS[embed]["licence"] if embed else None}
    _write_atomic(root / "active.json", json.dumps(doc, indent=2) + "\n")
    for kind, key, path in (("embed", embed, embed_path), ("rerank", rerank, rerank_path)):
        f = root / f"{kind}.env"
        if key and path:
            _write_atomic(f, _env_fragment(key, path))
        else:
            f.unlink(missing_ok=True)
    return doc


def _ask(log) -> str:
    for lang in ("en", "fr"):
        log("\n" + TERMS_SUMMARY[lang] + "\n")
    while True:
        try:
            a = input("[a] I accept and download / J'accepte et je télécharge\n"
                      "[m] Use the MIT model instead / Utiliser plutôt le modèle libre MIT\n> ")
        except EOFError:
            return "undecided"
        a = a.strip().lower()
        if a in ("a", "accept", "accepte", "j"):
            return "accepted"
        if a in ("m", "mit", "d", "decline", "refuse"):
            return "declined"


def _who() -> str:
    who = env("OWNER", "", "SOKKAN_OWNER_EMAIL") or ""
    if not who:
        try:
            who = getpass.getuser()
        except Exception:  # noqa: BLE001
            who = "unknown"
    return who


def decide(interactive: bool = False, forced: str | None = None, root: Path | None = None,
           log=print) -> str:
    """Décision effective sur les Gemma Terms : accepted | declined | undecided.

    Ordre : décision explicite (--accept/--decline) > décision journalisée pour la
    version courante des conditions > question à l'opérateur (interactif) >
    CORTHEXIS_ACCEPT_GEMMA_TERMS (installation sans surveillance, explicite) >
    décision journalisée pour une version antérieure (gardée, avec un avertissement)
    > undecided (le repli MIT tourne, l'UI pourra poser la question)."""
    root = root or models_dir()
    if forced:
        return record_decision(forced, _who(), "cli", root)["decision"]
    st = licence_state(root)
    if st["current"]:
        return st["decision"]
    if interactive:
        d = _ask(log)
        if d != "undecided":
            return record_decision(d, _who(), "installer", root)["decision"]
    flag = (env("ACCEPT_GEMMA_TERMS") or "").lower()
    if flag in _TRUE or flag in _FALSE:
        return record_decision("accepted" if flag in _TRUE else "declined", _who(),
                               f"env:{env_name('ACCEPT_GEMMA_TERMS')}", root)["decision"]
    if st["decision"]:
        log(f"note: the Gemma terms changed since they were {st['decision']} "
            f"({st['terms_version']} → {GEMMA_TERMS_VERSION}); keeping that decision")
        return st["decision"]
    return "undecided"


def setup(profile: str | None = None, interactive: bool = False, forced: str | None = None,
          root: Path | None = None, log=print, fetch=download) -> dict:
    """Premier lancement (idempotent) : décision de licence, téléchargement,
    sélection. Ne lève pas : sans aucun modèle, active.json dit pourquoi et la
    recherche reste lexicale."""
    root = root or models_dir()
    profile = (profile or env("MEMORY_PROFILE")
               or ("remote" if env("ML_SERVICE_URL", None, "ML_SERVICE_URL") else "leger"))
    if profile in ("legacy", "remote"):
        log(f"memory profile {profile}: 2.x embeddings, no model to install")
        return {"embed": None, "rerank": None, "reason": f"profile-{profile}", "errors": [],
                "decision": licence_state(root)["decision"]}
    decision = decide(interactive, forced, root, log)
    candidates = [DEFAULT_EMBED, fallback_key()] if decision == "accepted" else [fallback_key()]
    embed = embed_path = None
    errors = []
    for key in candidates:
        try:
            embed_path, embed = fetch(key, root, log=log), key
            break
        except Exception as e:  # noqa: BLE001 — hors ligne, hash faux, disque plein…
            errors.append(f"{key}: {e}")
            log(f"! {MODELS[key]['label']} unavailable: {e}")
    rerank = rerank_path = None
    rr_key = env("RERANK_MODEL") or PROFILE_RERANKER.get(profile)
    if rr_key and (env("RERANK", "1") or "").lower() not in _FALSE and rr_key in MODELS:
        try:
            rerank_path, rerank = fetch(rr_key, root, log=log), rr_key
        except Exception as e:  # noqa: BLE001 — le reranker est optionnel
            errors.append(f"{rr_key}: {e}")
            log(f"! reranker unavailable ({e}) — search keeps the hybrid order")
    if embed == DEFAULT_EMBED:
        reason = "gemma-terms-accepted"
    elif embed:
        reason = {"declined": "gemma-terms-declined",
                  "undecided": "gemma-terms-undecided"}.get(decision, "gemma-unavailable")
    else:
        reason = "no-model-available"
    doc = _write_active(root, embed, embed_path, rerank, rerank_path, reason)
    doc["errors"] = errors
    doc["decision"] = decision
    if embed:
        log(f"memory model: {MODELS[embed]['label']} ({reason})"
            + (f", reranker: {MODELS[rerank]['label']}" if rerank else ""))
        if MODELS[embed]["licence"] == "gemma":
            log(GEMMA_NOTICE)
    else:
        log("! no embedding model could be installed — memory search stays lexical-only "
            "until `models.py setup` succeeds")
    return doc


def present(key: str, root: Path | None = None) -> bool:
    """Cheap check (name + size), for status pages; `verified` checks the hash."""
    p = (root or models_dir()) / "gguf" / MODELS[key]["file"]
    try:
        return p.stat().st_size == MODELS[key]["size"]
    except OSError:
        return False


def status(root: Path | None = None, verify: bool = False) -> dict:
    root = root or models_dir()
    act = active(root)
    check = verified if verify else present
    files = {k: bool(check(k, root)) for k in MODELS}
    return {"models_dir": str(root), "active": act, "licence": licence_state(root),
            "terms_version": GEMMA_TERMS_VERSION, "installed": files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="models.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="licence decision + download + selection (idempotent)")
    s.add_argument("--interactive", action="store_true", help="ask the operator (needs a TTY)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--accept", action="store_true", help="accept the Gemma Terms of Use")
    g.add_argument("--decline", action="store_true", help="decline them (MIT model)")
    s.add_argument("--profile", choices=[*sorted(PROFILE_RERANKER), "legacy", "remote"],
                   help="memory profile")
    s.add_argument("--strict", action="store_true", help="exit 1 when no model is installed")
    sub.add_parser("status", help="print the state as JSON")
    t = sub.add_parser("terms", help="print the summary of the Gemma terms")
    t.add_argument("--lang", choices=sorted(TERMS_SUMMARY), default="en")
    a = ap.parse_args(argv)
    if a.cmd == "terms":
        print(TERMS_SUMMARY[a.lang])
        return 0
    if a.cmd == "status":
        print(json.dumps(status(verify=True), indent=2))
        return 0
    forced = "accepted" if a.accept else "declined" if a.decline else None
    interactive = a.interactive and sys.stdin.isatty()
    doc = setup(a.profile, interactive, forced, log=lambda m: print(m, flush=True))
    return 1 if a.strict and not doc.get("embed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
