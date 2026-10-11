# routine-sweeper

Claude Code's scheduled tasks (routines) leave a session behind on **every
run**, and the desktop app only lets you delete them one at a time. One hourly
routine makes 700+ sessions a month; a real-world report counted **618 open
sessions from a single routine**. They pile up in the routine's Runs list, eat
disk, and slow the app down when it loads them.

**routine-sweeper** is a zero-dependency Python CLI that prunes those runs
**unattended** — on a schedule you set — by policy instead of by hand:

- **Keep the newest N runs per routine** (default 10), optionally only ones
  older than `--older-than 7d`.
- **Never touches a run you replied in.** Each transcript is scanned for
  messages a person actually typed (tool results, skill loads, and
  harness-injected entries don't count). Engaged runs stay unless you pass
  `--include-engaged`.
- **Never touches ordinary sessions.** Only sessions whose kickoff message
  is the routine marker `<scheduled-task name="...">` are candidates; your
  normal conversations are invisible to the planner no matter the flags.
- **Never touches live sessions** or runs active in the last 15 minutes.
- **Whitelist** whole routines, session ids, or fnmatch patterns.
- **Dry-run preview** by default: `sweep` prints the plan and deletes nothing
  until you pass `--yes`.

Everything runs locally. Standard library only, no network, no accounts.

## Install

```bash
pip install routine-sweeper
```

or from source:

```bash
git clone https://github.com/hahahahahahahahah6/routine-sweeper
cd routine-sweeper
pip install .
```

## Usage

```bash
routine-sweeper status          # paths, is the app running, counts
routine-sweeper scan            # list routine runs and their status
routine-sweeper sweep           # print the deletion plan (changes nothing)
routine-sweeper sweep --yes     # apply the plan
```

Target one routine, keep fewer runs, age-gate:

```bash
routine-sweeper sweep --routine nightly-report --keep 5 --older-than 7d --yes
```

Protect a routine forever:

```bash
routine-sweeper sweep --whitelist "vip-*" --whitelist "nightly-report" --yes
```

Machine-wide defaults live in `~/.claude/routine-sweeper.json`
(`keep`, `older_than_days`, `include_engaged`, `live_window_minutes`,
`whitelist`) — every CLI flag overrides them. `--json` gives
machine-readable output for `scan` and `sweep`.

### Run it unattended

That's the point — set it and forget it. A cron entry that sweeps while the
app is closed (see *Honest limitations*):

```cron
# every night at 3:05am, keep 10 newest runs per routine
5 3 * * * /usr/local/bin/routine-sweeper sweep --yes >> ~/.claude/sweeper.log 2>&1
```

Or a launchd plist on macOS, or a Windows scheduled task. A Claude Code
routine can also run it — just don't sweep the run you're currently in
(the 15-minute live window already protects that).

## How it works

Sessions are append-only JSONL transcripts under
`~/.claude/projects/<project>/<session-id>.jsonl`. A routine-started session
opens with a kickoff message of the form:

```
<scheduled-task name="my-routine" file="...">
```

The sweeper streams each transcript once: the kickoff identifies the
routine, and any later human-typed message marks the run "engaged".
Deletion removes the transcript, its sidecar directory, and its entry in
the `~/.claude/sessions/` live registry.

## Honest limitations

- **The desktop app keeps its Runs list in memory.** If you delete a run's
  files while the app is running, the app can write its in-memory record
  back to disk and the run comes back as a ghost ("Session not found on
  disk"). So `sweep --yes` **refuses while the app is running**; schedule
  it when the app is closed (quit the app, or run from cron overnight).
  `--force` overrides this if you accept the risk. When the app is closed,
  its in-memory list is rebuilt from disk on next launch, so swept runs
  stay gone.
- **The transcript format is internal and undocumented.** Detection rests on
  the `<scheduled-task name="...">` kickoff marker and the
  `~/.claude/sessions/` registry layout as observed today; a future Claude
  Code release may change them. If detection stops matching, the tool does
  nothing rather than guess.
- **Deletion is permanent.** There is no trash. Engaged-run, live-run, and
  whitelist protections are the safety net — review the plan (`sweep`
  without `--yes`) before automating.
- **While-app-running, instant deletion** is a job for the app itself: the
  [`claude-routine-cleanup`](https://github.com/naw103/claude-routine-cleanup)
  skill deletes through the desktop app's own session tool, so its Runs
  list updates immediately with no restart. See *Differentiation* below.
- Claude Code's own `cleanupPeriodDays` (default 30) already prunes session
  history by age; routine-sweeper is for the runs you want gone sooner, or
  kept deliberately longer than that window.

## Differentiation

Two related tools exist; routine-sweeper is deliberately the other shape.

**vs
[`naw103/claude-routine-cleanup`](https://github.com/naw103/claude-routine-cleanup)
(skill + plugin, by the author of the original Show HN report).** That skill
is interactive: a planner script prints a plan, a human approves it, and
deletion goes through the desktop app's own `delete_session` MCP tool in
batches of 25 with an approval card per batch — correct while the app is
running, but it needs a person at the keyboard, an open app, and a normal
conversation (the tool is unavailable inside scheduled-task runs).
routine-sweeper is a **CLI built to run unattended**: policy-driven (keep N,
age gate, whitelist), dry-run preview, no approval cards, safe to run from
cron/launchd or from inside a routine itself. Use the skill when you want
per-batch human approval with the app open; use the sweeper when you want
the pile to never grow back.

**vs [`sdlc-skills` `config-gc`](https://github.com/m-nweke/sdlc-skills)
(skill).** Different domain and different posture. `config-gc` garbage
collects Claude Code *configuration* bloat (stale skills, hooks, permission
entries), and its core rule is "never delete autonomously" — every
candidate gets its own `[y/n/skip]` prompt. routine-sweeper is about
*routine run sessions*, and its core rule is the opposite: it is designed
for unattended scheduled runs, with protection expressed as policy
(engaged/live/whitelisted runs are never selected) instead of per-item
prompts.

## Development

```bash
python3 -m unittest discover -s tests   # 17 tests, all fixture-based
```

## License

MIT — see [LICENSE](LICENSE).
