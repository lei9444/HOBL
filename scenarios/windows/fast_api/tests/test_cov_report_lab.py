# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scenarios.windows.fast_api import fast_api
from scenarios.windows.fast_api.fast_api_resources import hobl_cov_lab


class CovReportLabTests(unittest.TestCase):
    def test_cached_prep_refreshes_all_lab_inputs_without_uploading_the_venv(self):
        scenario = object.__new__(fast_api.FastApi)
        scenario.dut_exec_path = r"D:\hobl_bin"
        scenario.checkPrepStatus = Mock(return_value=False)
        scenario._upload = Mock()
        scenario._call = Mock()
        params = {"platform": "Windows", "loops": "1", "workers": "20", "cov_variant": "stock"}
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
        self.assertCountEqual(uploaded, ["fast_api_run.ps1", "hobl_cov_lab.py", "report-reuse.patch"])
        self.assertTrue(scenario._call.call_args.args[0][1].endswith("-Workers 20 -CovVariant stock"))

    def test_wrong_controller_copy_cannot_produce_a_success_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            overlay = root / "reuse"
            overlay.mkdir()
            (overlay / "source.txt").write_text(str(root), encoding="utf-8")
            wrong = root / "engine.py"
            wrong.touch()
            controller = SimpleNamespace(summary=SimpleNamespace(__module__="other_engine"))
            manager = Mock()
            manager.getplugin.return_value = SimpleNamespace(cov_controller=controller)
            session = SimpleNamespace(config=SimpleNamespace(rootpath=root, pluginmanager=manager))
            with patch.dict(sys.modules, {"other_engine": SimpleNamespace(__file__=str(wrong))}), \
                    patch.object(hobl_cov_lab, "__file__", str(overlay / "hobl_cov_lab.py")), \
                    patch.dict(hobl_cov_lab.os.environ, {"PYTEST_CURRENT_TEST": ""}):
                with self.assertRaisesRegex(RuntimeError, "not the requested lab copy"):
                    hobl_cov_lab.pytest_sessionstart(session)
            self.assertFalse((overlay / "loaded.json").exists())


if __name__ == "__main__":
    unittest.main()
