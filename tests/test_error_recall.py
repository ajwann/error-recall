import os
import subprocess
import sys

from conftest import ROOT, run_hook
from error_recall.core import error_lines, fingerprint, handle, normalize, stem

PY_TB = """Traceback (most recent call last):
  File "/home/a/proj/app.py", line 3, in <module>
    import yaml
ModuleNotFoundError: No module named 'yaml'"""

TS_ERR = """src/index.ts:14:7 - error TS2322: Type 'string' is not assignable to type 'number'.

14   let x: number = "a";
Found 1 error in src/index.ts:14"""


def fail(project, cmd, output, session="s1"):
    return handle({"hook_event_name": "PostToolUseFailure", "cwd": str(project), "session_id": session,
                   "tool_name": "Bash", "tool_input": {"command": cmd}, "error": f"Exit code 1\n{output}",
                   "is_interrupt": False, "duration_ms": 50})


def ok(project, cmd, session="s1"):
    return handle({"hook_event_name": "PostToolUse", "cwd": str(project), "session_id": session, "tool_name": "Bash",
                   "tool_input": {"command": cmd}, "tool_response": {"stdout": "fine"}, "duration_ms": 50})


def edit(project, rel, session="s1"):
    return handle({"hook_event_name": "PostToolUse", "cwd": str(project), "session_id": session, "tool_name": "Edit",
                   "tool_input": {"file_path": str(project / rel)}, "tool_response": {}})


def test_error_line_extraction_prefers_exception_line():
    assert error_lines(PY_TB)[0] == "ModuleNotFoundError: No module named 'yaml'"
    assert "error TS2322" in error_lines(TS_ERR)[0]


def test_normalization_makes_paths_and_numbers_stable():
    a = normalize("/tmp/abc123/proj/src/x.py:12:3 error at 0xdeadbeef")
    b = normalize("/var/folders/zz/proj/src/x.py:99:1 error at 0xfeedface")
    assert a == b and ".../x.py" in a
    assert fingerprint(["E: a 1"]) == fingerprint(["E: a 22"])


def test_stem():
    assert stem("cd app && python3 -m pytest -x tests/test_a.py") == "python -m pytest"
    assert stem("npm run build 2>&1 | tail") == "npm run"
    assert stem("./gradlew test") == "gradlew test"


def test_first_failure_is_silent_then_learns_fix_and_recalls(project):
    assert fail(project, "python app.py", PY_TB) is None
    edit(project, "pyproject.toml")
    ok(project, "uv add pyyaml")
    ok(project, "python app.py")            # same stem -> resolves the failure
    out = fail(project, "python app.py --flag", PY_TB, session="s2")
    text = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "seen 1 time(s)" in text and "editing pyproject.toml" in text and "`uv add pyyaml`" in text
    assert "`python app.py` then succeeded" in text


def test_unresolved_recurrence_says_so(project):
    fail(project, "tsc -p .", TS_ERR)
    text = fail(project, "tsc -p .", TS_ERR)["hookSpecificOutput"]["additionalContext"]
    assert "No resolution was recorded" in text


def test_other_session_success_does_not_resolve(project):
    fail(project, "make", "make: *** [all] Error 2", session="a")
    ok(project, "make", session="b")
    text = fail(project, "make", "make: *** [all] Error 2", session="c")["hookSpecificOutput"]["additionalContext"]
    assert "No resolution" in text


def test_outputs_without_error_lines_are_ignored(project):
    assert fail(project, "grep foo x", "") is None
    assert fail(project, "test -f x", "") is None


def test_cli_lists_and_shows(project, isolated):
    run_hook({"hook_event_name": "PostToolUseFailure", "cwd": str(project), "session_id": "s", "tool_name": "Bash",
              "tool_input": {"command": "cargo build"}, "error": "Exit code 101\nerror[E0425]: cannot find value `x`"},
             isolated)
    env = dict(os.environ, HOME=str(isolated))
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "error-recall"), "--project", str(project)],
                       capture_output=True, text=True, env=env)
    assert "error[E0425]" in r.stdout
    fp = r.stdout.splitlines()[2].split("|")[1].strip()
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "error-recall"), "show", fp, "--project", str(project)],
                       capture_output=True, text=True, env=env)
    assert "seen 1 time(s)" in r.stdout and "cargo build" in r.stdout


def test_cli_sees_hook_data_even_when_hook_had_plugin_data_env(project, isolated, tmp_path):
    """Hooks get CLAUDE_PLUGIN_DATA, Bash-run CLIs don't; both must use the same store."""
    env = dict(os.environ, HOME=str(isolated), CLAUDE_PLUGIN_DATA=str(tmp_path / "plugin-data"))
    ev = {"hook_event_name": "PostToolUseFailure", "cwd": str(project), "session_id": "s", "tool_name": "Bash",
          "tool_input": {"command": "tsc"}, "error": "Exit code 2\nerror TS2304: Cannot find name 'foo'."}
    subprocess.run([sys.executable, str(ROOT / "scripts" / "hook.py")], input=__import__("json").dumps(ev),
                   text=True, env=env, check=True, capture_output=True)
    env.pop("CLAUDE_PLUGIN_DATA")
    r = subprocess.run([sys.executable, str(ROOT / "bin" / "error-recall"), "--project", str(project)],
                       capture_output=True, text=True, env=env)
    assert "TS2304" in r.stdout
