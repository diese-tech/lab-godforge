# Scheduled Custom Nights

GodForge scheduling is a narrow entry point into the existing party loop. It
does not manage general meetings or require a calendar account.

## Discord workflow

1. Run `/party schedule` with a natural time such as `Friday 8 PM` and an
   explicit IANA timezone such as `America/New_York`.
2. GodForge echoes Discord's normalized absolute and relative timestamps.
   Nothing is published until the organizer runs `/party confirm EVENT_ID`.
3. Players run `/party rsvp EVENT_ID`. Their saved primary, secondary, fill,
   and captain preferences are retained. Capacity overflow uses an ordered
   waitlist; releasing a seat promotes the earliest waiting player.
4. `/party calendar EVENT_ID` downloads a portable ICS file. Weekly nights
   contain an RRULE and create the next GodForge occurrence when opened.
5. GodForge sends configured reminder DMs once, with delivery claims persisted
   across restarts.
6. The organizer runs `/party open-scheduled EVENT_ID`. Retries resolve to the
   same ordinary party lobby, roster, and queue. Existing ready-check, draft,
   room, and results workflows apply unchanged.

Use `/party events` to find event IDs and `/party unrsvp` to release a seat.

## Supported time input

The deliberately small vocabulary is deterministic:

- `2026-08-01 8 PM`
- `tomorrow 20:00`
- `Friday 8:30 PM`

Timezone abbreviations such as `EST` are rejected because they do not identify
daylight-saving behavior. Scheduling depends on the pinned `tzdata` package so
the same input normalizes consistently on Windows and Linux.

## Data and scope

`scheduled_nights`, `scheduled_rsvps`, and `scheduled_reminders` share the
party SQLite database. Conversion uses stable lobby and operation IDs. No
OAuth token, external calendar account, generic attendee model, or separate
live-lobby state machine exists.
