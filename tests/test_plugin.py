#  SPDX-License-Identifier: MIT
#  Copyright (c) 2023-2024 Kilian Lackhove
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path
from socket import gethostname
from time import sleep

import coverage
import pytest

from coverage_sh import ShellPlugin
from coverage_sh.plugin import (
    CoverageParserThread,
    CoverageWriter,
    CovLineParser,
    LineData,
    MonitorThread,
    PatchedPopen,
    ShellFileReporter,
    filename_suffix,
)

SYNTAX_EXAMPLE_EXECUTABLE_LINES = {
    12,
    15,
    18,
    19,
    21,
    25,
    26,
    31,
    34,
    37,
    38,
    41,
    42,
    45,
    46,
    47,
    48,
    51,
    52,
    54,
    57,
    60,
    63,
}

SYNTAX_EXAMPLE_STDOUT = (
    "Hello, World!\n"
    "Variable is set to 'Hello, World!'\n"
    "Iteration 1\n"
    "Iteration 2\n"
    "Iteration 3\n"
    "Iteration 4\n"
    "Iteration 5\n"
    "Hello from a function!\n"
    "Current OS is: Linux\n"
    "5 + 3 = 8\n"
    "This is a sample file.\n"
    "You selected a banana.\n"
)
SYNTAX_EXAMPLE_COVERED_LINES = [
    12,
    15,
    18,
    19,
    25,
    26,
    31,
    34,
    37,
    38,
    41,
    42,
    45,
    46,
    47,
    48,
    51,
    52,
    57,
]
SYNTAX_EXAMPLE_MISSING_LINES = [
    21,
    54,
    60,
    63,
]
COVERAGE_LINE_CHUNKS = (
    b"""\
CCOV:::/home/dummy_user/dummy_dir_a:::1:::a normal line
COV:::/home/dummy_user/dummy_dir_b:::10:::a line
with a line fragment

COV:::/home/dummy_user/dummy_dir_a:::2:::a  line with ::: triple columns
COV:::/home/dummy_user/dummy_dir_a:::3:::a  line """,
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
    b"/dummy_user/dummy_dir_a:::4:::a chunked line",
)
COVERAGE_LINES = [
    "CCOV:::/home/dummy_user/dummy_dir_a:::1:::a normal line",
    "COV:::/home/dummy_user/dummy_dir_b:::10:::a line",
    "with a line fragment",
    "COV:::/home/dummy_user/dummy_dir_a:::2:::a  line with ::: triple columns",
    "COV:::/home/dummy_user/dummy_dir_a:::3:::a  line that spans multiple chunks",
    "COV:::/home/dummy_user/dummy_dir_a:::4:::a chunked line",
]
COVERAGE_LINE_COVERAGE = {
    "/home/dummy_user/dummy_dir_a": {1, 2, 3, 4},
    "/home/dummy_user/dummy_dir_b": {10},
}


@pytest.fixture()
def examples_dir(resources_dir):
    return resources_dir / "examples"


@pytest.fixture()
def syntax_example_path(resources_dir, tmp_path):
    original_path = resources_dir / "syntax_example.sh"
    working_copy_path = tmp_path / "syntax_example.sh"
    working_copy_path.write_bytes(original_path.read_bytes())
    return working_copy_path


@pytest.mark.parametrize("cover_always", [(True), (False)])
def test_end2end(dummy_project_dir, monkeypatch, cover_always: bool):
    monkeypatch.chdir(dummy_project_dir)

    coverage_file_path = dummy_project_dir.joinpath(".coverage")
    assert not coverage_file_path.is_file()

    if cover_always:
        pyproject_file = dummy_project_dir.joinpath("pyproject.toml")
        with pyproject_file.open("a") as fd:
            fd.write("\n[tool.coverage.coverage_sh]\ncover_always = true")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coverage", "run", "main.py"],
            cwd=dummy_project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except subprocess.TimeoutExpired as e:  # pragma: no cover
        assert e.stdout == "failed stdout"  # noqa: PT017
        assert e.stderr == "failed stderr"  # noqa: PT017
        assert False
    assert proc.stderr == ""
    assert proc.stdout == SYNTAX_EXAMPLE_STDOUT
    assert proc.returncode == 0

    assert dummy_project_dir.joinpath(".coverage").is_file()
    assert len(list(dummy_project_dir.glob(f".coverage.sh.{gethostname()}.*"))) == 1

    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "combine"],
        cwd=dummy_project_dir,
        check=False,
    )
    print("recombined")
    assert proc.returncode == 0

    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "html"], cwd=dummy_project_dir, check=False
    )
    assert proc.returncode == 0

    proc = subprocess.run(
        [sys.executable, "-m", "coverage", "json"], cwd=dummy_project_dir, check=False
    )
    assert proc.returncode == 0

    coverage_json = json.loads(dummy_project_dir.joinpath("coverage.json").read_text())
    assert coverage_json["files"]["test.sh"]["executed_lines"] == [8, 9]
    assert coverage_json["files"]["syntax_example.sh"]["excluded_lines"] == []
    assert (
        coverage_json["files"]["syntax_example.sh"]["executed_lines"]
        == SYNTAX_EXAMPLE_COVERED_LINES
    )
    assert (
        coverage_json["files"]["syntax_example.sh"]["missing_lines"]
        == SYNTAX_EXAMPLE_MISSING_LINES
    )


