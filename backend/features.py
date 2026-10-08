"""Feature registry — the single source of truth for what this instance runs.

SOKKAN Enterprise is NOT a fork: it is this same open-source app with some features turned
on. Every feature is declared here once — id, title, the environment variable(s) that switch
it, its default per edition, what it requires, what it cannot run with, its status and its
doc. Everything else reads it from here:

* the code asks `features.enabled("<id>")` (never the raw environment variable);
* `GET /api/features` serves the effective state and the reason (Setup › Organization › Features);
* `scripts/gen-features-doc.py` generates docs/enterprise/FEATURES.md (a test fails when the
  generated doc is stale).

Resolution, per feature (`resolve`):

1. `planned` → always off (asking for it is reported, never honoured);
   `invariant` → always on (a security property, not a switch);
   `integration` → on when its backing service is configured (no switch: unset the config).
2. a `forced` rule (e.g. SOKKAN Cloud managed instance) wins over everything else;
3. otherwise the first environment variable that holds a recognised value: the canonical
   `SOKKAN_FEATURE_<ID>` first, then the legacy names (still accepted, nothing to change on
   an existing install); an empty value counts as unset;
4. otherwise the default of the edition (`SOKKAN_EDITION` = community | enterprise,
   community when unset) — `auto` = on when `auto` says so (e.g. Operate configured);
5. a feature that wants to be on but has a required feature off, or a conflicting feature
   on, is turned OFF with the reason — the API always starts, a feature is never half on.
   When that switch was asked for explicitly (environment), it is a `problem`: logged at
   startup and shown in red in the cockpit.

Keep this module free of backend imports at import time: the probes import lazily.
"""
from __future__ import annotations

import os


def env_num(name: str, default, cast=int):
    """Read a numeric env var; an empty string (docker compose passes `${VAR:-}` as "") means default."""
    raw = os.environ.get(name, "")
    if raw is None or str(raw).strip() == "":
        return default
    return cast(raw)
import sys
from dataclasses import dataclass, field
from typing import Callable

EDITIONS = ("community", "enterprise")
STATUSES = ("stable", "beta", "experimental", "planned")
KINDS = ("toggle", "integration", "invariant", "planned")
DOC = "docs/enterprise/FEATURES.md"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


# ---- value parsers: raw string → True / False / None (= no decision here) ----------
def parse_bool(v: str) -> bool | None:
    v = v.strip().lower()
    return True if v in _TRUE else False if v in _FALSE else None


def parse_not_zero(v: str) -> bool | None:
    """0/false/no/off = off, anything else on (SOKKAN_UPDATE_CHECK)."""
    v = v.strip().lower()
    return None if not v else v not in _FALSE


def parse_zero_off(v: str) -> bool | None:
    """Historical `!= "0"` switches (SOKKAN_FEATURE_TMUX, …): exactly `0` = off, else on."""
    v = v.strip()
    return None if not v else v != "0"


def _mode(on: tuple[str, ...], off: tuple[str, ...]) -> Callable[[str], bool | None]:
    def p(v: str) -> bool | None:
        v = v.strip().lower()
        return True if v in on else False if v in off else None
    return p


@dataclass(frozen=True)
class Var:
    name: str
    parse: Callable[[str], bool | None] = parse_bool
    legacy: bool = False
    note: str = ""          # how the value maps, for the doc (e.g. "four_eyes = on")


@dataclass(frozen=True)
class Feature:
    id: str
    title: str
    description: str
    status: str = "stable"
    kind: str = "toggle"
    defaults: dict = field(default_factory=lambda: {"community": False, "enterprise": False})
    requires: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    vars: tuple[Var, ...] = ()            # switches; canonical first (toggle kind)
    config: tuple[str, ...] = ()          # configuration variables (integrations, documentation)
    doc: str = ""
    target: str = ""                      # planned: release it is planned for
    auto: Callable[[], bool] | None = None        # value of an `auto` default / an integration
    forced: Callable[[], str | None] | None = None  # reason when forced on
    check: Callable[[], str | None] | None = None   # informational readiness note (never gates)

    @property
    def canonical_var(self) -> str:
        return f"SOKKAN_FEATURE_{self.id.upper()}"


