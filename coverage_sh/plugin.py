#  SPDX-License-Identifier: MIT
#  Copyright (c) 2023-2024 Kilian Lackhove

from __future__ import annotations

import contextlib
import inspect
import os
import selectors
import stat
import string
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path
from random import Random
from socket import gethostname
from time import sleep
from typing import TYPE_CHECKING, Any, cast
from warnings import warn

import magic
import tree_sitter_bash
from coverage import Coverage, CoverageData, CoveragePlugin, FileReporter, FileTracer
from tree_sitter import Language, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from coverage.debug import DebugControl
    from coverage.types import TConfigurable, TLineNo
    from tree_sitter import Node

LineData = dict[str, set[int]]
ArcData = dict[str, set[tuple[int, int]]]

TMP_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))  # noqa: S108
TRACEFILE_PREFIX = "shelltrace"
EXECUTABLE_NODE_TYPES = {
    "redirected_statement",
    "variable_assignment",
    "variable_assignments",
    "command",
    "declaration_command",
    "unset_command",
    "test_command",
    "negated_command",
    "for_statement",
    "c_style_for_statement",
    "while_statement",
    "if_statement",
    "case_statement",
    "list",
}
SUPPORTED_MIME_TYPES = {"text/x-shellscript"}
MAX_BRANCH_EXITS = 2

PLUGIN_DEBUG_OPTION = "shell"


def debug_write(msg: str, debug_control: DebugControl | None = None) -> None:

    current_coverage = Coverage.current()
    if current_coverage is None and debug_control is None:
        # we are not recording coverage, so we have nowhere to send the message
        return

    try:
        debug_control = debug_control or Coverage.current()._debug  # type: ignore[union-attr]  # noqa: SLF001

        if debug_control.should(PLUGIN_DEBUG_OPTION):
            # DebugControl.write expects to be called from a frame with a "self" variable, so
            # we use the same code to fetch that and pass it down to emulate that behavior
            self = inspect.stack()[1][0].f_locals.get("self")  # noqa: F841

            debug_control.write(msg)
    except Exception as e:  # noqa: BLE001
        warn(f'Failed to log debug message: "{msg}": {e}', stacklevel=2)