class TestShellFileReporter:
    @pytest.fixture()
    def reporter(self, syntax_example_path):
        return ShellFileReporter(str(syntax_example_path))

    def test_source_should_be_cached(self, syntax_example_path, reporter):
        reference = Path(reporter.path).read_text()

        assert reporter.source() == reference
        syntax_example_path.unlink()
        assert reporter.source() == reference

    def test_lines_should_match_reference(self, reporter):
        assert reporter.lines() == SYNTAX_EXAMPLE_EXECUTABLE_LINES

    def test_invalid_syntax_should_be_treated_as_executable(self, resources_dir):
        reporter = ShellFileReporter(str(resources_dir / "invalid_syntax.sh"))
        assert reporter.lines() == {9, 12, 15, 18}


def test_filename_suffix_should_match_pattern():
    suffix = filename_suffix()
    assert re.match(r".+?\.\d+\.[a-zA-Z]+", suffix)


class TestCovLineParser:
    def test_parse_result_matches_reference(self):
        parser = CovLineParser()
        for chunk in COVERAGE_LINE_CHUNKS:
            parser.parse(chunk)
        parser.flush()

        assert parser.line_data == COVERAGE_LINE_COVERAGE

    def test_parse_should_raise_for_incomplete_line(self):
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

        def run(self):
            print("writer start")
            with self._fifo_path.open("wb") as fd:
                for c in COVERAGE_LINE_CHUNKS[0:2]:
                    fd.write(c)
                    sleep(0.1)

            sleep(0.1)
            with self._fifo_path.open("wb") as fd:
                for c in COVERAGE_LINE_CHUNKS[2:]:
                    fd.write(c)
                    sleep(0.1)

            print("writer done")

    class CovLineParserSpy(CovLineParser):
        def __init__(self):
            super().__init__()
            self.recorded_lines = []

        def _report_lines(self, lines: list[str]) -> None:
            self.recorded_lines.extend(lines)
            super()._report_lines(lines)

    class CovWriterFake:
        def __init__(self):
            self.line_data: LineData = defaultdict(set)

        def write(self, line_data: LineData) -> None:
            self.line_data.update(line_data)

    def test_lines_should_match_reference(self):
        parser = self.CovLineParserSpy()
        writer = self.CovWriterFake()
        parser_thread = CoverageParserThread(
            coverage_writer=writer,
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
    def test_write_should_produce_readable_file(self, dummy_project_dir):
        data_file_path = dummy_project_dir.joinpath("coverage-data.db")
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


class TestPatchedPopen:
    @pytest.mark.parametrize("is_recording", [(True), (False)])
    def test_call_should_execute_example(
        self,
        is_recording,
        resources_dir,
        dummy_project_dir,
        monkeypatch,
    ):
        monkeypatch.chdir(dummy_project_dir)

        cov = None
        if is_recording:
            cov = coverage.Coverage.current()
            if cov is None:  # pragma: no cover
                # start coverage in case pytest was not executed with the coverage module. Otherwise, we just recod to
                # the parent coverage
                cov = coverage.Coverage()
            cov.start()
        else:
            monkeypatch.setattr(coverage.Coverage, "current", lambda: None)

        test_sh_path = resources_dir / "testproject" / "test.sh"
        proc = PatchedPopen(
            ["/bin/bash", test_sh_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf8",
        )
        proc.wait()

        if cov is not None:  # pragma: no cover
            cov.stop()

        assert proc.stderr.read() == ""
        assert proc.stdout.read() == SYNTAX_EXAMPLE_STDOUT


class TestMonitorThread:
    class MainThreadStub:
        def join(self):
            return

    def test_run_should_wait_for_main_thread_join(self, dummy_project_dir):
        data_file_path = dummy_project_dir.joinpath("coverage-data.db")

        parser_thread = CoverageParserThread(
            coverage_writer=CoverageWriter(data_file_path),
        )
        parser_thread.start()

        monitor_thread = MonitorThread(
            parser_thread=parser_thread, main_thread=self.MainThreadStub()
        )
        monitor_thread.start()


class TestShellPlugin:
    def test_init_cover_always(self):
        plugin = ShellPlugin({"cover_always": True})
        del plugin

    def test_file_tracer_should_return_None(self):
        plugin = ShellPlugin({})
        assert plugin.file_tracer("foobar") is None

    def test_file_reporter_should_return_instance(self):
        plugin = ShellPlugin({})
        reporter = plugin.file_reporter("foobar")
        assert isinstance(reporter, ShellFileReporter)
        assert reporter.path == Path("foobar")

    def test_find_executable_files_should_find_shell_files(self, examples_dir):
        plugin = ShellPlugin({})

        executable_files = plugin.find_executable_files(str(examples_dir))

        assert [Path(f) for f in sorted(executable_files)] == [
            examples_dir / "shell-file.weird.suffix",
        ]

    def test_find_executable_files_should_find_symlinks(self, tmp_path):
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