# ---- probes (lazy imports: these modules import `features` themselves) ----------------
def _operate_configured() -> bool:
    import observability
    return bool(observability.ENABLED)


def _infra_configured() -> bool:
    import infra
    return bool(infra.ENABLED)


def _fleet_configured() -> bool:
    import fleet
    return bool(fleet.ENABLED)


def _sso_configured() -> bool:
    return (os.environ.get("SOKKAN_AUTH_MODE") or "local").strip() in ("oidc", "ldaps")


def _cortex_configured() -> bool:
    return bool((os.environ.get("SOKKAN_CORTEX_URL") or "").strip())


def _gitlab_check() -> str | None:
    if not (os.environ.get("SOKKAN_GITLAB_CLIENT_ID") or "").strip():
        return "no GitLab OAuth application (SOKKAN_GITLAB_CLIENT_ID): nobody can link an account"
    return None


def _managed_tier() -> str | None:
    if (os.environ.get("SOKKAN_TIER") or "").strip():
        return "SOKKAN Cloud managed instance (SOKKAN_TIER is set): always on"
    return None


def _assistant_check() -> str | None:
    try:
        import assistant
        return None if assistant.configured() else (
            "no LLM reachable for Nina (SOKKAN_ASSISTANT_LLM_*) or no knowledge base: hidden")
    except Exception as e:  # noqa: BLE001
        return f"readiness unknown: {e!r}"


def _both(v) -> dict:
    return {"community": v, "enterprise": v}


def _ed(community, enterprise) -> dict:
    return {"community": community, "enterprise": enterprise}


def _t(fid: str, *legacy: Var) -> tuple[Var, ...]:
    """Canonical SOKKAN_FEATURE_<ID> first, then the legacy names. When the canonical name
    already existed (SOKKAN_FEATURE_TMUX…), its historical parsing is kept exactly."""
    canon = f"SOKKAN_FEATURE_{fid.upper()}"
    same = [v for v in legacy if v.name == canon]
    first = Var(canon, same[0].parse, False, same[0].note) if same else Var(canon)
    return (first,) + tuple(v for v in legacy if v.name != canon)


A = "docs/AGENTS.md"
M = "docs/MULTIUSER.md"
O = "docs/OPERATE.md"

