#!/usr/bin/env bash
# Sunday re-authentication nudge for the IBKR gateway.
#
# IBKR invalidates the gateway's session token every Sunday ~01:00 ET (≈07:00
# Europe/Stockholm), forcing a fresh 2FA login before the feed works again. Left
# alone, that 2FA demand first surfaces on the Sunday-night auto-restart and, if
# unapproved overnight, leaves the feed down Monday morning — the worst day to lose
# it. This script forces the login attempt (and thus the IBKR Mobile push) at a
# civil Sunday hour and keeps re-sending it until you approve.
#
# It is self-limiting: each run first checks whether the feed is already
# authenticated, and does nothing if so. So the cron below can fire all Sunday and
# only acts while the feed is actually down — once you tap approve, it goes quiet.
#
# Install (marvin crontab — every 15 min on Sundays, 09:00–23:45 local time):
#
#   crontab -e
#   # then add these two lines:
#   CRON_TZ=Europe/Stockholm
#   */15 9-23 * * 0  /home/marvin/3-percent-strategy/sunday_reauth.sh >> /home/marvin/sunday_reauth.log 2>&1
#
# CRON_TZ pins the schedule to Swedish time regardless of the server's system clock.
set -euo pipefail
cd "$(dirname "$0")"

# Healthy = the fetcher has talked to the gateway within the last 3 minutes. On a
# Sunday the US market is closed, so a live gateway connection makes the fetcher
# print "market closed — skipping" every few seconds; a dead/unauthenticated gateway
# only ever prints "connect failed". So any positive marker means the gateway is
# authenticated and there's nothing to do.
if journalctl -u ibkr-fetcher --since "3 min ago" --no-pager \
     | grep -qE "wrote chains|market closed|Connected to 127"; then
  echo "$(date '+%F %T %Z')  feed healthy — 2FA already done, nothing to do"
  exit 0
fi

echo "$(date '+%F %T %Z')  feed down — recreating ib-gateway to trigger the 2FA push"
docker compose up -d --force-recreate ib-gateway

# --- optional: nudge yourself here so you know to open IBKR Mobile ---
# e.g. hand off to Hermes / any notifier of your choice:
#   /home/marvin/notify.sh "IBKR feed down — approve the 2FA push in IBKR Mobile"
