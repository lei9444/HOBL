# FastAPI Build & Test Workload

Python developer inner-loop benchmark. It builds the [FastAPI](https://github.com/fastapi/fastapi)
library from source using a PEP 517 build (PDM backend), then runs the project's full test
suite under `coverage`. It measures **build time** and **test time** separately and reports
their sum as the scenario runtime — a proxy for Python package build + test performance.

## What HOBL sets up (from `fast_api_resources/fast_api_prep.ps1`)

- Python 3.12.10 via pyenv (isolated venv)
- Visual Studio 2022 C++ build tools (for native extension builds)
- Git (winget)
- Clones `https://github.com/fastapi/fastapi.git` to `<drive>\fastapi`

## Run it standalone (Windows)

```powershell
# 1. Prerequisites
winget install --id Git.Git --source winget
pyenv install 3.12.10; pyenv local 3.12.10

# 2. Clone the repo
git clone https://github.com/fastapi/fastapi.git
cd fastapi

# 3. Isolated venv + build/test tooling
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install uv build
python -m uv sync --no-dev --group tests --extra all --inexact --python .\.venv\Scripts\python.exe

# 4. Timed workload
python -m build                        # PEP 517 build -> dist/
$env:PYTHONPATH = ".\docs_src"
python -m pytest -n 0 --cov --cov-context=test tests scripts\tests
```

## Notes

- The PEP 517 build spins up its own isolated environment. On a proxied corporate network,
  set `$env:PIP_INDEX_URL` to your approved PyPI mirror so build isolation can fetch deps.
- HOBL default: 5 loops (each loop = build + test). Works on x64 and ARM64.
- Set `fast_api:workers=auto` or a non-negative integer to select pytest workers;
  the default `0` keeps tests serial. On macOS, use `mac_fast_api:workers`.
  Example: `hobl.cmd -s fast_api fast_api:loops=1 fast_api:workers=auto`.

## Build timing diagnostic branch (Windows and Mac labs)

On the **Windows HOBL host**, in its clean HOBL checkout, fetch the fork branch
(do not change the FastAPI source checkout or its dependencies on the DUT):

```powershell
git fetch https://github.com/lei9444/HOBL.git lei9444-fastapi-build-timing
git switch -c fastapi-build-timing --no-track FETCH_HEAD
# Replace the profile path with the existing profile for your Mac DUT:
.\hobl.cmd -p "<Mac-DUT-profile.ini>" -s mac_fast_api mac_fast_api:loops=5 mac_fast_api:workers=0
```

For subsequent updates while on that diagnostic branch, repeat the fetch then
`git merge --ff-only FETCH_HEAD`. For a Windows DUT, use its profile with
`-s fast_api fast_api:loops=5 fast_api:workers=0`. Keep the lab's existing worker
setting if it differs. This branch does not fix or bypass pytest failures.

Each scenario setup uploads **only** the updated run script and the shared
`utilities/open_source/fast_api_build_probe.py` before measurement starts, even
when prep is already marked complete. Individual file uploads leave the existing
per-scenario `.venv`, FastAPI checkout, packages and prep marker untouched. Do not
delete `prep_status`, upload the entire resources directory, or run full prep
just for these statistics. A genuinely unprepared DUT still takes normal prep.
Updating only a clone on the Mac does not update a remote HOBL host: run from the
updated host checkout above.
Switching the host back to main alone does not revert already deployed run files;
restore only main's run script on the DUT to remove diagnostics, keeping `.venv`.

The probe invokes the installed `build.__main__.main` with the usual arguments
and working directory: isolated sdist, then isolated wheel from that sdist.
No installer, dependency, cache or workload options change. Both platforms use
the same probe and CSV keys. The original outer `Measure-Command` / `/usr/bin/time`
wall timers remain; `scenario_runtime` is still build + test. macOS additionally
exports canonical `build_time` / `test_time` aliases without removing `build_real`
or `test_real`.

Every loop creates a unique directory under the DUT's `hobl_data`:
`mac_fast_api_loop_<N>_<unique>/` (Mac) or `fast_api_loop_<NN>_<unique>/`
(Windows). Normal HOBL teardown collects these directories into the scenario's
**RunDir** when collection is enabled. On Mac they are also directly accessible
under `/Users/Shared/hobl_data/`. Each contains:

- `build_phases.csv` and `build_phases.json`: additive phase seconds, call counts,
  completion/exit status, interpreter/tool versions and each environment's actual
  `pdm-backend` version; JSON also contains individual nested spans.
- `mac_fast_api_build.log`, `mac_fast_api_test.log`, `mac_fast_api_run.log` and
  `mac_fast_api_results.csv` (Windows uses `fast_api_` instead of `mac_fast_api_`).
  Mac also retains `mac_fast_api_build_time.log` and `mac_fast_api_test_time.log`.
  Files for phases not reached are absent; the total CSV requires reaching the
  existing results-writing step. The root results CSV remains the latest loop.

`build_env1_*_s` is the sdist environment and `build_env2_*_s` the wheel
environment. Each has separate creation, backend installation, extra-requirement
installation (including an empty/no-op call), and cleanup timings.
`build_sdist_requirements_s` / `build_wheel_requirements_s` measure backend
requirement discovery; `build_sdist_backend_s` / `build_wheel_backend_s` measure
artifact creation. Validation, extraction and top-level directory cleanup have
their own keys. Nested operations use **exclusive** time, so these phase totals
plus `build_diagnostic_metadata_s` and `build_residual_s` sum to
`build_frontend_s`, never double-counting child spans. The residual covers CLI
orchestration and uninstrumented work. `build_frontend_s` excludes probe setup,
imports and report writing; the original outer `build_time` includes them.

An unsupported/missing hook fails with an explicit diagnostic instead of silently
omitting phases. A failed build preserves the CLI exit status and partial reports;
check `complete` before comparing results. Build/test stdout and stderr stay in
their per-loop logs. Forced termination can prevent in-memory phase reports from
being written, but the build log is redirected throughout execution.
