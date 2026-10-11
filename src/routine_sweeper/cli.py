"""Command-line interface for routine-sweeper. Standard library only."""

import argparse
import json
import os
import sys
import time

from . import __version__, config, scan, sweep


def default_claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")


def parse_older_than(value):
    v = str(value).strip().lower()
    if v.endswith("d"):
        v = v[:-1]
    try:
        days = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected a number of days, e.g. 7 or 7d")
    if days < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return days


def fmt_age(mtime, now):
    secs = max(0, now - mtime)
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def build_parser():
    p = argparse.ArgumentParser(
        prog="routine-sweeper",
        description="Unattended cleanup of Claude Code routine (scheduled "
                    "task) run sessions. Only sessions started by a routine "
                    "are ever touched; ordinary sessions are invisible.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    p.add_argument("--claude-dir", default=None,
                   help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="list routine run sessions and their status")
    s.add_argument("--routine", help="only this routine name")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    _policy_args(s)

    w = sub.add_parser("sweep", help="delete old routine runs by policy "
                                     "(preview unless --yes)")
    w.add_argument("--routine", help="only this routine name")
    w.add_argument("--yes", action="store_true",
                   help="actually delete; without it, only a plan is printed")
    w.add_argument("--dry-run", action="store_true",
                   help="print the plan and never delete (implies preview)")
    w.add_argument("--force", action="store_true",
                   help="delete even while the Claude desktop app is running "
                        "(not recommended: the app may restore its Runs list "
                        "from memory)")
    w.add_argument("--json", action="store_true", help="machine-readable output")
    _policy_args(w)

    sub.add_parser("status", help="show paths, app state, and counts")
    return p


def _policy_args(parser):
    parser.add_argument("--keep", type=int, default=None,
                        help="newest runs kept per routine (default: 10)")
    parser.add_argument("--older-than", type=parse_older_than, default=None,
                        metavar="DAYS",
                        help="only delete runs older than this, e.g. 7 or 7d")
    parser.add_argument("--include-engaged", action="store_true", default=None,
                        help="also delete runs a human replied in")
    parser.add_argument("--live-window-minutes", type=int, default=None,
                        help="runs active this recently are never touched "
                             "(default: 15)")
    parser.add_argument("--whitelist", action="append", default=None,
                        metavar="PATTERN",
                        help="protect a routine name, session id, or fnmatch "
                             "pattern (repeatable)")


def resolve_policy(args, cfg):
    return {
        "keep": args.keep if args.keep is not None else cfg["keep"],
        "older_than_days": (args.older_than if args.older_than is not None
                            else cfg["older_than_days"]),
        "include_engaged": (args.include_engaged if args.include_engaged is not None
                            else cfg["include_engaged"]),
        "live_window_minutes": (args.live_window_minutes
                                if args.live_window_minutes is not None
                                else cfg["live_window_minutes"]),
        "whitelist": (args.whitelist if args.whitelist is not None
                      else cfg["whitelist"]),
        "routine": args.routine,
    }


def cmd_scan(args, claude_dir, policy):
    candidates, protected = sweep.plan(claude_dir, **policy)
    now = time.time()
    if args.json:
        print(json.dumps({
            "claude_dir": claude_dir,
            "sweep": [_row(s, now) for s, _b, _r in candidates],
            "protected": [
                dict(_row(s, now), bucket=b, reason=r)
                for s, b, r in protected],
        }, indent=2))
        return 0
    groups = {}
    for s, _b, _r in candidates:
        groups.setdefault(s.routine_name, []).append(s)
    total = len(candidates) + len(protected)
    print("routine run sessions: %d total, %d would be swept, %d protected"
          % (total, len(candidates), len(protected)))
    for name in sorted(groups):
        print("\n[%s] %d to sweep" % (name, len(groups[name])))
        for s in sorted(groups[name], key=lambda x: x.mtime):
            print("  %s  age=%s size=%s replies=%d%s" % (
                s.session_id, fmt_age(s.mtime, now), fmt_size(s.size),
                s.human_replies,
                " ENGAGED" if s.is_engaged else ""))
    if protected and not args.json:
        print("\nprotected:")
        for s, bucket, reason in sorted(protected, key=lambda t: t[0].mtime):
            print("  %s  %s: %s" % (s.session_id, bucket, reason))
    return 0


