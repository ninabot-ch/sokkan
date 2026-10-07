"""Lot 5 — the forge abstraction: every provider honours the same interface; GitLab is
implemented (checked against a fake GitLab), GitHub and Gitea/Forgejo are skeletons that
fail cleanly. No real forge is ever called."""
import inspect

import pytest

from fake_gitlab import FakeGitLab

NETWORK = ("authorize_url", "exchange", "refresh", "revoke", "whoami", "access_level",
           "protected_branches")


@pytest.fixture()
def gl(tmp_path):
    f = FakeGitLab(tmp_path / "repos").start()
    f.add_user("7", "alice")
    yield f
    f.stop()


def _all():
    from forge import provider_class
    return [provider_class(n) for n in ("gitlab", "github", "gitea", "forgejo")]


@pytest.mark.parametrize("cls", _all(), ids=lambda c: c.name)
def test_every_provider_has_the_whole_interface(cls):
    import forge
    assert issubclass(cls, forge.Provider)
    for m in forge.PROVIDER_METHODS:
        assert callable(getattr(cls, m)), m
    base = inspect.signature(forge.Provider.access_level)
    assert inspect.signature(cls.access_level).parameters.keys() == base.parameters.keys()
    # mapping is total and lands in the SOKKAN roles (or None)
    for lv in (None, "", 0, 5, 10, 20, 30, 40, 50, "read", "write", "admin", "owner"):
        assert cls.role_for(lv) in (None, *forge.ROLES)


@pytest.mark.parametrize("name", ["github", "gitea", "forgejo"])
def test_skeletons_raise_a_clean_not_implemented(name):
    import forge
    p = forge.provider_class(name)("https://forge.example")
    assert p.implemented is False
    who, repo = forge.Identity("1", "x"), forge.Repo(name, "https://forge.example", "a/b")
    calls = {"authorize_url": ("s", "c", "r"), "exchange": ("c", "v", "r"), "refresh": ("t", "r"),
             "revoke": ("t",), "whoami": ("t",), "access_level": ("t", who, repo),
             "protected_branches": ("t", repo)}
    for m in NETWORK:
        with pytest.raises(forge.NotImplementedForge) as e:
            getattr(p, m)(*calls[m])
        assert "only GitLab ships" in str(e.value)
        assert isinstance(e.value, NotImplementedError)   # standard contract too
    with pytest.raises(forge.NotImplementedForge):
        forge.provider_class("bitbucket")


def test_level_mappings():
    from forge.gitea import Gitea
    from forge.github import GitHub
    from forge.gitlab import GitLab
    assert [GitLab.role_for(x) for x in (0, 5, 10, 15, 20, 30, 40, 50, 60)] == \
        [None, None, "viewer", "viewer", "viewer", "dev", "maintainer", "admin", "admin"]
    assert [GitHub.role_for(x) for x in ("read", "triage", "write", "maintain", "admin", "x")] == \
        ["viewer", "viewer", "dev", "maintainer", "admin", None]
    assert [Gitea.role_for(x) for x in ("none", "read", "write", "admin", "owner")] == \
        [None, "viewer", "dev", "maintainer", "admin"]


def test_lowest_role_over_repositories():
    from forge import lowest
    assert lowest(["admin", "dev", "maintainer"]) == "dev"
    assert lowest(["dev", None]) is None          # no access on one repo = none
    assert lowest([]) is None


def test_gitlab_against_the_fake(gl):
    import forge
    from forge.gitlab import GitLab
    from forge.links import pkce_pair
    p = GitLab(gl.url + "/", "cid", "csecret")
    v, ch = pkce_pair()
    url = p.authorize_url("st", ch, "http://cockpit/cb")
    assert url.startswith(gl.url + "/oauth/authorize?") and "code_challenge_method=S256" in url
    assert "scope=read_user+read_api+read_repository+write_repository" in url
    with pytest.raises(forge.ForgeUnauthorized):          # wrong verifier: PKCE refused
        p.exchange(gl.consent("7", ch), "not-the-verifier", "http://cockpit/cb")
    t = p.exchange(gl.consent("7", ch), v, "http://cockpit/cb")
    assert t.access_token and t.refresh_token and t.expires_at
    assert t.access_token not in repr(t)                 # never printed
    who = p.whoami(t.access_token)
    assert (who.user_id, who.username) == ("7", "alice")
    gl.add_repo("grp/sub/api")
    gl.set_level("grp/sub/api", "7", 30)                 # e.g. inherited from grp
    repo = forge.Repo("gitlab", gl.url, "grp/sub/api")
    assert p.access_level(t.access_token, who, repo) == "dev"
    assert p.protected_branches(t.access_token, repo) == ["main"]
    assert p.access_level(t.access_token, who, forge.Repo("gitlab", gl.url, "other/x")) is None
    # refresh rotates: the old refresh token is dead afterwards
    t2 = p.refresh(t.refresh_token, "http://cockpit/cb")
    assert t2.refresh_token != t.refresh_token
    with pytest.raises(forge.ForgeUnauthorized):
        p.refresh(t.refresh_token, "http://cockpit/cb")
    gl.revoked.append(t2.access_token)
    with pytest.raises(forge.ForgeUnauthorized):
        p.access_level(t2.access_token, who, repo)
    gl.down = True
    with pytest.raises(forge.ForgeUnavailable) as e:
        p.whoami(t2.access_token)
    assert t2.access_token not in str(e.value)
    assert p.git_credentials("tok") == ("oauth2", "tok")


def test_gitlab_unreachable_is_unavailable_not_unauthorized():
    import forge
    from forge.gitlab import GitLab
    p = GitLab("http://127.0.0.1:9", "cid", "s", timeout=1)
    with pytest.raises(forge.ForgeUnavailable):
        p.whoami("secret-token-xyz")