# ---- THE REGISTRY ------------------------------------------------------------------
# Order matters for conflicts only: of two conflicting features both asked for, the one
# declared FIRST wins. Ids are stable (they are API and env names): never rename one.
REGISTRY: tuple[Feature, ...] = (
    # -- core cockpit
    Feature("tmux", "Terminal sessions",
            "Raw tmux/ttyd terminal sessions in the cockpit (spawn, live view, send keys).",
            defaults=_both(True), vars=_t("tmux", Var("SOKKAN_FEATURE_TMUX", parse_zero_off, True, "0 = off, anything else = on"))),
    Feature("preview", "Preview",
            "Headless-browser previews of the apps built in a session (screenshots, envs).",
            defaults=_both(True),
            vars=_t("preview", Var("SOKKAN_FEATURE_PREVIEW", parse_zero_off, True, "0 = off, anything else = on"))),
    Feature("preview_private_targets", "Preview of private addresses",
            "Lets Preview open private / loopback targets (an app on the LAN). Off: SSRF guard.",
            requires=("preview",),
            vars=_t("preview_private_targets", Var("SOKKAN_PREVIEW_ALLOW_PRIVATE", _mode(("1",), ("0",)),
                                                   True, "1 = on"))),
    Feature("magnitude", "Magnitude (local LLM)",
            "Hardware profile, benchmark and llama.cpp serving of a local model; memory bench.",
            defaults=_both(True),
            vars=_t("magnitude", Var("SOKKAN_FEATURE_MAGNITUDE", parse_zero_off, True, "0 = off, anything else = on"))),
    Feature("assistant", "Nina (in-app assistant)",
            "Nina, the cockpit's help agent. Needs an LLM (SOKKAN_ASSISTANT_LLM_*) and its "
            "knowledge base; paid in the guaranteed offer, the user's own model in open source.",
            vars=_t("assistant", Var("SOKKAN_FEATURE_ASSISTANT", parse_zero_off, True, "0 = off, anything else = on")),
            config=("SOKKAN_ASSISTANT_LLM_URL", "SOKKAN_ASSISTANT_LLM_MODEL"),
            check=_assistant_check),
    Feature("missions_link", "SOKKAN Missions link",
            "Header link to the public SOKKAN Missions marketplace, with a counter this instance "
            "fetches (6 h cache). Off in the enterprise edition: no call to a public service.",
            defaults=_ed(True, False),
            vars=_t("missions_link", Var("SOKKAN_FEATURE_MISSIONS_LINK", parse_zero_off, True, "0 = off, anything else = on"))),
    Feature("update_check", "Update check",
            "One GET a day of dist/VERSION to tell the admin a new release exists.",
            defaults=_both(True),
            vars=_t("update_check", Var("SOKKAN_UPDATE_CHECK", parse_not_zero, True))),
    # -- security model
    Feature("named_secrets", "Secrets by name",
            "A human session only receives the vault secrets picked for it (or by its playbook). "
            "Off = `all`: every session receives the whole vault (3.1 behaviour, warned).",
            defaults=_both(True), forced=_managed_tier,
            vars=_t("named_secrets", Var("SOKKAN_SESSION_SECRETS",
                                         lambda v: (None if not v.strip() else v.strip().lower() != "all"),
                                         True, "named = on, all = off, anything else = on")),
            doc=M),
    Feature("memory_quarantine", "Memory quarantine",
            "A note written by an agent run waits in quarantine until a human approves it. "
            "A security invariant: it cannot be turned off.",
            kind="invariant", defaults=_both(True), config=("SOKKAN_MEMORY_QUARANTINE_DIR",), doc=A),
    # -- Crew (agents)
    Feature("agents", "Crew (agents)",
            "Scheduled and alert-triggered agents, their runs, deliverables and approvals.",
            defaults=_both(True),
            vars=_t("agents", Var("SOKKAN_FEATURE_AGENTS", parse_zero_off, True, "0 = off, anything else = on")), doc=A),
    Feature("agents_cli_login", "Agents on the CLI login",
            "Unattended agent runs may use the Claude CLI login found on the host (otherwise only "
            "credentials configured for this instance count).",
            requires=("agents",),
            vars=_t("agents_cli_login", Var("SOKKAN_AGENTS_USE_CLI_LOGIN", _mode(("1",), ("0",)),
                                            True, "1 = on")), doc=A),
    Feature("four_eyes", "Four-eyes approval",
            "Activating an agent (or a change to an approved one) needs ANOTHER person than its "
            "proposer and its owner.",
            defaults=_ed(False, True), requires=("agents",), conflicts=("admin_approval",),
            vars=_t("four_eyes", Var("SOKKAN_AGENTS_APPROVAL", _mode(("four_eyes",), ("owner", "admin")),
                                     True, "four_eyes = on, owner/admin = off")), doc=A),
    Feature("admin_approval", "Admin-only approval",
            "Only an instance admin activates an agent (default: its owner or an admin).",
            requires=("agents",), conflicts=("four_eyes",),
            vars=_t("admin_approval", Var("SOKKAN_AGENTS_APPROVAL", _mode(("admin",), ("owner", "four_eyes")),
                                          True, "admin = on, owner/four_eyes = off")), doc=A),
    Feature("agent_incidents", "Agent incidents",
            "A run ending failed / timeout / over budget opens (or joins) its agent's incident "
            "in Operate. Default `auto`: on when Operate is configured.",
            defaults=_both("auto"), auto=_operate_configured, requires=("agents",),
            vars=_t("agent_incidents", Var("SOKKAN_AGENTS_INCIDENTS", parse_bool, True)), doc=O),
    Feature("crew_viewer_readonly", "Crew visible to viewers",
            "A viewer sees the whole Crew read-only (secret names, never values). Public demo.",
            requires=("agents",),
            vars=_t("crew_viewer_readonly", Var("SOKKAN_CREW_VIEWER_READONLY", parse_bool, True)), doc=A),
    Feature("demo_banner", "Public demo mode",
            "Guided-tour banner of the public read-only demo instance.",
            vars=_t("demo_banner", Var("SOKKAN_DEMO_BANNER", parse_zero_off, True, "0 = off, anything else = on"))),
    Feature("demo_crew", "Simulated demo Crew",
            "Simulated agent runs (no model, no inference) for the public demo only.",
            requires=("agents", "demo_banner"),
            vars=_t("demo_crew", Var("SOKKAN_DEMO_CREW", parse_bool, True)), doc=A),
    # -- multi-user (3.2)
    Feature("sso", "Single sign-on",
            "OIDC / LDAPS login (SOKKAN_AUTH_MODE). Configure it to turn it on.",
            kind="integration", auto=_sso_configured,
            config=("SOKKAN_AUTH_MODE", "SOKKAN_OIDC_ISSUER", "SOKKAN_OIDC_CLIENT_ID"), doc=M),
    Feature("multi_project", "Projects", "Several isolated projects on one instance (sessions, "
            "board, agents, memory, workspace), roles per project. Off: no NEW project can be "
            "created; projects that exist keep their isolation (turning it off never opens data).",
            status="beta", defaults=_ed(False, True), vars=_t("multi_project"), doc=M),
    Feature("sso_teams", "SSO teams",
            "Teams = the IdP's groups (claim `groups`), re-synchronised at each login; a team can "
            "be granted a project role.", status="beta", defaults=_both(True), requires=("sso",),
            vars=_t("sso_teams"), config=("SOKKAN_OIDC_GROUPS_CLAIM",), doc=M),
    Feature("operate", "Operate",
            "Operate plane (Operate › Operate): alerts, incidents, dashboards. On when Prometheus or Grafana is "
            "configured.", kind="integration", auto=_operate_configured,
            config=("SOKKAN_PROM", "SOKKAN_GRAFANA_URL"), doc=O),
    Feature("ops_team", "Ops team",
            "Operate / Infra open to an SSO group (the ops team) besides the instance admins.",
            status="beta", defaults=_both(True), requires=("sso_teams",),
            vars=_t("ops_team"), config=("SOKKAN_OPS_GROUP",), doc=M),
    Feature("infra", "Infra topology", "Operate › Infra: host topology from Prometheus.",
            kind="integration", auto=_infra_configured, config=("SOKKAN_PROM",)),
    Feature("fleet", "Managed fleet", "Operate › Infra: the managed client VMs of SOKKAN Cloud.",
            kind="integration", auto=_fleet_configured, config=("SOKKAN_FLEET_URL", "SOKKAN_FLEET_TOKEN")),
    Feature("cortex", "CortHeXis link", "Link from Control › CortHeXis to a CortHeXis review UI.",
            kind="integration", auto=_cortex_configured, config=("SOKKAN_CORTEX_URL",)),
    # -- execution layer (Kubernetes runner and Helm chart)
    Feature("kubernetes_runner", "Container runner (docker / Kubernetes)",
            "Each session and agent run gets its own container (SOKKAN_RUNNER=docker, compose "
            "host) or Pod (SOKKAN_RUNNER=kubernetes, Helm chart): non-root, CPU/memory limits, "
            "only its project's workspace mounted, no network but the api's MCP relay and the "
            "egress gateway. Off (SOKKAN_RUNNER=local): the CLI runs inside the api, as before.",
            status="experimental", defaults=_both(False),
            vars=_t("kubernetes_runner",
                    Var("SOKKAN_RUNNER", _mode(("docker", "kubernetes"), ("local",)), False,
                        "docker | kubernetes = on, local = off")),
            config=("SOKKAN_RUNNER", "SOKKAN_SESSION_IMAGE", "SOKKAN_RUNNER_MOUNTS",
                    "SOKKAN_RUNNER_RELAY_ADDR"),
            doc="docs/enterprise/KUBERNETES.md"),
    # -- roadmap (planned: declared so the dependencies are agreed before the code exists)
    Feature("project_vault_budgets", "Vault and budgets per project",
            "Per-project vault, cost totals and budgets, agent names and CortHeXis review per "
            "project (lot 4). Off: a project other than `default` gets no vault secret, the "
            "CortHeXis review and the journal stay default / instance-admin only (fail-closed).",
            status="beta", defaults=_ed(False, True), target="3.2",
            requires=("multi_project", "named_secrets"), vars=_t("project_vault_budgets"), doc=M),
    Feature("gitlab", "GitLab projects",
            "Project access from GitLab roles read with the person's own account (OAuth PKCE; "
            "lowest level over the project's repositories, cached 10 min / 2 min), credential "
            "helper, push and merge requests in the person's name (lot 5).", status="beta",
            defaults=_ed(False, True), requires=("multi_project", "sso"), vars=_t("gitlab"),
            config=("SOKKAN_GITLAB_URL", "SOKKAN_GITLAB_CLIENT_ID", "SOKKAN_GITLAB_CLIENT_SECRET",
                    "SOKKAN_GITLAB_REDIRECT_URI", "SOKKAN_GITLAB_CA_BUNDLE"),
            check=_gitlab_check, doc=M),
    Feature("revocation", "Revocation",
            "SCIM 2.0 provisioning endpoint (Users, Groups) and the admin « Revoke now »: "
            "cockpit sessions invalidated, live sessions stopped, owned agents paused, forge "
            "tokens erased; teams recomputed at each SSO login; an agent never outlives its "
            "owner's access (lot 6).", status="beta", defaults=_ed(False, True),
            requires=("sso_teams",), vars=_t("revocation"),
            config=("SOKKAN_SCIM_TOKEN", "SOKKAN_SCIM_GROUP_KEY"), doc=M,
            check=lambda: __import__("revocation").readiness()),
    Feature("byok_admin", "BYOK admin screen",
            "Setup › Engines: the instance admin sets, replaces or deletes the model "
            "provider keys (Anthropic, …), stored encrypted with the vault key, never shown "
            "again (last 4 characters, date, who); optional validity test; exposed to the "
            "sessions; pushed to the SOKKAN gateway's BYOK endpoint when one is configured "
            "(lot 7). Per-project keys: the field exists, planned.",
            status="beta", defaults=_ed(False, True), requires=("multi_project",),
            vars=_t("byok_admin"),
            config=("SOKKAN_GATEWAY_URL", "SOKKAN_GATEWAY_ADMIN_TOKEN", "SOKKAN_GATEWAY_CLIENT"),
            doc="docs/enterprise/UI-FEATURES.md"),
    Feature("sandbox", "Project sandbox",
            "A session or an agent run of a project (not `default`) reaches only its project's "
            "space: file tools checked by a hook (paths resolved, symlinks followed), Bash "
            "inside bubblewrap when the host has it, refused otherwise (lot 8). Hooks-only "
            "(no bubblewrap, no Kubernetes runner): Bash is disabled outside `default`, never "
            "asked to a human; the session and the agent/session UI say so (« Bash is "
            "disabled in this project: sandbox is hooks-only… ») with the fix: install "
            "bubblewrap, enable the Kubernetes runner, or work in `default` (OPERATIONS.md "
            "§ 4.1).",
            status="beta", defaults=_ed(False, True), requires=("multi_project",),
            vars=_t("sandbox"),
            config=("SOKKAN_SANDBOX_BWRAP", "SOKKAN_SANDBOX_NETWORK", "SOKKAN_SANDBOX_RO_PATHS",
                    "SOKKAN_SANDBOX_READ_PATHS"), doc=M,
            check=lambda: __import__("sandbox").readiness()),
    Feature("shared_review", "Shared session / preview for review",
            "Share a session or a preview with a person or a team of its project, read or "
            "read-write, optionally for a limited time; the recipient sees it in their rail "
            "(« shared by … »); read-write can receive the session's delegated HITL approval. "
            "Never wider than the project role (a viewer never gets write); logged; revocable.",
            status="beta", defaults=_ed(False, True), requires=("preview", "multi_project"),
            vars=_t("shared_review"), doc="docs/enterprise/UI-FEATURES.md"),
    Feature("helm", "Helm",
            "Hierarchical cards (manager's project card → engineer's cards → sub-tasks): the "
            "parent's intent, constraints and decisions flow down into the sessions, progress "
            "flows up (computed, never declared); the Helm view for project managers; Nina "
            "interviews and breaks a project down, Helm suggests reframes (a manager approves "
            "or ignores); morning-brief agent template (ICS calendar).", status="beta",
            defaults=_ed(False, True), requires=("multi_project", "assistant"), vars=_t("helm"),
            config=("SOKKAN_HELM_TICK_S", "SOKKAN_HELM_DRIFT_MIN", "SOKKAN_HELM_SNOOZE_DAYS",
                    "SOKKAN_HELM_CALENDAR_ICS"), doc="docs/HELM.md"),
    Feature("classification", "Classification and clearances",
            "Notes, decisions, cards and agent deliverables carry a level (public < team < "
            "project < confidential < restricted); each person a clearance per project from "
            "their SSO groups and project role. Recall, memory_search / memory_get, the "
            "Control › CortHeXis, the board, Nina and Teams return only what the person is cleared "
            "for; derived content inherits the highest level of its sources; every note handed "
            "out is logged (audited recall). Off: nothing above `project` is reachable.",
            status="beta", defaults=_ed(False, True), requires=("multi_project", "sso_teams"),
            vars=_t("classification"),
            config=("SOKKAN_CLASSIFICATION_LABELS", "SOKKAN_CLEARANCE_ROLES"),
            doc="docs/enterprise/SECURITY.md"),
    Feature("teams", "Microsoft Teams",
            "@Nina in Teams channels and chats (project status, cards, proposed agents → "
            "approval), HITL approvals as signed single-use Adaptive Cards, decision capture "
            "into CortHeXis, channel ↔ project mapping, calendar via Graph for the brief. "
            "Single-tenant app, admin consent, Nina answers as the identified user only.",
            status="experimental", requires=("assistant", "classification", "sso"),
            vars=_t("teams"),
            config=("SOKKAN_TEAMS_APP_ID", "SOKKAN_TEAMS_APP_PASSWORD", "SOKKAN_TEAMS_TENANT_ID"),
            doc="docs/enterprise/TEAMS.md"),
    Feature("connect_ai", "Connect your AI",
            "Setup › Engines: one screen to connect the engines (Claude login or key, OpenAI/Codex, Gemini, "
            "OpenRouter, SOKKAN Router, Ollama/local, Magnitude). Personal mode (community: "
            "any engine, SOKKAN Router preselected) or governed mode (enterprise: the admin "
            "sets the allowed engines, zones and tiers; choice per project). A connected "
            "engine can drive a Crew card.", status="beta", defaults=_ed(False, True),
            vars=_t("connect_ai"),
            config=("SOKKAN_CONNECT_AI_MODE", "SOKKAN_ROUTER_URL", "SOKKAN_ROUTER_WELCOME_URL"),
            doc="docs/enterprise/UI-FEATURES.md"),
)

