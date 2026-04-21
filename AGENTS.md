# AGENTS.md — coverage-sh

Guidelines for agentic coding agents working in this repository.

## Project Overview

`coverage-sh` is a [coverage.py](https://coverage.readthedocs.io/) plugin that measures
code coverage for shell scripts executed from Python. The entire implementation lives in
a single file (`coverage_sh/plugin.py`). The package uses `hatchling` as the build
backend and `uv` as the package manager.

---

## Build & Tooling Commands

All commands are run via `uv run`. Sync dependencies first if needed:

```bash
uv sync --locked --all-extras --dev
```

| Task | Command |
|---|---|
| Auto-format | `uv run ruff format .` |
| Lint + auto-fix | `uv run ruff check --fix --unsafe-fixes .` |
| Type check | `uv run mypy .` |
| Run all tests | `uv run pytest` |
| Run tests with coverage | `uv run coverage run --parallel-mode -m pytest` |
| Build package | `uv build` |

**Before marking any task as complete, always run:**
```bash
uv run ruff check --fix --unsafe-fixes . && uv run mypy . && uv run pytest
```

---

## Running Tests

```bash
# All tests
uv run pytest

# Single test function
uv run pytest tests/test_plugin.py::test_filename_suffix_should_match_pattern

# Single test class
uv run pytest tests/test_plugin.py::TestShellFileReporter

# Single method in a class
uv run pytest tests/test_plugin.py::TestShellFileReporter::test_lines_should_match_reference

# Parametrized variant
uv run pytest "tests/test_plugin.py::test_end2end[True]"

# By keyword
uv run pytest -k "test_handle_missing_file"
```

CI requires **100% branch coverage** (`--fail-under=100`). Use `# pragma: no cover`
only for genuinely unreachable defensive branches.

---

## Code Style

PEP8 + Black. Run `uv run ruff check --fix --unsafe-fixes .` then `uv run ruff format .` before committing.

---



## Error Handling

Three patterns are used — pick the one appropriate to the situation:

**1. Suppress expected, ignorable errors (preferred for cleanup):**
```python
with contextlib.suppress(FileNotFoundError):
    self.fifo_path.unlink()
```

**2. Re-raise with context (for parse/validation errors):**
```python
try:
    _, path_, lineno_, _ = line.split(":::", maxsplit=3)
    lineno = int(lineno_)
except ValueError as e:
    raise ValueError(f"could not parse line {line}") from e
```

**3. Safe fallback / early return for non-fatal failures:**
```python
try:
    self._content = self.path.read_text()
except UnicodeDecodeError:
    return ""
```

Never use bare `except:` or `except Exception:` without a very specific reason.

---

## Testing Patterns

Framework: **pytest** (see `pyproject.toml` for version).

- One test class per source class, one test function per module-level function
- All tests live in `tests/test_plugin.py`; shared fixtures in `tests/conftest.py`
- Use plain `assert` statements (not `assertEqual`/`assertTrue` etc.)
- Use `pytest.raises(ExceptionType, match="regex")` for exception assertions
- **No monkeypatching** — do not use `monkeypatch.setattr()` to patch attributes; changing
  directories or environment variables directly is fine
- Use `tmp_path` (pytest built-in) for temporary file isolation
- Test method naming: `test_<what>_should_<expected_outcome>`
- Integration tests use `subprocess.run` to invoke `coverage run` against a real project
  in a temp directory

**Test double conventions (defined as inner classes inside test classes):**
- Spy: subclass that overrides one method and records calls — e.g., `CovLineParserSpy`
- Fake: minimal implementation of an interface — e.g., `CovWriterFake`
- Stub: minimal object satisfying a dependency — e.g., `MainThreadStub`

**Parametrize:**
```python
@pytest.mark.parametrize("cover_always", [(True), (False)])
def test_something(cover_always: bool) -> None:
    ...
```

---

## Architecture Notes

- **Single-module plugin:** all production logic is in `coverage_sh/plugin.py`;
  `coverage_sh/__init__.py` is only a registration entry point
- **File detection:** based on extension or shebang
- **Two coverage modes:** subprocess monkey-patching (default) and `cover_always` mode
  (sets `BASH_ENV`/`ENV` globally)
- **FIFO pipeline:** bash processes write `COV:::<path>:::<lineno>:::` lines via
  `BASH_XTRACEFD` into a named FIFO; `CoverageParserThread` reads and records them
- **Thread safety:** `CoverageParserThread` and `MonitorThread` coordinate shutdown;
  always call `stop()` then `join()` in that order
- The `src/coverage_sh/` directory exists but is **not used** — the real package is the
  root-level `coverage_sh/`
