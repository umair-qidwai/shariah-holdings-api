import json
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def test_vercel_routes_every_public_request_to_python_entrypoint():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["version"] == 2
    assert config["builds"] == [{"src": "api/index.py", "use": "@vercel/python"}]
    assert config["routes"] == [{"src": "/(.*)", "dest": "api/index.py"}]


def test_refresh_workflow_has_schedule_dispatch_security_and_change_guard():
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "workflow_dispatch" in workflow[True]  # YAML 1.1 parses the key `on` as true.
    assert workflow[True]["schedule"] == [{"cron": "30 23 * * 1-5"}]
    steps = workflow["jobs"]["refresh"]["steps"]
    assert all("@" in step["uses"] for step in steps if "uses" in step)
    script = "\n".join(step.get("run", "") for step in steps)
    assert "--mnzl-fallback-csv" in script
    assert "git diff --quiet --exit-code -- data" in script
    assert "git push" in script
