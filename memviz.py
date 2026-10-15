#!/usr/bin/env python3
"""memviz: browse AI harness memories (Claude Code, Codex) on this machine.

Single file, Python 3.9+ stdlib only. Strictly read-only.

  memviz.py serve [--port N] [--no-open]   # start a localhost UI
  memviz.py dump                           # print collected data as JSON

Use --home / --claude-dir / --codex-dir to inspect a copied home directory
from another machine.
"""

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Helpers


def item_id(*parts):
    return hashlib.sha1("\0".join(str(p) for p in parts).encode()).hexdigest()[:12]


def iso(ts):
    if ts is None:
        return None
    if ts > 1e12:  # milliseconds
        ts /= 1000
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def read_text(path, limit=2_000_000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError as e:
        return f"<<unreadable: {e}>>"


def file_stat(path):
    try:
        st = os.stat(path)
        return {"mtime": iso(st.st_mtime), "size": st.st_size}
    except OSError:
        return {"mtime": None, "size": None}


def parse_frontmatter(text):
    """Parse a small YAML subset: scalars and one level of nested mappings."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return {}, text
    fm, parent = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        key, sep, val = line.strip().partition(":")
        if not sep:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace('\\"', '"')
        if indented and isinstance(parent, dict):
            parent[key] = val
        elif val == "":
            parent = fm[key] = {}
        else:
            fm[key], parent = val, None
    return fm, text[m.end():]


def jsonl_iter(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def first_line_json(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.loads(f.readline())
    except (OSError, ValueError):
        return None


def ancestors(cwd, home):
    """cwd and its parents, stopping before $HOME or filesystem root."""
    p = Path(cwd)
    out = []
    while True:
        if p == home or p.parent == p:
            break
        out.append(p)
        p = p.parent
    if Path(cwd) == home:
        out.append(home)
    return out


def resolve_dashed(name):
    """Map e.g. '-Users-me-perf-cross-validation' to an existing directory."""
    toks = name.lstrip("-").split("-")

    def walk(base, i):
        if i == len(toks):
            return base
        # Grow the next path segment greedily, trying separators that encode to '-'
        segs = [toks[i]]
        for j in range(i + 1, len(toks) + 1):
            for seg in segs:
                p = os.path.join(base, seg)
                if os.path.isdir(p):
                    r = walk(p, j)
                    if r:
                        return r
            if j < len(toks):
                segs = [s + sep + toks[j] for s in segs for sep in "-._ "][:64]
        return None

    return walk("/", 0)


def is_noise_prompt(text):
    t = text.lstrip()
    return not t or t.startswith("<") or t.startswith("Caveat:")


# ---------------------------------------------------------------------------
# Glossary: what harness-specific fields and concepts mean. Shown inline in
# the UI next to metadata, and on the Glossary page. `fmt` controls display.
#
# Codex definitions are from codex-rs/memories/README.md and
# codex-rs/state/src/runtime/memories.rs in openai/codex.

GLOSSARY = {
    "concepts": [
        {"term": "Memory", "def": "A fact the harness saved for itself so later sessions can use it. "
         "Claude Code writes these as markdown files during a session; Codex extracts them "
         "afterwards from finished sessions."},
        {"term": "Index", "def": "A summary file loaded into context at session start that points at "
         "the individual memories (Claude's per-project MEMORY.md; Codex's consolidated "
         "MEMORY.md / memory_summary.md). A memory missing from the index is usually not seen."},
        {"term": "Instructions", "def": "Hand- or agent-written guidance files the harness always "
         "loads: CLAUDE.md / CLAUDE.local.md for Claude Code, AGENTS.md for Codex. Project files "
         "are found by walking up from the session's working directory."},
        {"term": "Rules", "def": "Claude: ~/.claude/rules/*.md, extra always-loaded instruction files. "
         "Codex: rules/*.rules, the exec policy (Starlark) deciding which shell commands are "
         "allowed, prompted, or forbidden. Not prose memory, but persistent learned state."},
        {"term": "Memory DB", "def": "Codex's SQLite state for its memory pipeline (memories_N.sqlite). "
         "Holds one stage-1 memory per session plus job bookkeeping."},
        {"term": "Rollout / thread", "def": "Codex's words for a session transcript "
         "(~/.codex/sessions/.../rollout-*.jsonl) and its id."},
        {"term": "Codex phase 1 (stage 1)", "def": "Per-session extraction. When a new Codex session "
         "starts, a background job picks recent, idle, interactive past sessions and asks a model "
         "to write a raw_memory and rollout_summary for each. Results are rows in stage1_outputs."},
        {"term": "Codex phase 2 (consolidation)", "def": "Global merge. A single locked job picks the "
         "top stage-1 memories (ranked by usage_count, then recency; dropping ones unused for "
         "max_unused_days), writes them to ~/.codex/memories/ (raw_memories.md, "
         "rollout_summaries/), then runs a sandboxed sub-agent that updates MEMORY.md, "
         "memory_summary.md and skills/. Only phase-2 output is what future sessions read."},
        {"term": "Usage / citation", "def": "Codex counts a memory as used when an agent cites it in "
         "a response; each citation bumps usage_count and sets last_usage. Heavily cited memories "
         "survive consolidation; uncited ones age out."},
    ],
    "fields": {
        "claude": {
            "name": {"def": "Short slug identifying the memory; other memories link to it as [[name]]."},
            "description": {"def": "One-line summary Claude uses to decide whether the memory is relevant."},
            "type": {"def": "user = who you are / preferences; feedback = guidance on how to work "
                     "(with Why / How to apply); project = ongoing work or constraints not in the "
                     "code; reference = pointers to external resources."},
            "node_type": {"def": "Internal record kind used by Claude Code's memory store; ordinary "
                          "memory files are 'memory'."},
            "originSessionId": {"def": "Id of the Claude Code session that created the memory; the "
                                "transcript is ~/.claude/projects/<project>/<id>.jsonl."},
            "modified": {"def": "When Claude Code last wrote the file.", "fmt": "time"},
        },
        "codex": {
            "thread_id": {"def": "Id of the Codex session (rollout) this memory was extracted from."},
            "rollout_slug": {"def": "Short model-chosen name for the session, used as the summary filename."},
            "source_updated_at": {"def": "Last-modified time of the session at extraction. If the "
                                  "session grows later, phase 1 re-extracts it.", "fmt": "time"},
            "generated_at": {"def": "When phase 1 produced this memory.", "fmt": "time"},
            "usage_count": {"def": "How many times an agent has cited this memory. Higher counts rank "
                            "first when phase 2 picks what to keep. Empty = never cited."},
            "last_usage": {"def": "When the memory was last cited. Memories not used within "
                           "max_unused_days (falling back to generated_at) are dropped from "
                           "consolidation.", "fmt": "time"},
            "selected_for_phase2": {"def": "Whether the last successful consolidation included this "
                                    "memory, i.e. it is in ~/.codex/memories/ and visible to future "
                                    "sessions. No = extracted but not (or no longer) in use.",
                                    "fmt": "bool"},
            "selected_for_phase2_source_updated_at": {
                "def": "The source_updated_at of the version phase 2 consumed. If it is older than "
                       "source_updated_at, the memory was re-extracted since and the consolidated "
                       "copy is stale until the next phase 2.", "fmt": "time"},
        },
    },
}


# ---------------------------------------------------------------------------
# Collector


class Collector:
    def __init__(self, home, claude_dir, codex_dir):
        self.home = Path(home)
        self.claude_dir = Path(claude_dir)
        self.codex_dir = Path(codex_dir)
        self.items = []
        self.projects = []
        self.warnings = []
        self.by_id = {}

    def add(self, **kw):
        kw.setdefault("notes", [])
        kw.setdefault("frontmatter", {})
        kw.setdefault("project", None)
        kw.setdefault("loaded_by", [])
        kw.setdefault("origin", None)
        if kw.get("path") and "mtime" not in kw:
            kw.update(file_stat(kw["path"]))
        self.items.append(kw)
        self.by_id[kw["id"]] = kw
        return kw

    def collect(self):
        self.collect_claude()
        self.collect_codex()
        return {
            "host": socket.gethostname(),
            "generated": datetime.now(timezone.utc).isoformat(),
            "roots": {"home": str(self.home), "claude": str(self.claude_dir),
                      "codex": str(self.codex_dir)},
            "projects": self.projects,
            "items": self.items,
            "warnings": self.warnings,
            "glossary": GLOSSARY,
        }

    # -- shared -------------------------------------------------------------

    def instruction_files(self, harness, cwds, names, kind="instructions"):
        """Find per-project instruction files by walking up from each cwd."""
        found = {}
        for cwd in cwds:
            for d in ancestors(cwd, self.home):
                for name in names:
                    cands = [d / name] if "*" not in name else sorted(d.glob(name))
                    for p in cands:
                        if p.is_file():
                            found.setdefault(str(p), set()).add(cwd)
        for path, users in sorted(found.items()):
            text = read_text(path)
            fm, body = parse_frontmatter(text)
            proj = str(Path(path).parent)
            if Path(proj).name == ".claude":
                proj = str(Path(proj).parent)
            if "/.claude/rules" in path:
                proj = path.split("/.claude/rules")[0]
            self.add(id=item_id(harness, "instr", path), harness=harness, kind=kind,
                     scope="project", project=proj, path=path,
                     title=os.path.relpath(path, proj), description=fm.get("description"),
                     body=body, frontmatter=fm, loaded_by=sorted(users))

    # -- Claude Code --------------------------------------------------------

    def claude_project_cwd(self, pdir):
        for f in sorted(pdir.glob("*.jsonl"), key=lambda p: -p.stat().st_mtime):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh):
                        m = re.search(r'"cwd":"((?:[^"\\]|\\.)*)"', line)
                        if m:
                            return json.loads(f'"{m.group(1)}"'), True
                        if i > 200:
                            break
            except OSError:
                continue
        # No transcripts left: decode the dir name, where non-alphanumerics became '-'
        resolved = resolve_dashed(pdir.name)
        return (resolved, True) if resolved else (pdir.name.replace("-", "/"), False)

    def collect_claude(self):
        cd = self.claude_dir
        if not cd.is_dir():
            self.warnings.append(f"Claude dir not found: {cd}")
            return
        for name in ["CLAUDE.md"]:
            p = cd / name
            if p.is_file():
                fm, body = parse_frontmatter(read_text(p))
                self.add(id=item_id("claude", "global", p), harness="claude",
                         kind="instructions", scope="global", path=str(p),
                         title=f"~/{p.relative_to(self.home)}" if self.home in p.parents else str(p),
                         description="User-level instructions loaded in every session",
                         body=body, frontmatter=fm)
        for p in sorted((cd / "rules").rglob("*.md")) if (cd / "rules").is_dir() else []:
            fm, body = parse_frontmatter(read_text(p))
            self.add(id=item_id("claude", "global", p), harness="claude", kind="rules",
                     scope="global", path=str(p), title=str(p.relative_to(cd)),
                     description=fm.get("description"), body=body, frontmatter=fm)

        cwds = []
        pdirs = sorted(p for p in (cd / "projects").iterdir() if p.is_dir()) \
            if (cd / "projects").is_dir() else []
        for pdir in pdirs:
            cwd, exact = self.claude_project_cwd(pdir)
            sessions = list(pdir.glob("*.jsonl"))
            last = max((s.stat().st_mtime for s in sessions), default=None)
            mdir = pdir / "memory"
            mfiles = sorted(mdir.glob("*.md")) if mdir.is_dir() else []
            self.projects.append({
                "harness": "claude", "cwd": cwd, "cwd_exact": exact,
                "exists": os.path.isdir(cwd), "store": str(pdir),
                "sessions": len(sessions), "last_active": iso(last),
                "memory_count": len([f for f in mfiles if f.name != "MEMORY.md"]),
            })
            cwds.append(cwd)
            if mfiles:
                self.claude_memory_dir(mdir, mfiles, cwd, pdir)
        self.instruction_files("claude", cwds, [
            "CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", ".claude/rules/*.md"])

    def claude_memory_dir(self, mdir, mfiles, cwd, pdir):
        index = mdir / "MEMORY.md"
        index_text = read_text(index) if index.is_file() else ""
        linked = set(re.findall(r"\]\(([^)\s]+\.md)\)", index_text))
        names = {}
        for f in mfiles:
            if f.name == "MEMORY.md":
                continue
            fm, body = parse_frontmatter(read_text(f))
            meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
            notes = []
            if index.is_file() and f.name not in linked:
                notes.append("Not listed in MEMORY.md, so it won't be surfaced in the index")
            sid = meta.get("originSessionId") or fm.get("originSessionId")
            it = self.add(
                id=item_id("claude", "mem", f), harness="claude", kind="memory",
                scope="project", project=cwd, path=str(f), store=str(pdir),
                title=fm.get("name") or f.stem, description=fm.get("description"),
                mtype=meta.get("type") or fm.get("type"), body=body, frontmatter=fm,
                origin={"session_id": sid} if sid else None, notes=notes)
            names[it["title"]] = it["id"]
        if index.is_file():
            missing = [l for l in sorted(linked) if not (mdir / l).is_file()]
            self.add(id=item_id("claude", "idx", index), harness="claude",
                     kind="memory-index", scope="project", project=cwd, path=str(index),
                     store=str(pdir), title="MEMORY.md",
                     description="Index loaded into context each session",
                     body=index_text,
                     notes=[f"Index links to missing file: {m}" for m in missing])

    def claude_origin(self, it):
        """Find the session(s) that wrote this memory and what prompted it."""
        path = it["path"]
        sid = (it.get("origin") or {}).get("session_id")
        store = Path(it.get("store") or "")
        cands = []
        if sid:
            cands = [store / f"{sid}.jsonl"] + list(
                (self.claude_dir / "projects").glob(f"*/{sid}.jsonl"))
        cands = [c for c in dict.fromkeys(cands) if c.is_file()]
        if not cands:  # no recorded origin: search this project's sessions
            needle = path.encode()
            for s in sorted(store.glob("*.jsonl")):
                try:
                    if needle in s.read_bytes():
                        cands.append(s)
                except OSError:
                    pass
        return [self.claude_session_summary(c, path) for c in cands[:5]]

    def claude_session_summary(self, spath, mem_path):
        info = {"session_id": spath.stem, "path": str(spath), "events": []}
        prompts, last_prompt = [], None
        for o in jsonl_iter(spath):
            if o.get("cwd") and "cwd" not in info:
                info["cwd"] = o["cwd"]
                info["git_branch"] = o.get("gitBranch")
            ts = o.get("timestamp")
            if ts and "started" not in info:
                info["started"] = ts
            if ts:
                info["ended"] = ts
            t = o.get("type")
            if t not in ("user", "assistant"):
                continue
            content = (o.get("message") or {}).get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if t == "user" and b.get("type") == "text" and not is_noise_prompt(b.get("text", "")):
                    last_prompt = b["text"]
                    prompts.append(b["text"])
                elif t == "assistant" and b.get("type") == "tool_use":
                    inp = json.dumps(b.get("input"))
                    if mem_path in inp or os.path.basename(mem_path) in inp and "/memory" in inp:
                        info["events"].append({
                            "timestamp": ts, "tool": b.get("name"),
                            "action": (b.get("input") or {}).get("file_path") and b.get("name") or
                                      ((b.get("input") or {}).get("command") or "")[:300],
                            "prompt": last_prompt,
                        })
        info["first_prompt"] = prompts[0] if prompts else None
        info["prompt_count"] = len(prompts)
        return info

    # -- Codex --------------------------------------------------------------

    def codex_rollouts(self):
        if not hasattr(self, "_rollouts"):
            self._rollouts = []
            for sub in ("sessions", "archived_sessions"):
                d = self.codex_dir / sub
                if d.is_dir():
                    self._rollouts += sorted(d.rglob("rollout-*.jsonl"))
        return self._rollouts

    def collect_codex(self):
        cx = self.codex_dir
        if not cx.is_dir():
            self.warnings.append(f"Codex dir not found: {cx}")
            return
        for name in ("AGENTS.md", "AGENTS.override.md"):
            p = cx / name
            if p.is_file():
                self.add(id=item_id("codex", "global", p), harness="codex",
                         kind="instructions", scope="global", path=str(p), title=name,
                         description="User-level instructions loaded in every session",
                         body=read_text(p))
        if (cx / "rules").is_dir():
            for p in sorted((cx / "rules").rglob("*.rules")):
                self.add(id=item_id("codex", "global", p), harness="codex", kind="rules",
                         scope="global", path=str(p), title=f"rules/{p.name}",
                         description="Exec-policy rules (command allow/deny)",
                         body=read_text(p), lang="python")
        # Newer Codex versions write consolidated memories as markdown files.
        mdir = cx / "memories"
        if mdir.is_dir():
            for p in sorted(mdir.rglob("*")):
                if p.is_file() and p.suffix in (".md", ".txt", ".json", ".jsonl"):
                    rel = str(p.relative_to(mdir))
                    idx = rel in ("MEMORY.md", "memory_summary.md")
                    fm, body = parse_frontmatter(read_text(p))
                    self.add(id=item_id("codex", "memfile", p), harness="codex",
                             kind="memory-index" if idx else "memory", scope="global",
                             path=str(p), title=f"memories/{rel}",
                             description=fm.get("description"), body=body, frontmatter=fm)
        for db in sorted(cx.glob("memories_*.sqlite")):
            self.codex_memory_db(db)

        # Projects from rollout session_meta headers
        projs = {}
        for r in self.codex_rollouts():
            meta = first_line_json(r)
            if not meta or meta.get("type") != "session_meta":
                continue
            cwd = (meta.get("payload") or {}).get("cwd")
            if not cwd:
                continue
            p = projs.setdefault(cwd, {"harness": "codex", "cwd": cwd, "cwd_exact": True,
                                       "exists": os.path.isdir(cwd), "store": None,
                                       "sessions": 0, "last_active": None, "memory_count": 0})
            p["sessions"] += 1
            ts = (meta.get("payload") or {}).get("timestamp") or meta.get("timestamp")
            if ts and (p["last_active"] or "") < ts:
                p["last_active"] = ts
        self.projects += sorted(projs.values(), key=lambda p: p["cwd"])
        self.instruction_files("codex", list(projs), ["AGENTS.md", "AGENTS.override.md"])

    def open_ro(self, db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.execute("select 1 from sqlite_master").fetchone()
            return con
        except sqlite3.Error:
            return sqlite3.connect(f"file:{db}?immutable=1", uri=True)

    def codex_memory_db(self, db):
        try:
            con = self.open_ro(db)
            con.row_factory = sqlite3.Row
            tables = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' order by name")]
            counts = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
            rows = con.execute("select * from stage1_outputs order by source_updated_at desc").fetchall() \
                if "stage1_outputs" in tables else []
            jobs = [dict(r) for r in con.execute("select * from jobs")] if "jobs" in tables else []
        except sqlite3.Error as e:
            self.warnings.append(f"Could not read {db}: {e}")
            return
        summary = "\n".join(f"- `{t}`: {n} rows" for t, n in counts.items())
        if jobs:
            summary += "\n\n## Jobs\n\n" + "\n".join(
                f"- {j['kind']} `{j['job_key']}`: {j['status']}"
                + (f" (error: {j['last_error']})" if j.get("last_error") else "") for j in jobs)
        self.add(id=item_id("codex", "db", db), harness="codex", kind="memory-db",
                 scope="global", path=str(db), title=db.name,
                 description=f"Codex memory pipeline database, {len(rows)} stage-1 memories",
                 body=summary,
                 notes=[] if rows else ["No memories recorded yet. Codex memories are off by default; "
                                        "enable with `[features] memories = true` in config.toml"])
        for r in rows:
            r = dict(r)
            raw = r.get("raw_memory") or ""
            title = r.get("rollout_slug") or next((l.strip("# ") for l in raw.splitlines() if l.strip()), r["thread_id"])
            body = raw + "\n\n---\n\n## Rollout summary\n\n" + (r.get("rollout_summary") or "")
            self.add(id=item_id("codex", "s1", r["thread_id"]), harness="codex", kind="memory",
                     scope="global", path=str(db), title=title[:120],
                     description=(r.get("rollout_summary") or "").strip().split("\n")[0][:200],
                     mtime=iso(r.get("generated_at")), size=len(raw), body=body,
                     frontmatter={k: v for k, v in r.items() if k not in ("raw_memory", "rollout_summary")},
                     origin={"session_id": r["thread_id"]},
                     notes=["Re-extracted since the last consolidation; the copy future sessions see is stale"]
                     if r.get("selected_for_phase2") and (r.get("selected_for_phase2_source_updated_at") or 0)
                     < (r.get("source_updated_at") or 0) else [])

    def codex_origin(self, it):
        sid = (it.get("origin") or {}).get("session_id")
        if not sid:
            return []
        out = []
        for r in self.codex_rollouts():
            if sid not in r.name:
                continue
            info = {"session_id": sid, "path": str(r), "events": []}
            prompts = []
            for o in jsonl_iter(r):
                p = o.get("payload") or {}
                ts = o.get("timestamp")
                info.setdefault("started", ts)
                info["ended"] = ts
                if o.get("type") == "session_meta":
                    info["cwd"] = p.get("cwd")
                    info["git_branch"] = (p.get("git") or {}).get("branch")
                elif o.get("type") == "event_msg" and p.get("type") == "user_message":
                    if not is_noise_prompt(p.get("message", "")):
                        prompts.append(p["message"])
            info["first_prompt"] = prompts[0] if prompts else None
            info["prompt_count"] = len(prompts)
            out.append(info)
        return out

    def origin(self, iid):
        it = self.by_id.get(iid)
        if not it:
            return None
        if it["harness"] == "claude" and it["kind"] == "memory":
            return self.claude_origin(it)
        if it["harness"] == "codex" and it.get("origin"):
            return self.codex_origin(it)
        return []


# ---------------------------------------------------------------------------
# Server


class App:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.collector = None

    def refresh(self):
        c = Collector(self.args.home, self.args.claude_dir, self.args.codex_dir)
        t = time.time()
        data = c.collect()
        data["collect_ms"] = int((time.time() - t) * 1000)
        with self.lock:
            self.collector = c
        return data


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/":
                    self.send(200, PAGE, "text/html; charset=utf-8")
                elif path == "/api/data":
                    self.send(200, json.dumps(app.refresh()), "application/json")
                elif path.startswith("/api/origin/"):
                    if app.collector is None:
                        app.refresh()
                    res = app.collector.origin(path.rsplit("/", 1)[1])
                    if res is None:
                        self.send(404, '{"error":"unknown item"}', "application/json")
                    else:
                        self.send(200, json.dumps(res), "application/json")
                else:
                    self.send(404, "not found", "text/plain")
            except Exception as e:  # keep the server alive, surface the error
                self.send(500, json.dumps({"error": repr(e)}), "application/json")

    return H


def main():
    home = Path.home()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=str(home), help="home directory to inspect (default: %(default)s)")
    ap.add_argument("--claude-dir", help="Claude config dir (default: $CLAUDE_CONFIG_DIR or HOME/.claude)")
    ap.add_argument("--codex-dir", help="Codex home (default: $CODEX_HOME or HOME/.codex)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="start the browser UI (default)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    sub.add_parser("dump", help="print collected data as JSON")
    args = ap.parse_args()
    default_home = args.home == str(home)
    args.claude_dir = args.claude_dir or (default_home and os.environ.get("CLAUDE_CONFIG_DIR")) \
        or str(Path(args.home) / ".claude")
    args.codex_dir = args.codex_dir or (default_home and os.environ.get("CODEX_HOME")) \
        or str(Path(args.home) / ".codex")

    app = App(args)
    if args.cmd == "dump":
        json.dump(app.refresh(), sys.stdout, indent=2)
        print()
        return
    port = getattr(args, "port", 8765)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"memviz serving {url}  (Ctrl-C to stop)")
    if not getattr(args, "no_open", False):
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# UI

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>memviz</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fafaf9;--panel:#fff;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--accent:#2563eb;
--claude:#c2410c;--codex:#047857;--warn:#b45309;--sel:#eff6ff;--code:#f5f5f4}
@media (prefers-color-scheme:dark){:root{--bg:#0c0a09;--panel:#1c1917;--fg:#e7e5e4;--muted:#a8a29e;
--line:#292524;--accent:#60a5fa;--claude:#fb923c;--codex:#34d399;--warn:#fbbf24;--sel:#172554;--code:#292524}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg);
height:100vh;display:grid;grid-template-rows:auto 1fr}
header{display:flex;gap:12px;align-items:center;padding:8px 14px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0}header .meta{color:var(--muted);font-size:12px}
header input{flex:1;max-width:420px;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}
button,.chip{font:inherit;font-size:12px;padding:3px 9px;border:1px solid var(--line);border-radius:999px;background:var(--panel);color:var(--fg);cursor:pointer}
.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
main{display:grid;grid-template-columns:360px 1fr;min-height:0}
nav{border-right:1px solid var(--line);overflow:auto;background:var(--panel);padding:8px 0}
.filters{padding:4px 12px 10px;display:flex;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--line)}
.grp{padding:8px 12px 2px;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.proj{padding:6px 12px 2px;font-size:12px;color:var(--muted);word-break:break-all;cursor:pointer}
.proj:hover{color:var(--fg)}
.it{padding:3px 12px 3px 22px;cursor:pointer;display:flex;gap:6px;align-items:baseline}
.it:hover{background:var(--bg)}.it.sel{background:var(--sel)}
.it .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.badge{font-size:10px;padding:0 6px;border-radius:4px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.b-claude{color:var(--claude);border-color:var(--claude)}.b-codex{color:var(--codex);border-color:var(--codex)}
.warn{color:var(--warn)}
section{overflow:auto;padding:18px 26px}
h2{margin:0 0 4px;font-size:20px}h3{margin:22px 0 6px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
table.kv{border-collapse:collapse;font-size:13px}table.kv td{padding:2px 14px 2px 0;vertical-align:top}
table.kv td:first-child{color:var(--muted);white-space:nowrap}
table.grid{border-collapse:collapse;width:100%;font-size:13px}
table.grid th,table.grid td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line)}
table.grid tr.click{cursor:pointer}table.grid tr.click:hover{background:var(--panel)}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:130px}
.card .n{font-size:24px;font-weight:600}.card .l{color:var(--muted);font-size:12px}
.md{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:4px 18px;max-width:900px}
.md pre,pre.raw{background:var(--code);padding:10px;border-radius:6px;overflow:auto;font-size:12px}
pre.raw{white-space:pre-wrap;max-width:900px;border:1px solid var(--line)}
code{background:var(--code);padding:0 3px;border-radius:3px;font-size:12.5px}
a{color:var(--accent);cursor:pointer}
.path{font-family:ui-monospace,monospace;font-size:12px;word-break:break-all}
.note{background:color-mix(in srgb,var(--warn) 12%,transparent);border-left:3px solid var(--warn);padding:4px 10px;margin:4px 0;max-width:900px}
.ev{border-left:3px solid var(--line);padding:4px 12px;margin:8px 0;max-width:900px}
.ev .p{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin-top:4px;max-height:220px;overflow:auto}
.muted{color:var(--muted)}
.def{color:var(--muted);font-size:12px;max-width:640px;margin-bottom:4px}
a.small{font-size:11px;text-transform:none;letter-spacing:0;font-weight:normal}
</style></head><body>
<header><h1>memviz</h1><span class="meta" id="meta"></span>
<input id="q" placeholder="Search titles, descriptions, bodies, paths…  (/)">
<button id="reload">Reload</button></header>
<main><nav id="nav"></nav><section id="view"></section></main>
<script>
let D=null, F={harness:new Set(), kind:new Set()}, Q="";
const KINDS={memory:"Memory","memory-index":"Index",instructions:"Instructions",rules:"Rules","memory-db":"Memory DB"};
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const when=s=>s?new Date(s).toLocaleString():"–";
const ago=s=>{if(!s)return"";const d=(Date.now()-new Date(s))/864e5;return d<1?"today":d<2?"yesterday":Math.floor(d)+"d ago"};
const home=p=>p&&D&&p.startsWith(D.roots.home)?"~"+p.slice(D.roots.home.length):p;

function md(src, it){
  // Minimal, escape-first markdown renderer.
  const out=[]; let list=null, para=[];
  const inline=t=>esc(t)
    .replace(/`([^`]+)`/g,"<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g,"$1<i>$2</i>")
    .replace(/\[\[([^\]]+)\]\]/g,(m,n)=>{const t=findByName(n,it);return t?`<a href="#/item/${t.id}">[[${n}]]</a>`:`<span class="muted" title="no such memory">[[${n}]]</span>`})
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,(m,txt,href)=>{
      if(/^https?:/.test(href))return `<a href="${href}" target="_blank" rel="noopener">${txt}</a>`;
      const t=it&&findByFile(href,it);return t?`<a href="#/item/${t.id}">${txt}</a>`:`${txt} <span class="muted">(${href})</span>`});
  const flushP=()=>{if(para.length){out.push("<p>"+para.map(inline).join("<br>")+"</p>");para=[]}};
  const flushL=()=>{if(list){out.push(`<${list.t}>`+list.items.map(i=>"<li>"+inline(i)+"</li>").join("")+`</${list.t}>`);list=null}};
  const lines=src.split("\n");
  for(let i=0;i<lines.length;i++){
    const l=lines[i];
    if(/^```/.test(l)){flushP();flushL();const buf=[];i++;while(i<lines.length&&!/^```/.test(lines[i]))buf.push(lines[i++]);out.push("<pre>"+esc(buf.join("\n"))+"</pre>");continue}
    let m;
    if(m=l.match(/^(#{1,6})\s+(.*)/)){flushP();flushL();const n=Math.min(m[1].length+2,6);out.push(`<h${n}>${inline(m[2])}</h${n}>`);continue}
    if(/^\s*(---|\*\*\*)\s*$/.test(l)){flushP();flushL();out.push("<hr>");continue}
    if(m=l.match(/^\s*([-*+]|\d+\.)\s+(.*)/)){flushP();const t=/\d/.test(m[1])?"ol":"ul";if(!list||list.t!==t){flushL();list={t,items:[]}}list.items.push(m[2]);continue}
    if(!l.trim()){flushP();flushL();continue}
    if(list&&/^\s+/.test(l)){list.items[list.items.length-1]+=" "+l.trim();continue}
    flushL();para.push(l);
  }
  flushP();flushL();return out.join("\n");
}
function fmtVal(v,g){
  if(v===null||v===undefined||v==="")return '<span class="muted">–</span>';
  if(g?.fmt==="time"){const d=typeof v==="number"||/^\d+$/.test(v)?new Date(+v*(+v>1e12?1:1000)):new Date(v);
    return isNaN(d)?esc(v):`${esc(d.toLocaleString())} <span class="muted">${ago(d.toISOString())}</span>`}
  if(g?.fmt==="bool")return +v?"yes":"no";
  return esc(v);
}
function glossaryView(){
  const kindDef=t=>D.glossary.concepts.find(c=>c.term===t);
  let h="<h2>Glossary</h2><h3>Concepts</h3><table class='kv'>"+D.glossary.concepts.map(c=>`<tr><td><b>${esc(c.term)}</b></td><td>${esc(c.def)}</td></tr>`).join("")+"</table>";
  for(const [hn,f] of Object.entries(D.glossary.fields))
    h+=`<h3>${hn==="claude"?"Claude Code":"Codex"} memory fields</h3><table class='kv'>`+Object.entries(f).map(([k,g])=>`<tr><td><code>${esc(k)}</code></td><td>${esc(g.def)}</td></tr>`).join("")+"</table>";
  return h;
}
function findByName(n,it){return D.items.find(x=>x.kind==="memory"&&x.title===n&&(!it||x.project===it.project&&x.harness===it.harness))||D.items.find(x=>x.kind==="memory"&&x.title===n)}
function findByFile(f,it){const dir=it.path.replace(/[^/]+$/,"");return D.items.find(x=>x.path===dir+f)}

function visible(){
  const q=Q.toLowerCase();
  return D.items.filter(it=>(!F.harness.size||F.harness.has(it.harness))&&(!F.kind.size||F.kind.has(it.kind))&&
    (!q||[it.title,it.description,it.body,it.path,it.project,it.mtype].join("\n").toLowerCase().includes(q)));
}

function renderNav(){
  const items=visible(), sel=location.hash.match(/item\/(\w+)/)?.[1];
  let h='<div class="filters">';
  for(const x of ["claude","codex"])h+=`<span class="chip ${F.harness.has(x)?"on":""}" data-f="harness" data-v="${x}">${x}</span>`;
  for(const [k,v] of Object.entries(KINDS))h+=`<span class="chip ${F.kind.has(k)?"on":""}" data-f="kind" data-v="${k}">${v}</span>`;
  h+=`</div><div class="it ${!sel&&location.hash!=="#/glossary"&&!location.hash.startsWith("#/project")?"sel":""}" onclick="location.hash='#/'"><span class="t"><b>Overview</b></span><span class="badge">${items.length}</span></div><div class="it ${location.hash==="#/glossary"?"sel":""}" onclick="location.hash='#/glossary'"><span class="t">Glossary</span></div>`;
  for(const hn of ["claude","codex"]){
    const hi=items.filter(i=>i.harness===hn); if(!hi.length)continue;
    h+=`<div class="grp" style="color:var(--${hn})">${hn==="claude"?"Claude Code":"Codex"}</div>`;
    const groups=new Map([["(global)",[]]]);
    for(const it of hi){const k=it.scope==="global"?"(global)":it.project;if(!groups.has(k))groups.set(k,[]);groups.get(k).push(it)}
    for(const [k,arr] of groups){ if(!arr.length)continue;
      arr.sort((a,b)=>(a.kind==="memory-index"?-1:0)-(b.kind==="memory-index"?-1:0)||a.kind.localeCompare(b.kind)||a.title.localeCompare(b.title));
      h+=k==="(global)"?`<div class="proj">Global</div>`:`<div class="proj" data-proj="${esc(hn)}|${esc(k)}">${esc(home(k))}</div>`;
      for(const it of arr){
        const warn=it.notes.length?`<span class="warn" title="${esc(it.notes.join("\n"))}">⚠</span>`:"";
        h+=`<div class="it ${sel===it.id?"sel":""}" data-id="${it.id}"><span class="t">${esc(it.title)}</span>${warn}<span class="badge">${esc(it.mtype||KINDS[it.kind])}</span></div>`;
      }
    }
  }
  $("#nav").innerHTML=h;
}

function overview(){
  const its=visible(), mem=its.filter(i=>i.kind==="memory");
  const card=(n,l)=>`<div class="card"><div class="n">${n}</div><div class="l">${l}</div></div>`;
  let h=`<h2>Harness memories on ${esc(D.host)}</h2><p class="muted">Collected ${when(D.generated)} in ${D.collect_ms} ms · read-only</p><div class="cards">`;
  for(const hn of ["claude","codex"]){const x=mem.filter(i=>i.harness===hn);h+=card(x.length,`${hn} memories`)}
  h+=card(its.filter(i=>i.kind==="instructions").length,"instruction files");
  h+=card(D.projects.length,"projects seen");
  h+=card(its.filter(i=>i.notes.length).length,"items with warnings")+"</div>";
  const types={};mem.forEach(m=>{const k=m.harness+" · "+(m.mtype||"untyped");types[k]=(types[k]||0)+1});
  if(Object.keys(types).length)h+="<h3>Memory types</h3><div class='cards'>"+Object.entries(types).map(([k,v])=>card(v,esc(k))).join("")+"</div>";
  if(D.warnings.length)h+="<h3>Collector warnings</h3>"+D.warnings.map(w=>`<div class="note">${esc(w)}</div>`).join("");
  const noted=its.filter(i=>i.notes.length);
  if(noted.length)h+="<h3>Needs attention</h3>"+noted.map(i=>`<div class="note"><a href="#/item/${i.id}">${esc(i.title)}</a> <span class="muted">${esc(home(i.project)||"")}</span>: ${esc(i.notes.join("; "))}</div>`).join("");
  h+="<h3>Recent memories</h3>"+table([...mem].sort((a,b)=>(b.mtime||"").localeCompare(a.mtime||"")).slice(0,15));
  h+=`<h3>Projects</h3><table class="grid"><tr><th>Harness</th><th>Directory</th><th>Sessions</th><th>Memories</th><th>Last active</th></tr>`;
  for(const p of [...D.projects].sort((a,b)=>(b.last_active||"").localeCompare(a.last_active||""))){
    h+=`<tr class="click" data-proj="${esc(p.harness)}|${esc(p.cwd)}"><td><span class="badge b-${p.harness}">${p.harness}</span></td><td class="path">${esc(home(p.cwd))}${p.exists?"":' <span class="warn" title="directory no longer exists">(missing)</span>'}${p.cwd_exact?"":' <span class="muted" title="decoded from store dir name">(guessed)</span>'}</td><td>${p.sessions}</td><td>${p.memory_count||""}</td><td>${ago(p.last_active)}</td></tr>`;
  }
  return h+"</table>";
}
function table(arr){
  if(!arr.length)return '<p class="muted">None.</p>';
  return `<table class="grid"><tr><th>Harness</th><th>Title</th><th>Type</th><th>Project</th><th>Modified</th></tr>`+arr.map(i=>
    `<tr class="click" data-id="${i.id}"><td><span class="badge b-${i.harness}">${i.harness}</span></td><td><b>${esc(i.title)}</b><br><span class="muted">${esc(i.description||"")}</span></td><td>${esc(i.mtype||KINDS[i.kind])}</td><td class="path">${esc(home(i.project)||"global")}</td><td>${ago(i.mtime)}</td></tr>`).join("")+"</table>";
}

function projectView(hn,cwd){
  const p=D.projects.find(p=>p.harness===hn&&p.cwd===cwd)||{};
  const its=D.items.filter(i=>i.harness===hn&&(i.project===cwd||i.loaded_by.includes(cwd)));
  let h=`<h2 class="path" style="font-size:18px">${esc(home(cwd))}</h2><span class="badge b-${hn}">${hn}</span><table class="kv" style="margin-top:10px">`;
  h+=`<tr><td>Exists on disk</td><td>${p.exists?"yes":"<span class=warn>no</span>"}</td></tr><tr><td>Sessions</td><td>${p.sessions??"–"}</td></tr><tr><td>Last active</td><td>${when(p.last_active)}</td></tr>`;
  if(p.store)h+=`<tr><td>Harness store</td><td class="path">${esc(home(p.store))}</td></tr>`;
  return h+"</table><h3>Memories & instructions in effect</h3>"+table(its);
}

async function itemView(id){
  const it=D.items.find(i=>i.id===id); if(!it)return '<p>Unknown item.</p>';
  let h=`<h2>${esc(it.title)}</h2><div><span class="badge b-${it.harness}">${it.harness}</span> <span class="badge" title="${esc(D.glossary.concepts.find(c=>c.term===KINDS[it.kind])?.def||"")}">${KINDS[it.kind]}</span> ${it.mtype?`<span class="badge" title="${esc(D.glossary.fields[it.harness]?.type?.def||"")}">${esc(it.mtype)}</span>`:""}</div>`;
  if(it.description)h+=`<p>${esc(it.description)}</p>`;
  h+=it.notes.map(n=>`<div class="note">${esc(n)}</div>`).join("");
  h+=`<h3>Where</h3><table class="kv"><tr><td>File</td><td class="path">${esc(it.path)} <button onclick="navigator.clipboard.writeText(${esc(JSON.stringify(it.path))})">copy</button></td></tr>`;
  h+=`<tr><td>Scope</td><td>${it.scope==="global"?"Global (all sessions)":`Project <a class="path" data-proj="${esc(it.harness)}|${esc(it.project)}">${esc(home(it.project))}</a>`}</td></tr>`;
  if(it.loaded_by.length)h+=`<tr><td>Loaded in</td><td class="path">${it.loaded_by.map(c=>esc(home(c))).join("<br>")}</td></tr>`;
  h+=`<tr><td>Modified</td><td>${when(it.mtime)} <span class="muted">${ago(it.mtime)}</span></td></tr><tr><td>Size</td><td>${it.size??"–"} bytes</td></tr></table>`;
  const fm=Object.entries(it.frontmatter||{});
  const row=([k,v])=>{const g=D.glossary.fields[it.harness]?.[k];
    return `<tr><td>${esc(k)}</td><td>${typeof v==="object"&&v?"<table class=kv>"+Object.entries(v).map(row).join("")+"</table>":fmtVal(v,g)}${g?`<div class="def">${esc(g.def)}</div>`:""}</td></tr>`};
  if(fm.length)h+="<h3>Metadata <a class='small' href='#/glossary'>glossary</a></h3><table class='kv'>"+fm.map(row).join("")+"</table>";
  const raw=localStorage.raw==="1"||it.lang;
  h+=`<h3>Content <button id="rawtog">${raw?"rendered":"raw"}</button></h3>`;
  h+=raw?`<pre class="raw">${esc(it.body)}</pre>`:`<div class="md">${md(it.body,it)}</div>`;
  const back=D.items.filter(x=>x.id!==it.id&&x.kind==="memory"&&(x.body.includes(`[[${it.title}]]`)||x.body.includes(it.path.split("/").pop()))&&x.project===it.project);
  if(back.length)h+="<h3>Referenced by</h3>"+back.map(b=>`<div><a href="#/item/${b.id}">${esc(b.title)}</a></div>`).join("");
  if(it.kind==="memory")h+=`<h3>Provenance</h3><div id="origin" class="muted">Searching sessions…</div>`;
  return h;
}

async function loadOrigin(id){
  const el=$("#origin"); if(!el)return;
  const res=await (await fetch("/api/origin/"+id)).json();
  if(!$("#origin"))return;
  if(res.error){el.innerHTML=`<div class="note">${esc(res.error)}</div>`;return}
  if(!res.length){el.innerHTML="No originating session found (it may have been deleted or recorded on another machine).";return}
  el.classList.remove("muted");
  el.innerHTML=res.map(s=>`<table class="kv"><tr><td>Session</td><td class="path">${esc(s.session_id)}</td></tr>
   <tr><td>Transcript</td><td class="path">${esc(home(s.path))}</td></tr><tr><td>Directory</td><td class="path">${esc(home(s.cwd))}${s.git_branch?` <span class="muted">(${esc(s.git_branch)})</span>`:""}</td></tr>
   <tr><td>When</td><td>${when(s.started)} → ${when(s.ended)}</td></tr><tr><td>User prompts</td><td>${s.prompt_count}</td></tr></table>
   ${s.first_prompt?`<div class="ev"><b>Session opened with</b><div class="p">${esc(s.first_prompt.slice(0,2000))}</div></div>`:""}
   ${s.events.map(e=>`<div class="ev"><b>${esc(e.tool)}</b> <span class="muted">${when(e.timestamp)}</span>${e.action&&e.action!==e.tool?` <code>${esc(e.action)}</code>`:""}${e.prompt?`<div class="muted" style="margin-top:4px">Prompted by:</div><div class="p">${esc(e.prompt.slice(0,3000))}</div>`:""}</div>`).join("")}`).join("<hr>");
}

async function route(){
  if(!D)return; renderNav();
  const h=location.hash, v=$("#view"); let m;
  if(m=h.match(/^#\/item\/(\w+)/)){v.innerHTML=await itemView(m[1]);v.scrollTop=0;loadOrigin(m[1])}
  else if(m=h.match(/^#\/project\/(\w+)\/(.*)/))v.innerHTML=projectView(m[1],decodeURIComponent(m[2]));
  else if(h==="#/glossary")v.innerHTML=glossaryView();
  else v.innerHTML=overview();
}
document.addEventListener("click",e=>{
  const t=e.target.closest("[data-id],[data-proj],[data-f],#rawtog"); if(!t)return;
  if(t.id==="rawtog"){localStorage.raw=localStorage.raw==="1"?"0":"1";route();return}
  if(t.dataset.f){const s=F[t.dataset.f];s.has(t.dataset.v)?s.delete(t.dataset.v):s.add(t.dataset.v);route();return}
  if(t.dataset.id)location.hash="#/item/"+t.dataset.id;
  else{const [hn,...c]=t.dataset.proj.split("|");location.hash=`#/project/${hn}/`+encodeURIComponent(c.join("|"))}
});
$("#q").addEventListener("input",e=>{Q=e.target.value;route()});
document.addEventListener("keydown",e=>{if(e.key==="/"&&document.activeElement!==$("#q")){e.preventDefault();$("#q").focus()}});
window.addEventListener("hashchange",route);
async function load(){
  D=await (await fetch("/api/data")).json();
  $("#meta").textContent=`${D.host} · ${D.items.length} items · ${D.projects.length} projects`;
  route();
}
$("#reload").onclick=load; load();
</script></body></html>
"""

if __name__ == "__main__":
    main()
