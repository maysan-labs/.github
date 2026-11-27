# Agent attribution standard

Every commit written by an agent says so, and says which model wrote it.

Git already records *who* committed. That is not enough once more than one agent
writes code: a `Hermes` author tells you nothing about which model produced the
line, and a commit authored as `sanjay mandal` tells you nothing about whether a
human or an agent wrote it. Review cannot be pointed at the harness that caused a
defect if the harness is invisible in history.

This standard fixes that with two commit trailers, one canonical identity per
agent, and a check that runs in CI.

## 1. The trailers

An agent commit ends with a trailer block, as the last paragraph of the message:

```
feat(mail): webmail alias resolves to the mailbox

The alias sent visitors to the landing page. It now hits the mailbox.

Agent: hermes
Model: deepseek-flash
```

| trailer | required | value |
| --- | --- | --- |
| `Agent` | yes | an agent id from the table below |
| `Model` | yes | the exact model name the agent ran, e.g. `deepseek-flash`, `claude-sonnet-4.6` |
| `Agent-Run` | no | board task, cron job or session id that produced the change |
| `Reviewed-By` | no | the agent or human that reviewed before merge |

Format rules, because machines parse this:

- one `Key: value` per line, at the end of the message;
- the trailer block is one paragraph — no blank line inside it, one blank line
  before it;
- no trailing punctuation on the value.

## 2. Canonical identities

One agent, one identity. `agent-attribution.json` is the machine-readable copy of
this table and the source of truth for the checker.

| agent id | author name | author email |
| --- | --- | --- |
| `hermes` | `Hermes Agent` | `agent@maysanlabs.com` |
| `antigravity` | `Antigravity (Maysan)` | `antigravity@maysanlabs.com` |
| `paperclip` | `Paperclip Agent` | `paperclip@maysanlabs.com` |
| `claude-code` | `Claude Code` | `claude@maysanlabs.com` |
| `codex` | `Codex` | `codex@maysanlabs.com` |

Humans keep their own identity: `Sanjay Mandal <sanjay@maysanlabs.com>` and
`sanjay mandal <rjsanjaymandal@gmail.com>`.

Set it once on the machine or container the agent runs in:

```bash
git config --global user.name  "Hermes Agent"
git config --global user.email "agent@maysanlabs.com"
```

or per invocation, which is safer when several agents share one box:

```bash
git -c user.name="Hermes Agent" -c user.email="agent@maysanlabs.com" commit -m "..." -m "Agent: hermes
Model: deepseek-flash"
```

Identities that were used before this standard (for example `Hermes (Agent)
<agent@maysanlabs.com>`, `Hermes <hermes@maysanlabs.com>`, `Ops Analyst
<ops@maysanlabs.com>`) are listed in `agent-attribution.json` under
`agent_identities`. They are recognised as agents, and reported as drift, so old
history stays readable while new commits converge on one identity.

## 3. Check it

The checker is `scripts/attribution_check.py` (on the ops box:
`/opt/data/bin/maysan-attribution-check`).

```bash
# the commits of one pull request
maysan-attribution-check --repo maysan-labs/aapna-finance --pr 224

# the last 20 commits of the default branch
maysan-attribution-check --repo maysan-labs/Mailsender --commits 20

# the commits you are about to push
maysan-attribution-check --range origin/main..HEAD

# the whole organisation, 5 commits per repo
maysan-attribution-check --org maysan-labs --per-repo 5

# one message, as a commit-msg hook does
maysan-attribution-check --message-file .git/COMMIT_EDITMSG
```

Exit code `0` means clean, `1` means violations, `2` means it could not run.
`--json` gives machine-readable output; `--strict` also fails on identity drift.

Use it as a local hook when an agent works unattended:

```bash
cp scripts/attribution_check.py .git/hooks/commit-msg   # then chmod +x, or wrap it
```

## 4. CI

`.github/workflows/agent-attribution.yml` is a reusable workflow. A repository
opts in with a caller file, which is also in `workflow-templates/`:

```yaml
name: agent-attribution
on:
  pull_request:
  workflow_dispatch:
jobs:
  attribution:
    uses: maysan-labs/.github/.github/workflows/agent-attribution.yml@master
    with:
      runs_on: '["self-hosted","linux","aapna"]'
```

`runs_on` is a JSON array because this org runs on the box, one runner per
repository, and GitHub-hosted runners are refused on billing.

Making the check *required* is a ruleset decision per repository, not part of this
workflow. Add it to the branch ruleset once a repository is clean, so a red check
cannot be mistaken for a queue.

## 5. What is enforced, and what is not

| rule | level | meaning |
| --- | --- | --- |
| R1 | error | an agent commit must carry `Agent:` and `Model:` |
| R2 | warning, error with `--strict` | an agent commit must use the canonical name and email |
| R3 | error | `Agent:` must be a known id, `Model:` must not be empty |
| R4 | error | a human identity must not carry `Agent:` |

Not enforced, on purpose: whether the trailer is true. A trailer is a
declaration, and a declaration can be typed by hand. The binding control is the
author identity — the `git config` of the agent, or a per-agent token — because
that is what the commit is signed with. The trailers make the declaration
explicit and searchable; the identity is what makes it accountable.

Merge commits and bot identities (`dependabot[bot]`, the Dokploy GitHub App) are
exempt.

## 6. Fixing history

Do not rewrite shared history to retrofit trailers. Amend before push:

```bash
git commit --amend             # add the two trailer lines
git rebase -i origin/main      # or reword each commit
```

Rewrite a pushed range only when it is a branch nobody else has pulled:

```bash
git rebase --exec 'git commit --amend --no-edit ...' origin/main
```

A retrofitted trailer written months later is not evidence of anything. The point
of the standard is that it is true at the moment the commit is created.
