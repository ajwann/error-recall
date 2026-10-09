"""Fingerprint command failures, learn what fixed them, and recall it when they recur."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import hookio

PLUGIN = "error-recall"

DEFAULTS = {
    "resolve_window_s": 7200,   # a success only resolves failures newer than this
    "max_lines": 3,             # error lines per fingerprint
}

EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
STRONG_RE = re.compile(r"^\s*(?:[\w.]+(?:Error|Exception)\b|error(?:\[\w+\])?:|fatal:|FATAL|panic:|E\s{2,}\w)")
ERR_RE = re.compile(
    r"(?i)\b(error|exception|failed|failure|fatal|panic|cannot|can't|could not|unable to|undefined|"
    r"not found|no such|denied|refused|invalid|missing|segmentation fault|unresolved|mismatch)\b")
NOISE_RE = re.compile(r"(?i)^\s*(traceback \(most recent call last\)|file \".*\", line \d+|at .+\(.+:\d+:\d+\)|"
                      r"npm err! (a complete log|\s*$)|exit code \d+$|-+$|=+$)")


def normalize(line: str) -> str:
    s = ANSI_RE.sub("", line).strip()
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s, flags=re.I)
    s = re.sub(r"(?:[A-Za-z]:)?(?:[~\w.\-@+]*/)+([\w.\-@+]+)", r".../\1", s)   # keep basenames only
    s = re.sub(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{7,40}\b", "<hex>", s)
    s = re.sub(r"\d+", "N", s)
    s = re.sub(r"\s+", " ", s)
    return s[:240]


def error_lines(output: str, limit: int = 3) -> list[str]:
    lines = [ANSI_RE.sub("", l).rstrip() for l in output.splitlines()]
    lines = [l for l in lines if l.strip() and not NOISE_RE.match(l)]
    strong = [l for l in lines if STRONG_RE.match(l)]
    weak = [l for l in lines if ERR_RE.search(l)]
    picked, seen = [], set()
    for l in strong + weak:
        n = normalize(l)
        if n not in seen:
            seen.add(n)
            picked.append(l.strip())
        if len(picked) >= limit:
            break
    return picked


def fingerprint(lines: list[str]) -> str:
    return hashlib.sha1("\n".join(normalize(l) for l in lines).encode()).hexdigest()[:12]


def stem(command: str) -> str:
    segs = [s for s in hookio.segments(command) if Path(s[0]).name not in ("cd", "echo", "tee", "tail", "head", "grep")]
    if not segs:
        return command.strip()[:60]
    w = segs[0]
    prog = Path(w[0]).name
    if prog.startswith("python") and len(w) > 2 and w[1] == "-m":
        return f"python -m {w[2]}"
    sub = next((x for x in w[1:] if not x.startswith("-")), "")
    return f"{prog} {sub}".strip() if re.fullmatch(r"[a-z][a-z0-9:_-]*", sub or "-") else prog


# ---------------------------------------------------------------- storage

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(hookio.data_dir(PLUGIN) / "recall.sqlite", timeout=5)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS failures (id INTEGER PRIMARY KEY, project TEXT, session TEXT, fp TEXT,
        stem TEXT, command TEXT, sample TEXT, ts REAL, resolved_by INTEGER);
    CREATE TABLE IF NOT EXISTS resolutions (id INTEGER PRIMARY KEY, project TEXT, fp TEXT, files TEXT,
        commands TEXT, fixed_by TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS activity (project TEXT, session TEXT, kind TEXT, detail TEXT, ts REAL);
    CREATE INDEX IF NOT EXISTS f_fp ON failures(project, fp);
    CREATE INDEX IF NOT EXISTS a_sess ON activity(project, session, ts);
    """)
    return con


