# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Prepare a private pytest-cov copy, then attest the implementation used by pytest."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from importlib.metadata import distribution


ENGINE_SHA256 = {
    "stock": "2c72f745ed5c9e3acb0de5a7ee8e2b93fc0d898d4a462cf699690a33929920a2",
    "reuse": "86e8cad549a42309482601ddd49821732b9d52fba45efd008ac6aef28ff9aea4",
}


def check_engine(path, variant):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != ENGINE_SHA256[variant]:
        raise ValueError(f"Unexpected {variant} pytest-cov engine at {path}: SHA-256 {actual}")
    return actual


def prepare(overlay):
    variant = overlay.name
    if variant not in ENGINE_SHA256:
        raise ValueError(f"Unknown pytest-cov variant: {variant}")
    dist = distribution("pytest-cov")
    package = Path(dist.locate_file("pytest_cov")).resolve(strict=True)
    if dist.version != "7.1.0" or sys.prefix == sys.base_prefix or not package.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError(f"Expected the scenario venv's pytest-cov 7.1.0, got {dist.version} at {package}")
    check_engine(package / "engine.py", "stock")
    git = shutil.which("git")
    if not git:
        raise FileNotFoundError("Git is required for the FastAPI lab experiment")
    revision = subprocess.check_output([git, "rev-parse", "HEAD"], text=True).strip()
    if revision != "5add7a458d629b07311d48261f696b5ac2bb9a5f" or distribution("fastapi").version != "0.162.5":
        raise ValueError(f"Expected the FastAPI 0.162.5 checkout and venv; found HEAD {revision}. Re-run FastAPI prep.")

    overlay.mkdir()  # Never reuse a directory, compiled bytecode, or a previous run's receipt.
    target = overlay / "pytest_cov"
    shutil.copytree(package, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if variant == "reuse":
        patch = Path(__file__).with_name("report-reuse.patch").read_text(encoding="utf-8")
        subprocess.run(
            [git, "-c", "core.autocrlf=false", "apply", "--no-index", "-p2", "-"],
            cwd=overlay, input=patch.encode("utf-8"), check=True,
        )
    check_engine(target / "engine.py", variant)
    shutil.copyfile(__file__, overlay / Path(__file__).name)
    (overlay / "source.txt").write_text(str(Path.cwd().resolve()), encoding="utf-8")
    print(f"Prepared pytest-cov 7.1.0 ({variant}) from {package} in {overlay}")


def pytest_sessionstart(session):
    """One receipt from the actual coverage controller, not a separate preflight import."""
    if hasattr(session.config, "workerinput") or os.environ.get("PYTEST_CURRENT_TEST"):
        return
    overlay = Path(__file__).resolve().parent
    if session.config.rootpath.resolve() != Path((overlay / "source.txt").read_text(encoding="utf-8")):
        return
    plugin = session.config.pluginmanager.getplugin("_cov")
    if plugin is None or plugin.cov_controller is None:
        raise RuntimeError("The lab run requires an active pytest-cov controller")
    module = sys.modules[plugin.cov_controller.summary.__module__]
    engine = Path(module.__file__).resolve(strict=True)
    if engine != overlay / "pytest_cov" / "engine.py":
        raise RuntimeError(f"pytest loaded {engine}, not the requested lab copy in {overlay}")
    receipt = dict(variant=overlay.name, pytest_cov="7.1.0", engine_sha256=check_engine(engine, overlay.name), engine_path=str(engine))
    with (overlay / "loaded.json").open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream)


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve())
