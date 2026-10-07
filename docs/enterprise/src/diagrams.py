#!/usr/bin/env python3
"""Generates the SVG diagrams of docs/enterprise/ (light + dark via prefers-color-scheme).

    python3 docs/enterprise/src/diagrams.py      # writes hld.svg, method.svg, pipeline.svg

Edit the content here, never the SVG by hand. Status markers: filled = shipped, half = in progress
(v3.2 branch, unreleased), ring = planned. Colour is never the only signal (shape + dashed border).
"""
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent
FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"

STYLE = f"""<style>
svg{{--bg:#ffffff;--panel:#f6f8fa;--tile:#ffffff;--ink:#1f2328;--muted:#59636e;--line:#d1d9e0;
--edge:#59636e;--acc:#6639ba;--accbg:#f3eeff;--ok:#1a7f37;--wip:#9a6700;--plan:#6e7781}}
@media (prefers-color-scheme:dark){{svg{{--bg:#0d1117;--panel:#151b23;--tile:#0d1117;--ink:#e6edf3;
--muted:#9198a1;--line:#3d444d;--edge:#9198a1;--acc:#a371f7;--accbg:#1e1534;--ok:#3fb950;--wip:#d29922;--plan:#9198a1}}}}
text{{font-family:{FONT};fill:var(--ink)}}
.bg{{fill:var(--bg)}} .panel{{fill:var(--panel);stroke:var(--line);stroke-width:1.2}}
.tile{{fill:var(--tile);stroke:var(--line);stroke-width:1}}
.acc{{fill:var(--accbg);stroke:var(--acc);stroke-width:1.4}}
.planned{{stroke-dasharray:6 4;stroke:var(--plan)}}
.h1{{font-size:22px;font-weight:700}} .h2{{font-size:15px;font-weight:700}} .h3{{font-size:13px;font-weight:700}}
.t{{font-size:12.5px}} .s{{font-size:11.5px;fill:var(--muted)}} .k{{font-size:11px;font-weight:700;fill:var(--acc);letter-spacing:.06em}}
.ok{{fill:var(--ok)}} .wip{{fill:var(--wip)}} .plan{{fill:var(--plan)}}
.edge{{stroke:var(--edge);stroke-width:1.6;fill:none}} .edged{{stroke:var(--edge);stroke-width:1.6;fill:none;stroke-dasharray:5 4}}
.ah{{fill:var(--edge)}} .lbl{{font-size:11.5px;fill:var(--muted);font-style:italic}} .lblbg{{fill:var(--bg)}}
</style>"""



def svg(w, h, body, title):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{escape(title)}">\n<title>{escape(title)}</title>\n{STYLE}\n'
            '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ah"/></marker></defs>\n'
            f'<rect class="bg" width="{w}" height="{h}"/>\n' + "\n".join(body) + "\n</svg>\n")


def text(x, y, s, cls="t", anchor="start"):
    return f'<text x="{x}" y="{y}" class="{cls}" text-anchor="{anchor}">{escape(s)}</text>'


def mark(cx, cy, st, r=4.6):
    """Status marker drawn as a shape (font-independent): filled = shipped, half = in progress, ring = planned."""
    if st == "ok":
        return f'<circle cx="{cx}" cy="{cy}" r="{r}" class="ok"/>'
    if st == "wip":
        return (f'<circle cx="{cx}" cy="{cy}" r="{r - .7}" fill="none" stroke="var(--wip)" stroke-width="1.4"/>'
                f'<path d="M{cx} {cy - r + .7} A{r - .7} {r - .7} 0 0 0 {cx} {cy + r - .7} Z" class="wip"/>')
    return f'<circle cx="{cx}" cy="{cy}" r="{r - .7}" fill="none" stroke="var(--plan)" stroke-width="1.4"/>'


