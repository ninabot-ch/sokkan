"""Turn an article title into the last part of its address."""
import re


def slugify(title: str, max_len: int = 40) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]", "-", s)
    return s[:max_len]
