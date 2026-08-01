import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
FULL_SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def test_vercel_routes_every_public_request_to_python_entrypoint():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["version"] == 2
    assert config["builds"] == [{"src": "api/index.py", "use": "@vercel/python"}]
    assert config["routes"] == [{"src": "/(.*)", "dest": "api/index.py"}]


def test_refresh_workflow_separates_read_only_validation_from_minimal_publish():
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "workflow_dispatch" in workflow[True]
    assert workflow[True]["schedule"] == [{"cron": "30 23 * * 1-5"}]
    assert set(workflow["jobs"]) == {"validate", "publish"}
    assert "permissions" not in workflow["jobs"]["validate"]
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == "validate"
    assert publish["permissions"] == {"contents": "write"}

    all_steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    assert all(FULL_SHA.fullmatch(step["uses"]) for step in all_steps if "uses" in step)
    checkout = [step for step in all_steps if step.get("uses", "").startswith("actions/checkout@")]
    assert checkout and all(step["with"]["persist-credentials"] is False for step in checkout)
    script = "\n".join(step.get("run", "") for step in all_steps)
    assert "pip install --upgrade" not in script
    assert "-c constraints-ci.txt" in script
    assert "--mnzl-fallback-csv" in script
    assert "sha256sum -c" in script
    assert "git rev-parse FETCH_HEAD" in script
    assert "git push" in publish["steps"][-1]["run"]
    assert "GITHUB_TOKEN" in publish["steps"][-1]["env"]
    push_script = publish["steps"][-1]["run"]
    assert "".join(["*", "*", "*"]) not in push_script
    push_line = next(line for line in push_script.splitlines() if "git push" in line)
    assert "${GITHUB_TOKEN}" in push_line


def test_pull_request_workflow_is_read_only_and_actions_are_pinned():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8"))
    assert "pull_request" in workflow[True]
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["test"]["steps"]
    assert all(FULL_SHA.fullmatch(step["uses"]) for step in steps if "uses" in step)
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_ci_constraints_are_exact_and_cover_browser_transitives():
    entries = [line for line in (ROOT / "constraints-ci.txt").read_text(encoding="utf-8").splitlines()
               if line and not line.startswith("#")]
    assert entries and all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", entry) for entry in entries)
    names = {entry.split("==", 1)[0].lower() for entry in entries}
    assert {"playwright", "greenlet", "pyee", "pygments", "setuptools"} <= names
