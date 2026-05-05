#  SPDX-License-Identifier: MIT
#  Copyright (c) 2023-2024 Kilian Lackhove
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path
from socket import gethostname
from time import sleep
from typing import TYPE_CHECKING, cast

import coverage
import pytest
from coverage.config import CoverageConfig
from coverage.debug import DebugControl
from packaging.version import Version

from coverage_sh.plugin import (
    ArcData,
    CoverageParserThread,
    CoverageWriter,
    CovLineParser,
    LineData,
    MonitorThread,
    PatchedPopen,
    ShellFileReporter,
    ShellPlugin,
    debug_write,
    filename_suffix,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

COVERAGE_LINE_CHUNKS = (
    b"""\
CCOV:::/home/dummy_user/dummy_dir_a:::1:::main:::a normal line
COV:::/home/dummy_user/dummy_dir_b:::10:::main:::a line
with a line fragment

COV:::/home/dummy_user/dummy_dir_a:::2:::main:::a  line with ::: triple columns
COV:::/home/dummy_user/dummy_dir_a:::3:::main:::a  line """,
    b"that spans multiple chunks\n",
    b"C",
    b"O",
    b"V",
    b":",
    b":",
    b":",
    b"/",
    b"ho",
    b"m",
    b"e",
    b"/dummy_user/dummy_dir_a:::18:::some_func:::a chunked line\n",
    b"COV:::/home/dummy_user/dummy_dir_a:::4:::main:::final line",
)
COVERAGE_LINES = [
    "CCOV:::/home/dummy_user/dummy_dir_a:::1:::main:::a normal line",
    "COV:::/home/dummy_user/dummy_dir_b:::10:::main:::a line",
    "with a line fragment",
    "COV:::/home/dummy_user/dummy_dir_a:::2:::main:::a  line with ::: triple columns",
    "COV:::/home/dummy_user/dummy_dir_a:::3:::main:::a  line that spans multiple chunks",
    "COV:::/home/dummy_user/dummy_dir_a:::18:::some_func:::a chunked line",
    "COV:::/home/dummy_user/dummy_dir_a:::4:::main:::final line",
]
COVERAGE_LINE_COVERAGE = {
    "/home/dummy_user/dummy_dir_a": {1, 2, 3, 4, 18},
    "/home/dummy_user/dummy_dir_b": {10},
}
COVERAGE_ARC_COVERAGE = {
    "/home/dummy_user/dummy_dir_a": {
        (-1, 1),
        (1, -1),
        (-1, 2),
        (2, 3),
        (3, -1),
        (-1, 18),
        (18, -1),
        (-1, 4),
        (4, -1),
    },
    "/home/dummy_user/dummy_dir_b": {(-1, 10), (10, -1)},
}

END2END_SUBPROCESS_TIMEOUT = 5


@pytest.fixture
def example_project_dir(tmp_path: Path) -> Path:
    """Fixture for a temporary copy of `testproject`"""
    source = Path(__file__).parent.parent / "example"
    dest = tmp_path / "example"
    shutil.copytree(source, dest)

    return dest


class DebugControlString(DebugControl):
    """A `DebugControl` that writes to a StringIO, for testing."""

    def __init__(self, options: Iterable[str]) -> None:
        self.io = io.StringIO()
        super().__init__(options, self.io)

    def get_output(self) -> str:
        """Get the output text from the `DebugControl`."""
        return self.io.getvalue()


@pytest.mark.parametrize("cover_always", [(True), (False)])
def test_end2end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cover_always: bool,
) -> None:
    test_sh = Path(__file__).parent.parent / "example" / "syntax_example.sh"

    pyproject_toml = tmp_path / "pyproject.toml"
    pyproject_toml.write_text(
        '[tool.coverage.run]\nplugins = ["coverage_sh"]\nsource = ["."]\n'
    )

    main_py = tmp_path / "main.py"
    main_py.write_text(f"import subprocess\nsubprocess.run(['{test_sh}'])\n")

    if cover_always:
        with pyproject_toml.open("a") as fd:
            fd.write("\n[tool.coverage.coverage_sh]\ncover_always = true")

    monkeypatch.chdir(tmp_path)

    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "main.py"],
        capture_output=True,
        text=True,
        check=True,
        timeout=END2END_SUBPROCESS_TIMEOUT,
    )

    if sys.version_info < (3, 14):
        # we raise a warning when sysmon run.core is set to sysmon, which is the default since 3.14
        assert proc.stderr == ""
    assert proc.returncode == 0

    # Plugin produced a shell coverage sidecar file
    assert len(list(tmp_path.glob(f".coverage.sh.{gethostname()}.*"))) == 1

    subprocess.check_call([sys.executable, "-m", "coverage", "combine"])

    # Shell script appears in the combined coverage data
    cov_data = coverage.CoverageData(basename=".coverage", suffix="")
    cov_data.read()
    assert str(test_sh) in cov_data.measured_files()


