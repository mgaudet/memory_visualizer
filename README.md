# memviz

Browse the AI harness memories on a machine (Claude Code and Codex) in your browser.
It's a single Python file that uses only the standard library and never writes anything.

    ./memviz.py            # serve on http://127.0.0.1:8765 and open a browser
    ./memviz.py serve --port 9000 --no-open
    ./memviz.py dump       # collected data as JSON
    ./memviz.py --home /path/to/other-machine-home serve

What it collects:

- **Claude Code**: `~/.claude/projects/*/memory/*.md` (memories + `MEMORY.md` index),
  `~/.claude/CLAUDE.md`, `~/.claude/rules/`, and project `CLAUDE.md` / `CLAUDE.local.md` /
  `.claude/CLAUDE.md` / `.claude/rules/*.md` found by walking up from each project directory.
- **Codex**: `~/.codex/memories_*.sqlite` (stage-1 memories), `~/.codex/memories/`,
  `~/.codex/AGENTS.md`, `~/.codex/rules/*.rules`, and project `AGENTS.md` files.

Project directories come from session transcripts. For each memory, the Provenance section
looks up the session that wrote it (Claude's `originSessionId`, Codex `thread_id`) and shows
the user prompt behind each write. Field meanings (e.g. Codex phase 1/2) are on the Glossary page.

It also flags memories that `MEMORY.md` doesn't list, index links that point at missing
files, and project directories that no longer exist.
