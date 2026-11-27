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
        params = {"platform": "Windows", "loops": "1", "workers": "20", "cov_variant": "combined"}
        with patch.object(fast_api.Params, "get", side_effect=lambda section, key: params[key]), \
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
        self.assertCountEqual(uploaded, ["fast_api_run.ps1", "hobl_cov_lab.py", "report-reuse.patch", "coverage-bitmap.patch"])
        self.assertTrue(scenario._call.call_args.args[0][1].endswith("-Workers 20 -CovVariant combined"))

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
