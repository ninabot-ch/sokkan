"""Run in the KERNEL container (stdin of `docker compose exec -T kernel python3 -`): the socket
proxy must refuse what a compromised kernel would ask — exit 0 when every request is refused."""
import json
import os
import urllib.error
import urllib.request

PROXY = "http://kernel-dockerproxy:2375/v1.44"
IMAGE = os.environ.get("NOKKER_SANDBOX_IMAGE") or "busybox"
ASKS = {
    "privileged": {"Image": IMAGE, "Labels": {"nokker.sandbox": "x"},
                   "HostConfig": {"Privileged": True, "SecurityOpt": ["no-new-privileges"]}},
    "host root": {"Image": IMAGE, "Labels": {"nokker.sandbox": "x"},
                  "HostConfig": {"Binds": ["/:/host"], "SecurityOpt": ["no-new-privileges"]}},
    "docker socket": {"Image": IMAGE, "Labels": {"nokker.sandbox": "x"},
                      "HostConfig": {"Binds": ["/var/run/docker.sock:/s"], "SecurityOpt": ["no-new-privileges"]}},
    "named volume": {"Image": IMAGE, "Labels": {"nokker.sandbox": "x"},
                     "HostConfig": {"Mounts": [{"Type": "volume", "Source": "x", "Target": "/x"}],
                                    "SecurityOpt": ["no-new-privileges"]}},
}
bad = []
for name, body in ASKS.items():
    req = urllib.request.Request(PROXY + "/containers/create", method="POST", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        bad.append(name)
    except urllib.error.HTTPError as e:
        if e.code != 403:
            bad.append(f"{name}: {e.code}")
# a container that is not a sandbox: invisible and untouchable
try:
    urllib.request.urlopen(PROXY + "/networks", timeout=10)
    bad.append("networks listed")
except urllib.error.HTTPError as e:
    if e.code != 403:
        bad.append(f"networks: {e.code}")
print(json.dumps({"refused_all": not bad, "not_refused": bad}))
raise SystemExit(1 if bad else 0)
