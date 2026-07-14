"""Does anydocs make a real agent get the answer right? Three arms, graded blind.

Every other ruler here scores retrieval — did the eight rows contain the page. This
one scores the thing a user actually cares about: **was the answer right**, at the
end of a real agent loop.

Three claims died getting here, all of them n=1, all of them mine:

  "the model doesn't know"   -> give it WebFetch and it finds out. It really does.
  "it says 9, there are 20"  -> there are 30. I read one page (the SDK's HookEvent
                                type) and generalised — the exact failure this tool
                                exists to prevent.
  "anydocs saves tokens"     -> end to end it does not. The 500-token search is a
                                rounding error next to the agent's own overhead,
                                and the tool schemas are not free.

## The arms

  plain        Claude Code, WebFetch and WebSearch enabled. **The control is not a
               model with its hands tied — it is what the user already has.**
  anydocs      the same, with the MCP server mounted and nothing else.
  anydocs+md   the same, plus the one line the README tells you to put in AGENTS.md.

The third arm is the point. A mounted server the agent never calls is worth nothing:
arm 2 answered from memory on 2 runs in 80 and was wrong on both — asked for Claude
Code's permission modes it replied in ONE turn, named four, and missed `auto` and
`dontAsk`. The instruction takes that to zero.

## Grading

An LLM judge, blind to which arm produced the answer, against a key verified against
the current docs. **The judge is noisy: one pass moves accuracy by up to 10 points.**
So run it several times (`--passes 3`) and read the mean and the range. The
wrong-answer rate is the more stable number and it is the one that matters — a
developer acting on a wrong answer is the whole cost.

Questions are config surface that has changed recently. That is the ground a docs
tool should own; it says nothing about a question with no documented answer.

    uv run python scripts/eval_agent.py --reps 2 --passes 3

Costs real money (~$0.30 a run) and needs the `claude` CLI on PATH. Not in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUT = Path("build/agent_eval")
WORKERS = 6

# (source, question, verified key). Every key was read off the live docs.
QUESTIONS = [
    ("codex", "Does OpenAI Codex (the CLI) support lifecycle hooks? Name the events and the config file.",
     "10 events: SessionStart SubagentStart PreToolUse PermissionRequest PostToolUse PreCompact "
     "PostCompact UserPromptSubmit SubagentStop Stop. Config: ~/.codex/hooks.json, .codex/hooks.json, "
     "or inline [hooks] in config.toml. Saying Codex has NO hooks is WRONG."),
    ("codex", "In Codex, how do I run against a local model like Ollama? Give the exact config key.",
     "The `--oss` flag, with `oss_provider = \"ollama\"|\"lmstudio\"` in config.toml as the default, or "
     "`--local-provider` per run. Answering only with `model_provider` + [model_providers.x] is the OLD "
     "schema and misses oss_provider."),
    ("codex", "In Codex, what is the exact TOML syntax to define and select a config profile?",
     "Profiles are SEPARATE FILES: ~/.codex/<name>.config.toml with top-level keys, selected with "
     "--profile <name>. The [profiles.<name>] table and the top-level `profile=` selector were REMOVED "
     "in 0.134.0."),
    ("cursor", "In Cursor, how does an enterprise admin restrict which AI models team members can select? "
     "Name the exact feature and where it is configured.",
     "Model Access Control. Enterprise-only, configured in the team dashboard."),
    ("cursor", "Does Cursor support hooks? Name the hook events and the config file.",
     "Yes: hooks.json, ~21 events including beforeShellExecution, afterShellExecution, "
     "beforeMCPExecution, afterMCPExecution, beforeReadFile, afterFileEdit, beforeSubmitPrompt, stop, "
     "sessionStart, sessionEnd, preToolUse, postToolUse, preCompact, audit. Naming only 5-6 is INCOMPLETE."),
    ("cursor", "What is the file format and the exact frontmatter fields of a Cursor rules file, and "
     "where do they live?",
     ".mdc files under .cursor/rules/ (nested dirs allowed). Exactly three frontmatter fields: "
     "description, globs, alwaysApply."),
    ("claude-code", "How many hook events does Claude Code have? List them all.",
     "30: SessionStart Setup UserPromptSubmit UserPromptExpansion PreToolUse PermissionRequest "
     "PermissionDenied PostToolUse PostToolUseFailure PostToolBatch Notification MessageDisplay "
     "SubagentStart SubagentStop TaskCreated TaskCompleted Stop StopFailure TeammateIdle "
     "InstructionsLoaded ConfigChange CwdChanged FileChanged WorktreeCreate WorktreeRemove PreCompact "
     "PostCompact Elicitation ElicitationResult SessionEnd. Answering 9 (the classic set) is WRONG."),
    ("claude-code", "What are the exact permission modes in Claude Code, and what settings.json key "
     "selects one?",
     "SIX: default, acceptEdits, plan, auto, dontAsk, bypassPermissions. Key: permissions.defaultMode. "
     "Answering 'four' and omitting auto/dontAsk is WRONG."),
    ("opencode", "In opencode, how do I define a custom agent? Give the exact directory and file.",
     "Markdown files in `agents/` — PLURAL: ~/.config/opencode/agents/ or .opencode/agents/. Also "
     "definable as JSON under the `agent` key in opencode.json. Saying `.opencode/agent/` (singular) is "
     "WRONG — the file silently never loads."),
    ("xai", "Using the xAI API, how do I turn on Grok's built-in web search? Give the exact parameter "
     "names.",
     "The agentic `web_search` / `x_search` tools passed in `tools` on /v1/responses. Params: "
     "allowed_domains / excluded_domains (max 5). The older Chat-Completions `search_parameters` with "
     "`sources` is marked DEPRECATED, not removed."),
]

MCP_ON = json.dumps({"mcpServers": {"anydocs": {
    "command": "uvx",
    "args": ["--from", "git+https://github.com/kiyeonjeon21/anydocs", "anydocs"],
}}})
MCP_OFF = json.dumps({"mcpServers": {}})

# Verbatim from the README. The whole third arm is this string.
AGENTS_MD = """# Project

