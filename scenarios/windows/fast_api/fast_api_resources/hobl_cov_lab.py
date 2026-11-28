# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Prepare private coverage packages and attest the implementations loaded by pytest."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from importlib.metadata import distribution
from time import perf_counter_ns


ENGINE_SHA256 = {
    "stock": "2c72f745ed5c9e3acb0de5a7ee8e2b93fc0d898d4a462cf699690a33929920a2",
    "reuse": "86e8cad549a42309482601ddd49821732b9d52fba45efd008ac6aef28ff9aea4",
}
SQLDATA_SHA256 = {
    "stock": "fd84fbd228966e363a0b0771c71c9630d8a8d47acebfa7dee781c3218882306e",
    "bitmap": "44f80a3a3bf8db35881f17dd71582a0a27a7050263d5f56586961988b070c768",
}
VARIANTS = {"stock": ("stock", "stock"), "reuse": ("reuse", "stock"), "combined": ("reuse", "bitmap")}


def check_source(path, expected):
    data = path.read_bytes()
    # The Windows coverage wheel ships CRLF. Verify the LF source, but report the actual file's digest.
    normalized = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
    if normalized != expected:
        raise ValueError(f"Unexpected source at {path}: LF SHA-256 {normalized}, expected {expected}")
    return hashlib.sha256(data).hexdigest()


def venv_package(project, module, version):
    dist = distribution(project)
    package = Path(dist.locate_file(module)).resolve(strict=True)
    if dist.version != version or sys.prefix == sys.base_prefix or not package.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError(f"Expected the scenario venv's {project} {version}, got {dist.version} at {package}")
    return package


def apply_delta(git, overlay, target, patch_name, strip):
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))
    delta = Path(__file__).with_name(patch_name).read_text(encoding="utf-8")
    subprocess.run([git, "-c", "core.autocrlf=false", "apply", "--no-index", f"-p{strip}", "-"],
                   cwd=overlay, input=delta.encode("utf-8"), check=True)


def prepare_realpath(git, overlay, variant):
    import marshal
    import ntpath
    from types import CodeType

    if variant not in ("stock", "exact") or sys.platform != "win32" or sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 12, 10):
        raise ValueError("The realpath lab requires Windows CPython 3.12.10 and variant 'stock' or 'exact'")
    frozen = ntpath.realpath.__code__
    source = Path(ntpath.__file__).resolve(strict=True)
    check_source(source, "0ddb6ebfe8cb6e2d2af73136a0792bd99c951a909635d79fe9d931980ed2986e")
    root = overlay / "realpath"
    root.mkdir()
    (root / "receipts").mkdir()
    target = root / "ntpath.py"
    shutil.copyfile(source, target)

    def function_code():
        module = compile(target.read_bytes(), frozen.co_filename, "exec", dont_inherit=True)
        code, = (c for c in module.co_consts if isinstance(c, CodeType)
                 and c.co_name == "realpath" and "_getfinalpathname" in c.co_names)
        return code

    if frozen.co_filename != "<frozen ntpath>" or function_code() != frozen:
        raise RuntimeError("The installed source does not match the running frozen ntpath.realpath")
    if variant == "exact":
        apply_delta(git, root, target, "ntpath-realpath.patch", 2)
    (root / "code.bin").write_bytes(marshal.dumps(function_code(), 2))
    (root / "settings.json").write_text(json.dumps({"variant": variant, "python": sys.executable}), encoding="utf-8")
    package = overlay / "coverage"
    shutil.copyfile(Path(__file__).with_name("hobl_realpath_lab.py"), package / "_hobl_realpath_lab.py")
    # The private coverage import reaches xdist and subprocess measurement without a site-wide .pth.
    with (package / "__init__.py").open("a", encoding="utf-8") as stream:
        stream.write("\nfrom . import _hobl_realpath_lab\n_hobl_realpath_lab.activate()\n")


