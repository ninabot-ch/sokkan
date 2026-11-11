"""extract_worker.py — the child process that opens documents (never the API process).

Protocol: one JSON request per line on stdin `{"path": …, "max_chars": …, "docling": …}`,
one JSON answer per line on stdout. A parser that hangs, crashes or eats memory takes this
child down, not the pass: the parent (extract.py) kills it on timeout and starts another.

* MarkItDown (MIT) reads Office, PDF with a text layer, HTML, CSV… — `convert_local` only:
  a path is never interpreted as a URL, nothing is fetched.
* Docling (MIT) is the fallback for PDFs MarkItDown gets almost nothing from (scans,
  complex layouts). Optional: without it such a PDF is recorded as `needs_ocr`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile

PLAIN = (".md", ".txt", ".csv")
OOXML = (".docx", ".pptx", ".xlsx")
MIN_PDF_CHARS = 200          # below: the PDF has no usable text layer
MIN_PDF_CHARS_PER_PAGE = 80
ZIP_MAX_RATIO = 200          # uncompressed / compressed
ZIP_MAX_BYTES = 400 * 1024 * 1024

_md = None
_docling = None


def _markitdown():
    global _md
    if _md is None:
        from markitdown import MarkItDown
        _md = MarkItDown(enable_plugins=False)
    return _md


def docling_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("docling") is not None
    except Exception:  # noqa: BLE001
        return False


def _zip_guard(path: str) -> str | None:
    """An Office file is a zip: refuse a decompression bomb before any parser sees it."""
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(i.file_size for i in z.infolist())
            packed = max(1, sum(i.compress_size for i in z.infolist()))
    except zipfile.BadZipFile:
        return "not a valid Office file (bad zip)"
    if total > ZIP_MAX_BYTES or total / packed > ZIP_MAX_RATIO:
        return f"refused: decompresses to {total // (1024 * 1024)} MB (ratio {total // packed})"
    return None


def _ooxml_meta(path: str) -> dict:
    out: dict = {}
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("docProps/core.xml").decode("utf-8", "replace")
    except (KeyError, zipfile.BadZipFile, OSError):
        return out
    for key, tag in (("title", "dc:title"), ("author", "dc:creator"),
                     ("created", "dcterms:created"), ("modified", "dcterms:modified")):
        m = re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml)
        if m and m.group(1).strip():
            out[key] = m.group(1).strip()[:200]
    return out


def _pdf_pages(path: str) -> int:
    try:
        from pdfminer.pdfpage import PDFPage
        with open(path, "rb") as f:
            return sum(1 for _ in PDFPage.get_pages(f))
    except Exception:  # noqa: BLE001
        return 0


def _pdf_meta(path: str) -> dict:
    out: dict = {}
    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfparser import PDFParser
        with open(path, "rb") as f:
            info = (PDFDocument(PDFParser(f)).info or [{}])[0]
        for key, k in (("title", "Title"), ("author", "Author"), ("created", "CreationDate"),
                       ("modified", "ModDate")):
            v = info.get(k)
            if isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            if isinstance(v, str) and v.strip():
                m = re.match(r"D:(\d{4})(\d{2})(\d{2})", v)
                out[key] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else v.strip()[:200]
    except Exception:  # noqa: BLE001
        pass
    return out


def _read_plain(path: str) -> str:
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _docling_text(path: str) -> str:
    global _docling
    if _docling is None:
        from docling.document_converter import DocumentConverter
        _docling = DocumentConverter()
    return _docling.convert(path).document.export_to_markdown()


def headings_of(text: str, limit: int = 40) -> list[str]:
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}(#{1,4})\s+(.{2,120}?)\s*#*\s*$", line)
        if m:
            out.append(m.group(2).strip())
            if len(out) >= limit:
                break
    return out


def extract(path: str, max_chars: int = 240_000, docling: str = "auto") -> dict:
    ext = os.path.splitext(path)[1].lower()
    meta: dict = {}
    extractor = "markitdown"
    title = ""
    if ext in PLAIN:
        text, extractor = _read_plain(path), "plain"
    else:
        if ext in OOXML:
            bad = _zip_guard(path)
            if bad:
                return {"ok": False, "error": bad,
                        "kind": "refused" if bad.startswith("refused") else "unreadable"}
            meta = _ooxml_meta(path)
        elif ext == ".pdf":
            meta = _pdf_meta(path)
        text = ""
        err = None
        if not (ext == ".pdf" and docling == "always"):
            try:
                res = _markitdown().convert_local(path)
                text = res.text_content or ""
                title = (res.title or "").strip()
            except Exception as e:  # noqa: BLE001 — any parser failure = an unreadable file
                err = f"{type(e).__name__}: {str(e)[:200]}"
        if ext == ".pdf":
            pages = _pdf_pages(path)
            meta["pages"] = pages
            dense = len(re.sub(r"\s+", "", text))
            thin = dense < MIN_PDF_CHARS or (pages and dense / pages < MIN_PDF_CHARS_PER_PAGE)
            if thin or docling == "always":
                if docling != "off" and docling_available():
                    try:
                        text, extractor, err = _docling_text(path), "docling", None
                    except Exception as e:  # noqa: BLE001
                        err = f"docling: {type(e).__name__}: {str(e)[:200]}"
                elif thin:
                    return {"ok": False, "kind": "needs_ocr", "meta": meta,
                            "error": "PDF without a usable text layer (scan?) and Docling is "
                                     "not installed or turned off"}
        if err and not text.strip():
            return {"ok": False, "error": err, "kind": "unreadable", "meta": meta}
    text = text.replace("\x00", "").strip()
    if not text:
        return {"ok": False, "error": "no text in this file", "kind": "empty", "meta": meta}
    heads = headings_of(text)
    title = title or meta.get("title") or (heads[0] if heads else "")
    truncated = len(text) > max_chars
    if truncated:
        # keep the beginning and the end (decisions and signatures sit at the end of minutes)
        head = int(max_chars * 0.75)
        text = (text[:head] + "\n\n[… document truncated for length …]\n\n"
                + text[-(max_chars - head):])
    return {"ok": True, "text": text, "title": title[:200], "headings": heads, "meta": meta,
            "extractor": extractor, "chars": len(text), "truncated": truncated}


def main() -> None:
    # no RLIMIT_AS here: ONNX Runtime / PyTorch reserve address space per core, and on a
    # 36-core host a 2 GB address-space limit made every extraction hang (measured). The
    # parent watches this process's RESIDENT memory instead and kills it past the limit.
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)                   # a chatty parser writes to stderr, never into the protocol
    for line in sys.stdin:
        try:
            req = json.loads(line)
            res = extract(req["path"], int(req.get("max_chars") or 240_000),
                          str(req.get("docling") or "auto"))
        except MemoryError:
            res = {"ok": False, "error": "out of memory while reading this file",
                   "kind": "unreadable"}
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                   "kind": "unreadable"}
        out.write(json.dumps(res, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