def recall(con: sqlite3.Connection, project: str, fp: str) -> Optional[str]:
    n, first, last = con.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM failures WHERE project=? AND fp=?",
                                 (project, fp)).fetchone()
    if not n:
        return None
    res = con.execute("SELECT files, commands, fixed_by, ts FROM resolutions WHERE project=? AND fp=? "
                      "ORDER BY ts DESC LIMIT 1", (project, fp)).fetchone()
    when = time.strftime("%Y-%m-%d", time.localtime(last))
    msg = f"error-recall: this error (fingerprint {fp}) has been seen {n} time(s) before in this repo, last on {when}."
    if res:
        files, commands, fixed_by, rts = res
        parts = []
        if files:
            parts.append("editing " + ", ".join(files.split("\n")[:8]))
        if commands:
            parts.append("running " + ", ".join(f"`{c}`" for c in commands.split("\n")[:5]))
        how = " and ".join(parts) if parts else "no recorded edits (possibly an environment change)"
        msg += (f" On {time.strftime('%Y-%m-%d', time.localtime(rts))} it was resolved by {how}; "
                f"`{fixed_by}` then succeeded.")
    else:
        msg += " No resolution was recorded for it."
    return msg


def on_failure(event: dict, project: str, session: str, cfg: dict) -> Optional[dict]:
    run = hookio.BashRun(event)
    if run.interrupted or not run.command:
        return None
    lines = error_lines(run.output, int(cfg["max_lines"]))
    if not lines:
        return None
    fp = fingerprint(lines)
    with connect() as con:
        text = recall(con, project, fp)
        con.execute("INSERT INTO failures (project, session, fp, stem, command, sample, ts) VALUES (?,?,?,?,?,?,?)",
                    (project, session, fp, stem(run.command), run.command[:500], "\n".join(lines)[:1000], time.time()))
        con.execute("INSERT INTO activity VALUES (?,?,?,?,?)", (project, session, "fail", run.command[:300], time.time()))
    return hookio.context("PostToolUseFailure", text) if text else None


def on_success(event: dict, project: str, session: str, cfg: dict) -> None:
    run = hookio.BashRun(event)
    if run.background or not run.command:
        return
    now = time.time()
    st = stem(run.command)
    with connect() as con:
        open_rows = con.execute(
            "SELECT id, fp, ts FROM failures WHERE project=? AND session=? AND stem=? AND resolved_by IS NULL "
            "AND ts>=?", (project, session, st, now - cfg["resolve_window_s"])).fetchall()
        for fid, fp, fts in open_rows:
            files = [r[0] for r in con.execute(
                "SELECT DISTINCT detail FROM activity WHERE project=? AND session=? AND kind='edit' AND ts>=?",
                (project, session, fts))]
            cmds = [r[0] for r in con.execute(
                "SELECT detail FROM activity WHERE project=? AND session=? AND kind='ok' AND ts>=? "
                "ORDER BY ts", (project, session, fts))]
            cmds = [c for c in dict.fromkeys(cmds) if stem(c) != st][:5]
            cur = con.execute("INSERT INTO resolutions (project, fp, files, commands, fixed_by, ts) VALUES (?,?,?,?,?,?)",
                              (project, fp, "\n".join(files), "\n".join(cmds), run.command[:300], now))
            con.execute("UPDATE failures SET resolved_by=? WHERE id=? OR (project=? AND session=? AND fp=? "
                        "AND resolved_by IS NULL)", (cur.lastrowid, fid, project, session, fp))
        con.execute("INSERT INTO activity VALUES (?,?,?,?,?)", (project, session, "ok", run.command[:300], now))
        con.execute("DELETE FROM activity WHERE ts < ?", (now - 7 * 86400,))


def on_edit(event: dict, project: str, session: str) -> None:
    ti = event.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not path:
        return
    try:
        rel = str(Path(path).resolve().relative_to(Path(project).resolve()))
    except ValueError:
        rel = path
    with connect() as con:
        con.execute("INSERT INTO activity VALUES (?,?,?,?,?)", (project, session, "edit", rel, time.time()))


def handle(event: dict) -> Optional[dict]:
    name = event.get("hook_event_name")
    project = str(hookio.project_dir(event))
    session = event.get("session_id") or ""
    cfg = hookio.load_config(PLUGIN, Path(project), DEFAULTS)
    tool = event.get("tool_name")
    if name == "PostToolUseFailure" and tool == "Bash":
        return on_failure(event, project, session, cfg)
    if name == "PostToolUse" and tool == "Bash":
        on_success(event, project, session, cfg)
    elif name == "PostToolUse" and tool in EDIT_TOOLS:
        on_edit(event, project, session)
    return None
