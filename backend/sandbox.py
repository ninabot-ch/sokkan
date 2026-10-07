#!/usr/bin/env python3
"""sandbox.py — SOKKAN 3.2 lot 8: a session or an agent run of a project reaches only its
project's space.

Feature `sandbox` (requires `multi_project`). It applies to every project except `default`
(the instance's workspace keeps its 3.1 behaviour). Two layers:

1. **Tool hook (always, `hooks-only` and `bwrap`)** — a PreToolUse hook of the Agent SDK,
   which runs BEFORE the permission rules (an `allow` rule of the user's settings or a
   SAFE_TOOLS entry cannot short-circuit it). Read / Write / Edit / MultiEdit /
   NotebookEdit / Glob / Grep on a path outside the project's space are refused. Paths are
   made absolute against the session's working directory, `..` is collapsed and symlinks
   are resolved (`os.path.realpath`) before the check, so a link that points outside is
   refused too. Every refusal is logged (stderr + audit `sandbox.deny`).
2. **Bash** — without an OS boundary a shell can open any file the API user can read, so:
   * `hooks-only` (bubblewrap absent or not usable on this host): Bash is refused outside
     `default`;
   * `bwrap`: the command is rewritten to run inside bubblewrap — only the project's
     workspace is mounted read-write, its memory directory and the `shared` project
     read-only, /usr (+ the /bin, /lib… links, a few /etc files) read-only, a private
     /tmp, /proc, /dev; no network unless `SOKKAN_SANDBOX_NETWORK=1`; an empty
     environment except a short allow-list (no API secret reaches the shell).

Modes, detected once at startup (`detect()`) and served by `GET /api/features`
(`sandbox`: off | hooks-only | bwrap | pod):

* `pod` — the reference strong isolation (SOKKAN Enterprise on Kubernetes / OpenShift
  restricted SCC): sessions and runs execute in their own pod through the session runner
  (`SOKKAN_RUNNER=kubernetes`), which mounts only the project's volume. The tool hook stays
  on as a second layer; Bash runs in the pod, not wrapped.
* `bwrap` — opportunistic, for a compose / k3s host where bubblewrap AND user namespaces
  are available (a probe run must succeed).
* `hooks-only` — everything else (stock Docker, restricted SCC without the runner): the
  hook, and no Bash outside `default`.

Space of a project ``<slug>`` (``$SOKKAN_DATA_DIR/projects/<slug>``):
  write: ``work/`` (the session's working directory);
  read:  the whole project directory, ``projects/shared``, and the CLI's own files for
         this working directory (tool results it asks the model to Read).
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

OFF, HOOKS, BWRAP, POD = "off", "hooks-only", "bwrap", "pod"
READ_TOOLS = ("Read", "Glob", "Grep", "LS", "NotebookRead")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
FILE_TOOLS = READ_TOOLS + WRITE_TOOLS
HOOK_MATCHER = "|".join((*FILE_TOOLS, "Bash"))
# environment handed to a sandboxed shell (names; the values are read at run time)
ENV_PASS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "USER", "LOGNAME")
# /etc entries a shell usually needs (read-only, each only if present)
ETC_RO = ("passwd", "group", "hosts", "resolv.conf", "nsswitch.conf", "ssl",
          "ca-certificates", "ca-certificates.conf", "localtime", "alternatives",
          "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "bash.bashrc", "profile", "inputrc",
          "mime.types", "gitconfig")
_GLOB_CHARS = "*?[{"

_mode: str | None = None
_detail: str = ""
_probe_note: str | None = None   # cached probe result for readiness() ("" = bwrap works)


# ---- detection ------------------------------------------------------------------------

def bwrap_path() -> str:
    return (os.environ.get("SOKKAN_SANDBOX_BWRAP") or "").strip() or (shutil.which("bwrap") or "")


def pod_isolation() -> bool:
    """Sessions and runs execute in their own pod / container (the session runner,
    `SOKKAN_RUNNER=kubernetes`): the reference strong boundary on Kubernetes / OpenShift
    (restricted SCC: no root, arbitrary uid, no user namespaces → no bubblewrap)."""
    return (os.environ.get("SOKKAN_RUNNER") or "").strip().lower() in ("kubernetes", "k8s")


def network_allowed() -> bool:
    return (os.environ.get("SOKKAN_SANDBOX_NETWORK") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _probe(path: str) -> str | None:
    """None when bubblewrap runs here, else why not."""
    if not path:
        return "bubblewrap (bwrap) not found"
    if not os.access(path, os.X_OK):
        return f"{path} is not executable"
    args = [path, "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
            *_root_links(), "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true"]
    try:
        r = subprocess.run(args, capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        return f"bwrap probe failed: {e!r}"
    if r.returncode != 0:
        return ("bwrap probe failed (user namespaces unavailable?): "
                + (r.stderr or b"").decode(errors="replace").strip()[:200])
    return None


def detect(force: bool = False) -> str:
    """Effective mode, computed once (startup) — `force` re-probes (tests, admin)."""
    global _mode, _detail
    if _mode is not None and not force:
        return _mode
    import features
    if not features.enabled("sandbox"):
        _mode, _detail = OFF, "feature `sandbox` is off"
        return _mode
    if pod_isolation():
        _mode, _detail = POD, ("sessions run in their own pod (SOKKAN_RUNNER=kubernetes); the "
                               "tool hook stays on as a second layer")
        print(f"[sandbox] mode {_mode}: {_detail}", file=sys.stderr)
        return _mode
    why = _probe(bwrap_path())
    if why is None:
        _mode = BWRAP
        _detail = f"bubblewrap {bwrap_path()}" + (", network allowed"
                                                  if network_allowed() else ", no network")
    else:
        _mode, _detail = HOOKS, why + " — Bash is refused outside the default project"
    print(f"[sandbox] mode {_mode}: {_detail}", file=sys.stderr)
    return _mode


def mode() -> str:
    return detect()


def state() -> dict:
    return {"mode": mode(), "detail": _detail, "network": network_allowed()}


def reset() -> None:
    """Forget the detected mode (tests)."""
    global _mode, _detail, _probe_note
    _mode, _detail, _probe_note = None, "", None


def applies(project: str | None) -> bool:
    """Does the sandbox confine a session of this project? Never `default`."""
    return mode() != OFF and project != "default"


# ---- the project's space --------------------------------------------------------------

def _data() -> Path:
    return Path(os.environ.get("SOKKAN_DATA_DIR", os.path.expanduser("~/.local/share/sokkan")))


def project_dir(project: str) -> Path:
    return _data() / "projects" / (project or "_no-project")


def _cli_dirs(cwd: str) -> list[str]:
    """Files the Claude Code CLI itself writes for this working directory and may ask the
    model to Read (large tool results, background task output)."""
    slug = cwd.replace("/", "-")
    claude = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    out = [os.path.join(claude, "projects", slug)]
    try:
        out.append(os.path.join("/tmp", f"claude-{os.getuid()}", slug))
    except AttributeError:  # pragma: no cover — not POSIX
        pass
    return out


def roots(project: str, cwd: str) -> tuple[list[str], list[str]]:
    """(read roots, write roots), real paths."""
    rp = os.path.realpath
    write = [rp(cwd)]
    read = [*write, rp(project_dir(project)), rp(project_dir("shared")),
            *(rp(d) for d in _cli_dirs(cwd))]
    extra = os.environ.get("SOKKAN_SANDBOX_READ_PATHS", "")
    read += [rp(p) for p in extra.split(":") if p.strip()]
    return read, write


def resolve(path: str, cwd: str) -> str:
    """Absolute, `..` collapsed, symlinks resolved (also for a file that does not exist
    yet: its existing parent is resolved)."""
    p = os.path.expanduser(str(path))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(p)


def _inside(p: str, roots_: list[str]) -> bool:
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots_)


def _glob_base(pattern: str) -> str:
    """Static part of a glob pattern (what it can reach): `/etc/**/x` → `/etc`."""
    cut = min([pattern.find(c) for c in _GLOB_CHARS if c in pattern] or [len(pattern)])
    head = pattern[:cut]
    if cut == len(pattern):
        return head
    if "/" not in head:
        return ""
    return head.rsplit("/", 1)[0] or "/"


def tool_paths(tool: str, inp: dict, cwd: str) -> list[str]:
    """Every path a file tool call can touch, resolved."""
    inp = inp or {}
    out: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            out.append(resolve(v, cwd))
    raw = inp.get("path")
    base = resolve(raw, cwd) if isinstance(raw, str) and raw.strip() else cwd
    # Glob `pattern` / Grep `glob` are path patterns: their static part is a path too
    key = {"Glob": "pattern", "Grep": "glob"}.get(tool)
    v = inp.get(key) if key else None
    if isinstance(v, str) and v.strip():
        b = _glob_base(v.strip())
        if b:
            out.append(resolve(b, base))
    if tool in ("Glob", "Grep", "LS") and not out:
        out.append(resolve(cwd, cwd))
    return out


def check_file_tool(tool: str, inp: dict, project: str, cwd: str) -> str | None:
    """None = allowed, else the reason of the refusal."""
    read, write = roots(project, cwd)
    allowed = write if tool in WRITE_TOOLS else read
    for p in tool_paths(tool, inp, cwd):
        if not _inside(p, allowed):
            what = "write" if tool in WRITE_TOOLS else "read"
            return (f"Sandbox: {tool} cannot {what} {p} — a session of project "
                    f"'{project or '?'}' only reaches its own workspace ({cwd})"
                    + ("" if tool in WRITE_TOOLS else " and the shared project") + ".")
    return None


# ---- Bash inside bubblewrap -----------------------------------------------------------

def _root_links() -> list[str]:
    """/bin, /lib… : symlinks into /usr on merged-/usr systems, bound read-only otherwise."""
    out: list[str] = []
    for d in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        p = "/" + d
        if os.path.islink(p):
            out += ["--symlink", os.readlink(p), p]
        elif os.path.isdir(p):
            out += ["--ro-bind", p, p]
    return out


def bwrap_argv(project: str, cwd: str) -> list[str]:
    """The bubblewrap command line (without the shell part)."""
    work = os.path.realpath(cwd)
    pdir = project_dir(project)
    a = [bwrap_path(), "--unshare-all"]
    if network_allowed():
        a.append("--share-net")
    a += ["--die-with-parent", "--new-session", "--clearenv",
          "--ro-bind", "/usr", "/usr", *_root_links()]
    for e in ETC_RO:
        a += ["--ro-bind-try", f"/etc/{e}", f"/etc/{e}"]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for extra in (os.environ.get("SOKKAN_SANDBOX_RO_PATHS") or "").split(":"):
        if extra.strip():
            a += ["--ro-bind-try", extra.strip(), extra.strip()]
    for ro in (pdir / "memory", project_dir("shared")):
        a += ["--ro-bind-try", str(ro), str(ro)]
    a += ["--bind", work, work, "--chdir", work,
          "--setenv", "HOME", work, "--setenv", "SOKKAN_SANDBOX", "bwrap",
          "--setenv", "SOKKAN_SESSION_PROJECT", project or ""]
    return a


def wrapper_script(sid: str, project: str, cwd: str, env_names: list[str] | tuple = ()
                   ) -> str:
    """Write the session's wrapper (outside every sandbox mount) and return its path. The
    model's command becomes `<wrapper> '<command>'`: short in the transcript and the
    permission prompt, and the values of the environment never appear on a command line."""
    d = _data() / "sandbox" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d.parent, 0o700)
    path = d / f"{sid}.sh"
    argv = bwrap_argv(project, cwd)
    lines = ["#!/bin/bash", "# SOKKAN sandbox wrapper (lot 8) — generated, do not edit", "set -u",
             "args=(" + " ".join(shlex.quote(x) for x in argv) + ")"]
    for name in (*ENV_PASS, *env_names):
        if name.replace("_", "").isalnum():
            lines.append(f'[ -n "${{{name}+x}}" ] && args+=(--setenv {name} "${name}")')
    lines.append('exec "${args[@]}" -- /bin/bash -c "$1"')
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return str(path)


def wrap(script: str, command: str) -> str:
    return f"{shlex.quote(script)} {shlex.quote(command)}"


def unwrap(script: str, command: str) -> str | None:
    """The inner command when `command` is exactly `<script> '<inner>'`, else None."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) == 2 and parts[0] == script and wrap(script, parts[1]) == command:
        return parts[1]
    return None


