"""routine-sweeper: unattended cleanup of Claude Code routine (scheduled task) run sessions.

Every scheduled-task run leaves a session behind in the Claude desktop app's
Runs list, and the app only lets you delete them one at a time. This package
scans the local session transcripts, identifies runs started by routines, and
prunes the old ones by policy (keep newest N, protect runs a human replied
in), with a dry-run preview and a whitelist.
"""

__version__ = "0.1.0"
