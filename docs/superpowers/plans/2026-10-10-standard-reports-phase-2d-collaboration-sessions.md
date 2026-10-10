# Standard reports, Phase 2d: Collaboration Session Activity. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Collaboration Session Activity: three pages, primary vs comparison ranges.
- **Session Activity and Duration:** sessions launched, active rooms and minutes by day; average, median, total,
  maximum and minimum session length vs the comparison range.
- **Session Attendance:** unique attendees and attendances by day; average, median, maximum and minimum attendees
  per session vs the comparison range.
- **Session Engagement:** attendee status and chat type donuts; chat messages, sessions with raised hands, hands
  raised, polls shown, sessions using polls and sessions using the whiteboard, each by day and as a KPI.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEADING_COLLABORATION_SESSION_ACTIVITY`.

## Global constraints

As in Phase 2a.

## Findings that shape the design

- `CDM_TLM.COLLAB_EVENTS` has no session column. An event's session is `DATA:contextId` when
  `DATA:contextType = 'SESSION_INSTANCE'`, else `DATA:objectId`. It matches `CDM_CLB.SESSION.STAGE:session_instance_uid`
  for 100% of the chat, hand, poll, whiteboard and status events on the test tenant.
- `SESSION.ATTENDED_DURATION` is in seconds.
- A room can link to several courses, so a session can belong to several terms. The term filter therefore uses a
  bridge dataset (one row per session × linked course) and the existing semi-join on `collab_session`.
- Most activity on the test tenant is 2020–2023, so the default last-30-days range is near empty. That is real
  data.

## Review focus

1. Per-session statistics (average, median, maximum and minimum length and attendance) are over sessions, never
   over attendance or event rows.
2. The term filter narrows sessions linked to a course in that term. It must not multiply sessions linked to
   several courses.
3. "Exclude sessions not in a course" removes sessions whose room has no linked course, everywhere.
4. Event counts come only from the event types each visual names. The 13M network-stats rows never enter the
   dataset.
5. The minimum-attendees control applies to the per-session attendance KPIs only, as in the source.

---

### Task 2d.1 (B): Collaborate session, session-course and event datasets

- **`dataset.collab_sessions.v1`**, one row per session (not deleted). It has:
  - `start_date`, local to the instance;
  - `room`;
  - `minutes` (attended duration / 60);
  - `attendee_count` (distinct attendances);
  - `in_course` ('Yes' / 'No': the room links to at least one course);
  - `ih_nodes`.

  Measures: sessions, rooms, total, average, median, maximum and minimum minutes, and average, median, maximum
  and minimum attendees.
- **`dataset.collab_session_courses.v1`**, one row per session × linked course, with `term_name`. It is used only as
  a filter target.
- **`dataset.collab_events.v1`**, one row per chat, hand, poll, whiteboard or status event. It has:
  - `event_date`, local;
  - `event_type`;
  - `event_group` (Chat, Hand raised, Poll, Whiteboard, Status);
  - `event_label` (Everyone, Moderators, Private, Other group, Away, Available …);
  - `in_course`;
  - `ih_nodes`.

  Measures: `events`, `sessions`, `hands_raised` (HAND_RAISED events), `polls_shown` (POLL_VISIBLE with result
  true).
- `collab_attendance` gains `in_course`.
- **Tests first:**
  - each dataset compiles;
  - a term filter on `collab_session_courses:term_name` from a sessions, events or attendance query compiles to a
    semi-join on the session id;
  - session-level measures aggregate the session dataset with no join.

### Task 2d.2 (B): The report

**`canonical/reports/collaboration_session_activity.yaml`**

**Filters:**
- primary and comparison date ranges (last 30 days and previous 30 days);
- `in_course`: select, "Exclude sessions not in a course", Yes / No, default No;
- term: multi-select, through the bridge;
- ih1–4: path, cascading, as in Platform Adoption;
- `min_attendees`: number, default 1, applied through `param_filters` to the attendance KPIs only.

**Visuals:** as in the goal. KPIs use period_over_period, and by-day lines and bars come in a primary and a
comparison pair. Each page has its help text.

**Verification:** the report validates, and every query runs live with no unexpected `ignored_filters`.
