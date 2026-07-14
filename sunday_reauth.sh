#!/usr/bin/env bash
# Sunday re-authentication nudge for the IBKR gateway.
#
# IBKR invalidates the gateway's session token every Sunday ~01:00 ET (≈07:00
# Europe/Stockholm), forcing a fresh 2FA login before the feed works again. Left
# alone, that 2FA demand first surfaces on the Sunday-night auto-restart and, if
# unapproved overnight, leaves the feed down Monday morning — the worst day to lose
# it. This forces the login (and thus the IBKR Mobile push) at a civil Sunday hour.
#
# Behaviour:
#   * Sunday      — force ONE re-auth (the weekly token is always dead by 09:00), then
#                   nudge every 15 min until the feed is back, then stay quiet all day.
#   * Other days  — do NOTHING if the feed is healthy; only recreate on a *clear*
#                   "connect failed" signal. So a manual test on a live feed is a no-op
#                   and never forces a needless 2FA.
#
# Install (marvin crontab — every 15 min on Sundays, 09:00–23:45 local time):
#   crontab -e
#   CRON_TZ=Europe/Stockholm
#   */15 9-23 * * 0  /home/marvin/3-percent-strategy/sunday_reauth.sh >> /home/marvin/sunday_reauth.log 2>&1
#
# NOTE: no `grep -q` on a journalctl pipe — that closes the pipe early, journalctl
# takes SIGPIPE (141), and `pipefail` makes the check fail even on a match. We read
# the journal into a variable and match with a here-string instead.
set -uo pipefail
cd "$(dirname "$0")"

STAMP="${HOME}/.sunday_reauth.day"   # YYYY-MM-DD of the last forced recreate
log() { echo "$(date '+%F %T %Z')  $*"; }

recreate() {
  log "$1 — forcing gateway login; approve the 2FA push in IBKR Mobile"
  date +%F > "$STAMP"
  docker compose up -d --force-recreate ib-gateway
}

# One journal read, matched via here-strings (no pipe into grep -> no SIGPIPE).
recent="$(journalctl -u ibkr-fetcher --since '3 min ago' --no-pager 2>/dev/null || true)"

heartbeat=no   # feed is talking to the gateway right now
grep -qE 'wrote chains|market closed|Connected to 127' <<<"$recent" && heartbeat=yes
failing=no     # feed is clearly unable to reach the gateway
grep -qE 'connect failed|API connection failed|Lost gateway' <<<"$recent" && failing=yes

if [ "$(date +%u)" -eq 7 ]; then           # Sunday
  if [ "$(cat "$STAMP" 2>/dev/null || true)" = "$(date +%F)" ] && [ "$heartbeat" = yes ]; then
    log "Sunday re-auth already done and feed live — nothing to do"
    exit 0
  fi
  # First run today (refresh even if the stale-token session still streams), or we've
  # recreated but the feed hasn't come back yet (2FA not approved).
  recreate "Sunday weekly re-auth"
  exit 0
fi

# Any other day: only step in on a genuine outage; never touch a healthy/uncertain feed.
if [ "$heartbeat" = yes ]; then
  log "feed live — nothing to do"
elif [ "$failing" = yes ]; then
  recreate "feed down (connect failing)"
else
  log "no heartbeat but no failure markers — uncertain, leaving gateway untouched"
fi