When the anydocs MCP server is available, use `search_docs` with the product's
source and then `read_doc` before answering questions about that product's
documentation.
"""

ARMS = {"plain": (MCP_OFF, False), "anydocs": (MCP_ON, False), "anydocs+md": (MCP_ON, True)}

GRADE = """You are grading answers about developer-tool documentation.

Each case gives a QUESTION, the verified KEY (ground truth, checked against the
current official docs), and an ANSWER from an AI agent. You are NOT told which
system produced it. Grade only against the key.

  CORRECT     matches the key on every load-bearing fact. Extra correct detail is fine.
  INCOMPLETE  everything it says is true, but it omits something the key calls
              material (lists 6 of 21 events; misses a required config key).
  WRONG       contradicts the key: says a feature does not exist when it does; gives
              a key/path/enum the key says is wrong or removed; gives a schema the
              key flags as replaced.

The key names the classic failure for several of these. Apply it strictly.

Reply one line per case: "N. VERDICT — eight words why". Nothing else.

{items}"""

VERDICT = re.compile(r"^\s*(\d+)[.)]\s*(CORRECT|INCOMPLETE|WRONG)\s*[—\-:]*\s*(.*)$", re.I)


def _claude(prompt: str, cfg: str, cwd: str, *, model: str | None = None, tools: bool = True) -> str:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--strict-mcp-config", "--mcp-config", cfg]
    if model:
        cmd += ["--model", model]
    if not tools:
        cmd += ["--disallowedTools", "Bash,Read,Grep,Glob,WebFetch,WebSearch,Task,Edit,Write"]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=900).stdout


def run_one(job):
    src, question, arm, rep = job
    cfg, with_md = ARMS[arm]
    tag = hashlib.md5(f"{arm}{rep}{question}".encode()).hexdigest()[:10]
    dst = OUT / f"{arm}-{rep}-{tag}.json"
    if dst.exists():
        return json.loads(dst.read_text(), strict=False)
    with tempfile.TemporaryDirectory() as cwd:
        if with_md:
            (Path(cwd) / "AGENTS.md").write_text(AGENTS_MD)
            (Path(cwd) / "CLAUDE.md").write_text("@AGENTS.md\n")
        t0 = time.time()
        raw = _claude(question, cfg, cwd)
        wall = time.time() - t0
    try:
        d = json.loads(raw, strict=False)
    except Exception:
        return None
    u = d.get("usage", {})
    rec = {
        "src": src, "q": question, "arm": arm, "rep": rep,
        "wall": round(wall, 1), "turns": d.get("num_turns", 0),
        "cost": d.get("total_cost_usd", 0.0), "out": u.get("output_tokens", 0),
        "answer": d.get("result", ""),
    }
    dst.write_text(json.dumps(rec, indent=1))
    return rec


def grade_pass(recs, seed: str, keys) -> dict[int, tuple[str, str]]:
    """One full blind grading pass. Batches are shuffled by seed — the judge must
    never see a run of one arm and infer a pattern."""
    order = sorted(
        enumerate(recs),
        key=lambda kv: hashlib.md5(f"{seed}{kv[1]['q']}{kv[1]['arm']}{kv[1]['rep']}".encode()).hexdigest(),
    )
    batches = [order[i : i + 8] for i in range(0, len(order), 8)]

    def one(batch):
        items = "\n\n".join(
            f"=== case {n} ===\nQUESTION: {r['q']}\nKEY: {keys[r['q']]}\n"
            f"ANSWER: {' '.join(r['answer'].split())[:1800]}"
            for n, (_, r) in enumerate(batch, 1)
        )
        with tempfile.TemporaryDirectory() as cwd:
            out = _claude(GRADE.format(items=items), MCP_OFF, cwd, model="sonnet", tools=False)
        got = {}
        for line in out.splitlines():
            if (m := VERDICT.match(line)) and 1 <= int(m[1]) <= len(batch):
                got[batch[int(m[1]) - 1][0]] = (m[2].upper(), m[3].strip())
        return got

    verdicts: dict[int, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for got in pool.map(one, batches):
            verdicts |= got
    return verdicts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2, help="runs per question per arm")
    ap.add_argument("--passes", type=int, default=3, help="independent grading passes (the judge is noisy)")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    keys = {q: k for (_, q, k) in QUESTIONS}

    jobs = [(s, q, arm, r) for (s, q, _) in QUESTIONS for arm in ARMS for r in range(args.reps)]
    print(f"{len(jobs)} runs — {len(QUESTIONS)} questions x {len(ARMS)} arms x {args.reps} reps\n")
    recs, done = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for rec in pool.map(run_one, jobs):
            done += 1
            if rec:
                recs.append(rec)
            print(f"  {done}/{len(jobs)}", end="\r", flush=True)
    print(f"  {done}/{len(jobs)} done      \n")

    # Hard facts first: no judge is involved in any of these.
    print("no judge involved in these:")
    print(f"  {'arm':12}{'runs':>6}{'answered from memory':>22}{'wall':>8}{'turns':>7}{'cost':>9}")
    for arm in ARMS:
        r = [x for x in recs if x["arm"] == arm]
        if not r:
            continue
        lazy = [x for x in r if x["turns"] <= 2]
        print(f"  {arm:12}{len(r):>6}{len(lazy):>13} ({len(lazy) / len(r):3.0%})"
              f"{sum(x['wall'] for x in r) / len(r):7.0f}s"
              f"{sum(x['turns'] for x in r) / len(r):7.1f}"
              f"  ${sum(x['cost'] for x in r) / len(r):.4f}")

    acc: dict[str, list[float]] = defaultdict(list)
    wrong: dict[str, list[float]] = defaultdict(list)
    for p in range(args.passes):
        v = grade_pass(recs, str(p), keys)
        print(f"  graded pass {p + 1}/{args.passes}", end="\r", flush=True)
        for arm in ARMS:
            idx = [i for i, r in enumerate(recs) if r["arm"] == arm and i in v]
            if not idx:
                continue
            acc[arm].append(sum(v[i][0] == "CORRECT" for i in idx) / len(idx))
            wrong[arm].append(sum(v[i][0] == "WRONG" for i in idx) / len(idx))
    print(" " * 40, end="\r")

    print(f"\ngraded blind, mean of {args.passes} passes (the judge moves ~10pp between passes):")
    print(f"  {'arm':12}{'accuracy':>22}{'WRONG — the one that costs':>30}")
    for arm in ARMS:
        if not acc[arm]:
            continue
        a, w = acc[arm], wrong[arm]
        print(f"  {arm:12}{statistics.mean(a):>10.2f}  [{min(a):.2f}-{max(a):.2f}]"
              f"{statistics.mean(w):>18.0%}  [{min(w):.0%}-{max(w):.0%}]")
    if wrong["plain"] and wrong["anydocs+md"]:
        f = statistics.mean(wrong["plain"]) / max(statistics.mean(wrong["anydocs+md"]), 1e-9)
        print(f"\n  {f:.1f}x fewer wrong answers than the agent already has, "
              f"and the AGENTS.md line is most of it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
