"""Turn an article title into the last part of its address."""
import re
import unicodedata


def slugify(title: str, max_len: int = 40) -> str:
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    if s[max_len] != "-" and "-" in cut:
        cut = cut[:cut.rindex("-")]
    return cut.strip("-")
