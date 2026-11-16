# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE file in the project root for full license information.

"""Time the installed build CLI's default sdist-to-wheel flow without changing it."""

import argparse
import csv
import functools
import importlib.metadata
import json
import platform
import shutil
import sys
import sysconfig
import time
from contextlib import contextmanager
from pathlib import Path


PHASES = (
    "build_env1_create", "build_env1_install_backend", "build_sdist_requirements",
    "build_env1_install_extra", "build_sdist_backend", "build_env1_cleanup",
    "build_sdist_validate", "build_sdist_extract",
    "build_env2_create", "build_env2_install_backend", "build_wheel_requirements",
    "build_env2_install_extra", "build_wheel_backend", "build_env2_cleanup",
    "build_directory_cleanup", "build_diagnostic_metadata",
)


class DiagnosticError(RuntimeError):
    pass


class Recorder:
    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.spans = []
        self.stack = []

    @contextmanager
    def measure(self, phase):
        span = {
            "id": len(self.spans),
            "parent": self.stack[-1]["id"] if self.stack else None,
            "phase": phase,
            "children_s": 0.0,
            "succeeded": False,
        }
        self.spans.append(span)
        self.stack.append(span)
        begin = self.clock()
        try:
            yield
            span["succeeded"] = True
        finally:
            span["inclusive_s"] = self.clock() - begin
            span["exclusive_s"] = span["inclusive_s"] - span["children_s"]
            self.stack.pop()
            if self.stack:
                self.stack[-1]["children_s"] += span["inclusive_s"]

    def totals(self, frontend_s):
        phases = dict.fromkeys(PHASES, 0.0)
        for span in self.spans:
            phases[span["phase"]] += span["exclusive_s"]
        residual = frontend_s - sum(phases.values())
        if residual < -1e-9:
            raise DiagnosticError("Build phase accounting exceeds the frontend wall time.")
        return {**{f"{key}_s": value for key, value in phases.items()},
                "build_residual_s": residual, "build_frontend_s": frontend_s}


def package_version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


@contextmanager
def instrument(build, cli, env_module, recorder, environments):
    required = (
        (cli, "main"), (cli, "_validate_sdist_archive"), (cli, "safe_extractall"),
        (env_module.DefaultIsolatedEnv, "__enter__"),
        (env_module.DefaultIsolatedEnv, "__exit__"),
        (env_module.DefaultIsolatedEnv, "install"),
        (build.ProjectBuilder, "get_requires_for_build"),
        (build.ProjectBuilder, "build"), (shutil, "rmtree"),
    )
    for owner, name in required:
        if not callable(getattr(owner, name, None)):
            raise DiagnosticError(
                f"Unsupported build {build.__version__}: missing timing hook {owner.__name__}.{name}. "
                "Update the probe for this API; do not change build isolation or upgrade the lab."
            )

    originals = []
    env_records = {}

    def patch(owner, name, wrapper):
        original = getattr(owner, name)
        originals.append((owner, name, original))
        setattr(owner, name, functools.wraps(original)(wrapper(original)))

    def enter(original):
        def measured(env, *args, **kwargs):
            number = len(environments) + 1
            if number > 2:
                raise DiagnosticError("Unsupported build flow: more than two isolated environments.")
            entry = {"number": number, "install_calls": 0}
            environments.append(entry)
            env_records[env] = entry
            with recorder.measure(f"build_env{number}_create"):
                return original(env, *args, **kwargs)
        return measured

    def install(original):
        def measured(env, *args, **kwargs):
            entry = env_records[env]
            entry["install_calls"] += 1
            if entry["install_calls"] > 2:
                raise DiagnosticError("Unsupported build flow: more than two install calls per environment.")
            kind = "backend" if entry["install_calls"] == 1 else "extra"
            with recorder.measure(f"build_env{entry['number']}_install_{kind}"):
                return original(env, *args, **kwargs)
        return measured

    def cleanup(original):
        def measured(env, *args, **kwargs):
            entry = env_records[env]
            if args and args[0] is not None:
                # Never let diagnostic metadata collection mask a build failure.
                with recorder.measure(f"build_env{entry['number']}_cleanup"):
                    return original(env, *args, **kwargs)
            try:
                # Read metadata before deletion, outside the cleanup/backend spans.
                # This works with both stdlib venv and virtualenv, without a subprocess.
                with recorder.measure("build_diagnostic_metadata"):
                    purelib = sysconfig.get_path(
                        "purelib", scheme="nt" if sys.platform == "win32" else "posix_prefix",
                        vars={"base": env.path, "platbase": env.path},
                    )
                    versions = {}
                    for dist in importlib.metadata.distributions(path=[purelib]):
                        name = dist.metadata["Name"].lower().replace("_", "-")
                        if name in ("pdm-backend", "pip"):
                            versions[name] = dist.version
                    entry["versions"] = versions
            finally:
                with recorder.measure(f"build_env{entry['number']}_cleanup"):
                    original(env, *args, **kwargs)
        return measured

    def timed(phase):
        def wrapper(original):
            def measured(*args, **kwargs):
                label = phase(*args, **kwargs) if callable(phase) else phase
                with recorder.measure(label):
                    return original(*args, **kwargs)
            return measured
        return wrapper

    def distribution_phase(suffix):
        def label(*args, **kwargs):
            distribution = args[1] if len(args) > 1 else kwargs.get("distribution")
            if distribution not in ("sdist", "wheel"):
                raise DiagnosticError("Unsupported build distribution hook arguments.")
            return f"build_{distribution}_{suffix}"
        return label

    def directory_cleanup(original):
        def measured(*args, **kwargs):
            # Nested removals belong to the containing phase (not counted twice).
            if recorder.stack:
                return original(*args, **kwargs)
            with recorder.measure("build_directory_cleanup"):
                return original(*args, **kwargs)
        return measured

    try:
        patch(env_module.DefaultIsolatedEnv, "__enter__", enter)
        patch(env_module.DefaultIsolatedEnv, "install", install)
        patch(env_module.DefaultIsolatedEnv, "__exit__", cleanup)
        patch(build.ProjectBuilder, "get_requires_for_build", timed(distribution_phase("requirements")))
        patch(build.ProjectBuilder, "build", timed(distribution_phase("backend")))
        patch(cli, "_validate_sdist_archive", timed("build_sdist_validate"))
        patch(cli, "safe_extractall", timed("build_sdist_extract"))
        patch(shutil, "rmtree", directory_cleanup)
        yield
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


