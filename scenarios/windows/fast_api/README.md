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
python -m pip install uv==0.12.23
python -m uv sync --no-dev --group tests --extra all --inexact --python .\.venv\Scripts\python.exe

# 4. Timed workload
uv build --python .\.venv\Scripts\python.exe    # PEP 517 build -> dist/
$env:PYTHONPATH = ".\docs_src"
python -m pytest -n auto --dist loadgroup --cov --cov-context=test tests scripts\tests
```

## Notes

- The PEP 517 build spins up its own isolated environment. On a proxied corporate network,
  set `$env:UV_DEFAULT_INDEX` (uv) and `$env:PIP_INDEX_URL` (pip) to your approved PyPI mirror.
- HOBL default: 5 loops (each loop = build + test). Works on x64 and ARM64.
- Set `fast_api:workers=auto` or a non-negative integer to select pytest workers;
  the default `0` keeps tests serial. On macOS, use `mac_fast_api:workers`.
  Example: `hobl.cmd -s fast_api fast_api:loops=1 fast_api:workers=auto`.
- For serial runs, use `pytest -n 0 --dist no` with the same coverage options and test paths.

## Windows lab: coverage report variants

| `fast_api:cov_variant` | pytest-cov 7.1.0 | coverage.py 7.17.2 |
|---|---|---|
| `stock` | Original | Original |
| `reuse` | Reuse the total from the terminal report | Original |
| `combined` (default) | Reuse the total from the terminal report | Union the selected contexts' bitmaps before decoding line numbers |

`stock` vs. `combined` measures both optimizations together; `reuse` vs.
`combined` isolates the new coverage.py change. All three run the same FastAPI
0.162.5 build, tests and per-test coverage, with private copies of both packages.
No package is installed into or changed in the prepared venv.

Example for a previously prepared N1X using 20 workers (replace the profile path):

```powershell
.\hobl.cmd -p ".\lab-profile.ini" -s fast_api fast_api:loops=1 fast_api:workers=20 fast_api:cov_variant=stock
.\hobl.cmd -p ".\lab-profile.ini" -s fast_api fast_api:loops=1 fast_api:workers=20 fast_api:cov_variant=combined
# Optional: previously tested pytest-cov optimization alone
.\hobl.cmd -p ".\lab-profile.ini" -s fast_api fast_api:loops=1 fast_api:workers=20 fast_api:cov_variant=reuse
```

If the lab host is already on `lei9444-fastapi-cov-reuse-lab`, run `git pull --ff-only`.
A valid prep version 11 is reusable; the host refreshes the
runner and patch files without replacing the DUT's venv.

Keep the DUT, drive, power mode, worker count and starting checkout/cache state
the same. For warm measurements, warm each compared variant once (exclude those
runs), then run four pairs in order AB, BA, BA, AB. Use the official
`fast_api_results.csv`: `scenario_runtime` is **Build + Test**, `test_time`
is **Test**, and `build_time` is a control (the build is unchanged).
`fast_api_cov_trials.jsonl` retains each loop's three timings, both loaded
implementations and their file hashes, requested workers and pytest exit code;
the ordinary CSV is overwritten by the next loop. Compare only successful runs
with matching test outcomes and coverage. Rerun the control on this branch
rather than adding savings measured on earlier revisions or other DUTs.
