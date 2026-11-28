# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scenarios.windows.fast_api import fast_api
from scenarios.windows.fast_api.fast_api_resources import hobl_cov_lab


class CovReportLabTests(unittest.TestCase):
    def test_report_times_include_only_nested_line_reads_and_preserve_the_summary(self):
        class Data:
            def lines(self, filename):
                return [len(filename)]

        data = Data()
        original = Data.lines

        def summary(stream):
            for filename in ("a", "abc"):
                stream.extend(data.lines(filename))
            return 98.5

        controller = SimpleNamespace(summary=summary, cov=SimpleNamespace(config=SimpleNamespace(branch=False)))
        receipt = {"variant": "stock"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loaded.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            run = hobl_cov_lab.timed_report(controller, Data, receipt, path)
            ticks = [0, 100_000_000, 200_000_000, 300_000_000, 500_000_000, 900_000_000]
            with patch.object(hobl_cov_lab, "perf_counter_ns", side_effect=ticks):
                self.assertEqual(data.lines("outside"), [7])
                stream = []
                self.assertEqual(run(stream), 98.5)
                self.assertEqual(stream, [1, 3])
                self.assertEqual(data.lines("after"), [5])
            self.assertIs(Data.lines, original)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
                "variant": "stock", "coverage_report_time_s": 0.9, "coverage_bitmap_read_time_s": 0.3,
            })

    def test_incomplete_report_restores_reader_and_leaves_the_identity_receipt(self):
        class Data:
            def lines(self, filename):
                raise ValueError("unreadable bitmap")

        original = Data.lines
        controller = SimpleNamespace(summary=lambda stream: Data().lines("a"),
                                     cov=SimpleNamespace(config=SimpleNamespace(branch=False)))
        receipt = {"variant": "combined"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "loaded.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unreadable bitmap"):
                hobl_cov_lab.timed_report(controller, Data, receipt, path)(None)
            self.assertIs(Data.lines, original)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), receipt)

    def test_cached_prep_refreshes_all_lab_inputs_without_uploading_the_venv(self):
        scenario = object.__new__(fast_api.FastApi)
        scenario.dut_exec_path = str(Path(__file__).parent / "hobl_bin")
        scenario.checkPrepStatus = Mock(return_value=False)
        scenario._upload = Mock()
        scenario._call = Mock()
        base = ["fast_api_run.ps1", "hobl_cov_lab.py", "report-reuse.patch", "coverage-bitmap.patch"]
        for variant, workers in (("off", None), ("stock", "0"), ("exact", "20")):
            scenario._upload.reset_mock()
            profile = {"global": {"platform": "Windows"}, "fast_api": {"loops": "1", "realpath_variant": variant}}
            if workers is not None:
                profile["fast_api"]["workers"] = workers
            with self.subTest(realpath=variant), patch.dict(fast_api.Params.fileParams, profile, clear=True), \
                    patch.dict(fast_api.Params.overrides, clear=True), \
                    patch.object(fast_api.core.app_scenario.Scenario, "setUp"):
                scenario.setUp()
                scenario.runTest()
                uploaded = []
                for call in scenario._upload.call_args_list:
                    source, target = call.args
                    self.assertEqual(target, scenario.target)
                    self.assertFalse(call.kwargs["check_modified"])
                    self.assertTrue((Path(__file__).resolve().parents[1] / "fast_api_resources" / Path(source).name).is_file())
                    uploaded.append(Path(source).name)
                self.assertCountEqual(uploaded, base + ([] if variant == "off" else ["hobl_realpath_lab.py", "ntpath-realpath.patch"]))
                self.assertTrue(scenario._call.call_args.args[0][1].endswith(f"-Workers {workers or 'auto'} -CovVariant combined -RealpathVariant {variant}"))

    def test_fastapi_power_tool_respects_profile_and_cli_without_changing_other_scenarios(self):
        from tools.power_mode import Tool as PowerMode

        params = fast_api.Params
        globals_ = dict(platform="Windows", dut_coord_scaler="1", hobl_external="", prep_tools="")
        original_mode = PowerMode.mode
        for tools, profile_mode, cli_mode, expected in (
            ("run_report", {}, {}, "best performance"),
            ("run_report power_mode", {"mode": "better"}, {}, "better"),
            ("run_report power_mode", {"mode": "better"}, {"mode": "recommended/balanced"}, "recommended/balanced"),
        ):
            profile = {"global": dict(globals_, tools=tools), "power_mode": profile_mode}
            with self.subTest(tools=tools, mode=expected), patch.dict(params.fileParams, profile, clear=True), \
                    patch.dict(params.overrides, {"power_mode": cli_mode}, clear=True), \
                    patch.dict(params.calculated, {"dut_alive": "0"}, clear=True):
                scenario = fast_api.FastApi()
                self.assertEqual([tool.module for tool in scenario.tool_instances], ["run_report", "power_mode"])
                power = scenario.tool_instances[-1]
                self.assertEqual(power.mode, expected)
                self.assertEqual(params.get("global", "tools"), tools)
                self.assertEqual(PowerMode.mode, original_mode)
                with patch.object(power, "power_manager_call", return_value=True), \
                        patch.object(power, "save_and_set_power_mode") as activate, \
                        patch.object(power, "restore_power_mode") as restore:
                    scenario.toolCallBacks("initCallback")
                    self.assertEqual([c.args for c in activate.call_args_list], [("AC",), ("DC",)])
                    scenario.doCleanups()
                    self.assertEqual([c.args for c in restore.call_args_list], [("AC",), ("DC",)])

    def test_neither_package_can_fall_back_to_the_installed_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            overlay = root / "combined"
            engine = overlay / "pytest_cov" / "engine.py"
            reader = overlay / "coverage" / "sqldata.py"
            for module in (engine, reader):
                module.parent.mkdir(parents=True)
                module.touch()
            (overlay / "source.txt").write_text(str(root), encoding="utf-8")
            wrong = root / "installed.py"
            wrong.touch()
            controller = SimpleNamespace(summary=SimpleNamespace(__module__="other_engine"))
            manager = Mock()
            manager.getplugin.return_value = SimpleNamespace(cov_controller=controller)
            session = SimpleNamespace(config=SimpleNamespace(rootpath=root, pluginmanager=manager))
            for engine_path, reader_path in ((wrong, reader), (engine, wrong)):
                modules = {"other_engine": SimpleNamespace(__file__=str(engine_path)),
                           "coverage.sqldata": SimpleNamespace(__file__=str(reader_path))}
                with self.subTest(engine=engine_path, reader=reader_path), patch.dict(sys.modules, modules), \
                        patch.object(hobl_cov_lab, "__file__", str(overlay / "hobl_cov_lab.py")), \
                        patch.dict(hobl_cov_lab.os.environ, {"PYTEST_CURRENT_TEST": ""}):
                    with self.assertRaisesRegex(RuntimeError, "not the requested lab copy"):
                        hobl_cov_lab.pytest_sessionstart(session)
                    self.assertFalse((overlay / "loaded.json").exists())


if __name__ == "__main__":
    unittest.main()
