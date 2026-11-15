"""Conformance C1–C8 against the kernel engine (backend/harness/kernel.py), for real: a
running kernel whose model is a scripted endpoint started by this test
(tests/kernel_upstream.py). No model is called.

The kernel is not started here (it is a container image, not a dependency of this
repository). Point the test at one:

    SOKKAN_KERNEL_TEST_URL         the kernel's API (e.g. http://127.0.0.1:18742)
    SOKKAN_KERNEL_TEST_TOKEN_FILE  its local token
    SOKKAN_KERNEL_TEST_ROOT        a folder declared as a code root of the kernel, at the
                                   same path for the kernel and for this test
    SOKKAN_KERNEL_TEST_UPSTREAM_PORT  the port the kernel's model tiers point to
                                   (``http://127.0.0.1:<port>/v1``)

Kernel settings the scenarios assume: the question piece on (``host_clarify_options``), the
session cap on (``host_session_budget``), long memory, judge and web search off. Without the
variables, the tests skip.
"""
import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from harness import contract  # noqa: E402
from harness.conformance import driver, scenarios  # noqa: E402
from kernel_upstream import ScriptedUpstream  # noqa: E402

ENV = ("SOKKAN_KERNEL_TEST_URL", "SOKKAN_KERNEL_TEST_TOKEN_FILE", "SOKKAN_KERNEL_TEST_ROOT",
       "SOKKAN_KERNEL_TEST_UPSTREAM_PORT")


@pytest.fixture(scope="module")
def upstream():
    if not all(os.environ.get(k) for k in ENV):
        pytest.skip("no kernel to play against (SOKKAN_KERNEL_TEST_*)")
    with ScriptedUpstream(port=int(os.environ["SOKKAN_KERNEL_TEST_UPSTREAM_PORT"])) as up:
        yield up


class KernelRig(driver.Rig):
    name = "kernel"

    def __init__(self, up: ScriptedUpstream, root: Path) -> None:
        tag = uuid.uuid4().hex[:8]
        self.workspace = root / f"work-{tag}"
        self.outside = root.parent / f"outside-{tag}"
        self.workspace.mkdir(parents=True)
        self.outside.mkdir(parents=True)
        self.up = up
        up.turns.clear()
        up.requests.clear()
        up.workspace = str(self.workspace)

    def cleanup(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def capabilities(self):
        from harness import kernel
        return kernel.KernelHarness().capabilities()

    def harness(self):
        from harness import kernel
        return kernel.KernelHarness()

    def spec(self, *, context=None, resume=None, confined=False):
        return contract.RunSpec(
            run_id="run-" + uuid.uuid4().hex[:8], cwd=str(self.workspace), project="p-7f3a",
            user="u-91c2", context=context, resume=resume,
            confinement=contract.Confinement(project="p-7f3a") if confined else None)

    def script(self, *turns):
        self.up.turns.extend(turns)

    def model_saw(self, text):
        return self.up.saw(text)

    def last_request_saw(self, text):
        return self.up.last_saw(text)


@pytest.fixture()
def kernel_rig(upstream, monkeypatch):
    monkeypatch.setenv("SOKKAN_KERNEL_URL", os.environ["SOKKAN_KERNEL_TEST_URL"])
    monkeypatch.setenv("SOKKAN_KERNEL_TOKEN_FILE", os.environ["SOKKAN_KERNEL_TEST_TOKEN_FILE"])
    monkeypatch.delenv("SOKKAN_KERNEL_TOKEN", raising=False)
    rig = KernelRig(upstream, Path(os.environ["SOKKAN_KERNEL_TEST_ROOT"]))
    yield rig
    rig.cleanup()


def _play(scenario, rig):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(scenario(rig), 120))
    finally:
        loop.close()


@pytest.mark.parametrize("scenario", [s for _, s in scenarios.ALL],
                         ids=[n for n, _ in scenarios.ALL])
def test_kernel_engine(scenario, kernel_rig):
    _play(scenario, kernel_rig)


def test_kernel_refuses_what_it_cannot_honour(kernel_rig):
    """A run that relies on removed tools or on a plan mode is refused, never run wider."""
    from harness import kernel
    spec = kernel_rig.spec()
    spec.deny_tools = ("terminal",)
    with pytest.raises(contract.Unsupported):
        kernel.KernelHarness().start(spec)
    spec = kernel_rig.spec()
    spec.plan = True
    with pytest.raises(contract.Unsupported):
        kernel.KernelHarness().start(spec)


def test_a_resume_the_kernel_does_not_have_is_refused(kernel_rig):
    """The kernel would reopen an unknown thread as an empty one: the adapter refuses."""
    from harness import kernel

    async def go():
        stream = kernel.KernelHarness().start(kernel_rig.spec(resume="th_0unknown0thread"))
        with pytest.raises(contract.ContractError, match="unknown engine session"):
            await stream.__anext__()

    _play(lambda _rig: go(), kernel_rig)


def test_a_run_resumes_after_a_restart_of_the_kernel(kernel_rig):
    """C4 with the process really gone: the kernel is restarted between the two runs
    (SOKKAN_KERNEL_TEST_RESTART_CMD, e.g. `docker restart <kernel>`; the kernel must keep its
    threads in Postgres). The history is intact."""
    import subprocess
    import time
    import urllib.request
    cmd = os.environ.get("SOKKAN_KERNEL_TEST_RESTART_CMD")
    if not cmd:
        pytest.skip("SOKKAN_KERNEL_TEST_RESTART_CMD not set")
    rig = kernel_rig
    rig.script([driver.say("Noted.")], [driver.say("heron")])

    async def first():
        async with driver.Run(rig.harness(), rig.spec()) as run:
            await run.turn("Remember this word: heron.")
        return run.ended.engine_session

    loop = asyncio.new_event_loop()
    try:
        session = loop.run_until_complete(first())
    finally:
        loop.close()
    subprocess.run(cmd, shell=True, check=True, capture_output=True)   # noqa: S602 — operator's command
    url = os.environ["SOKKAN_KERNEL_TEST_URL"] + "/healthz"
    for _ in range(120):
        try:
            if urllib.request.urlopen(url, timeout=2).status == 200:     # noqa: S310 — the test's kernel
                break
        except OSError:
            time.sleep(1)

    async def again():
        async with driver.Run(rig.harness(), rig.spec(resume=session)) as run:
            assert run.started.resumed_from == session
            t = await run.turn("Which word did I ask you to remember?")
        assert t.ended.reported and not t.ended.is_error

    _play(lambda _rig: again(), rig)
    assert rig.last_request_saw("Remember this word: heron."), "the history was lost on restart"