BY_ID: dict[str, Feature] = {f.id: f for f in REGISTRY}


# ---- resolution ----------------------------------------------------------------------
@dataclass
class State:
    id: str
    enabled: bool
    source: str           # default | env:VAR | forced | auto | integration | invariant | planned
    reason: str
    problem: bool = False  # an explicit request that could not be honoured
    var: str = ""          # variable that decided, if any
    legacy: bool = False   # decided by a legacy variable name
    note: str = ""         # informational (readiness)


def edition(env=None) -> str:
    env = os.environ if env is None else env
    e = (env.get("SOKKAN_EDITION") or "community").strip().lower()
    return e if e in EDITIONS else "community"


def _requested(f: Feature, env) -> tuple[bool | None, Var | None, str]:
    """(value, the Var that decided, raw) — first recognised value wins."""
    for v in f.vars:
        raw = env.get(v.name)
        if raw is None or not raw.strip():
            continue
        val = v.parse(raw)
        if val is not None:
            return val, v, raw
    return None, None, ""


def _default(f: Feature, ed: str) -> tuple[bool, str]:
    d = f.defaults.get(ed, False)
    if d == "auto":
        on = bool(f.auto()) if f.auto else False
        return on, f"default ({ed}): auto — {'on' if on else 'off'}"
    return bool(d), f"default ({ed} edition)"


