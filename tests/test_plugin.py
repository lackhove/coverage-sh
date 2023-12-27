import subprocess
from pathlib import Path

import pytest

from coverage_sh import ShellPlugin
from coverage_sh.plugin import PatchedPopen


@pytest.fixture()
def examples_dir(resources_dir):
    return resources_dir / "examples"


def test_ShellPlugin_file_tracer():
    assert False


def test_ShellPlugin_file_reporter():
    assert False


def test_ShellPlugin_find_executable_files(examples_dir):
    plugin = ShellPlugin({})

    executable_files = plugin.find_executable_files(str(examples_dir))

    assert [Path(f) for f in sorted(executable_files)] == [
        examples_dir / "atom.sh",
        examples_dir / "shell-file.weird.suffix",
    ]


def test_patched_popen(resources_dir, monkeypatch, tmp_path):

    monkeypatch.chdir(tmp_path)

    test_sh_path = resources_dir / "testproject" / "test.sh"
    proc = PatchedPopen(["/bin/bash", test_sh_path], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, encoding="utf8")

    proc.wait()

    assert proc.stderr.read() == ""
    assert proc.stdout.read() == "hello from shell\n"


    line_data = proc._parse_tracefile()
    assert  {str(test_sh_path): {3}} == dict(line_data)