def item(x, y, s, st=None, cls="t"):
    """A line with its status marker (the shape carries the status, colour repeats it)."""
    if st is None:
        return text(x, y, s, cls)
    return mark(x + 5, y - 4.5, st) + text(x + 15, y, s, cls)


def box(x, y, w, h, title=None, lines=(), cls="panel", planned=False, kicker=None, title_st=None, lh=19):
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="{cls}{" planned" if planned else ""}"/>']
    ty = y + 22
    if kicker:
        out.append(text(x + 14, ty - 2, kicker, "k"))
        ty += 16
    if title:
        out.append(item(x + 14, ty, title, title_st, "h2"))
        ty += 22
    for ln in lines:
        st, s = (ln if isinstance(ln, tuple) else (None, ln))
        if st is None and s.startswith(" "):
            out.append(text(x + 29, ty, s.strip()))  # continuation of the previous item
        else:
            out.append(item(x + 14, ty, s, st))
        ty += lh
    return out


def tile(x, y, w, h, name, sub, st):
    return [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" class="tile{" planned" if st == "plan" else ""}"/>',
            item(x + 10, y + 22, name, st, "h3"), text(x + 10, y + 42, sub, "s")]


def arrow(pts, label=None, at=None, dashed=False):
    d = "M" + " L".join(f"{a} {b}" for a, b in pts)
    out = [f'<path d="{d}" class="{"edged" if dashed else "edge"}" marker-end="url(#a)"/>']
    if label:
        lx, ly = at
        wpx = len(label) * 6.2 + 10
        out.append(f'<rect x="{lx - 5}" y="{ly - 12}" width="{wpx}" height="16" class="lblbg"/>')
        out.append(text(lx, ly, label, "lbl"))
    return out


def legend(x, y):
    return [item(x, y, "shipped (3.1.x)", "ok", "s"),
            item(x + 125, y, "in progress (3.2 branch)", "wip", "s"),
            item(x + 290, y, "planned (3.2+ · 3.3 · 3.4)", "plan", "s")]


