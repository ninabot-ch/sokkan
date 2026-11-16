"""The parity bench is run by tests/parity/run.py, not collected by pytest: the repository
tasks are deliberately failing code bases (tests/test_parity_bench.py tests the bench itself)."""
collect_ignore = ["repo_tasks", "fixtures", "lab", "run.py", "wire.py", "journeys.py", "policy.py",
                  "report.py"]
