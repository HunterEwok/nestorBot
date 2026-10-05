"""The watchdog: one oneshot run every 5 minutes (deploy/systemd/nestor-watchdog.timer) that checks the
collector, the database, Hermes, the backups, the retention and the Pi, and is the only sender of alerts
to ALERT_CHAT_ID. Its state is in its StateDirectory, so it survives database outages and reboots; its
snapshot in ops_state('watchdog') is what /status shows. See run.py for one run, alerts.py for when a
problem is said, checks.py for what is looked at."""