class ShellFileReporter(FileReporter):
    def __init__(self, filename: str) -> None:
        super().__init__(filename)

        self.path = Path(filename)
        self._content: str | None = None
        self._executable_lines: set[int] = set()
        self._arcs: set[tuple[int, int]] = set()
        self._no_branch_lines: set[int] = set()
        self._translate_lines: dict[int, int] = {}
        self._parser = Parser(Language(tree_sitter_bash.language()))

    def _is_exhaustive_if_statement(self, node: Node) -> bool:
        """Return true when an if statement has an else branch.

        We use this to detect control-flow nodes where all outcomes are handled
        inside the block, so a direct fallthrough arc from the if header to the
        next statement would be incorrect.
        """
        if node.type != "if_statement":
            return False
        return any(child.type == "else_clause" for child in node.children)

    def _is_exhaustive_case_statement(self, node: Node) -> bool:
        """Return true when a case statement has a default ``*)`` pattern.

        Tree-sitter exposes case patterns as ``case_item`` children. A default
        arm is represented by an ``extglob_pattern`` node with text ``*``.
        """
        if node.type != "case_statement":
            return False

        for child in node.children:
            if child.type != "case_item":
                continue

            first_named_child = next(
                (
                    case_item_child
                    for case_item_child in child.children
                    if case_item_child.is_named
                ),
                None,
            )
            if (
                first_named_child is not None
                and first_named_child.type == "extglob_pattern"
                and first_named_child.text == b"*"
            ):
                return True

        return False

    def source(self) -> str:
        if self._content is None:
            if not self.path.is_file():
                return ""
            try:
                self._content = self.path.read_text()
            except UnicodeDecodeError:
                return ""

        return self._content

    def _parse_ast(
        self,
        node: Node,
        executable_parent: Node | None = None,
        previous_executable: Node | None = None,
    ) -> Node | None:
        # For exhaustive control nodes (if+else / case+*), we keep track of the
        # last executable statement reached in any branch so callers can chain
        # control-flow from inside the block, not from the header line.
        exhaustive_control = False
        branch_last_executable: Node | None = previous_executable

        if node.is_named and node.type in EXECUTABLE_NODE_TYPES:
            sline = node.start_point.row + 1
            eline = node.end_point.row + 1
            self._executable_lines.add(sline)

            exhaustive_control = self._is_exhaustive_if_statement(
                node
            ) or self._is_exhaustive_case_statement(node)

            # for multi-line commands translate to the first line
            if sline != eline and node.type == "command":
                for index in range(sline + 1, eline + 1):
                    self._translate_lines[index] = sline

            if previous_executable is None:
                # first executable node in file / function
                self._arcs.add((-1, sline))
            else:
                self._arcs.add((previous_executable.start_point.row + 1, sline))

            executable_parent = node
            previous_executable = node
            branch_last_executable = node

        for child in node.children:
            if node.type == "function_definition":
                # Function bodies are independent arc graphs: arcs inside the
                # body must not connect to the call-site context, and the
                # call-site context must not be affected by what happens inside.
                func_last = self._parse_ast(
                    child,
                    executable_parent=node,
                    previous_executable=None,
                )
                if func_last is not None:
                    self._arcs.add((func_last.start_point.row + 1, -1))
            else:
                child_last = self._parse_ast(
                    child,
                    executable_parent=executable_parent,
                    # Each direct child of an executable node starts fresh from
                    # that node, so alternative branches (e.g. else) arc from
                    # the parent rather than from the last sibling branch.
                    # This avoids creating fake sequential arcs between sibling
                    # branches that are mutually exclusive.
                    previous_executable=node
                    if node is executable_parent
                    else previous_executable,
                )
                previous_executable = child_last
                if (
                    node is executable_parent
                    and child_last is not None
                    and child_last is not node
                ):
                    branch_last_executable = child_last

        if node is executable_parent and exhaustive_control:
            # For exhaustive controls, returning the branch tail prevents a
            # synthetic fallthrough arc from the control header to the next
            # statement after the block.
            return branch_last_executable

        return previous_executable

    def _ensure_parsed(self) -> None:
        if self._executable_lines:
            return  # already parsed
        tree = self._parser.parse(self.source().encode("utf-8"))
        self._parse_ast(tree.root_node)
        self._collapse_multiway_exits()
        if self._executable_lines:
            self._arcs.add((max(self._executable_lines), -1))

    def _collapse_multiway_exits(self) -> None:
        """Collapse multi-way branch exits to binary form for HTML compatibility.

        Coverage.py's HTML reporter has an assertion that each branch line has at
        most one "long" annotation (the verbose description of a missing arc). This
        assertion fails for shell scripts with multi-way branches like:

            case $var in
              a) echo A ;;
              b) echo B ;;
              c) echo C ;;
              *) echo D ;;
            esac

        The AST parser produces a single branch line (the "case" keyword) with 4
        exits to each case arm. When coverage reports missing branches, this creates
        3 long annotations -> assertion failure.

        Instead of modeling the case statement as one line with N exits, we collapse
        it to a binary form: keep only the first and last exit destinations, and mark
        the source line as "no branch" so coverage won't emit branch annotations.

        Implementation:
        - Scan all arcs to find source lines with > MAX_BRANCH_EXITS (2) destinations
        - For each such line, keep only the min and max destination arcs
        - Record the source line in _no_branch_lines so exit_counts still reports 2
          but HTML won't annotate it as a multi-way branch
        """
        exits_by_line: dict[int, set[int]] = defaultdict(set)
        for src, dst in self._arcs:
            if src > 0 and dst > 0 and dst != src:
                exits_by_line[src].add(dst)

        for src, exits in exits_by_line.items():
            if len(exits) <= MAX_BRANCH_EXITS:
                continue

            self._no_branch_lines.add(src)
            sorted_exits = sorted(exits)
            keep = {sorted_exits[0], sorted_exits[-1]}
            self._arcs = {
                (arc_src, arc_dst)
                for arc_src, arc_dst in self._arcs
                if arc_src != src or arc_dst <= 0 or arc_dst in keep
            }

    def no_branch_lines(self) -> set[TLineNo]:
        self._ensure_parsed()
        return self._no_branch_lines

    def lines(self) -> set[TLineNo]:
        self._ensure_parsed()
        return self._executable_lines

    def translate_lines(self, lines: Iterable[TLineNo]) -> set[TLineNo]:
        self._ensure_parsed()
        result: set[TLineNo] = set()
        for index in lines:
            result.add(self._translate_lines.get(index, index))
        return result

    def arcs(self) -> set[tuple[TLineNo, TLineNo]]:
        self._ensure_parsed()
        return {(src, dst) for src, dst in self._arcs if src != dst}

    def exit_counts(self) -> dict[TLineNo, int]:
        self._ensure_parsed()
        exits: dict[TLineNo, set[TLineNo]] = defaultdict(set)
        for src, dst in self.arcs():
            if src > 0:
                exits[src].add(dst)
        return {src: len(dsts) for src, dsts in exits.items() if len(dsts) > 1}


