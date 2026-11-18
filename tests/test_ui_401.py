"""4.0.1 — what the launch video showed on screen and should not have (static checks of the
front's sentences): a path of the repository in the Atelier, a real company in the Cockpit."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"


def _strings(path: pathlib.Path) -> list[str]:
    return re.findall(r'^\s+"[\w.]+": "(.*)",$', path.read_text(), re.M)


def test_no_repository_path_in_the_atelier_sentences():
    for f in (FRONT / "lib" / "simple").glob("dict*.ts"):
        bad = [s for s in _strings(f) if re.search(r"\bdocs/|/api/", s)]
        assert not bad, (f.name, bad[:3])


def test_the_cockpit_example_names_no_real_company():
    board = (FRONT / "components" / "Board.tsx").read_text()
    assert "jobup" not in board.lower()


def test_the_tour_names_excel():
    s = (FRONT / "lib" / "simple" / "dictOnboarding.ts").read_text()
    tour = re.findall(r'"wl\.t3\.text": "(.*)"', s)
    assert len(tour) == 2 and all("Excel" in x for x in tour)
