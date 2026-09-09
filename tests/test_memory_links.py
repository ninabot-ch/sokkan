"""Wikilinks indexés + navigation : parse_links, table links, outil memory_links."""
import sqlite3

import index_memory as im


def test_parse_links_alias_dedup_self():
    body = "see [[note-a]] and [[note-b|a label]], again [[note-a]] and [[self-note]]"
    assert im.parse_links(body, "self-note") == ["note-a", "note-b"]


def test_parse_links_case_and_empty():
    assert im.parse_links("[[Note-A]]", "x") == ["note-a"]
    assert im.parse_links("", "x") == []
    assert im.parse_links(None, "x") == []


def _mini_db(tmp_path):
    con = sqlite3.connect(tmp_path / "memory.db")
    im.init_db(con)
    con.executemany("INSERT INTO notes(name, description, type, mtime, source_path) VALUES(?,?,?,0,?)",
                    [("a", "note a", "project", "/x/a.md"),
                     ("b", "note b", "reference", "/x/b.md")])
    con.executemany("INSERT INTO links(src, dst) VALUES(?,?)",
                    [("a", "b"), ("a", "ghost"), ("b", "a")])
    con.commit()
    con.close()
    return tmp_path / "memory.db"


def test_memory_links_tool(tmp_path, monkeypatch):
    import memory_search_server as mem
    db = _mini_db(tmp_path)
    monkeypatch.setattr(mem, "DB_PATH", db)
    out = mem.memory_links("a")
    assert [l["name"] for l in out["links"]] == ["b", "ghost"]
    assert out["links"][0]["exists"] is True
    assert out["links"][1]["exists"] is False   # lien vers une note pas encore écrite
    assert [b["name"] for b in out["backlinks"]] == ["b"]
    assert "error" in mem.memory_links("nope")


def test_memorykb_reads_links_from_db(tmp_path, monkeypatch):
    import memorykb
    db = _mini_db(tmp_path)
    monkeypatch.setattr(memorykb, "MEM_DB", db)
    notes = {n["name"]: n for n in memorykb.list_notes()}
    assert notes["a"]["links"] == ["b", "ghost"]
    assert notes["a"]["backlinks"] == ["b"]
    assert notes["b"]["backlinks"] == ["a"]
