"""Sweep planning and deletion for routine run sessions.

Safety invariants (all enforced here, all covered by tests):

1. Only sessions identified as routine runs (``<scheduled-task name=...>``
   kickoff marker) are ever candidates. Ordinary sessions are invisible to
   the planner, no matter how aggressive the flags.
2. Runs a human replied in ("engaged") are protected unless
   ``include_engaged`` is set.
3. Live sessions (a process in ``~/.claude/sessions/``) are never selected.
4. Runs active within the live window (default 15 minutes) are never
   selected -- the sweeper itself may run inside a routine.
5. Whitelisted routine names, session ids, or fnmatch patterns exclude
   everything they match.
6. Deletion is a preview unless the caller passes ``apply=True``.
"""

import fnmatch
import os
import shutil
import time

from . import scan

# Buckets a candidate run can land in.
SWEEP = "sweep"                    # will be deleted
KEEP_NEWEST = "keep-newest"        # inside keep-N window
KEEP_RECENT = "keep-recent"        # active within live window
KEEP_ENGAGED = "keep-engaged"      # human replied in it
KEEP_LIVE = "keep-live"            # session process alive
KEEP_WHITELIST = "keep-whitelist"  # matched whitelist
KEEP_YOUNG = "keep-young"          # younger than older_than_days


def whitelisted(session, whitelist):
    for pat in whitelist or []:
        pat = str(pat)
        if (fnmatch.fnmatchcase(session.routine_name or "", pat)
                or fnmatch.fnmatchcase(session.session_id, pat)
                or session.session_id.startswith(pat)):
            return True
    return False


def plan(claude_dir, keep=10, older_than_days=None, include_engaged=False,
         live_window_minutes=15, whitelist=None, routine=None):
    """Return (candidates, protected) lists of (Session, bucket, reason)."""
    now = time.time()
    live = scan.live_sessions(claude_dir)
    live_window = live_window_minutes * 60
    older_than = (older_than_days * 86400) if older_than_days else None

    runs = [s for s in scan.iter_sessions(claude_dir) if s.is_routine_run]
    if routine:
        runs = [s for s in runs if s.routine_name == routine]

    # Newest-first per routine; the first `keep` of each routine survive.
    by_routine = {}
    for s in runs:
        by_routine.setdefault(s.routine_name, []).append(s)
    survivors = set()
    for name, group in by_routine.items():
        group.sort(key=lambda s: s.mtime, reverse=True)
        for s in group[:max(keep, 0)]:
            survivors.add(s.session_id)

    candidates, protected = [], []
    for s in runs:
        if s.session_id in survivors:
            protected.append((s, KEEP_NEWEST, "inside keep-newest-%d window" % keep))
            continue
        if s.session_id in live:
            protected.append((s, KEEP_LIVE, "session process is alive"))
            continue
        if now - s.mtime < live_window:
            protected.append((s, KEEP_RECENT,
                              "active within the last %d minutes" % live_window_minutes))
            continue
        if s.is_engaged and not include_engaged:
            protected.append((s, KEEP_ENGAGED,
                              "%d human repl%s -- pass --include-engaged to allow"
                              % (s.human_replies,
                                 "y" if s.human_replies == 1 else "ies")))
            continue
        if whitelisted(s, whitelist):
            protected.append((s, KEEP_WHITELIST, "matched whitelist"))
            continue
        if older_than is not None and now - s.mtime < older_than:
            protected.append((s, KEEP_YOUNG,
                              "younger than --older-than %s" % _fmt_days(older_than_days)))
            continue
        candidates.append((s, SWEEP, "old autonomous routine run"))
    return candidates, protected


def _fmt_days(days):
    return "%dd" % days if float(days).is_integer() else "%s days" % days


def delete_session(claude_dir, session, live_registry):
    """Delete one routine run: transcript, sidecar dir, registry entry.

    Returns a list of removed paths (for the report).
    """
    removed = []
    for path in (session.path,):
        if os.path.isfile(path):
            os.remove(path)
            removed.append(path)
    sidecar = os.path.join(os.path.dirname(session.path), session.session_id)
    if os.path.isdir(sidecar):
        shutil.rmtree(sidecar)
        removed.append(sidecar + "/")
    reg = live_registry.get(session.session_id)
    if reg is None:
        # stale registry entry (dead process) is also cleaned up
        reg = scan.read_registry(claude_dir).get(session.session_id)
    if reg:
        try:
            os.remove(reg["path"])
            removed.append(reg["path"])
        except OSError:
            pass
    return removed


def apply(claude_dir, candidates):
    """Delete every candidate; return {session_id: [removed paths]}."""
    live = scan.live_sessions(claude_dir)
    report = {}
    for session, _bucket, _reason in candidates:
        report[session.session_id] = delete_session(claude_dir, session, live)
    return report
