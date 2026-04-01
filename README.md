# Coverage.sh

[![PyPI - Version](https://img.shields.io/pypi/v/coverage-sh?color=blue)](https://pypi.org/project/coverage-sh/)
[![PyPI - Status](https://img.shields.io/pypi/status/coverage-sh)](https://github.com/lackhove/coverage-sh/blob/main/pyproject.toml)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/coverage-sh)](https://github.com/lackhove/coverage-sh/blob/main/pyproject.toml)
[![PyPI - License](https://img.shields.io/pypi/l/coverage-sh)](https://github.com/lackhove/coverage-sh/blob/main/LICENSE.txt)
![Coverage](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/lackhove/f16009049fe5091e6d750a7bb7b4d68a/raw/covbadge.json)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)

A  [Coverage.py](https://github.com/nedbat/coveragepy) plugin to measure code coverage of shell (sh or bash) scripts
executed from python.

## Installation

```shell
pip install coverage-sh
```

## Usage

In your `pyproject.toml`, set

```toml
[tool.coverage.run]
plugins = ["coverage_sh"]
```

and run

```shell
coverage run main.py
coverage combine
coverage html
```

to measure coverage of all shell scripts executed via
the [subprocess](https://docs.python.org/3/library/subprocess.html) module, e.g.:

```python
subprocess.run(["bash", "test.sh"])
```

The resulting coverage is then displayed alongside the coverage of the python files:

![coverage.sh report screenshot](doc/media/screenshot_html-report.png)

### Caveats

The plugin works by patching the `subprocess.Popen` class to set the "ENV" and "BASH_ENV" environment variables before
execution, to source a helper script which enables tracing. This approach comes with a few caveats:

- It will only cover shell scripts that are executed via the subprocess module.
- Only bash and sh are supported

### Cover-Always Mode

When using the subprocess modue is not an option, coverage-sh can operate in "cover-always-mode", which is activated by
setting

```toml
[tool.coverage.coverage_sh]
cover_always = true
```

in the `pyproject.toml`. In this mode, Coverage.sh will not respect the `coverage.start()` and `coverage.stop()` calls
and instead cover every shell script executed after the plugin gets loaded until the main process is finished.
This mode is also incompatible with the popular [pytest-cov](https://github.com/pytest-dev/pytest-cov) but works with
starting pytest from coverage , e.g.:

```bash
coverage run -m pytest arg1 arg2 arg3
```

## Debug Options

The coverage-sh plugin uses coveragepy debug infrastructure. You can enable debug by setting the `COVERAGE_DEBUG` variable or by running coverage with the `--debug` flag. Logging in coverage-sh is enabled by the `shell` option. More options are documented in the [coveragepy documentation](https://coverage.readthedocs.io/en/latest/commands/cmd_debug.html#debug-option).

## License

Licensed under the [MIT License](LICENSE.txt).