def test_example(example_project_dir: Path) -> None:
    """Test that the example project runs without error."""
    subprocess.run(
        [sys.executable, "-m", "coverage", "run", "main.py"],
        capture_output=True,
        text=True,
        check=True,
        timeout=END2END_SUBPROCESS_TIMEOUT,
        cwd=example_project_dir,
    )


class TestDebugWrite:
    def test_should_not_log_when_dsabled(self) -> None:
        debug_control = DebugControlString([])

        debug_write("foo", debug_control)

        assert debug_control.get_output() == ""

    def test_should_log_when_enabled(self) -> None:
        debug_control = DebugControlString(["shell"])

        debug_write("foo", debug_control)

        assert debug_control.get_output() == "foo\n"

    def test_should_log_self_when_enabled(self) -> None:
        debug_control = DebugControlString(["self", "shell"])

        debug_write("foo", debug_control)

        assert (
            "self: <tests.test_plugin.TestDebugWrite object at "
            in debug_control.get_output()
        )

    def test_should_log_callers_when_enabled(self) -> None:
        debug_control = DebugControlString(["self", "callers", "shell"])

        debug_write("foo", debug_control)

        assert "test_should_log_callers_when_enabled" in debug_control.get_output()


class TestShellFileReporter:
    def test_source_should_be_cached(self, tmp_path: Path) -> None:
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello\n")
        reporter = ShellFileReporter(str(script))

        assert reporter.source() == "#!/bin/bash\necho hello\n"
        script.unlink()
        assert reporter.source() == "#!/bin/bash\necho hello\n"

    def test_lines_should_return_executable_lines(self, tmp_path: Path) -> None:
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho hello\n")
        reporter = ShellFileReporter(str(script))
        # line 1 is the shebang, line 2 is the only executable statement
        assert reporter.lines() == {2}

    @pytest.mark.parametrize(
        ("script_body", "expected_lines"),
        [
            # --- executable node types ---
            # Each script has a shebang on line 1 (not executable) and the
            # construct under test starting on line 2.
            # For compound statements the body command on line 3 is also
            # executable; closing keywords (done/fi/esac) are not.
            pytest.param("x=1\n", {2}, id="variable_assignment"),
            pytest.param("x=1 y=2\n", {2}, id="variable_assignments"),
            pytest.param("declare x=1\n", {2}, id="declaration_command"),
            pytest.param("unset x\n", {2}, id="unset_command"),
            pytest.param("echo hello\n", {2}, id="command"),
            pytest.param("echo hello | cat\n", {2}, id="pipeline"),
            pytest.param("true && echo yes\n", {2}, id="list"),
            pytest.param("[ -n hello ]\n", {2}, id="test_command"),
            pytest.param("! false\n", {2}, id="negated_command"),
            pytest.param("echo hello > /dev/null\n", {2}, id="redirected_statement"),
            pytest.param("(echo hello)\n", {2}, id="subshell"),
            pytest.param(
                "for i in 1 2; do\n  echo $i\ndone\n", {2, 3}, id="for_statement"
            ),
            pytest.param(
                "for ((i=0; i<2; i++)); do\n  echo $i\ndone\n",
                {2, 3},
                id="c_style_for_statement",
            ),
            pytest.param(
                "while false; do\n  echo loop\ndone\n", {2, 3}, id="while_statement"
            ),
            pytest.param("if true; then\n  echo yes\nfi\n", {2, 3}, id="if_statement"),
            pytest.param(
                "case x in\n  x) echo match ;;\nesac\n", {2, 3}, id="case_statement"
            ),
            # --- non-executable lines ---
            pytest.param("# a comment\necho hello\n", {3}, id="comment_excluded"),
            pytest.param("\necho hello\n", {3}, id="blank_line_excluded"),
            pytest.param("if true; then\n  echo yes\nfi\n", {2, 3}, id="fi_excluded"),
            pytest.param(
                "for i in 1; do\n  echo $i\ndone\n", {2, 3}, id="done_excluded"
            ),
            pytest.param(
                "case x in\n  x) echo match ;;\nesac\n", {2, 3}, id="esac_excluded"
            ),
            pytest.param(
                """\
                echo aaa
                func()
                {
                    echo bbb
                }
                func
                """,
                {2, 5, 7},
                id="func_brace",
            ),
            pytest.param(
                """\
                echo aaa
                func()
                (
                    echo aaa
                )
                func
                """,
                {2, 5, 7},
                id="func_paren",
            ),
            pytest.param(
                """\
                {
                    echo aaa
                    # comment
                    echo bbb
                } | grep a
                """,
                {3, 5, 6},
                id="long_pipeline",
            ),
        ],
    )
    def test_executable_lines(
        self, tmp_path: Path, script_body: str, expected_lines: set[int]
    ) -> None:
        script = tmp_path / "script.sh"
        script.write_text(f"#!/bin/bash\n{script_body}")
        reporter = ShellFileReporter(str(script))
        assert reporter.lines() == expected_lines

    @pytest.mark.parametrize(
        ("script_body", "expected_arcs"),
        [
            pytest.param(
                "echo one\necho two\necho three\n",
                {(-1, 2), (2, 3), (3, 4), (4, -1)},
                id="sequential",
            ),
            pytest.param(
                "if true; then\n  echo yes\nelse\n  echo no\nfi\n",
                {(-1, 2), (2, 3), (2, 5), (5, -1)},
                id="if_else",
            ),
            pytest.param(
                "if true; then\n  echo yes\nelse\n  echo no\nfi\necho after\n",
                {(-1, 2), (2, 3), (2, 5), (5, 7), (7, -1)},
                id="if_else_with_following_statement",
            ),
            pytest.param(
                "if true; then\n  echo yes\nfi\necho after\n",
                {(-1, 2), (2, 3), (2, 5), (5, -1)},
                id="if_no_else",
            ),
            pytest.param(
                "for i in 1 2; do\n  echo $i\ndone\necho after\n",
                {(-1, 2), (2, 3), (3, 5), (5, -1)},
                id="for_loop",
            ),
            pytest.param(
                "echo before\nfunction say_hello() {\n    echo hello\n    echo world\n}\nsay_hello\n",
                {(-1, 2), (-1, 4), (2, 7), (4, 5), (5, -1), (7, -1)},
                id="function_definition",
            ),
        ],
    )
    def test_arcs(
        self, tmp_path: Path, script_body: str, expected_arcs: set[tuple[int, int]]
    ) -> None:
        # Each script has a shebang on line 1; the construct under test starts on line 2.
        script = tmp_path / "script.sh"
        script.write_text(f"#!/bin/bash\n{script_body}")
        reporter = ShellFileReporter(str(script))
        assert reporter.arcs() == expected_arcs

    def test_exit_counts_should_collapse_multiway_if_elif_branches(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "script.sh"
        script.write_text(
            "#!/bin/bash\n"
            "if false; then\n"
            "  echo if\n"
            "elif false; then\n"
            "  echo elif\n"
            "else\n"
            "  echo else\n"
            "fi\n"
        )
        reporter = ShellFileReporter(str(script))
        assert reporter.exit_counts() == {2: 2}
        assert reporter.no_branch_lines() == {2}

    def test_exit_counts_should_include_case_branches(self, tmp_path: Path) -> None:
        script = tmp_path / "script.sh"
        script.write_text(
            "#!/bin/bash\n"
            "case $x in\n"
            "  a) echo A ;;\n"
            "  b) echo B ;;\n"
            "  *) echo D ;;\n"
            "esac\n"
        )
        reporter = ShellFileReporter(str(script))
        assert reporter.exit_counts() == {2: 2}
        assert reporter.no_branch_lines() == {2}

    def test_invalid_syntax_should_be_treated_as_executable(
        self, tmp_path: Path
    ) -> None:
        # Lines 3 and 5 are valid; lines 4 and 6 contain invalid syntax.
        # The parser should treat all non-comment, non-blank lines as executable.
        script = tmp_path / "invalid.sh"
        script.write_text(
            "#!/bin/sh\n"  # 1 — shebang
            "# comment\n"  # 2 — comment
            'variable="hello"\n'  # 3 — valid, executable
            "echo $variable\n"  # 4 — valid, executable
            "a = b\n"  # 5 — invalid syntax, treated as executable
            "a = b echo $variable\n"  # 6 — invalid syntax, treated as executable
        )
        reporter = ShellFileReporter(str(script))
        assert reporter.lines() == {3, 4, 5, 6}

    def test_handle_missing_file(self, tmp_path: Path) -> None:
        reporter = ShellFileReporter(str(tmp_path / "missing_file.sh"))
        assert reporter.lines() == set()

    def test_handle_binary_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "binary_file.sh"
        file_path.write_bytes(bytes.fromhex("348765F32190"))
        reporter = ShellFileReporter(str(file_path))
        assert reporter.lines() == set()

    def test_handle_non_file(self, tmp_path: Path) -> None:
        file_path = tmp_path / "a_directory"
        file_path.mkdir()
        reporter = ShellFileReporter(str(file_path))
        assert reporter.lines() == set()

    def test_translate_multi_line(self, tmp_path: Path) -> None:
        # Line map:
        #   1  #!/bin/bash
        #   2  echo hello            — executable (single line)
        #   3  echo multi \          — executable (start of multi-line command)
        #   4      line              — continuation, maps to line 3
        #   5  echo done             — executable (single line)
        script = tmp_path / "multiline.sh"
        script.write_text(
            "#!/bin/bash\necho hello\necho multi \\\n    line\necho done\n"
        )
        reporter = ShellFileReporter(str(script))
        reporter.lines()  # trigger parse
        # line 4 is a continuation of the command starting at line 3
        assert reporter.translate_lines([3, 4]) == {3}
        # line 5 is its own command
        assert reporter.translate_lines([5]) == {5}


def test_filename_suffix_should_match_pattern() -> None:
    suffix = filename_suffix()
    assert re.match(r".+?\.\d+\.[a-zA-Z]+", suffix)


class TestCovLineParser:
    def test_parse_result_matches_reference(self) -> None:
        parser = CovLineParser()
        for chunk in COVERAGE_LINE_CHUNKS:
            parser.parse(chunk)
        parser.flush()

        assert parser.line_data == COVERAGE_LINE_COVERAGE

    @pytest.mark.parametrize(
        ("chunks", "expected_arcs"),
        [
            pytest.param(
                COVERAGE_LINE_CHUNKS,
                COVERAGE_ARC_COVERAGE,
                id="chunked_lines",
            ),
            pytest.param(
                (b"COV:::/path/a:::1:::x\nCOV:::/path/a:::2:::x\n",),
                {"/path/a": {(-1, 1), (1, 2), (2, -1)}},
                id="single_file_sequential",
            ),
            pytest.param(
                (
                    b"COV:::/path/a:::5:::x\nCOV:::/path/a:::3:::x\nCOV:::/path/a:::7:::x\n",
                ),
                {"/path/a": {(-1, 5), (5, 3), (3, 7), (7, -1)}},
                id="single_file_non_sequential",
            ),
            pytest.param(
                (
                    b"COV:::/path/a:::5:::x\nCOV:::/path/b:::3:::x\nCOV:::/path/b:::7:::x\n",
                ),
                {
                    "/path/a": {(-1, 5), (5, -1)},
                    "/path/b": {(-1, 3), (3, 7), (7, -1)},
                },
                id="multi_file",
            ),
            pytest.param(
                (b"COV:::/path/single:::1:::x\n",),
                {"/path/single": {(-1, 1), (1, -1)}},
                id="single_line_no_arcs",
            ),
        ],
    )
    def test_arcs_should_match_expected(
        self, chunks: tuple[bytes, ...], expected_arcs: ArcData
    ) -> None:
        parser = CovLineParser()
        for chunk in chunks:
            parser.parse(chunk)
        parser.flush()
        parser.finalize()

        assert parser.arc_data == expected_arcs

    def test_parse_should_raise_for_incomplete_line(self) -> None:
        parser = CovLineParser()
        with pytest.raises(ValueError, match="could not parse line"):
            parser.parse(
                b"COV:::/home/dummy_user/dummy_dir_b:::a line with missing line number\n"
            )


class TestCoverageParserThread:
    class WriterThread(threading.Thread):
        def __init__(self, fifo_path: Path):
            super().__init__()
            self._fifo_path = fifo_path

        def run(self) -> None:
            with self._fifo_path.open("wb") as fd:
                for c in COVERAGE_LINE_CHUNKS[0:2]:
                    fd.write(c)
                    sleep(0.1)

            sleep(0.1)
            with self._fifo_path.open("wb") as fd:
                for c in COVERAGE_LINE_CHUNKS[2:]:
                    fd.write(c)
                    sleep(0.1)

    class CovLineParserSpy(CovLineParser):
        def __init__(self) -> None:
            super().__init__()
            self.recorded_lines: list[str] = []

        def _report_lines(self, lines: list[str]) -> None:
            self.recorded_lines.extend(lines)
            super()._report_lines(lines)

    class CovWriterFake:
        def __init__(self) -> None:
            self.line_data: LineData = defaultdict(set)

        def write(
            self,
            line_data: LineData,
            *,
            arc_data: ArcData | None = None,  # noqa: ARG002
        ) -> None:
            self.line_data.update(line_data)

    def test_lines_should_match_reference(self) -> None:
        parser = self.CovLineParserSpy()
        writer = self.CovWriterFake()
        parser_thread = CoverageParserThread(
            coverage_writer=cast("CoverageWriter", writer),
            name="CoverageParserThread",
            parser=parser,
        )
        parser_thread.start()

        writer_thread = self.WriterThread(fifo_path=parser_thread.fifo_path)
        writer_thread.start()
        writer_thread.join()

        parser_thread.stop()
        parser_thread.join()

        assert parser.recorded_lines == COVERAGE_LINES

        for filename, lines in COVERAGE_LINE_COVERAGE.items():
            assert writer.line_data[filename] == lines


class TestCoverageWriter:
    def test_write_should_produce_readable_file(self, tmp_path: Path) -> None:
        data_file_path = tmp_path / "coverage-data.db"
        writer = CoverageWriter(data_file_path)
        writer.write(COVERAGE_LINE_COVERAGE)

        concrete_data_file_path = next(
            data_file_path.parent.glob(data_file_path.stem + "*")
        )
        cov_db = coverage.CoverageData(
            basename=str(concrete_data_file_path), suffix=False
        )
        cov_db.read()

        assert cov_db.measured_files() == set(COVERAGE_LINE_COVERAGE.keys())
        for filename, lines in COVERAGE_LINE_COVERAGE.items():
            assert cov_db.lines(filename) == sorted(lines)

    def test_write_should_annotate_file_tracer(self, tmp_path: Path) -> None:
        data_file_path = tmp_path / "coverage-data.db"
        writer = CoverageWriter(data_file_path)
        writer.write(COVERAGE_LINE_COVERAGE)

        concrete_data_file_path = next(
            data_file_path.parent.glob(data_file_path.stem + "*")
        )
        cov_db = coverage.CoverageData(
            basename=str(concrete_data_file_path), suffix=False
        )
        cov_db.read()

        for filename in COVERAGE_LINE_COVERAGE:
            assert cov_db.file_tracer(filename) == "coverage_sh.ShellPlugin"

    def test_writer_should_prefer_pytest_cov_env_vars(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pytest_cov_path = tmp_path / "pytest-cov-data"
        monkeypatch.setenv("COV_CORE_DATAFILE", str(pytest_cov_path))

        data_file_path = tmp_path / "coverage-data.db"
        writer = CoverageWriter(data_file_path)
        writer.write(COVERAGE_LINE_COVERAGE)

        assert list(data_file_path.parent.glob(data_file_path.stem + "*")) == []

        concrete_pytest_cov_path = next(
            pytest_cov_path.parent.glob(pytest_cov_path.stem + "*")
        )
        assert concrete_pytest_cov_path.is_file()


class TestPatchedPopen:
    @pytest.mark.parametrize("is_recording", [(True), (False)])
    def test_call_should_execute_script(
        self,
        is_recording: bool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        script = tmp_path / "hello.sh"
        script.write_text("#!/bin/bash\necho hello\n")

        cov = None
        if is_recording:
            cov = coverage.Coverage.current()
            if cov is None:  # pragma: no cover
                cov = coverage.Coverage()
            cov.start()
        else:
            monkeypatch.setattr(coverage.Coverage, "current", lambda: None)

        proc = PatchedPopen(
            ["/bin/bash", str(script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf8",
        )
        proc.wait()

        if cov is not None:  # pragma: no cover
            cov.stop()

        assert proc.stderr is not None
        assert proc.stderr.read() == ""
        assert proc.stdout is not None
        assert proc.stdout.read() == "hello\n"

    def test_poll_should_return_none_while_process_is_running(self) -> None:
        proc = PatchedPopen(
            ["/bin/bash", "-c", "read"],
            stdin=subprocess.PIPE,
        )
        assert proc.poll() is None
        proc.communicate(b"")

    def test_asyncio_subprocess_should_complete(
        self,
        tmp_path: Path,
    ) -> None:
        script = tmp_path / "hello.sh"
        script.write_text("#!/bin/bash\necho hello\n")
        script.chmod(0o755)

        cov = coverage.Coverage.current()
        if cov is None:  # pragma: no cover
            cov = coverage.Coverage()
        cov.start()

        async def run() -> str:
            proc = await asyncio.create_subprocess_exec(
                str(script),
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return stdout.decode()

        result = asyncio.run(run())

        if cov is not None:  # pragma: no cover
            cov.stop()

        assert result == "hello\n"

    def test_wait_should_raise_timeout_while_process_is_running(self) -> None:
        proc = PatchedPopen(
            ["/bin/bash", "-c", "read"],
            stdin=subprocess.PIPE,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(0.1)
        proc.communicate(b"")

    def test_call_should_record_coverage_for_shell_script_invoking_python(
        self,
        tmp_path: Path,
    ) -> None:
        """Shell script lines are measured when a shell script also invokes a Python subprocess.

        Verifies that the FIFO/BASH_XTRACEFD mechanism records coverage for the shell
        script itself even when the script calls out to Python. The Python child's
        own coverage is handled by coveragepy's subprocess mechanism and is not
        asserted here.
        """
        if Version(version("coverage")) < Version("7.13.0"):
            pytest.skip(
                "coverage < 7.13.0 does not install .pth file at install time"
            )  # pragma: no cover

        inner_py = tmp_path / "inner.py"
        inner_py.write_text("#! /usr/bin/env python3\nprint('hello from python')\n")
        inner_py.chmod(0o755)

        # Line map:
        #   1  #!/bin/bash
        #   2  echo hello        — executable, covered
        #   3  <inner_py>        — executable, covered (invokes Python child)
        caller_sh = tmp_path / "caller.sh"
        caller_sh.write_text(f"#!/bin/bash\necho hello\n{inner_py}\n")
        caller_sh.chmod(0o755)

        data_file_path = tmp_path / "coverage-data.db"
        PatchedPopen.data_file_path = data_file_path

        cov = coverage.Coverage.current()
        if cov is None:  # pragma: no cover
            cov = coverage.Coverage()
        cov.start()

        proc = PatchedPopen(
            ["/bin/bash", str(caller_sh)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf8",
        )
        proc.wait()

        if cov is not None:  # pragma: no cover
            cov.stop()

        assert proc.stdout is not None
        assert proc.stdout.read() == "hello\nhello from python\n"

        # Coverage data for the shell script should have been written to a sidecar file
        sidecar = next(data_file_path.parent.glob(data_file_path.name + ".sh.*"))
        cov_db = coverage.CoverageData(basename=str(sidecar), suffix=False)
        cov_db.read()
        assert str(caller_sh) in cov_db.measured_files()
        assert cov_db.lines(str(caller_sh)) == [2, 3]


class TestMonitorThread:
    class MainThreadStub:
        def join(self) -> None:
            return

    def test_run_should_wait_for_main_thread_join(self, tmp_path: Path) -> None:
        data_file_path = tmp_path / "coverage-data.db"

        parser_thread = CoverageParserThread(
            coverage_writer=CoverageWriter(data_file_path),
        )
        parser_thread.start()

        monitor_thread = MonitorThread(
            parser_thread=parser_thread,
            main_thread=cast("threading.Thread", self.MainThreadStub()),
        )
        monitor_thread.start()


class TestShellPlugin:
    def test_file_tracer_should_return_null(self) -> None:
        plugin = ShellPlugin({})
        assert plugin.file_tracer("foobar") is None

    def test_file_reporter_should_return_instance(self) -> None:
        plugin = ShellPlugin({})
        reporter = plugin.file_reporter("foobar")
        assert isinstance(reporter, ShellFileReporter)
        assert reporter.path == Path("foobar")

    def test_find_executable_files_should_find_shell_files(
        self, tmp_path: Path
    ) -> None:
        # A shell script with a non-standard extension — detected by MIME type
        (tmp_path / "shell-file.weird.suffix").write_text("#!/bin/bash\necho hi\n")
        # A .sh file that contains Python — should NOT be detected as shell
        (tmp_path / "non-bash-file.sh").write_text("def main(): pass\n")
        # A Python file — should not be detected
        (tmp_path / "python_file.py").write_text("print('hello')\n")
        # A shell file inside a hidden directory — should be excluded
        hidden_dir = tmp_path / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "hidden.sh").write_text("#!/bin/bash\necho hidden\n")

        plugin = ShellPlugin({})
        executable_files = list(plugin.find_executable_files(str(tmp_path)))

        assert [Path(f) for f in sorted(executable_files)] == [
            tmp_path / "shell-file.weird.suffix",
        ]

    def test_find_executable_files_should_find_symlinks(self, tmp_path: Path) -> None:
        plugin = ShellPlugin({})

        foo_file_path = tmp_path.joinpath("foo.sh")
        foo_file_path.write_text("#!/bin/sh\necho foo")
        foo_file_link = foo_file_path.with_suffix(".link.sh")
        foo_file_link.symlink_to(foo_file_path)

        bar_dir_path = tmp_path.joinpath("bar")
        bar_dir_path.mkdir()
        bar_link_path = bar_dir_path.with_suffix(".link")
        bar_link_path.symlink_to(bar_dir_path)

        executable_files = plugin.find_executable_files(str(tmp_path))

        assert set(executable_files) == {str(f) for f in (foo_file_path, foo_file_link)}

    def test_configure_should_update_data_file_path(self) -> None:
        plugin = ShellPlugin({})
        old_data_file_path = Path("/old/value")
        PatchedPopen.data_file_path = old_data_file_path
        config = CoverageConfig()
        plugin.configure(config)
        assert PatchedPopen.data_file_path != old_data_file_path
        assert PatchedPopen.data_file_path.name == ".coverage"

    def test_configure_should_patch_subprocess_popen(self) -> None:
        plugin = ShellPlugin({})
        config = CoverageConfig()
        plugin.configure(config)
        assert subprocess.Popen is PatchedPopen

    def test_configure_should_warn_when_sysmon(self) -> None:
        config = CoverageConfig()
        config.set_option("run:core", "sysmon")
        plugin = ShellPlugin({})
        with pytest.warns(UserWarning, match="sysmon tracer is not supported"):
            plugin.configure(config)
        assert config.get_option("run:core") == "ctrace"

    def test_configure_should_set_bash_env_when_cover_always(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """configure() should set BASH_ENV when cover_always is enabled.

        When cover_always=True, configure() must export BASH_ENV so that bash sources
        the coverage helper script automatically for every shell invocation, including
        those not spawned via subprocess.
        """
        monkeypatch.delenv("BASH_ENV", raising=False)
        plugin = ShellPlugin({"cover_always": True})
        config = CoverageConfig()
        plugin.configure(config)
        assert os.getenv("BASH_ENV")
