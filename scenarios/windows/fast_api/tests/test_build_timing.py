# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Bounded timing/host tests: no installers, network, RPC or workload execution."""

import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[4]
PROBE_PATH = ROOT / "utilities" / "open_source" / "fast_api_build_probe.py"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load_module("fast_api_build_probe", PROBE_PATH)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hobl-build-timing-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / "build_phases"
        self.calls = []
        root, calls = self.root, self.calls

        class Env:
            def __enter__(self):
                self.path = tempfile.mkdtemp(dir=root)
                calls.append("create")
                return self

            def install(self, requirements):
                calls.append(("install", requirements))

            def __exit__(self, *args):
                shutil.rmtree(self.path)
                calls.append("cleanup")

        class Builder:
            def get_requires_for_build(self, distribution):
                calls.append(("requirements", distribution))
                return set()

            def build(self, distribution, outdir):
                calls.append(("build", distribution, outdir))

        self.env = types.ModuleType("build.env")
        self.env.DefaultIsolatedEnv = Env
        self.cli = types.ModuleType("build.__main__")
        self.cli._validate_sdist_archive = Mock()
        self.cli.safe_extractall = Mock()
        self.cli.main = Mock(side_effect=self.build_flow)
        self.build = types.ModuleType("build")
        self.build.__version__ = "fixture"
        self.build.ProjectBuilder = Builder
        self.build.env = self.env
        self.build.__main__ = self.cli
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"build": self.build, "build.env": self.env,
                                 "build.__main__": self.cli}).start()
        patch.object(probe, "package_version", return_value="fixture").start()
        dist = types.SimpleNamespace(metadata={"Name": "pdm-backend"}, version="2.5.0")
        patch.object(probe.importlib.metadata, "distributions", return_value=[dist]).start()

    def build_flow(self, cli_args, prog):
        for distribution in ("sdist", "wheel"):
            with self.env.DefaultIsolatedEnv() as env:
                builder = self.build.ProjectBuilder()
                env.install({"pdm-backend"})
                env.install(builder.get_requires_for_build(distribution))
                builder.build(distribution, "dist")
            if distribution == "sdist":
                self.cli._validate_sdist_archive("sdist.tar.gz")
                self.cli.safe_extractall("archive", "extracted")
        shutil.rmtree(tempfile.mkdtemp(dir=self.root))

    def report(self):
        return json.loads(self.prefix.with_suffix(".json").read_text(encoding="utf-8"))

    def test_default_flow_preserves_arguments_and_separates_both_installs(self):
        original = self.env.DefaultIsolatedEnv.install
        probe.run(self.prefix, ["--outdir", "scratch dist"])
        self.cli.main.assert_called_once_with(["--outdir", "scratch dist"], "python -m build")
        self.assertIs(self.env.DefaultIsolatedEnv.install, original)
        report = self.report()
        self.assertTrue(report["complete"])
        self.assertEqual([entry["install_calls"] for entry in report["environments"]], [2, 2])
        self.assertEqual([entry["versions"]["pdm-backend"] for entry in report["environments"]],
                         ["2.5.0", "2.5.0"])
        totals = report["totals"]
        self.assertAlmostEqual(sum(v for k, v in totals.items() if k != "build_frontend_s"),
                               totals["build_frontend_s"], places=9)
        for phase in probe.PHASES:
            self.assertGreater(totals[f"{phase}_s"], 0)
        self.assertEqual(self.calls.count(("install", {"pdm-backend"})), 2)
        self.assertEqual(self.calls.count(("install", set())), 2)
        self.assertEqual(sum(s["phase"] == "build_directory_cleanup" for s in report["spans"]), 1)
        self.assertTrue(self.prefix.with_suffix(".csv").is_file())

    def test_nested_spans_are_exclusive(self):
        clock = iter([0.0, 1.0, 4.0, 7.0])
        recorder = probe.Recorder(clock=lambda: next(clock))
        with recorder.measure("build_env1_create"):
            with recorder.measure("build_env1_cleanup"):
                pass
        totals = recorder.totals(8.0)
        self.assertEqual(totals["build_env1_create_s"], 4)
        self.assertEqual(totals["build_env1_cleanup_s"], 3)
        self.assertEqual(totals["build_residual_s"], 1)
        self.assertEqual(recorder.spans[1]["parent"], 0)

    def test_failed_cli_preserves_exit_status_and_partial_evidence(self):
        def failing_cli(*args):
            with self.env.DefaultIsolatedEnv() as env:
                env.install({"pdm-backend"})
                raise SystemExit(23)
        self.cli.main.side_effect = failing_cli
        original = self.env.DefaultIsolatedEnv.install
        probe.importlib.metadata.distributions.side_effect = OSError("metadata unavailable")
        with self.assertRaises(SystemExit) as error:
            probe.run(self.prefix, [])
        self.assertEqual(error.exception.code, 23)
        report = self.report()
        self.assertEqual(report["exit_code"], 23)
        self.assertFalse(report["complete"])
        self.assertIn("build_env1_cleanup", [s["phase"] for s in report["spans"]])
        self.assertIs(self.env.DefaultIsolatedEnv.install, original)
        probe.importlib.metadata.distributions.assert_not_called()

    def test_failed_hook_is_recorded_and_cleanup_still_runs(self):
        def fail(*args):
            raise RuntimeError("backend failed")
        self.build.ProjectBuilder.build = fail
        with self.assertRaisesRegex(RuntimeError, "backend failed"):
            probe.run(self.prefix, [])
        failed = [s for s in self.report()["spans"] if not s["succeeded"]]
        self.assertEqual([s["phase"] for s in failed], ["build_sdist_backend"])
        self.assertIn("cleanup", self.calls)

    def test_missing_api_fails_before_workload(self):
        del self.cli.safe_extractall
        with self.assertRaisesRegex(probe.DiagnosticError, "missing timing hook"):
            probe.run(self.prefix, [])
        self.cli.main.assert_not_called()
        self.assertEqual(self.report()["exit_code"], 2)

    def test_present_but_unused_hook_does_not_silently_succeed(self):
        self.cli.main.side_effect = lambda *args: None
        with self.assertRaisesRegex(probe.DiagnosticError, "incomplete/unexpected"):
            probe.run(self.prefix, [])
        self.assertFalse(self.report()["complete"])

    def test_missing_backend_version_fails_instead_of_guessing_outer_version(self):
        probe.importlib.metadata.distributions.return_value = []
        with self.assertRaisesRegex(probe.DiagnosticError, "pdm-backend version"):
            probe.run(self.prefix, [])
        self.assertEqual(self.report()["exit_code"], 2)
        self.assertEqual(self.calls.count("cleanup"), 2)

    def test_metadata_read_error_still_cleans_environment(self):
        probe.importlib.metadata.distributions.side_effect = OSError("metadata unavailable")
        with self.assertRaisesRegex(OSError, "metadata unavailable"):
            probe.run(self.prefix, [])
        self.assertIn("cleanup", self.calls)
        self.assertFalse(self.report()["complete"])

    def test_separate_loop_prefixes_retain_both_reports(self):
        probe.run(self.prefix, [])
        first = self.prefix.with_suffix(".json").read_bytes()
        probe.run(self.root / "loop2", [])
        self.assertEqual(first, self.prefix.with_suffix(".json").read_bytes())
        self.assertTrue((self.root / "loop2.csv").is_file())


