# Releasing SOKKAN

Rules for numbering and cutting a release. The cut itself (tag, dist, site, fleet) is done
by the maintainers' release script; this page is what every release must respect.

## Version numbers (semver)

SOKKAN versions are `MAJOR.MINOR.PATCH`. From 3.2 on:

* **Patch (`X.Y.Z+1`) = fixes only.** A patch corrects behaviour that was wrong or
  unsafe against what the release already promised. It adds no feature, no new
  configuration variable, no new API route or field, and changes no default.
* **Minor (`X.Y+1.0`) = anything else that is backward compatible**: a new feature, a new
  `SOKKAN_*` variable, a new API route, field or MCP tool, a new UI element that is not a
  fix, **a changed default** (even towards safer), a new database column.
* **Major (`X+1.0.0`) = a break**: removed or renamed variable, route, field or tool, a
  data migration that cannot be rolled back, a behaviour change that requires operators
  to act before upgrading.
* **Security exception (approved by the maintainers, 07.10.2026).** A security fix ships as a patch even when closing the hole
  needs a new variable or a stricter behaviour, provided that (1) the new variable only
  tunes the fix and has a safe default, (2) nothing else rides along, and (3) the
  changelog entry says "Security patch" and lists every behaviour change under
  *Upgrade notes*. Anything not strictly needed to close the hole waits for the next
  minor.
* **A patch keeps the name of its minor** (`3.1.2 — "Crew up"`).
* **Never re-tag.** A published tag is never moved or deleted; a mistake is fixed by the
  next version. Releases already published (e.g. 3.1.1, which shipped features under a
  patch number) stay as they are.
* **Check before cutting**: `git diff vX.Y.Z..HEAD` — any new `SOKKAN_*` name in
  `backend/` or `docker-compose.yml`, any new route in `app.py`, any changed default
  ⇒ not a patch (unless the security exception applies, written in the changelog).
