"""4.0 — kernel host-3f's tool loop guard (`tool.loop_guard`): the adapter turns it into a
recoverable error the cockpit says in words; the simple view shows the suspension (the
first warning stays in the technical pane). Never silent, never a failure of the turn."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import plain  # noqa: E402
from harness import contract, kernel  # noqa: E402

TH = "th_0123456789abcdef"


def _events(action: str, n: int):
    h = kernel.KernelHarness()
    h._thread, h._turn = TH, "turn-1"
    h._on_event("tool.loop_guard", {"thread_id": TH, "turn_id": f"{TH}:0", "tool": "search_files",
                                    "n": n, "action": action})
    out = []
    while not h._mail.empty():
        out.append(h._mail.get_nowait())
    return h, out


def test_the_guard_is_a_recoverable_error_with_its_facts():
    h, evs = _events("suspend", 8)
    errs = [e for e in evs if isinstance(e, contract.Error)]
    assert len(errs) == 1 and errs[0].recoverable and errs[0].code == "tool_loop_guard"
    assert errs[0].data == {"tool": "search_files", "n": 8, "action": "suspend"}
    assert "suspended" in errs[0].message
    assert h._turn == "turn-1"                     # the turn goes on


def test_another_threads_guard_is_not_ours():
    h = kernel.KernelHarness()
    h._thread, h._turn = TH, "turn-1"
    h._on_event("tool.loop_guard", {"thread_id": "th_other0000000000", "tool": "x", "n": 4,
                                    "action": "note"})
    assert h._mail.empty()


def test_the_simple_view_says_the_suspension_in_words():
    v = plain.View()
    out = v.feed({"type": "error", "code": "tool_loop_guard", "action": "suspend",
                  "message": "search_files suspended for this turn after 8 calls"})
    assert out == [{"type": "notice", "code": "loop_guard",
                    "detail": "search_files suspended for this turn after 8 calls"}]
    assert v.feed({"type": "error", "code": "tool_loop_guard", "action": "note",
                   "message": "search_files called 4 times"}) == []


def test_the_thread_has_a_sentence_for_it():
    root = Path(__file__).resolve().parent.parent / "frontend"
    dic = (root / "lib/simple/dict.ts").read_text()
    assert dic.count('"no.loop_guard"') == 2      # fr and en
    assert '"loop_guard"]' in (root / "components/simple/Thread.tsx").read_text()