_UNSAFE = set(";&|`$()<>\n\r")


def rule_allows(rules: list[str], command: str) -> bool:
    """Would one of these Claude Code `Bash(...)` auto-approve rules approve `command`?
    Conservative: a command with shell operators never matches a prefix rule."""
    for r in rules or []:
        r = r.strip()
        if r == "Bash":
            return True
        if not (r.startswith("Bash(") and r.endswith(")")):
            continue
        body = r[5:-1]
        if any(c in _UNSAFE for c in command):
            continue
        if body.endswith(":*"):
            pre = body[:-2]
            if command == pre or command.startswith(pre + " "):
                return True
        elif command.strip() == body:
            return True
    return False


# ---- the SDK hook ---------------------------------------------------------------------

def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _log_deny(sid: str, user: str, project: str, tool: str, reason: str) -> None:
    print(f"[sandbox] denied {tool} in session {sid} ({project}): {reason}", file=sys.stderr)
    try:
        import audit
        audit.log(user or f"session:{sid}", "sandbox.deny", f"{project}:{tool}", reason[:300],
                  project=project)
    except Exception:  # noqa: BLE001 — the refusal stands even if the journal is down
        pass


def decide(tool: str, inp: dict, *, sid: str, user: str, project: str, cwd: str,
           script: str | None, auto_rules: list[str] | None = None, pod: bool = False
           ) -> dict:
    """Hook output for one tool call ({} = no opinion: the normal permission flow)."""
    if tool in FILE_TOOLS:
        why = check_file_tool(tool, inp, project, cwd)
        if why:
            _log_deny(sid, user, project, tool, why)
            return _deny(why)
        return {}
    if tool == "Bash":
        cmd = str((inp or {}).get("command") or "")
        if pod:
            return {}       # the pod is the boundary: normal permission flow
        if not script:
            why = (f"Sandbox: Bash is not available in project '{project or '?'}' on this "
                   "instance (no OS sandbox: bubblewrap is not installed or not usable). Use "
                   "Read/Glob/Grep/Edit/Write on the project's workspace.")
            _log_deny(sid, user, project, tool, why)
            return _deny(why)
        inner = unwrap(script, cmd)
        new = {**inp, "command": wrap(script, inner if inner is not None else cmd)}
        # an agent run's auto-approve rule still approves (now inside the sandbox);
        # everything else goes through the normal permission flow with the wrapped command
        decision = "allow" if auto_rules and rule_allows(auto_rules, inner or cmd) else "ask"
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": decision,
                                       "updatedInput": new}}
    return {}


