# Heartbeat Schedule
# Copy to runtime/config/heartbeat.md and customize.
#
# The heartbeat daemon checks these items on each tick.
# Items prefixed with a time constraint only run at that time.
# Edit freely — this is plain markdown, not code.

## Every Heartbeat (default: every 30 min during business hours)
- Check inbox/raw/ for new files (transcripts, exports, notes)
- Check GitLab export age — if >7 days, trigger new export

## Daily
- If 8:00 AM Monday: generate weekly status draft
- If 4:00 PM Friday: generate pulse check reminders for team

## Stale Detection
- Flag people with no GitLab/Linear activity in 14+ days
- Flag initiatives with no signals in 14+ days
- Flag inbox items unprocessed for >48 hours
