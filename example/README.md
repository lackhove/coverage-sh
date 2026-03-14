# coverage-sh example

A minimal project demonstrating shell script coverage measurement with `coverage-sh`.

## Structure

```
example/
├── main.py           # Python entry point — runs test.sh via subprocess
├── test.sh           # Shell script that calls syntax_example.sh and inner.py
├── syntax_example.sh # Shell script with various bash constructs
├── inner.py          # Python script invoked directly by test.sh
└── pyproject.toml    # Coverage configuration
```

`main.py` calls `test.sh` via `subprocess.run`. `coverage-sh` intercepts that
subprocess call and measures which lines of `test.sh` and `syntax_example.sh`
are executed.

`parallel = true` is set in `pyproject.toml` so that both the Python coverage
file and the shell sidecar file get a unique suffix, allowing `coverage combine`
to pick both of them up.

## Running

All commands must be run from the `example/` directory. Use `uv run --project ..`
to use `coverage-sh` from the parent repository's virtualenv while keeping
`example/pyproject.toml` as the active coverage configuration.

```sh
cd example/

# 1. Collect coverage
uv run --project .. coverage run main.py

# 2. Combine the Python and shell coverage data files
uv run --project .. coverage combine

# 3. Report
uv run --project .. coverage report -m
```

Expected output:

```
Name                Stmts   Miss  Cover   Missing
-------------------------------------------------
inner.py                1      1     0%   2
main.py                 5      0   100%
syntax_example.sh      25      4    84%   21, 54, 60-63
test.sh                 3      0   100%
-------------------------------------------------
TOTAL                  34      5    85%
```

`inner.py` shows 0% because it is invoked as a subprocess by `test.sh`, not by
Python directly. Measuring it would require coveragepy's subprocess measurement
feature (`patch = ["subprocess"]` plus a `.pth` file in site-packages).

To browse an annotated HTML report:

```sh
uv run --project .. coverage html
open htmlcov/index.html
```
