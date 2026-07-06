import pytest


@pytest.fixture()
def iam(tmp_path, monkeypatch):
    import iam as i

    monkeypatch.setattr(i, "DB", tmp_path / "iam.db")
    i.init(force=True)
    return i


def test_rank_is_strictly_increasing(iam):
    assert iam.rank("viewer") < iam.rank("dev") < iam.rank("admin") < iam.rank("owner")
    assert iam.rank("not-a-role") == -1


def test_owner_is_seeded(iam):
    assert any(u["role"] == "owner" for u in iam.list_users())


def test_upsert_rejects_invalid_role(iam):
    with pytest.raises(ValueError):
        iam.upsert_user("a@b.c", "root")


def test_upsert_and_case_insensitive_get(iam):
    iam.upsert_user("a@b.c", "dev", "Alice")
    u = iam.get_user("A@B.C")
    assert u["known"] and u["role"] == "dev"


def test_delete_owner_is_forbidden(iam):
    owner = next(u for u in iam.list_users() if u["role"] == "owner")
    with pytest.raises(ValueError):
        iam.delete_user(owner["email"])


def test_delete_regular_user(iam):
    iam.upsert_user("a@b.c", "dev")
    iam.delete_user("a@b.c")
    assert not iam.get_user("a@b.c")["known"]


def test_default_role_for_unknown_email(iam, monkeypatch):
    assert iam.get_user("ghost@x.y")["role"] == "viewer"
    monkeypatch.setattr(iam, "DEFAULT_ROLE", "none")
    u = iam.get_user("ghost@x.y")
    assert u["role"] == "none" and not u["known"]