def _resolve_one(fid: str, env, memo: dict[str, State], stack: tuple[str, ...]) -> State:
    if fid in memo:
        return memo[fid]
    if fid in stack:  # guarded by tests; never crash the API on a bad registry
        return State(fid, False, "error", f"dependency cycle: {' → '.join(stack + (fid,))}", True)
    f = BY_ID.get(fid)
    if f is None:
        return State(fid, False, "error", "unknown feature", True)
    ed = edition(env)
    val, var, raw = _requested(f, env)
    note = ""
    if f.kind == "planned":
        st = State(fid, False, "planned", f"planned for {f.target or 'a later release'} — "
                   "not available in this version", problem=bool(val), var=var.name if var else "")
        memo[fid] = st
        return st
    if f.kind == "invariant":
        st = State(fid, True, "invariant", "security invariant — always on")
        memo[fid] = st
        return st
    if f.kind == "integration":
        try:
            on = bool(f.auto()) if f.auto else False
        except Exception as e:  # noqa: BLE001
            on, note = False, f"probe failed: {e!r}"
        want, source = on, "integration"
        reason = "configured" if on else f"not configured ({', '.join(f.config) or 'see doc'})"
        explicit = False
    else:
        forced = f.forced() if f.forced else None
        if forced:
            want, source, reason, explicit = True, "forced", forced, False
        elif val is not None:
            want, source, explicit = val, f"env:{var.name}", True
            reason = f"{var.name}={raw.strip()}"
            if var.legacy and var.name != f.canonical_var:
                reason += f" (legacy name; {f.canonical_var} preferred)"
        else:
            try:
                want, reason = _default(f, ed)
            except Exception as e:  # noqa: BLE001
                want, reason = False, f"auto probe failed: {e!r}"
            source, explicit = ("auto" if f.defaults.get(ed) == "auto" else "default"), False
    st = State(fid, want, source, reason, var=var.name if (var and source.startswith("env")) else "",
               legacy=bool(var and var.legacy and source.startswith("env")), note=note)
    if want:
        for dep in f.requires:
            ds = _resolve_one(dep, env, memo, stack + (fid,))
            if not ds.enabled:
                st.enabled = False
                st.reason = f"requires `{dep}`, which is off ({ds.reason}) — {reason}"
                st.problem = explicit
                break
    if st.enabled:
        idx = [x.id for x in REGISTRY].index(fid)
        for other in f.conflicts:
            if other in BY_ID and [x.id for x in REGISTRY].index(other) < idx:
                os_ = _resolve_one(other, env, memo, stack + (fid,))
                if os_.enabled:
                    st.enabled = False
                    st.reason = f"conflicts with `{other}`, which is on — {reason}"
                    st.problem = explicit
                    break
    if st.enabled and f.check:
        try:
            st.note = f.check() or ""
        except Exception as e:  # noqa: BLE001
            st.note = f"readiness unknown: {e!r}"
    memo[fid] = st
    return st