def filename_suffix() -> str:
    die = Random(os.urandom(8))
    letters = string.ascii_uppercase + string.ascii_lowercase
    rolls = "".join(die.choice(letters) for _ in range(6))
    return f"{gethostname()}.{os.getpid()}.X{rolls}x"


class CovLineParser:
    def __init__(self) -> None:
        self._last_line_fragment = ""
        self._last_line = -1
        self._last_path = ""
        self._last_function = ""
        self.line_data: LineData = defaultdict(set)
        self.arc_data: ArcData = defaultdict(set)

    def parse(self, buf: bytes) -> None:
        self._report_lines(list(self._buf_to_lines(buf)))

    def _buf_to_lines(self, buf: bytes) -> Iterator[str]:
        raw = self._last_line_fragment + buf.decode()
        self._last_line_fragment = ""

        for line in raw.splitlines(keepends=True):
            if line == "\n":
                pass
            elif line.endswith("\n"):
                yield line[:-1]
            else:
                self._last_line_fragment = line

    def _report_lines(self, lines: list[str]) -> None:
        if not lines:
            return

        for line in lines:
            if "COV:::" not in line:
                continue

            try:
                path_, lineno_, func_ = self._parse_trace_line(line)
                lineno = int(lineno_)
                path = Path(path_).absolute()
            except ValueError as e:
                raise ValueError(f"could not parse line {line}") from e

            path_str = str(path)
            self.line_data[path_str].add(lineno)

            if self._last_path == "":
                # first line ever
                self.arc_data[path_str].add((-1, lineno))
            elif path_str == self._last_path:
                # same file
                if func_ != self._last_function:
                    # function scope changed
                    self.arc_data[self._last_path].add((self._last_line, -1))
                    self.arc_data[path_str].add((-1, lineno))
                else:
                    # same function
                    self.arc_data[path_str].add((self._last_line, lineno))
            else:
                # different file
                self.arc_data[self._last_path].add((self._last_line, -1))
                self.arc_data[path_str].add((-1, lineno))

            self._last_line = lineno
            self._last_path = path_str
            self._last_function = func_

    def _parse_trace_line(self, line: str) -> tuple[str, str, str]:
        _, path_, lineno_, func_ = line.split(":::", maxsplit=3)
        func_ = func_.split(":::", 1)[0]
        return (path_, lineno_, func_)

    def flush(self) -> None:
        self.parse(b"\n")

    def finalize(self) -> None:
        self.arc_data[self._last_path].add((self._last_line, -1))


class CoverageWriter:
    def __init__(self, coverage_data_path: Path, *, branch: bool = False):
        # pytest-cov uses the COV_CORE_DATAFILE env var to configure the datafile base path
        coverage_data_env_var = os.environ.get("COV_CORE_DATAFILE")
        if coverage_data_env_var is not None:
            coverage_data_path = Path(coverage_data_env_var).absolute()

        self._coverage_data_path = coverage_data_path
        self._branch = branch

    def write(self, line_data: LineData, arc_data: ArcData | None = None) -> None:
        suffix_ = "sh." + filename_suffix()
        coverage_data = CoverageData(
            basename=self._coverage_data_path,
            suffix=suffix_,
        )

        coverage_data.add_file_tracers(
            dict.fromkeys(line_data, "coverage_sh.ShellPlugin")
        )
        if self._branch:
            if arc_data:
                for path, arcs in arc_data.items():
                    if arcs:
                        coverage_data.add_arcs({path: arcs})
        else:
            coverage_data.add_lines(line_data)
        coverage_data.write()