class DeploymentTests(unittest.TestCase):
    def test_cached_prep_refreshes_only_files_and_passes_distinct_loop_numbers(self):
        # Exercise the real upload helper in both local-copy and RPC modes.
        tree = ast.parse((ROOT / "core" / "app_scenario.py").read_text(encoding="utf-8"))
        scenario_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Scenario")
        upload = next(n for n in scenario_class.body if isinstance(n, ast.FunctionDef) and n.name == "_upload")
        for platform, folder, module_name, class_name, suffix in (
            ("windows", "windows", "fast_api", "FastApi", "ps1"),
            ("macos", "macos", "mac_fast_api", "MacFastApi", "sh"),
        ):
            for local in ("0", "1"):
                with self.subTest(platform=platform, local=local), tempfile.TemporaryDirectory() as temp:
                    params = types.ModuleType("core.parameters")
                    values = {"platform": platform, "loops": "2", "workers": "0", "local_execution": local}
                    params.Params = types.SimpleNamespace(setDefault=Mock(), get=lambda section, key: values[key])
                    rpc = types.SimpleNamespace(upload=Mock(side_effect=lambda ip, port, src, dst: shutil.copy(src, dst)))
                    namespace = {"os": os, "shutil": shutil, "logging": logging, "Params": params.Params, "rpc": rpc}
                    exec(compile(ast.Module(body=[upload], type_ignores=[]), "<upload>", "exec"), namespace)
                    app = types.ModuleType("core.app_scenario")
                    app.Scenario = type("Scenario", (), {"setUp": Mock(), "_upload": namespace["_upload"]})
                    core = types.ModuleType("core")
                    core.app_scenario = app
                    with patch.dict(sys.modules, {"core": core, "core.app_scenario": app, "core.parameters": params}):
                        path = ROOT / "scenarios" / folder / module_name / f"{module_name}.py"
                        module = load_module(module_name, path)
                        scenario = getattr(module, class_name)()
                        scenario.dut_exec_path = temp
                        scenario.dut_ip, scenario.rpc_port = "unused", 0
                        scenario.checkPrepStatus = Mock(return_value="")
                        scenario.createPrepStatusControlFile = Mock()
                        scenario._call = Mock()
                        target = Path(temp) / f"{module_name}_resources"
                        venv = target / ".venv"
                        venv.mkdir(parents=True)
                        marker = venv / "preserve.txt"
                        marker.write_text("installed packages", encoding="utf-8")
                        scenario.setUp()
                        self.assertEqual(marker.read_text(encoding="utf-8"), "installed packages")
                        self.assertEqual((target / PROBE_PATH.name).read_bytes(), PROBE_PATH.read_bytes())
                        self.assertTrue((target / f"{module_name}_run.{suffix}").is_file())
                        scenario._call.assert_not_called()
                        scenario.createPrepStatusControlFile.assert_not_called()
                        app.Scenario.setUp.assert_called_once_with(scenario)
                        scenario.runTest()
                        commands = [call.args[0][1] for call in scenario._call.call_args_list]
                        for index, command in enumerate(commands, 1):
                            self.assertTrue(command.endswith(f"-Loop {index}" if platform == "windows" else f"0 {index}"))
                        if local == "0":
                            self.assertEqual(rpc.upload.call_count, 2)
                            self.assertTrue(all(Path(c.args[2]).is_file() for c in rpc.upload.call_args_list))


if __name__ == "__main__":
    unittest.main()
