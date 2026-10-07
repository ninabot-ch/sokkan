"""Helm chart deploy/helm/sokkan: `helm lint` + `helm template` for every overlay, and what
the rendered manifests must guarantee — no root, no privileged, limits everywhere, dedicated
ServiceAccounts, OpenShift restricted SCC compatibility. Needs the `helm` binary (PATH or
SOKKAN_HELM); otherwise SKIPPED with the reason."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy" / "helm" / "sokkan"
HELM = os.environ.get("SOKKAN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="helm binary not found (PATH or SOKKAN_HELM)")

DB = ["--set", "database.url=postgresql://u:p@db.example.com:5432/sokkan?sslmode=require"]
OVERLAYS = {
    "default": DB,
    "sks": ["-f", str(CHART / "values-sks.yaml"), "--set", "database.existingSecret=sokkan-db"],
    "openshift": ["-f", str(CHART / "values-openshift.yaml")] + DB,
    "k3d": ["-f", str(CHART / "ci" / "k3d-values.yaml")],
    "everything": DB + ["--set", "embeddings.enabled=true", "--set", "vllm.enabled=true",
                        "--set", "externalSecret.enabled=true",
                        "--set", "secrets.vaultKey.existingSecret=vk"],
}
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")


def render(*args: str) -> list[dict]:
    r = subprocess.run([HELM, "template", "rel", str(CHART), "--namespace", "sk", *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def pod_specs(docs: list[dict]):
    for d in docs:
        if d["kind"] in WORKLOADS:
            yield d, d["spec"]["template"]["spec"]


@pytest.mark.parametrize("overlay", list(OVERLAYS))
def test_helm_lint(overlay):
    r = subprocess.run([HELM, "lint", str(CHART), "--strict", *OVERLAYS[overlay]],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("overlay", list(OVERLAYS))
def test_no_root_no_privileged_limits_everywhere(overlay):
    docs = render(*OVERLAYS[overlay])
    assert list(pod_specs(docs)), "no workload rendered"
    for d, spec in pod_specs(docs):
        where = f"{overlay}/{d['kind']}/{d['metadata']['name']}"
        psc = spec.get("securityContext") or {}
        assert psc.get("runAsNonRoot") is True, where
        assert psc.get("runAsUser", 1) != 0, where
        assert psc.get("seccompProfile", {}).get("type") == "RuntimeDefault", where
        for k in ("hostNetwork", "hostPID", "hostIPC", "hostUsers"):
            assert k not in spec, f"{where}: {k}"
        assert not any("hostPath" in v for v in spec.get("volumes") or []), where
        for c in (spec.get("initContainers") or []) + spec["containers"]:
            cw = f"{where}/{c['name']}"
            sc = c.get("securityContext") or {}
            assert sc.get("privileged") is not True, cw
            assert sc.get("allowPrivilegeEscalation") is False, cw
            assert sc.get("capabilities", {}).get("drop") == ["ALL"], cw
            assert not sc.get("capabilities", {}).get("add"), cw
            assert sc.get("runAsUser", 1) != 0, cw
            lim = (c.get("resources") or {}).get("limits") or {}
            req = (c.get("resources") or {}).get("requests") or {}
            assert lim.get("cpu") and lim.get("memory"), f"{cw}: limits"
            assert req.get("cpu") and req.get("memory"), f"{cw}: requests"


def test_dedicated_service_accounts_and_minimal_rbac():
    docs = render(*DB)
    sas = {d["metadata"]["name"] for d in docs if d["kind"] == "ServiceAccount"}
    by = {d["metadata"]["name"]: s for d, s in pod_specs(docs)}
    api, web = by["rel-sokkan-api"], by["rel-sokkan-web"]
    assert api["serviceAccountName"] == "rel-sokkan-api" in sas
    assert web["serviceAccountName"] == "rel-sokkan-web" in sas
    assert web["automountServiceAccountToken"] is False
    assert "rel-sokkan-session" in sas
    assert not [d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    role = next(d for d in docs if d["kind"] == "Role")
    for rule in role["rules"]:
        assert "*" not in rule["verbs"] and "*" not in rule["resources"], rule
    assert {r for rule in role["rules"] for r in rule["resources"]} == {
        "pods", "pods/log", "secrets"}
    env = {e["name"]: e.get("value") for e in api["containers"][0]["env"]}
    assert env["SOKKAN_RUNNER"] == "kubernetes"
    assert env["SOKKAN_K8S_SESSION_SERVICE_ACCOUNT"] == "rel-sokkan-session"
    assert env["SOKKAN_RUNNER_MOUNTS"] == "/data=pvc:rel-sokkan-data"
    assert env["SOKKAN_K8S_SESSION_AFFINITY"] == "api"   # RWO default
    assert env["SOKKAN_SESSION_MEMORY_LIMIT"] == "2Gi"


def test_openshift_overlay_sets_no_uid_or_fsgroup():
    docs = render(*OVERLAYS["openshift"])
    for d, spec in pod_specs(docs):
        psc = spec.get("securityContext") or {}
        assert not {"runAsUser", "runAsGroup", "fsGroup"} & set(psc), d["metadata"]["name"]
        for c in (spec.get("initContainers") or []) + spec["containers"]:
            assert "runAsUser" not in (c.get("securityContext") or {})
    api = next(s for d, s in pod_specs(docs) if d["metadata"]["name"].endswith("-api"))
    env = {e["name"]: e.get("value") for e in api["containers"][0]["env"]}
    assert env["SOKKAN_K8S_RUN_AS_USER"] == "" and env["SOKKAN_K8S_FS_GROUP"] == ""
    ing = next(d for d in docs if d["kind"] == "Ingress")
    paths = {p["path"]: p["backend"]["service"]["name"]
             for p in ing["spec"]["rules"][0]["http"]["paths"]}
    assert paths == {"/api": "rel-sokkan-api", "/term": "rel-sokkan-api", "/": "rel-sokkan-web"}


def test_network_policies_isolate_the_sessions():
    docs = render(*DB)
    np = {d["metadata"]["name"]: d for d in docs if d["kind"] == "NetworkPolicy"}
    sess = np["rel-sokkan-session"]["spec"]
    assert set(sess["policyTypes"]) == {"Ingress", "Egress"}
    ports = sorted(p["port"] for rule in sess["egress"] for p in rule["ports"])
    assert ports == [53, 53, 3128, 5353, 5353, 8098]   # relay, egress gateway, DNS: nothing else
    assert [p["port"] for p in sess["ingress"][0]["ports"]] == [7070]


def test_the_chart_refuses_unsupported_setups():
    for args, msg in ((DB + ["--set", "api.replicas=2"], "not supported yet"),
                      (DB + ["--set", "api.autoscaling.enabled=true"], "not supported yet"),
                      ([], "a Postgres is required")):
        r = subprocess.run([HELM, "template", "rel", str(CHART), *args],
                           capture_output=True, text=True)
        assert r.returncode != 0 and msg in r.stderr, (args, r.stderr)
    docs = render(*DB, "--set", "api.replicas=2", "--set", "api.allowMultipleReplicas=true")
    assert any(d["kind"] == "PodDisruptionBudget" and d["metadata"]["name"].endswith("-api")
               for d in docs)


def test_embed_script_is_the_compose_one():
    assert (CHART / "files" / "embed-run.sh").read_text() == (
        ROOT / "docker" / "embed" / "run.sh").read_text(), "cp docker/embed/run.sh into the chart"
