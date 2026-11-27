#!/usr/bin/env python3
"""maysan-attribution-check — enforce agent code attribution in Git commits.

Why this exists: an agent that writes code must say so in the commit, under one
canonical identity. Without that, "which agent wrote this?" is unanswerable and
review cannot target the harness that produced the defect.

Rules
  R1 (error)   A commit authored by an agent identity must carry the trailers
               `Agent: <id>` and `Model: <name>` in its message.
  R2 (warning) An agent commit should use that agent's canonical name/email pair.
               Any other pair for the same agent is identity drift.
  R3 (error)   `Agent:` / `Model:` values must be non-empty and `Agent:` must be
               a known agent id.
  R4 (error)   A human identity must not carry an `Agent:` trailer (stops an
               agent from hiding under a human name).

Merge commits and bot identities (dependabot, GitHub App) are exempt.

Modes
  --repo OWNER/NAME --commits N          last N commits of the default branch
  --repo OWNER/NAME --pr N               the commits of one pull request
  --repo OWNER/NAME --ref BRANCH --commits N
  --org [--limit N] [--per-repo N]       sweep an organisation
  --range BASE..HEAD                     local git range in the current repo
  --message-file PATH                    one commit message (commit-msg hook)

Exit codes: 0 clean, 1 violations, 2 usage/connection error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# Canonical map. Single source of truth is agent-attribution.json in
# maysan-labs/.github; --config points at a local copy. These defaults are the
# same data, so the tool works with no network config.
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "schema": 1,
    # agent id -> canonical identity
    "agents": {
        "hermes":      {"name": "Hermes Agent",         "email": "agent@maysanlabs.com"},
        "antigravity": {"name": "Antigravity (Maysan)", "email": "antigravity@maysanlabs.com"},
        "paperclip":   {"name": "Paperclip Agent",      "email": "paperclip@maysanlabs.com"},
        "claude-code": {"name": "Claude Code",          "email": "claude@maysanlabs.com"},
        "codex":       {"name": "Codex",                "email": "codex@maysanlabs.com"},
    },
    # identities an agent has used that are NOT canonical -> drift when seen
    "agent_identities": [
        {"name": "Hermes Agent",         "email": "agent@maysanlabs.com"},
        {"name": "Hermes (Agent)",       "email": "agent@maysanlabs.com"},
        {"name": "Hermes",               "email": "hermes@maysanlabs.com"},
        {"name": "Hermes (Maysan Labs)", "email": "hermes@maysanlabs.com"},
        {"name": "Hermes Agent",         "email": "hermes@maysanlabs.com"},
        {"name": "hermes",               "email": "ops@maysanlabs.com"},
        {"name": "Hermes",               "email": "ops@maysanlabs.com"},
        {"name": "Ops Analyst",          "email": "ops@maysanlabs.com"},
        {"name": "Antigravity (Maysan)", "email": "sanjay@maysanlabs.com"},
    ],
    # name patterns that mark an author as an agent even on an unknown email
    "agent_name_patterns": [
        r"\bhermes\b", r"\bantigravity\b", r"\bpaperclip\b", r"\bclaude code\b",
        r"\bcodex\b", r"\bcursor\b", r"\bdevin\b", r"\bops analyst\b",
    ],
    # never agents, never need trailers
    "exempt_emails": [
        "rjsanjaymandal@gmail.com",
        "sanjay@maysanlabs.com",
        "business@maysanlabs.com",
        "dev@maysanlabs.com",
        "hello@maysanlabs.com",
        "ops@maysanlabs.com",
    ],
    "exempt_email_patterns": [r"(?i)dependabot", r"\[bot\]@users\.noreply\.github\.com"],
    # humans whose identity must never carry Agent: (R4)
    "humans": [
        {"name": "Sanjay Mandal", "email": "sanjay@maysanlabs.com"},
        {"name": "sanjay mandal", "email": "rjsanjaymandal@gmail.com"},
        {"name": "rjsanjaymandal", "email": "rjsanjaymandal@gmail.com"},
    ],
    "required_trailers": ["Agent", "Model"],
    "known_agents": ["hermes", "antigravity", "paperclip", "claude-code", "codex"],
}


def load_config(path: str | None) -> dict:
    if not path:
        for cand in ("agent-attribution.json",
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-attribution.json")):
            if os.path.exists(cand):
                path = cand
                break
    if not path:
        return DEFAULT_CONFIG
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


# --------------------------------------------------------------------------
# GitHub access
# --------------------------------------------------------------------------

def _token() -> str | None:
    for env in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(env):
            return os.environ[env]
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


_TOKEN = _token()


def api(path: str):
    if not _TOKEN:
        raise SystemExit("error: no GitHub token (set GH_TOKEN or log in with gh)")
    url = "https://api.github.com" + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "maysan-attribution-check",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200]
        raise SystemExit(f"error: {url} -> {exc.code} {detail}")


def norm_commit(payload: dict) -> dict:
    c = payload.get("commit", {})
    author = c.get("author") or {}
    msg = c.get("message", "")
    parents = payload.get("parents") or []
    return {
        "sha": (payload.get("sha") or "")[:10],
        "name": author.get("name") or "",
        "email": author.get("email") or "",
        "message": msg,
        "subject": msg.split("\n", 1)[0],
        "merge": len(parents) > 1,
    }


def commits_api(repo: str, branch: str | None, limit: int) -> list[dict]:
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        per = min(100, limit - len(out))
        q = f"/repos/{repo}/commits?per_page={per}&page={page}"
        if branch:
            q += f"&sha={branch}"
        batch = api(q)
        if not batch:
            break
        out.extend(norm_commit(x) for x in batch)
        if len(batch) < per:
            break
        page += 1
    return out[:limit]


def pr_commits(repo: str, number: int) -> list[dict]:
    return [norm_commit(x) for x in api(f"/repos/{repo}/pulls/{number}/commits?per_page=100")]


def local_range(rev_range: str) -> list[dict]:
    fmt = "%H%x1f%an%x1f%ae%x1f%P%x1f%B%x1e"
    raw = subprocess.check_output(
        ["git", "log", f"--format={fmt}", rev_range], text=True, stderr=subprocess.PIPE)
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, name, email, parents, msg = rec.split("\x1f", 4)
        out.append({
            "sha": sha[:10], "name": name, "email": email, "message": msg,
            "subject": msg.split("\n", 1)[0], "merge": len(parents.split()) > 1,
        })
    return out


def message_commit_file(path: str) -> list[dict]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        msg = fh.read()
    name = subprocess.check_output(["git", "config", "user.name"], text=True).strip()
    email = subprocess.check_output(["git", "config", "user.email"], text=True).strip()
    return [{"sha": "(staged)", "name": name, "email": email, "message": msg,
             "subject": msg.split("\n", 1)[0], "merge": False}]


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

TRAILER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(\S.*)$")


def parse_trailers(message: str) -> dict[str, str]:
    lines = [ln.rstrip() for ln in message.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    block: list[str] = []
    for ln in reversed(lines):
        if not ln.strip():
            break
        block.append(ln)
    block.reverse()
    trailers: dict[str, str] = {}
    for ln in block:
        m = TRAILER_RE.match(ln)
        if m:
            trailers.setdefault(m.group(1), m.group(2).strip())
    return trailers


def classify(commit: dict, cfg: dict) -> str:
    """agent | exempt | human"""
    name, email = commit["name"], commit["email"]
    pairs = {(i["name"], i["email"]) for i in cfg["agent_identities"]}
    if (name, email) in pairs:
        return "agent"
    for pat in cfg["exempt_email_patterns"]:
        if re.search(pat, email):
            return "exempt"
    for pat in cfg["agent_name_patterns"]:
        if re.search(pat, name, re.I):
            return "agent"
    if email in cfg["exempt_emails"]:
        return "human"
    return "human"


def canonical_agent(name: str, email: str, trailers: dict[str, str], cfg: dict) -> str | None:
    declared = trailers.get("Agent")
    if declared and declared in cfg["known_agents"]:
        return declared
    for ident in cfg["agent_identities"]:
        if ident["name"] == name and ident["email"] == email:
            for agent_id, canon in cfg["agents"].items():
                if canon["email"] == ident["email"]:
                    return agent_id
    for pat in cfg["agent_name_patterns"]:
        if re.search(pat, name, re.I):
            return "hermes"
    return None


def check(commits: list[dict], cfg: dict, strict: bool = False) -> dict:
    violations: list[dict] = []
    drift: list[dict] = []
    stats = {"total": 0, "agent": 0, "attributed": 0, "human": 0, "exempt": 0, "merge": 0}

    for c in commits:
        stats["total"] += 1
        if c["merge"]:
            stats["merge"] += 1
            continue
        kind = classify(c, cfg)
        trailers = parse_trailers(c["message"])
        stats[kind] += 1

        if kind == "human":
            if trailers.get("Agent"):
                violations.append({
                    "rule": "R4", "sha": c["sha"], "subject": c["subject"][:60],
                    "detail": f"human identity {c['name']!r} carries Agent: {trailers['Agent']}",
                })
            continue
        if kind == "exempt":
            continue

        agent_id = canonical_agent(c["name"], c["email"], trailers, cfg)
        missing = [t for t in cfg["required_trailers"] if not trailers.get(t)]
        if missing:
            violations.append({
                "rule": "R1", "sha": c["sha"], "subject": c["subject"][:60],
                "detail": "missing trailer(s): " + ", ".join(missing),
            })
        else:
            stats["attributed"] += 1
            if trailers["Agent"] not in cfg["known_agents"]:
                violations.append({
                    "rule": "R3", "sha": c["sha"], "subject": c["subject"][:60],
                    "detail": f"unknown agent id {trailers['Agent']!r}",
                })
        if agent_id and agent_id in cfg["agents"]:
            canon = cfg["agents"][agent_id]
            if (c["name"], c["email"]) != (canon["name"], canon["email"]):
                drift.append({
                    "rule": "R2", "sha": c["sha"], "agent": agent_id,
                    "detail": f"{c['name']!r} <{c['email']}> should be "
                              f"{canon['name']!r} <{canon['email']}>",
                })

    if strict:
        violations.extend(drift)
        drift = []
    return {"stats": stats, "violations": violations, "drift": drift}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def report_text(label: str, res: dict) -> str:
    s = res["stats"]
    out = [f"  {label}"]
    if s["agent"]:
        pct = 100.0 * s["attributed"] / s["agent"]
        out.append(f"    commits {s['total']} (merge {s['merge']}) · agent {s['agent']} · "
                   f"attributed {s['attributed']}/{s['agent']} = {pct:.0f}% · "
                   f"human {s['human']} · exempt {s['exempt']}")
    else:
        out.append(f"    commits {s['total']} (merge {s['merge']}) · agent 0 · "
                   f"human {s['human']} · exempt {s['exempt']}")
    for v in res["violations"]:
        out.append(f"    [{v['rule']}] {v['sha']} {v['detail']} — {v['subject']}")
    for d in res["drift"]:
        out.append(f"    [R2] {d['sha']} drift: {d['detail']}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Check agent attribution trailers in commits.")
    ap.add_argument("--repo", help="OWNER/NAME")
    ap.add_argument("--pr", type=int, help="pull request number")
    ap.add_argument("--ref", help="branch or sha (default: default branch)")
    ap.add_argument("--commits", type=int, default=20, help="how many commits to inspect (default 20)")
    ap.add_argument("--org", help="sweep an organisation")
    ap.add_argument("--limit", type=int, default=25, help="max repos in --org mode (default 25)")
    ap.add_argument("--per-repo", type=int, default=5, help="commits per repo in --org mode (default 5)")
    ap.add_argument("--range", help="local git range, e.g. origin/main..HEAD")
    ap.add_argument("--message-file", help="commit message file (commit-msg hook)")
    ap.add_argument("--config", help="path to agent-attribution.json")
    ap.add_argument("--strict", action="store_true", help="treat identity drift as a failure")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    cfg = load_config(args.config)
    results: dict[str, dict] = {}

    if args.message_file:
        results["(staged commit)"] = check(message_commit_file(args.message_file), cfg, args.strict)
    elif args.range:
        results["local:" + args.range] = check(local_range(args.range), cfg, args.strict)
    elif args.org:
        repos = [r["name"] for r in api(f"/orgs/{args.org}/repos?per_page=100&sort=pushed")
                 if not r.get("archived")][: args.limit]
        for name in repos:
            full = f"{args.org}/{name}"
            try:
                results[full] = check(commits_api(full, None, args.per_repo), cfg, args.strict)
            except SystemExit as exc:
                print(f"  {full}: skipped ({exc})", file=sys.stderr)
    elif args.repo:
        if args.pr:
            results[f"{args.repo} PR#{args.pr}"] = check(pr_commits(args.repo, args.pr), cfg, args.strict)
        else:
            results[f"{args.repo}" + (f"@{args.ref}" if args.ref else "")] = check(
                commits_api(args.repo, args.ref, args.commits), cfg, args.strict)
    else:
        ap.error("one of --repo, --org, --range or --message-file is required")

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print("maysan-attribution-check")
        for label, res in results.items():
            print(report_text(label, res))
        tot = {k: sum(r["stats"][k] for r in results.values())
               for k in ("total", "agent", "attributed", "human", "exempt", "merge")}
        nviol = sum(len(r["violations"]) for r in results.values())
        ndrift = sum(len(r["drift"]) for r in results.values())
        pct = (100.0 * tot["attributed"] / tot["agent"]) if tot["agent"] else 0.0
        print(f"\nTOTAL commits {tot['total']} · agent {tot['agent']} · "
              f"attributed {tot['attributed']} ({pct:.0f}%) · violations {nviol} · drift {ndrift}")
        if tot["agent"] and pct < 100.0:
            print("  fix: add `Agent: <id>` and `Model: <name>` as the last trailers of each agent commit")

    return 1 if any(r["violations"] for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
