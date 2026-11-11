# Results kept with the code

One file per run that was played to the end: `<version>_<commit>.json` (what `suite.py report`
writes). Machine: Docker-in-Docker container, 6 GB, on our build host; local model and Deep
gateway = `mock_llm.py`; release signed with a throw-away key.

| Run | Result | Notes |
|---|---|---|
| `3.5.1_4a3b745.json` (2026-11-11) | T0-T14 all PASS (16 records) | Times with a warm build cache: T0 build 50 s (cold: 182-191 s), source 3.4.6 built in 24 s (cold: 150 s), upgrades 3-7 s (cold: + the build of the new version, during which the old one keeps serving). Memory of the machine's processes: 563 MB after install, 2.2 GB with 3 simultaneous sessions, 5.8 GB at the end of the appliance phase with 16 sessions left open (≈ 0.3-0.4 GB per open session). |
