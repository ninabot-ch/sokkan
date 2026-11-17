"""Launcher of the kernel's docker-socket proxy for SOKKAN (docker-compose.kernel.yml, service
`kernel-dockerproxy`). Runs the kernel's OWN `dockerproxy.py` (its `sandboxd` image), unchanged,
with one compatibility rule — until the kernel's proxy accepts it itself (kernel gap n°24):

The library under the kernel probes once whether docker applies memory/CPU caps, with a
throwaway container `docker run --rm --cpus 0.5 --memory 64m --pids-limit 32 <image> sleep 0`
(unlabelled, no mount). The proxy accepts an unlabelled probe only with the command `true`: it
refuses `sleep 0`, the library concludes « caps unsupported », starts every sandbox WITHOUT caps,
and the kernel — rightly — refuses every session whose caps are set (503 sandbox_unavailable).
Seen on the kernel-compose battery (tests/e2e_kernel_compose). The rule below turns exactly
that command into `true` before the proxy's own checks run: same image, same caps, nothing
mounted, the proxy still labels it, gives it no network and checks everything else. Any other
command is left as is (and refused by the proxy, as before).
"""
import asyncio
import sys

sys.path.insert(0, "/opt/sandboxd")

import dockerproxy  # noqa: E402  — the kernel's proxy, from its image

PROBE_CMDS = (["sleep", "0"],)
_check_create = dockerproxy.check_create


def check_create(body: dict, cfg) -> dict:
    labels = body.get("Labels") or {}
    if dockerproxy.LABEL not in labels and body.get("Cmd") in PROBE_CMDS:
        body["Cmd"] = ["true"]
    return _check_create(body, cfg)


dockerproxy.check_create = check_create

if __name__ == "__main__":
    asyncio.run(dockerproxy.main())