# ---------------------------------------------------------------- HLD
def hld():
    b = [text(24, 38, "SOKKAN Enterprise — high-level design", "h1"),
         text(24, 60, "One open-source app; enterprise features switched on one by one. Self-hosted or SOKKAN Cloud.", "s")]
    b += legend(800, 36)
    # people / identity / clients
    b += box(24, 90, 176, 196, "People", ["Developers", "DevOps · system eng.", "DBAs · QA", "Managers", "Instance admins",
                                          "Ops team (SSO group)"], kicker="WHO")
    b += box(24, 306, 176, 196, "Identity", [("ok", "OIDC single sign-on"), ("ok", "any OIDC IdP"),
                                             ("ok", "DEFAULT_ROLE=none"), ("wip", "groups → teams"),
                                             ("wip", "roles per project"), ("plan", "SCIM · revoke now")], kicker="SSO", lh=20)
    b += box(24, 522, 176, 138, "Where they work", [("ok", "Cockpit (browser)"), ("ok", "VS Code + Claude Code"),
                                                     ("ok", "terminal / CLI")], kicker="CLIENTS")
    # cockpit
    b += box(220, 90, 566, 320, "SOKKAN cockpit — one app, Apache-2.0", cls="acc", kicker="CONTROL PLANE")
    tiles = [("Board", "kanban, MCP-driven", "ok"), ("Sessions", "parallel Claude Code", "ok"),
             ("Crew", "governed agents", "ok"), ("Preview", "see the change live", "ok"),
             ("CortHeXis", "team memory", "ok"), ("Operate", "event-driven ops", "ok"),
             ("Costs", "spend · budgets", "ok"), ("Magnitude", "your own GPUs", "ok"),
             ("Nina", "built-in assistant", "ok"), ("Journal", "audit trail", "ok"),
             ("Admin", "projects & teams", "wip"), ("Helm", "manager view (3.3)", "plan")]
    for i, (n, s, st) in enumerate(tiles):
        b += tile(234 + (i % 4) * 136, 140 + (i // 4) * 62, 128, 54, n, s, st)
    b += ['<rect x="234" y="330" width="538" height="30" rx="6" class="tile"/>',
          item(246, 350, "Project gate — every route, MCP server, WebSocket scoped; role = role in that project", "wip", "t"),
          '<rect x="234" y="368" width="538" height="30" rx="6" class="tile"/>',
          item(246, 388, "Feature registry — each enterprise feature on/off, dependencies checked", "wip", "t")]
    # sessions + MCP
    b += box(220, 450, 276, 210, "Sessions & agent runs", [("ok", "Claude Agent SDK / CLI"),
                                                            ("ok", "human go on every mutating call"),
                                                            ("ok", "four-eyes approval of agents"),
                                                            ("ok", "secrets by name, redacted"),
                                                            ("ok", "budget per session / run"),
                                                            ("wip", "workspace per project"),
                                                            ("plan", "sandbox per sensitive project")], kicker="EXECUTION", lh=20)
    b += box(510, 450, 276, 210, "Embedded MCP servers", [("ok", "sokkan-memory  search · get · write"),
                                                           ("ok", "sokkan-board  drive the kanban"),
                                                           ("ok", "sokkan-agents  propose agents"),
                                                           ("ok", "observability  query · dashboards"),
                                                           ("ok", "preview  publish a preview"),
                                                           ("wip", "scope = the session's project")], kicker="TOOLS", lh=20)
    # inference
    b += box(808, 90, 448, 320, "SOKKAN Inference — tiered gateway", [("ok", "Anthropic Messages API (ANTHROPIC_BASE_URL)"),
                                                                       ("ok", "scrubber: secrets blocked, PII masked"),
                                                                       ("ok", "prepaid balance; refused before a billed tier")],
             kicker="INFERENCE (OPERATED SERVICE)", lh=20)
    for i, (n, s, st) in enumerate([("Ship", "fast open model", "ok"), ("Deep", "large open model", "ok"),
                                    ("Claude", "customer's key", "wip")]):
        b += tile(822 + i * 144, 214, 128, 54, n, s, st)
    b += arrow([(950, 241), (964, 241)]) + arrow([(1094, 241), (1108, 241)])
    b += [item(822, 292, "escalation: keywords · <<ESCALATE>> · upstream failure", "ok"),
          item(822, 312, "tier held for one human turn, then decided again", "wip"),
          item(822, 332, "prompt cache billed at the cache rate", "wip"),
          item(822, 352, "“avoided Claude cost” report: day · month · user · tier", "wip"),
          item(822, 372, "Claude tier = customer's key, passthrough, not billed", "wip"),
          item(822, 392, "no sticky routing: every turn can come back down", "ok")]
    b += box(808, 450, 448, 210, "Compute", [("ok", "EU GPU endpoints — current Ship / Deep upstreams"),
                                             ("plan", "dedicated Swiss GPUs (Exoscale) — primary"),
                                             ("ok", "Magnitude nodes: your GPUs, multi-node, 1 GPU/node"),
                                             ("plan", "small 10–20B model on a customer VM"),
                                             ("ok", "local embeddings for the memory (CPU or GPU)")], kicker="WHERE MODELS RUN", lh=21)
    # bottom row
    b += box(220, 690, 380, 168, "CortHeXis — team memory", [("wip", "one memory per project + read-only “shared”"),
                                                              ("wip", "recall filtered at every step, fail-closed"),
                                                              ("ok", "agent writes → quarantine → human review"),
                                                              ("ok", "Postgres + pgvector, local embeddings")], kicker="KNOWLEDGE", lh=21)
    b += box(620, 690, 200, 168, "GitLab (forge)", [("plan", "OAuth link per person"), ("plan", "push with their token"),
                                                     ("plan", "merge requests"), ("plan", "role = access level")],
             kicker="CODE", planned=True, lh=21)
    b += box(838, 690, 200, 168, "Microsoft Teams", [("plan", "@Nina in channels"), ("plan", "HITL approval cards"),
                                                      ("plan", "decisions from a thread"), ("plan", "calendar · presence")],
             kicker="3.4", planned=True, lh=21)
    b += box(1056, 690, 200, 168, "Observability", [("ok", "Prometheus · Grafana"), ("ok", "Loki logs"),
                                                     ("ok", "alert → incident"), ("ok", "alert = untrusted input")],
             kicker="OPERATE", lh=21)
    b += box(24, 690, 176, 168, "Audit", [("ok", "journal of actions"), ("ok", "agent.* · approvals"),
                                          ("wip", "project.grant / revoke"), ("wip", "scope violations = 0")], kicker="TRACE", lh=21)
    # edges
    b += arrow([(112, 286), (112, 306)])
    b += arrow([(200, 400), (220, 400)], )
    b += arrow([(200, 590), (220, 590)])
    b += arrow([(358, 410), (358, 450)])
    b += arrow([(648, 410), (648, 450)])
    b += arrow([(496, 520), (510, 520)])
    b += arrow([(420, 660), (420, 690)], "recall · write", (428, 680))
    b += arrow([(700, 660), (700, 690)], dashed=True)
    b += arrow([(1032, 410), (1032, 450)], "model calls", (1040, 434))
    b += arrow([(786, 300), (808, 300)])
    return svg(1280, 880, b, "SOKKAN Enterprise high-level design")


# ---------------------------------------------------------------- METHOD
def method():
    b = [text(24, 38, "SOKKAN Enterprise — the working method", "h1"),
         text(24, 60, "From a manager's card to production, with a human go at every gate and every decision kept in memory.", "s")]
    b += legend(800, 36)
    row1 = [("1", "Project card", "MANAGER", ["the manager writes the goal,", "Nina asks the right questions"], "plan", "3.3 Helm"),
            ("2", "Engineer kanban", "NINA + MANAGER", ["the card splits into engineer", "cards; context flows down"], "plan", "3.3 Helm"),
            ("3", "▶ Spawn a session", "ENGINEER", ["the project memory is injected;", "plan first, then the human go"], "ok", "shipped"),
            ("4", "Agents do the routine", "CREW", ["recurring or one-shot agents,", "approved, budgeted, audited"], "ok", "shipped")]
    for i, (n, t, who, ls, st, tag) in enumerate(row1):
        x = 40 + i * 310
        b += step(x, 96, 270, n, t, who, ls, st, tag)
    row2 = [("5", "Preview = validation", "REVIEWER", ["see the change running;", "share read / read-write"], "wip", "share: 3.2"),
            ("6", "Human approval", "REVIEWER", ["HITL on mutating calls,", "four-eyes for agents"], "ok", "shipped"),
            ("7", "Merge request", "ENGINEER", ["pushed with the person's", "own forge token"], "plan", "3.2 lot 5"),
            ("8", "Operate", "OPS TEAM", ["alert → incident + diagnosis", "session, runbooks"], "ok", "shipped"),
            ("9", "Decisions → CortHeXis", "EVERYONE", ["one fact per note, reviewed;", "agent notes via quarantine"], "ok", "shipped")]
    for i, (n, t, who, ls, st, tag) in enumerate(row2):
        x = 40 + i * 245
        b += step(x, 300, 220, n, t, who, ls, st, tag)
    for i in range(3):
        b += arrow([(40 + i * 310 + 270, 156), (40 + (i + 1) * 310, 156)])
    b += arrow([(1105, 216), (1105, 258), (150, 258), (150, 300)])
    for i in range(4):
        b += arrow([(40 + i * 245 + 220, 360), (40 + (i + 1) * 245, 360)])
    b += arrow([(1150, 420), (1150, 462), (20, 462), (20, 156), (40, 156)], dashed=True)
    b += ['<rect x="300" y="452" width="560" height="20" class="lblbg"/>',
          item(310, 467, "progress rolls up to the manager automatically — Helm direction view, morning brief (3.3)", "plan", "lbl")]
    return svg(1280, 500, b, "SOKKAN Enterprise working method")


def step(x, y, w, n, title, who, lines, st, tag):
    h = 120
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="{"acc" if st != "plan" else "panel"}{" planned" if st == "plan" else ""}"/>',
           f'<circle cx="{x + 22}" cy="{y + 24}" r="13" class="tile"/>', text(x + 22, y + 29, n, "h3", "middle"),
           text(x + 44, y + 22, who, "k"), item(x + 44, y + 40, title, None, "h3")]
    for i, ln in enumerate(lines):
        out.append(text(x + 14, y + 68 + i * 18, ln, "t"))
    out.append(item(x + 14, y + h - 10, tag, st, "s"))
    return out


# ---------------------------------------------------------------- PIPELINE
def pipeline():
    b = [text(24, 38, "SOKKAN Enterprise — pipelines", "h1"),
         text(24, 60, "A. One model call, from the prompt to the audit trail.   B. One release, from the branch to the fleet.", "s")]
    b += legend(800, 36)
    b.append(text(24, 98, "A · REQUEST / TASK PIPELINE", "k"))
    r1 = [("1 Prompt", ["cockpit, VS Code, CLI", "or an agent run"], "ok"),
          ("2 Session policy", ["allowed tools, HITL gate,", "secrets by name, budget"], "ok"),
          ("3 Scrubber", ["secrets → request blocked", "PII → masked, irreversible"], "ok"),
          ("4 Balance gate", ["prepaid balance checked", "before any billed tier"], "ok")]
    for i, (t, ls, st) in enumerate(r1):
        b += box(40 + i * 224, 112, 200, 112, None, [], "panel")
        b += [item(54 + i * 224, 138, t, st, "h3")] + [text(54 + i * 224, 164 + j * 18, l, "t") for j, l in enumerate(ls)]
        if i < 3:
            b += arrow([(240 + i * 224, 168), (264 + i * 224, 168)])
    b += arrow([(912, 168), (936, 168)])
    b += box(936, 112, 304, 232, None, [], "acc")
    b += [item(950, 136, "5 Tier chain", "ok", "h3"), text(950, 154, "start tier = keywords of the human turn", "s")]
    tiers = [("Ship", "fast open model", "ok"), ("Deep", "large open model", "ok"), ("Claude", "customer's key (BYOK)", "wip")]
    for i, (n, s, st) in enumerate(tiers):
        b += tile(950, 166 + i * 58, 150, 46, n, s, st)
        if i < 2:
            b += arrow([(1025, 212 + i * 58), (1025, 224 + i * 58)])
    b += [text(1112, 196, "<<ESCALATE>>", "s"), text(1112, 212, "or upstream", "s"), text(1112, 228, "failure → next", "s"),
          text(1112, 254, "tier kept for", "s"), text(1112, 270, "the human turn,", "s"), text(1112, 286, "then decided", "s"),
          text(1112, 302, "again", "s")]
    r2 = [("6 Metering", ["tokens in / cache / out,", "avoided-Claude-cost report"], "wip"),
          ("7 Response", ["secrets redacted in live events,", "replay and deliverables"], "ok"),
          ("8 Memory quarantine", ["notes written by agent runs", "wait for a human review"], "ok"),
          ("9 Audit journal", ["who did what, in which", "project, approved by whom"], "ok")]
    xs = [964, 676, 388, 40]
    for (t, ls, st), x in zip(r2, xs):
        w = 276 if x != 40 else 324
        b += box(x, 372, w, 104, None, [], "panel")
        b += [item(x + 14, 398, t, st, "h3")] + [text(x + 14, 424 + j * 18, l, "t") for j, l in enumerate(ls)]
    b += arrow([(1088, 344), (1088, 372)])
    b += arrow([(964, 424), (952, 424)]) + arrow([(676, 424), (664, 424)]) + arrow([(388, 424), (364, 424)])
    b.append(text(24, 520, "B · RELEASE PIPELINE (docs/RELEASING.md)", "k"))
    rel = [("Branch + tests", ["pytest (+ Postgres), ruff,", "tsc, next build, e2e CLI"]),
           ("Semver check", ["patch = fixes only;", "git diff vX.Y.Z..HEAD"]),
           ("Changelog", ["VERSION + upgrade notes", "for every behaviour change"]),
           ("Cut", ["tag, never moved;", "tarball named by hash"]),
           ("Channels", ["installer · managed cloud", "rollout · public demo"]),
           ("Roll back", ["scripts/rollback.sh <hash>", "keeps .env and volumes"])]
    for i, (t, ls) in enumerate(rel):
        x = 40 + i * 203
        b += box(x, 534, 185, 96, None, [], "panel")
        b += [item(x + 14, 558, t, "ok", "h3")] + [text(x + 14, 584 + j * 18, l, "t") for j, l in enumerate(ls)]
        if i < 5:
            b += arrow([(x + 185, 582), (x + 203, 582)])
    b.append(text(40, 656, "Enterprise features are switched on per instance, one by one, after the upgrade "
                           "(see FEATURES.md).", "s"))
    return svg(1280, 676, b, "SOKKAN Enterprise pipelines")


# ---------------------------------------------------------------- ONE-PAGE OVERVIEW (French)
def overview_svg():
    b = []
    b.append(text(0, 16, "LES ÉQUIPES", "k"))
    for i, (n, s_) in enumerate([("Développeurs", "code, revues"), ("DevOps · systèmes", "infra, déploiements"),
                                 ("DBA · QA", "données, tests"), ("Managers", "objectifs, suivi"),
                                 ("Administrateurs", "comptes, règles")]):
        b += tile(i * 212, 28, 196, 56, n, s_, None)
    b += ['<rect x="0" y="96" width="1056" height="34" rx="8" class="tile"/>',
          item(14, 118, "Connexion unique (SSO) : les équipes viennent de votre annuaire · droits par projet · "
                        "un compte inconnu est refusé", "wip", "t")]
    b += arrow([(528, 130), (528, 152)])
    b += box(0, 152, 1056, 252, None, [], "acc")
    b += [text(16, 178, "LE COCKPIT SOKKAN — UNE SEULE APPLICATION, OPEN SOURCE", "k"),
          text(1040, 178, "aussi depuis VS Code et le terminal", "s", "end")]
    tiles = [("Tableau", "kanban piloté par les sessions", "ok"), ("Sessions", "Claude Code en parallèle", "ok"),
             ("Crew", "agents gouvernés", "ok"), ("Aperçu", "valider sur le résultat", "ok"),
             ("Mémoire", "CortHeXis, par projet", "wip"), ("Ops", "alerte → incident piloté", "ok"),
             ("Coûts", "budgets, dépenses", "ok"), ("Magnitude", "vos propres GPU", "ok"),
             ("Nina", "assistante intégrée", "ok"), ("Helm", "vue manager (3.3)", "plan")]
    for i, (n, s_, st) in enumerate(tiles):
        b += tile(16 + (i % 5) * 206, 194 + (i // 5) * 70, 194, 60, n, s_, st)
    b += ['<rect x="16" y="340" width="1024" height="48" rx="8" class="tile"/>',
          item(30, 362, "Chaque fonction entreprise s'active une à une, avec ses dépendances vérifiées —", "wip", "t"),
          text(46, 380, "même code que la version communautaire, pas de version parallèle qui dérive.", "t")]
    b += arrow([(528, 404), (528, 424)])
    b.append(text(0, 444, "LES GARDE-FOUS", "k"))
    for i, (n, st) in enumerate([("Feu vert humain avant tout changement", "ok"), ("Double contrôle des agents", "ok"),
                                 ("Secrets donnés par nom, masqués", "ok"), ("Cloisonnement par projet", "wip"),
                                 ("Journal d'audit", "ok")]):
        x = i * 212
        b += [f'<rect x="{x}" y="454" width="196" height="40" rx="8" class="tile"/>']
        words = n.split(" ")
        if len(n) > 26:
            cut = len(words) // 2 + (1 if len(words) > 3 else 0)
            b += [item(x + 10, 470, " ".join(words[:cut]), st, "t"), text(x + 25, 487, " ".join(words[cut:]), "t")]
        else:
            b += [item(x + 10, 479, n, st, "t")]
    b += arrow([(170, 494), (170, 522)]) + arrow([(528, 494), (528, 522)]) + arrow([(886, 494), (886, 522)])
    b += box(0, 522, 340, 232, "Mémoire d'équipe", [("wip", "une mémoire par projet"), ("wip", "+ un fonds partagé en lecture"),
                                                    ("ok", "chaque session part en sachant"), (None, "     ce que l'équipe a appris"),
                                                    ("ok", "notes d'agents en quarantaine,"), (None, "     validées par un humain"),
                                                    ("ok", "recherche filtrée, sans fuite")],
             kicker="CORTHEXIS", lh=21)
    b += box(358, 522, 340, 232, "Inférence à paliers", [], kicker="SOUVERAINE", cls="acc")
    for i, (n, s_, st) in enumerate([("Ship", "rapide", "ok"), ("Deep", "réfléchi", "ok"), ("Claude", "votre clé", "wip")]):
        b += tile(372 + i * 108, 584, 96, 52, n, s_, st)
    b += arrow([(468, 610), (480, 610)]) + arrow([(576, 610), (588, 610)])
    b += [item(372, 662, "on monte d'un palier seulement si la tâche l'exige", "ok"),
          item(372, 683, "données nettoyées avant envoi (secrets, PII)", "ok"),
          item(372, 704, "rapport du coût Claude évité", "wip"),
          item(372, 725, "budget par session, par agent, par projet", "wip")]
    b += box(716, 522, 340, 232, "Où tournent les modèles", [("plan", "GPU dédiés en Suisse"), (None, "     (Exoscale, Genève) — en premier"),
                                                             ("ok", "vos machines, via Magnitude"), (None, "     (plusieurs nœuds)"),
                                                             ("ok", "points d'accès UE en secours"),
                                                             ("ok", "Claude avec votre clé, en dernier")], kicker="CALCUL", lh=21)
    b.append(text(0, 786, "INTÉGRATIONS", "k"))
    for i, (n, s_, st) in enumerate([("GitLab", "merge requests avec le jeton de la personne (3.2)", "plan"),
                                     ("Microsoft Teams", "@Nina, approbations, décisions (3.4)", "plan"),
                                     ("Supervision", "Prometheus · Grafana · Loki", "ok")]):
        b += tile(i * 358, 796, 340, 54, n, s_, st)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 0 1062 852" width="1062" height="852" role="img" '
            f'aria-label="Architecture de SOKKAN Enterprise">{STYLE}<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ah"/></marker></defs>'
            + "\n".join(b) + "</svg>")


def overview_html():
    tpl = (Path(__file__).resolve().parent / "overview.template.html").read_text(encoding="utf-8")
    return tpl.replace("<!--DIAGRAM-->", overview_svg())


if __name__ == "__main__":
    for name, fn in (("hld.svg", hld), ("method.svg", method), ("pipeline.svg", pipeline),
                     ("hld-overview.html", overview_html)):
        (OUT / name).write_text(fn(), encoding="utf-8")
        print("wrote", OUT / name)