def validate_flow(recorder, environments, build_version):
    counts = {phase: sum(span["phase"] == phase for span in recorder.spans) for phase in PHASES}
    missing = [phase for phase, count in counts.items()
               if (count < 1 if phase == "build_directory_cleanup"
                   else count != (2 if phase == "build_diagnostic_metadata" else 1))]
    if missing or len(environments) != 2:
        raise DiagnosticError(
            f"Unsupported build {build_version}: incomplete/unexpected timing hooks: {', '.join(missing)}. "
            "Expected the default isolated sdist-to-wheel build; update the probe for this API."
        )
    if any(not span["succeeded"] for span in recorder.spans):
        raise DiagnosticError("A build phase failed despite a successful CLI exit.")
    if any("pdm-backend" not in entry.get("versions", {}) for entry in environments):
        raise DiagnosticError("Could not record pdm-backend version in both isolated environments.")


def run(report_prefix, cli_args):
    recorder = Recorder()
    environments = []
    frontend_s = 0.0
    exit_code = 1
    failure = None
    tools = {
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "build_version": package_version("build"),
        "pip_version": package_version("pip"),
        "virtualenv_version": package_version("virtualenv"),
        "pyproject_hooks_version": package_version("pyproject-hooks"),
    }
    try:
        import build
        import build.__main__ as cli
        import build.env as env_module

        with instrument(build, cli, env_module, recorder, environments):
            begin = time.perf_counter()
            try:
                # Same entrypoint, arguments and cwd as `python -m build`.
                try:
                    cli.main(cli_args, "python -m build")
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise
            finally:
                frontend_s = time.perf_counter() - begin
        validate_flow(recorder, environments, build.__version__)
        exit_code = 0
    except BaseException as exc:
        failure = type(exc).__name__
        if isinstance(exc, SystemExit):
            exit_code = exc.code if isinstance(exc.code, int) else 1
        elif isinstance(exc, DiagnosticError):
            exit_code = 2
        raise
    finally:
        totals = recorder.totals(frontend_s)
        report = {
            "schema_version": 1, "exit_code": exit_code, "failure": failure,
            "complete": exit_code == 0, "tools": tools, "environments": environments,
            "totals": totals, "spans": recorder.spans,
        }
        # No report I/O inside measured inner spans. Never overwrite loop evidence.
        with Path(f"{report_prefix}.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
        with Path(f"{report_prefix}.csv").open("x", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerows({"exit_code": exit_code, "complete": int(exit_code == 0), **tools, **totals}.items())
            writer.writerows((f"{phase}_calls", sum(s["phase"] == phase for s in recorder.spans))
                             for phase in PHASES)
            for number in (1, 2):
                versions = next((e.get("versions", {}) for e in environments if e["number"] == number), {})
                for name in ("pdm-backend", "pip"):
                    writer.writerow((f"build_env{number}_{name.replace('-', '_')}_version", versions.get(name)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-prefix", type=Path, required=True)
    parser.add_argument("build_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cli_args = args.build_args[1:] if args.build_args[:1] == ["--"] else args.build_args
    try:
        run(args.report_prefix, cli_args)
    except DiagnosticError as exc:
        print(f" ERROR - Build timing diagnostic: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