class CoverageParserThread(threading.Thread):
    def __init__(
        self,
        coverage_writer: CoverageWriter,
        name: str | None = None,
        parser: CovLineParser | None = None,
    ) -> None:
        super().__init__(name=name)
        self._keep_running = True
        self._listening = False
        self._parser = parser or CovLineParser()
        self._coverage_writer = coverage_writer

        self.fifo_path = TMP_PATH / f"coverage-sh.{filename_suffix()}.pipe"
        with contextlib.suppress(FileNotFoundError):
            self.fifo_path.unlink()
        os.mkfifo(self.fifo_path, mode=stat.S_IRUSR | stat.S_IWUSR)
        debug_write(
            f"init done fifo_path={self.fifo_path}",
        )

    def start(self) -> None:
        debug_write("start")
        super().start()
        while not self._listening:
            sleep(0.0001)

    def stop(self) -> None:
        debug_write("stop")
        self._keep_running = False

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        while self._keep_running:
            # we need to keep reopening the fifo as long as the subprocess is running because multiple bash processes
            # might write EOFs to it
            fifo = os.open(self.fifo_path, flags=os.O_RDONLY | os.O_NONBLOCK)
            sel.register(fifo, selectors.EVENT_READ)
            self._listening = True

            eof = False
            data_incoming = True
            while not eof and (data_incoming or self._keep_running):
                events = sel.select(timeout=1)
                if not len(events):
                    debug_write(
                        "select timeout, retry ...",
                    )
                data_incoming = len(events) > 0
                for key, _ in events:
                    buf = os.read(key.fd, 2**10)
                    if not buf:
                        eof = True
                        break
                    self._parser.parse(buf)

            self._parser.flush()

            sel.unregister(fifo)
            os.close(fifo)

        self._parser.finalize()

        self._coverage_writer.write(
            self._parser.line_data, arc_data=self._parser.arc_data
        )
        with contextlib.suppress(FileNotFoundError):
            self.fifo_path.unlink()


OriginalPopen = subprocess.Popen


def init_helper(fifo_path: Path) -> Path:
    helper_path = Path(TMP_PATH, f"coverage-sh.{filename_suffix()}.sh")
    helper_path.write_text(
        rf"""#!/bin/sh
PS4="COV:::\${{BASH_SOURCE}}:::\${{LINENO}}:::\${{FUNCNAME[0]}}:::"
exec {{BASH_XTRACEFD}}>>"{fifo_path!s}"
export BASH_XTRACEFD
set -x
"""
    )
    helper_path.chmod(mode=stat.S_IRUSR | stat.S_IWUSR)
    return helper_path


# the proper way to do this would be using OriginalPopen[Any] but that is not supported by python 3.8, so we jusrt
# ignore this for the time being
class PatchedPopen(OriginalPopen):  # type: ignore[type-arg]
    data_file_path: Path = Path.cwd()
    branch: bool = False

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if Coverage.current() is None:
            # we are not recording coverage, so just act like the original Popen
            self._parser_thread = None
            super().__init__(*args, **kwargs)
            return

        debug_write("__init__")

        # convert args into kwargs
        sig = inspect.signature(subprocess.Popen)
        kwargs.update(dict(zip(sig.parameters.keys(), args)))

        self._parser_thread = CoverageParserThread(
            coverage_writer=CoverageWriter(
                coverage_data_path=self.data_file_path, branch=self.branch
            ),
            name="CoverageParserThread(None)",
        )
        self._parser_thread.start()

        self._helper_path = init_helper(self._parser_thread.fifo_path)

        env = kwargs.get("env", os.environ.copy())
        env["BASH_ENV"] = str(self._helper_path)
        env["ENV"] = str(self._helper_path)
        kwargs["env"] = env

        super().__init__(**kwargs)

    def wait(self, timeout: float | None = None) -> int:
        debug_write(f"wait timeout={timeout}")
        result = super().wait(timeout)
        self._clean_helper()
        debug_write(f"wait result={result}")
        return result

    def poll(self) -> int | None:
        result = super().poll()
        if result is not None:
            self._clean_helper()
        return result

    def __del__(self) -> None:
        super().__del__()
        self._clean_helper()

    def _clean_helper(self) -> None:
        if self._parser_thread is None:
            return
        self._parser_thread.stop()
        self._parser_thread.join()
        with contextlib.suppress(FileNotFoundError):
            self._helper_path.unlink()