def _row(s, now):
    return {
        "session_id": s.session_id,
        "routine": s.routine_name,
        "project": s.project_slug,
        "age": fmt_age(s.mtime, now),
        "size": s.size,
        "human_replies": s.human_replies,
        "path": s.path,
    }


def cmd_sweep(args, claude_dir, policy):
    candidates, protected = sweep.plan(claude_dir, **policy)
    now = time.time()
    if args.json:
        print(json.dumps({
            "claude_dir": claude_dir,
            "dry_run": not args.yes,
            "sweep": [_row(s, now) for s, _b, _r in candidates],
            "protected": [
                dict(_row(s, now), bucket=b, reason=r)
                for s, b, r in protected],
        }, indent=2))
        if args.yes and not args.dry_run:
            return _do_sweep(args, claude_dir, candidates)
        return 0

    print("plan: %d routine run(s) to delete, %d protected"
          % (len(candidates), len(protected)))
    freed = sum(s.size for s, _b, _r in candidates)
    for s, _b, _r in sorted(candidates, key=lambda t: t[0].mtime):
        print("  DEL %s  [%s] age=%s size=%s replies=%d" % (
            s.session_id, s.routine_name, fmt_age(s.mtime, now),
            fmt_size(s.size), s.human_replies))
    for s, bucket, reason in sorted(protected, key=lambda t: t[0].mtime):
        print("  KEEP %s  %s: %s" % (s.session_id, bucket, reason))
    print("would free ~%s" % fmt_size(freed))

    if not candidates:
        print("nothing to do.")
        return 0
    if not args.yes or args.dry_run:
        print("\ndry run: nothing deleted. Re-run with --yes to apply.")
        return 0
    return _do_sweep(args, claude_dir, candidates)


def _do_sweep(args, claude_dir, candidates):
    if scan.app_running(claude_dir) and not args.force:
        print("refusing: the Claude desktop app appears to be running.\n"
              "Its Runs list lives in memory and can be written back to disk,\n"
              "which would restore the deleted runs as ghosts. Quit the app\n"
              "(e.g. run this from cron/launchd when the app is closed) or\n"
              "re-run with --force if you accept that risk.",
              file=sys.stderr)
        return 3
    report = sweep.apply(claude_dir, candidates)
    total = 0
    for sid, removed in report.items():
        print("deleted %s (%d path%s)" % (
            sid, len(removed), "" if len(removed) == 1 else "s"))
        for path in removed:
            print("    - %s" % path)
        total += len(removed)
    print("done: %d session(s), %d path(s) removed." % (len(report), total))
    return 0


def cmd_status(args, claude_dir):
    cfg = config.load(claude_dir)
    n_routine = n_other = 0
    for s in scan.iter_sessions(claude_dir):
        if s.is_routine_run:
            n_routine += 1
        else:
            n_other += 1
    live = scan.live_sessions(claude_dir)
    print("claude dir:      %s" % claude_dir)
    print("desktop app:     %s" % ("RUNNING" if scan.app_running(claude_dir)
                                   else "not running"))
    print("routine runs:    %d" % n_routine)
    print("other sessions:  %d (never touched)" % n_other)
    print("live processes:  %d" % len(live))
    print("policy: keep=%s older_than_days=%s include_engaged=%s "
          "live_window_minutes=%s whitelist=%s" % (
              cfg["keep"], cfg["older_than_days"], cfg["include_engaged"],
              cfg["live_window_minutes"], cfg["whitelist"]))
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    claude_dir = args.claude_dir or default_claude_dir()
    if args.command == "status":
        return cmd_status(args, claude_dir)
    cfg = config.load(claude_dir)
    policy = resolve_policy(args, cfg)
    if policy["keep"] < 0:
        print("error: --keep must be >= 0", file=sys.stderr)
        return 2
    if args.command == "scan":
        return cmd_scan(args, claude_dir, policy)
    if args.command == "sweep":
        return cmd_sweep(args, claude_dir, policy)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    sys.exit(main())
