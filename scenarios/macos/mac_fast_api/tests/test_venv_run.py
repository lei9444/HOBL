# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


RUN_SCRIPT = Path(__file__).resolve().parents[1] / "mac_fast_api_resources/mac_fast_api_run.sh"


@unittest.skipUnless(sys.platform == "darwin", "Requires macOS /usr/bin/time and zsh")
class VenvRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="legacy fastapi ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.resources = self.root / "mac_fast_api_resources"
        self.resources.mkdir()
        shutil.copy2(RUN_SCRIPT, self.resources / RUN_SCRIPT.name)
        self.source = self.root / "fastapi"
        self.source.joinpath("scripts").mkdir(parents=True)
        self.source.joinpath("scripts/test.sh").write_text("exit 99\n")
        self.calls = self.root / "calls.jsonl"
        self.logs = self.root / "logs"
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env.pop("HOBL_FASTAPI_PYTHON", None)
        self.env.pop("HOBL_FASTAPI_DIR", None)
        self.env.update(
            HOME=str(self.home), HOBL_LOG_DIR=str(self.logs), CALL_LOG=str(self.calls),
            PYTHONDONTWRITEBYTECODE="1",
        )

    def make_python(self, venv):
        python = venv / "bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["CALL_LOG"]).open("a") as stream:
    stream.write(json.dumps({"args": args, "venv": os.environ.get("VIRTUAL_ENV")}) + "\\n")
if args == ["--version"]:
    print("Python 3.12.10")
elif args[-1:] == ["-"]:
    sys.stdin.read()
elif args == ["-m", "build"]:
    sys.exit(int(os.environ.get("BUILD_EXIT", "0")))
elif args == ["-m", "coverage", "run", "-m", "pytest", "tests"]:
    pass
else:
    raise RuntimeError(f"Unexpected Python command: {args}")
""")
        python.chmod(0o755)
        return python

    def run_script(self):
        return subprocess.run(
            ["/bin/zsh", "-f", str(self.resources / RUN_SCRIPT.name)],
            env=self.env, text=True, capture_output=True,
        )

    def test_missing_venv_fails_without_falling_back_to_global_python(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("scenario environment is missing", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_default_venv_runs_original_payload_without_a_shell_profile(self):
        venv = self.resources / ".venv"
        self.make_python(venv)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertTrue(all(Path(call["venv"]).resolve() == venv.resolve() for call in calls))
        self.assertEqual(
            [call["args"] for call in calls if call["args"][:1] == ["-m"]],
            [["-m", "build"], ["-m", "coverage", "run", "-m", "pytest", "tests"]],
        )
        with (self.logs / "mac_fast_api_results.csv").open() as stream:
            metrics = {key: float(value) for key, value in csv.reader(stream)}
        self.assertAlmostEqual(metrics["scenario_runtime"], metrics["build_time"] + metrics["test_time"])

    def test_explicit_interpreter_override_uses_its_own_environment(self):
        venv = self.root / "independent venv"
        self.env["HOBL_FASTAPI_PYTHON"] = str(self.make_python(venv))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertTrue(all(Path(call["venv"]).resolve() == venv.resolve() for call in calls))

    def test_build_failure_stops_before_tests_and_metrics(self):
        self.make_python(self.resources / ".venv")
        self.env["BUILD_EXIT"] = "7"
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Building Fast API failed", result.stdout)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any("coverage" in call["args"] for call in calls))
        self.assertFalse((self.logs / "mac_fast_api_results.csv").exists())