def resolve(env=None) -> dict[str, State]:
    env = os.environ if env is None else env
    memo: dict[str, State] = {}
    for f in REGISTRY:
        _resolve_one(f.id, env, memo, ())
    return {f.id: memo[f.id] for f in REGISTRY}


def enabled(fid: str, env=None) -> bool:
    """THE call the code makes. Resolves only this feature and what it depends on."""
    if fid not in BY_ID:
        raise KeyError(f"unknown feature {fid!r} (declare it in backend/features.py)")
    env = os.environ if env is None else env
    return _resolve_one(fid, env, {}, ()).enabled


def requested(fid: str, env=None) -> bool | None:
    """What the environment asks for this feature (None = nothing), before any rule."""
    return _requested(BY_ID[fid], os.environ if env is None else env)[0]


def problems(env=None) -> list[State]:
    return [s for s in resolve(env).values() if s.problem]


def validate_registry() -> list[str]:
    """Registry consistency (tests): known ids, no cycle, sane fields."""
    errs: list[str] = []
    seen: set[str] = set()
    for f in REGISTRY:
        if f.id in seen:
            errs.append(f"duplicate id {f.id}")
        seen.add(f.id)
        if f.status not in STATUSES:
            errs.append(f"{f.id}: bad status {f.status}")
        if f.kind not in KINDS:
            errs.append(f"{f.id}: bad kind {f.kind}")
        if (f.kind == "planned") != (f.status == "planned"):
            errs.append(f"{f.id}: kind planned ⇔ status planned")
        for d in f.requires + f.conflicts:
            if d not in BY_ID:
                errs.append(f"{f.id}: unknown feature {d}")
            if d == f.id:
                errs.append(f"{f.id}: refers to itself")
        if f.kind == "toggle" and (not f.vars or f.vars[0].name != f.canonical_var):
            errs.append(f"{f.id}: a toggle's first variable must be {f.canonical_var}")
        if set(f.defaults) != set(EDITIONS):
            errs.append(f"{f.id}: defaults must name {EDITIONS}")
        for v in f.defaults.values():
            if v == "auto" and f.auto is None:
                errs.append(f"{f.id}: auto default without an auto probe")
        for c in f.conflicts:
            if c in BY_ID and f.id not in BY_ID[c].conflicts:
                errs.append(f"{f.id}: conflict with {c} must be declared on both sides")
        if f.kind != "planned":
            for d in f.requires:
                if d in BY_ID and BY_ID[d].kind == "planned":
                    errs.append(f"{f.id}: a shipped feature cannot require planned {d}")
    # cycles (requires)
    color: dict[str, int] = {}

    def visit(n: str, path: tuple[str, ...]) -> None:
        if color.get(n) == 1:
            errs.append("cycle: " + " → ".join(path + (n,)))
            return
        if color.get(n) == 2 or n not in BY_ID:
            return
        color[n] = 1
        for d in BY_ID[n].requires:
            visit(d, path + (n,))
        color[n] = 2
    for f in REGISTRY:
        visit(f.id, ())
    return errs