class MonitorThread(threading.Thread):
    def __init__(
        self,
        parser_thread: CoverageParserThread,
        main_thread: threading.Thread | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self._main_thread = main_thread or threading.main_thread()
        self.parser_thread = parser_thread

    def run(self) -> None:
        self._main_thread.join()
        self.parser_thread.stop()
        self.parser_thread.join()


def _iterdir(path: Path) -> Iterator[Path]:
    """Recursively iterate over path. Race-condition safe(r) alternative to Path.rglob("*")"""
    for p in path.iterdir():
        yield p
        if p.is_dir():
            yield from _iterdir(p)


class ShellFileTracer(FileTracer):
    def __init__(self, filename: str) -> None:
        super().__init__(filename)  # type: ignore[call-arg]
        self._reporter = ShellFileReporter(filename)

    def source(self) -> str:
        return self._reporter.source()

    def source_token_lines(self) -> Iterable[object]:
        return []  # pragma: no cover

    def find_executable_statements(self, _source: str, _filename: str) -> set[int]:
        return self._reporter.lines()


class ShellPlugin(CoveragePlugin):
    def __init__(self, options: dict[str, Any]):
        self.options = options
        self._helper_path: None | Path = None

    def configure(self, config: TConfigurable) -> None:
        data_file_option = config.get_option("run:data_file")
        coverage_data_path = Path(cast("str", data_file_option)).absolute()
        branch = bool(config.get_option("run:branch"))

        if config.get_option("run:core") == "sysmon" or (
            sys.version_info >= (3, 14) and config.get_option("run:core") is None
        ):
            warn(
                "The sysmon tracer is not supported by coverage-sh, falling back to ctrace. "
                "Please set [run] core = ctrace in your configuration to silence this warning.",
                stacklevel=2,
            )
            config.set_option("run:core", "ctrace")

        if self.options.get("cover_always", False):
            parser_thread = CoverageParserThread(
                coverage_writer=CoverageWriter(coverage_data_path, branch=branch),
                name=f"CoverageParserThread({coverage_data_path!s})",
            )
            parser_thread.start()

            monitor_thread = MonitorThread(
                parser_thread=parser_thread, name="MonitorThread"
            )
            monitor_thread.start()

            self._helper_path = init_helper(parser_thread.fifo_path)
            os.environ["BASH_ENV"] = str(self._helper_path)
            os.environ["ENV"] = str(self._helper_path)
        else:
            PatchedPopen.data_file_path = coverage_data_path
            PatchedPopen.branch = branch
            # https://github.com/python/mypy/issues/1152
            subprocess.Popen = PatchedPopen  # type: ignore[misc]

    def __del__(self) -> None:
        if self._helper_path is not None:
            with contextlib.suppress(FileNotFoundError):
                self._helper_path.unlink()

    @staticmethod
    def _is_relevant(path: Path) -> bool:
        return magic.from_file(path.resolve(), mime=True) in SUPPORTED_MIME_TYPES

    def file_tracer(self, filename: str) -> FileTracer | None:
        path = Path(filename)
        if not path.exists() or not self._is_relevant(path):
            return None
        return ShellFileTracer(filename)

    def file_reporter(
        self,
        filename: str,
    ) -> ShellFileReporter | str:
        return ShellFileReporter(filename)

    def find_executable_files(
        self,
        src_dir: str,
    ) -> Iterable[str]:
        for f in _iterdir(Path(src_dir)):
            # TODO: Use coverage's logic for figuring out if a file should be excluded
            if not (f.is_file() or (f.is_symlink() and f.resolve().is_file())) or any(
                p.startswith(".") for p in f.parts
            ):
                continue

            if self._is_relevant(f):
                yield str(f)
