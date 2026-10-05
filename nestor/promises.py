"""The numbers /privacy promises. Everything that states or enforces one of them reads it from here:
the /privacy and /forget_me texts, the nightly purge (nestor.retention) and the backups."""
from __future__ import annotations

RETENTION_MONTHS = 12                         # a message stays this long after it entered the archive
BACKUP_MAX_AGE_DAYS = 35                      # no backup copy is older than this
BACKUP_WEEKS = -(-BACKUP_MAX_AGE_DAYS // 7)   # 5, as /privacy and FORGOTTEN say it
BACKUP_KEEP_LAST = 2                          # never fewer verified copies than this, however old