def all_vars() -> list[str]:
    """Every switch variable the registry reads (compose must pass them all)."""
    out: list[str] = ["SOKKAN_EDITION"]
    for f in REGISTRY:
        if f.kind == "planned":
            continue
        for v in f.vars:
            if v.name not in out:
                out.append(v.name)
    return out


def as_api(env=None) -> dict:
    states = resolve(env)
    items = []
    for f in REGISTRY:
        s = states[f.id]
        items.append({
            "id": f.id, "title": f.title, "description": f.description, "status": f.status,
            "kind": f.kind, "enabled": s.enabled, "source": s.source, "reason": s.reason,
            "problem": s.problem, "note": s.note, "legacy_var": s.var if s.legacy else "",
            "requires": list(f.requires), "conflicts": list(f.conflicts),
            "required_by": [g.id for g in REGISTRY if f.id in g.requires],
            "env": [v.name for v in f.vars], "config": list(f.config),
            "defaults": dict(f.defaults), "target": f.target, "doc": f.doc or DOC,
        })
    return {"edition": edition(env), "items": items,
            "problems": [i["id"] for i in items if i["problem"]]}


def startup_report(log=None) -> list[State]:
    """Called once at API startup: logs every switch that was asked for but cannot be
    honoured (the feature stays OFF — never half on, never a crash)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    raw_ed = (os.environ.get("SOKKAN_EDITION") or "").strip().lower()
    if raw_ed and raw_ed not in EDITIONS:
        log(f"[features] SOKKAN_EDITION={raw_ed!r} unknown — using community")
    try:
        bad = problems()
    except Exception as e:  # noqa: BLE001 — the registry never takes the API down
        log(f"[features] resolution failed: {e!r}")
        return []
    for s in bad:
        log(f"[features] `{s.id}` is OFF: {s.reason}")
    on = [k for k, s in resolve().items() if s.enabled]
    log(f"[features] edition {edition()}: {len(on)} on — {', '.join(on)}")
    return bad
