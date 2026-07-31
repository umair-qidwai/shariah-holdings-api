import subprocess
import sys


def test_installed_cli_help_and_failure_exit_codes(tmp_path):
    installed_cli = str(__import__("pathlib").Path(sys.executable).parent / "shariah-holdings-refresh")
    help_result = subprocess.run([installed_cli, "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    assert "--mnzl-as-of" in help_result.stdout

    failure = subprocess.run(
        [sys.executable, "-m", "shariah_holdings.cli", "--mnzl-csv", str(tmp_path / "missing.csv")],
        capture_output=True, text=True,
    )
    assert failure.returncode == 1
    assert "refresh failed" in failure.stderr