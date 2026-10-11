"""Scanning Claude Code session transcripts for routine (scheduled task) runs.

How detection works (facts about the on-disk format as observed; the format
is internal and undocumented, so treat field names as today's facts, not a
contract):

- Every session is an append-only JSONL file:
      ~/.claude/projects/<project-slug>/<session-id>.jsonl
- A session started by a scheduled task opens with a first user message of
  the form:  <scheduled-task name="<routine-name>" file="...">
  Any session whose kickoff message matches that marker is a routine run.
  Sessions whose first human message is anything else are ordinary sessions
  and are NEVER selected by the sweeper.
- A live-session registry lives at:
      ~/.claude/sessions/*.json
  mapping live processes to session ids. Entries carry an `entrypoint`
  field (e.g. "claude-desktop"); a run with a live process is never
  selected for deletion.
"""

import json
import os
import re

SCHEDULED_TASK_RE = re.compile(r'^<scheduled-task\s+name="([^"]+)"')

# Prefixes of user message text that are harness-injected, not human-typed.
_INJECTED_PREFIXES = (
    "<scheduled-task",   # the routine kickoff itself
    "<system-reminder>",
    "<task-notification>",
    "<local-command-",
)


def _user_texts(path):
    """Return (kickoff_text, human_reply_count) for a transcript.

    Streams the whole file once (line by line, cheap string pre-filter
    before json parsing): the kickoff is the first user message, and a
    human reply can appear anywhere later in the run, so every user message
    is checked. Tool results and harness meta entries never count.
    """
    kickoff_text = None
    human_replies = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type"' not in line or '"user"' not in line:
                    # fast path: assistant/tool/summary lines skipped
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "user":
                    continue
                if rec.get("isMeta") or rec.get("toolUseResult"):
                    continue
                content = rec.get("message", {}).get("content")
                if kickoff_text is None:
                    kickoff_text = _blocks_to_text(content)
                elif _is_human_reply(content):
                    human_replies += 1
    except OSError:
        pass
    return kickoff_text, human_replies


def _is_human_reply(content):
    """True when a user message looks like something a person actually typed."""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts)
    elif isinstance(content, str):
        text = content
    else:
        return False
    text = text.strip()
    if not text:
        return False
    if text.startswith(_INJECTED_PREFIXES):
        return False
    if text.startswith("<command-name>"):  # typed slash commands count
        text = text  # a person typed a slash command -> human reply
    return True


class Session:
    """One on-disk session transcript."""

    def __init__(self, path, session_id, project_slug):
        self.path = path
        self.session_id = session_id
        self.project_slug = project_slug
        try:
            st = os.stat(path)
            self.mtime = st.st_mtime
            self.size = st.st_size
        except OSError:
            self.mtime = 0.0
            self.size = 0
        self.routine_name = None
        self.human_replies = 0
        self._analyze()

    def _analyze(self):
        kickoff_text, human_replies = _user_texts(self.path)
        m = SCHEDULED_TASK_RE.match((kickoff_text or "").strip())
        if m:
            self.routine_name = m.group(1)
        # A run is "engaged" when a person typed something after the kickoff.
        self.human_replies = human_replies

    @property
    def is_routine_run(self):
        return self.routine_name is not None

    @property
    def is_engaged(self):
        return self.human_replies > 0


def _blocks_to_text(content):
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return content if isinstance(content, str) else ""


def read_registry(claude_dir):
    """Map session_id -> info for every entry in ~/.claude/sessions/.

    info = {"path": ..., "record": ..., "alive": bool}. Entries whose pid
    is gone are kept but marked dead -- the desktop app leaves these stale
    entries behind, and the sweeper should clean them up, not protect them.
    """
    entries = {}
    reg_dir = os.path.join(claude_dir, "sessions")
    try:
        names = os.listdir(reg_dir)
    except OSError:
        return entries
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(reg_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        sid = rec.get("sessionId")
        if not sid:
            continue
        pid = rec.get("pid")
        entries[sid] = {
            "path": path,
            "record": rec,
            "alive": pid is None or _pid_alive(pid),
        }
    return entries


def live_sessions(claude_dir):
    """Map session_id -> registry record for sessions with a live process.

    `~/.claude/sessions/*.json` is the registry of currently running
    sessions. Each file names a pid; we verify the pid is still alive.
    Stale entries (dead pid) are excluded -- they are not protected, and
    the sweeper removes them along with the transcript.
    """
    return {sid: info for sid, info in read_registry(claude_dir).items()
            if info["alive"]}


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    # /proc check first (Linux); fall back to os.kill signal 0.
    if os.path.isdir("/proc/%d" % pid):
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except ValueError:
        return False
    return True


def iter_sessions(claude_dir):
    """Yield a Session for every transcript under <claude_dir>/projects/."""
    projects = os.path.join(claude_dir, "projects")
    try:
        slugs = os.listdir(projects)
    except OSError:
        return
    for slug in sorted(slugs):
        slug_dir = os.path.join(projects, slug)
        if not os.path.isdir(slug_dir):
            continue
        try:
            names = os.listdir(slug_dir)
        except OSError:
            continue
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            session_id = name[:-len(".jsonl")]
            yield Session(os.path.join(slug_dir, name), session_id, slug)


def app_running(claude_dir):
    """True when the Claude desktop app appears to be running.

    The desktop app keeps its Runs list in memory and can write a run's
    record back to disk when it saves; deleting a run's files while the app
    is running leaves "Session not found on disk" ghosts. Sweeping is only
    safe when the app is closed (the in-memory list is then rebuilt from
    disk on next launch).
    """
    for sid, info in live_sessions(claude_dir).items():
        if info["record"].get("entrypoint") == "claude-desktop":
            return True
    return False