def sdk_hooks(*, sid: str, user: str, project: str, cwd: str,
              auto_rules: list[str] | None = None, env_names: list[str] | tuple = ()
              ) -> dict:
    """`ClaudeAgentOptions.hooks` entries confining one session ({} when not applicable)."""
    if not applies(project):
        return {}
    from claude_agent_sdk import HookMatcher  # type: ignore
    script = wrapper_script(sid, project, cwd, env_names) if mode() == BWRAP else None

    async def pre_tool(payload, _tool_use_id, _context):
        try:
            return decide(str(payload.get("tool_name") or ""), payload.get("tool_input") or {},
                          sid=sid, user=user, project=project, cwd=cwd, script=script,
                          auto_rules=auto_rules, pod=mode() == POD)
        except Exception as e:  # noqa: BLE001 — fail closed
            return _deny(f"Sandbox check failed: {e!r}")

    return {"PreToolUse": [HookMatcher(matcher=HOOK_MATCHER, hooks=[pre_tool])]}


def recheck(tool: str, inp: dict, *, sid: str, user: str, project: str, cwd: str) -> dict | str:
    """After a human approval (the browser may send an edited input): the input that will
    really run, or the reason it may not. Bash is (re)wrapped; file paths re-checked."""
    if not applies(project):
        return inp
    if tool in FILE_TOOLS:
        why = check_file_tool(tool, inp, project, cwd)
        if why:
            _log_deny(sid, user, project, tool, why)
            return why
        return inp
    if tool == "Bash":
        if mode() == POD:
            return inp
        if mode() != BWRAP:
            return "Sandbox: Bash is not available in this project on this instance."
        script = str(_data() / "sandbox" / "sessions" / f"{sid}.sh")
        cmd = str(inp.get("command") or "")
        inner = unwrap(script, cmd)
        return {**inp, "command": wrap(script, inner if inner is not None else cmd)}
    return inp


def display_command(command: str) -> str:
    """`<wrapper> 'cmd'` → `cmd` for titles (the UI shows what the model asked)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if (len(parts) == 2 and parts[0].endswith(".sh") and "/sandbox/sessions/" in parts[0]):
        return parts[1]
    return command


def readiness() -> str | None:
    """Informational note for Profile → Features (never gates)."""
    global _probe_note
    if pod_isolation():
        return "pod: sessions run in their own pod (reference isolation); tool hook on top"
    if _probe_note is None:
        _probe_note = _probe(bwrap_path()) or ""
    if not _probe_note:
        return "bwrap: Bash runs inside bubblewrap" + (
            " (network allowed)" if network_allowed() else " (no network)")
    return "hooks-only: bubblewrap absent or unusable — Bash refused outside `default`"