def prepare(overlay, realpath_variant="off"):
    if overlay.name not in VARIANTS:
        raise ValueError(f"Unknown coverage lab variant: {overlay.name}")
    report, reader = VARIANTS[overlay.name]
    pytest_cov = venv_package("pytest-cov", "pytest_cov", "7.1.0")
    coverage = venv_package("coverage", "coverage", "7.17.2")
    check_source(pytest_cov / "engine.py", ENGINE_SHA256["stock"])
    check_source(coverage / "sqldata.py", SQLDATA_SHA256["stock"])
    git = shutil.which("git")
    if not git:
        raise FileNotFoundError("Git is required for the FastAPI lab experiment")
    revision = subprocess.check_output([git, "rev-parse", "HEAD"], text=True).strip()
    if revision != "5add7a458d629b07311d48261f696b5ac2bb9a5f" or distribution("fastapi").version != "0.162.5":
        raise ValueError(f"Expected the FastAPI 0.162.5 checkout and venv; found HEAD {revision}. Re-run FastAPI prep.")

    overlay.mkdir()  # Never reuse a directory, compiled bytecode, or a previous run's receipt.
    for package in (pytest_cov, coverage):
        shutil.copytree(package, overlay / package.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    engine = overlay / "pytest_cov" / "engine.py"
    sqldata = overlay / "coverage" / "sqldata.py"
    if report == "reuse":
        apply_delta(git, overlay, engine, "report-reuse.patch", 2)
    if reader == "bitmap":
        apply_delta(git, overlay, sqldata, "coverage-bitmap.patch", 1)
    check_source(engine, ENGINE_SHA256[report])
    check_source(sqldata, SQLDATA_SHA256[reader])
    shutil.copyfile(__file__, overlay / Path(__file__).name)
    (overlay / "source.txt").write_text(str(Path.cwd().resolve()), encoding="utf-8")
    if realpath_variant != "off":
        prepare_realpath(git, overlay, realpath_variant)
    print(f"Prepared {overlay.name} in {overlay} from {pytest_cov} and {coverage}")


def timed_report(controller, data_class, receipt, path):
    summary = controller.summary

    def measure(stream):
        if controller.cov.config.branch:
            raise RuntimeError("FastAPI lab bitmap timing requires line coverage, not branch coverage")
        lines = data_class.lines
        bitmap_ns = 0

        def timed_lines(data, filename):
            nonlocal bitmap_ns
            start = perf_counter_ns()
            try:
                return lines(data, filename)
            finally:
                bitmap_ns += perf_counter_ns() - start

        # Bracket reads only during the controller's report, never collection or worker execution.
        data_class.lines = timed_lines
        try:
            start = perf_counter_ns()
            total = summary(stream)
            report_ns = perf_counter_ns() - start
        finally:
            data_class.lines = lines

        measured = dict(receipt, coverage_report_time_s=report_ns / 1e9,
                        coverage_bitmap_read_time_s=bitmap_ns / 1e9)
        # Keep the original identity receipt intact if reporting is interrupted.
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps(measured), encoding="utf-8")
        pending.replace(path)
        return total

    return measure


def pytest_sessionstart(session):
    """One receipt from the actual coverage controller, not a separate preflight import."""
    experiment = sys.modules.get("coverage._hobl_realpath_lab")
    worker = hasattr(session.config, "workerinput")
    if (worker and experiment is None) or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    overlay = Path(__file__).resolve().parent
    if session.config.rootpath.resolve() != Path((overlay / "source.txt").read_text(encoding="utf-8")):
        return
    if experiment is not None:
        experiment.record_pytest(session.config)
    if worker:
        return
    plugin = session.config.pluginmanager.getplugin("_cov")
    if plugin is None or plugin.cov_controller is None:
        raise RuntimeError("The lab run requires an active pytest-cov controller")
    module = sys.modules[plugin.cov_controller.summary.__module__]
    engine = Path(module.__file__).resolve(strict=True)
    if engine != overlay / "pytest_cov" / "engine.py":
        raise RuntimeError(f"pytest loaded {engine}, not the requested lab copy in {overlay}")
    data_module = sys.modules["coverage.sqldata"]
    sqldata = Path(data_module.__file__).resolve(strict=True)
    if sqldata != overlay / "coverage" / "sqldata.py":
        raise RuntimeError(f"pytest loaded {sqldata}, not the requested lab copy in {overlay}")
    report, reader = VARIANTS[overlay.name]
    receipt = dict(variant=overlay.name, pytest_cov="7.1.0", report_variant=report,
                   engine_sha256=check_source(engine, ENGINE_SHA256[report]), engine_path=str(engine),
                   coverage_py="7.17.2", reader_variant=reader,
                   sqldata_sha256=check_source(sqldata, SQLDATA_SHA256[reader]), sqldata_path=str(sqldata))
    path = overlay / "loaded.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream)
    controller = plugin.cov_controller
    controller.summary = timed_report(controller, data_module.CoverageData, receipt, path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("overlay", type=Path)
    parser.add_argument("realpath_variant", choices=("off", "stock", "exact"), nargs="?", default="off")
    args = parser.parse_args()
    prepare(args.overlay.resolve(), args.realpath_variant)
