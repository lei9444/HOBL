# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

import ctypes
import json
import marshal
import ntpath
from pathlib import Path
import shutil
import sys
import tempfile
from types import FunctionType
import unittest
from unittest.mock import patch

# These run on the DUT without importing the HOBL controller or its dependencies.
with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "fast_api_resources"), *sys.path]):
    import hobl_cov_lab
    import hobl_realpath_lab


class RealpathLabTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32" and sys.version_info[:3] == (3, 12, 10), "requires the pinned Windows DUT Python")
    def test_exact_arm_only_avoids_the_second_lookup_after_an_identical_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "target")
            final = "\\\\?\\" + path
            for arm, expected_calls in (("stock", 2), ("exact", 1)):
                overlay = Path(tmp) / arm
                (overlay / "coverage").mkdir(parents=True)
                hobl_cov_lab.prepare_realpath(shutil.which("git"), overlay, arm)
                payload = (overlay / "realpath" / "code.bin").read_bytes()
                self.assertEqual(hobl_realpath_lab.digest(payload), hobl_realpath_lab.CODE_SHA256[arm])
                realpath = FunctionType(marshal.loads(payload), vars(ntpath))
                with patch.object(ntpath, "_getfinalpathname", return_value=final) as lookup:
                    self.assertEqual(realpath(path, strict=True), path)
                    self.assertEqual(lookup.call_count, expected_calls)
                with patch.object(ntpath, "_getfinalpathname", side_effect=[final, final + "different"]) as lookup:
                    self.assertEqual(realpath(path + "-link", strict=True), final)
                    self.assertEqual(lookup.call_count, 2)
                with patch.object(ntpath, "_getfinalpathname", side_effect=[ctypes.WinError(2), final]) as lookup, \
                        patch.object(ntpath, "_getfinalpathname_nonstrict", return_value=final):
                    self.assertEqual(realpath(path, strict=False), path)
                    self.assertEqual(lookup.call_count, 2)

    def test_missing_or_mixed_processes_cannot_publish_a_verified_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp)
            root = overlay / "realpath"
            receipts = root / "receipts"
            receipts.mkdir(parents=True)
            (root / "settings.json").write_text('{"variant": "exact"}', encoding="utf-8")
            path = overlay / "loaded.json"
            path.write_text('{"variant": "combined"}', encoding="utf-8")
            code = hobl_realpath_lab.CODE_SHA256

            def put(name, role, sha, **data):
                (receipts / name).write_text(json.dumps(dict(role=role, realpath_code_sha256=sha, **data)), encoding="utf-8")

            put("c.json", "controller", code["exact"], workers=1)
            with self.assertRaisesRegex(ValueError, "worker receipts"):
                hobl_realpath_lab.verify(overlay, "exact", "1")
            put("w.json", "worker", code["exact"], worker="gw0")
            with self.assertRaisesRegex(ValueError, "43 pinned FastAPI"):
                hobl_realpath_lab.verify(overlay, "exact", "1")
            put("s.json", "child", code["stock"], call="other")
            with self.assertRaisesRegex(ValueError, "did not run the requested"):
                hobl_realpath_lab.verify(overlay, "exact", "1")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"variant": "combined"})


if __name__ == "__main__":
    unittest.main()
