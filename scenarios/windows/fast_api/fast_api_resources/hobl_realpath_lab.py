# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Activate and verify a single run's Windows CPython 3.12.10 realpath experiment."""
import atexit
import hashlib
import json
import marshal
import ntpath
import os
from pathlib import Path
import sys
from time import perf_counter_ns


# Hash the entire code object, including constants, names and line/exception tables.
CODE_SHA256 = {
    "stock": "85fc0a53f086f345e4a1fe54827641bb05689c6cb42edab45f81ee0a706b1f87",
    "exact": "e1262d03abf1eab340060dd94240c6cc8c8a1b6297d284cf33d4373af580de18",
}
# The 43 distinct module/function/arguments triples of the pinned FastAPI telemetry subprocess.
CHILD_CALLSET = "5d95a264709e15ea4c021328fc4118399563bf3b3a71bb48c21bfba6f90ae31d"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def record(role, **details):
    folder = Path(__file__).resolve().parents[1] / "realpath" / "receipts"
    code_hash = digest(marshal.dumps(ntpath.realpath.__code__, 2))
    with (folder / f"{os.getpid()}-{perf_counter_ns()}.json").open("x", encoding="utf-8") as stream:
        json.dump(dict(role=role, realpath_code_sha256=code_hash, **details), stream)


def activate():
    root = Path(__file__).resolve().parents[1] / "realpath"
    settings = read(root / "settings.json")
    variant = settings["variant"]
    if not os.path.samefile(settings["python"], sys.executable) or digest(marshal.dumps(ntpath.realpath.__code__, 2)) != CODE_SHA256["stock"]:
        raise RuntimeError("Expected the scenario venv and the original frozen ntpath.realpath")
    payload = (root / "code.bin").read_bytes()
    if digest(payload) != CODE_SHA256[variant]:
        raise RuntimeError(f"Unexpected realpath code for {variant}; refusing to load it")
    # Keep the original function object and globals, including previously imported references.
    ntpath.realpath.__code__ = marshal.loads(payload)
    argv = sys.orig_argv
    if "-m" in argv:
        invocation = argv[argv.index("-m") + 1:]
        if invocation[:1] == ["tests.test_telemetry._subprocess"]:
            key = digest("\0".join(invocation[1:]).encode())
            atexit.register(record, "child", call=key)


def record_pytest(config):
    if hasattr(config, "workerinput"):
        record("worker", worker=config.workerinput["workerid"])
    else:
        record("controller", workers=len(config.getoption("tx")))


def verify(overlay, variant, requested_workers):
    root = overlay / "realpath"
    if read(root / "settings.json")["variant"] != variant:
        raise ValueError(f"Expected realpath variant {variant}")
    rows = [read(p) for p in (root / "receipts").glob("*.json")]
    expected_hash = CODE_SHA256[variant]
    if any(r["realpath_code_sha256"] != expected_hash for r in rows):
        raise ValueError(f"At least one process did not run the requested {variant} realpath code")
    controllers = [r for r in rows if r["role"] == "controller"]
    if len(controllers) != 1:
        raise ValueError(f"Expected one realpath controller receipt, found {len(controllers)}")
    expected = controllers[0]["workers"]
    workers = [r["worker"] for r in rows if r["role"] == "worker"]
    if (requested_workers != "auto" and int(requested_workers) != expected
            or len(workers) != expected or set(workers) != {f"gw{i}" for i in range(expected)}):
        raise ValueError(f"Realpath worker receipts do not match {requested_workers} requested / {expected} configured: {workers}")
    calls = sorted(r["call"] for r in rows if r["role"] == "child")
    if len(calls) != 43 or digest(json.dumps(calls, separators=(",", ":")).encode()) != CHILD_CALLSET:
        raise ValueError(f"Expected exactly the 43 pinned FastAPI telemetry calls, found {len(calls)}; check the test log")
    path = overlay / "loaded.json"
    receipt = read(path)
    receipt.update(realpath_variant=variant, realpath_code_sha256=expected_hash,
                   realpath_workers=len(workers), realpath_telemetry_children=len(calls))
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(receipt), encoding="utf-8")
    pending.replace(path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verify the realpath experiment after pytest exits")
    parser.add_argument("overlay", type=Path)
    parser.add_argument("variant", choices=CODE_SHA256)
    parser.add_argument("workers")
    args = parser.parse_args()
    verify(args.overlay, args.variant, args.workers)
