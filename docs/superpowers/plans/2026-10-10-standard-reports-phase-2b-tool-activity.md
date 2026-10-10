# Standard reports, Phase 2b: Learning Tool Activity & Use. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Learning Tool Activity & Use, which has two pages and 10 visuals:
- weekday × 3-hour heat maps of minutes and of distinct users;
- minutes and users by date;
- users and minutes KPIs vs a comparison range;
- users by role (top 5 plus Other);
- courses using vs not using learning tools.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEADING_LEARNING_TOOL_ACTIVITY_AND_USE`.

## Global constraints

As in Phase 2a.

## Review focus

1. The heat-map hours and weekdays must be the institution's local time, not UTC.
2. The users heat map is the average of daily distinct users. It must not be distinct users over the whole range
   divided by the number of days.
3. "Not using tools" must be active courses in the range minus those with tool activity, and never negative.
4. The role filter applies to the user visuals only, as in the source. The tool filter applies everywhere.

---

### Task 2b.1 (B): Local-time weekday and 3-hour groups on course tool activity

- `course_tool_activity` takes its activity hour in the course instance's timezone.
- It gains dimensions `activity_date` (local, day to year), `day_of_week` (Sun to Sat), `hour_group`
  ("12 AM–3 AM" … "9 PM–12 AM") and `hour_group_start` (0, 3 … 21).
- **Tests first:** the compiled CTE has `CONVERT_TIMEZONE`, and grouping by the new dimensions compiles. Smoke it
  live after deploy.

### Task 2b.2 (B): Date overlap, and the `average_by` and `part_of_whole` kinds

- A visual query may set `time_overlap: {start: <dim>, end: <dim>}`. The date-range filter then applies as
  `start ≤ range end AND end ≥ range start` (two contract filters), instead of a `time_range`.
- `TRANSFORM_KINDS` gains `average_by` and `part_of_whole`.
- **Tests first:**
  - overlap gives `lte` and `gte` filters on the two dimensions;
  - validation checks that both dimensions exist;
  - the two new kinds are accepted.

### Task 2b.3 (F): The `average_by` and `part_of_whole` transforms

- **`average_by`** `{query, field, by: [cols]}`: the mean of `field` over the rows sharing the `by` values. Used for
  daily totals averaged per weekday and slot.
- **`part_of_whole`** `{whole, part, field, labels: [part label, remainder label]}`: two rows, the part and
  whole − part, floored at 0.
- Vitest tests first, including a remainder floored at 0 and an empty part.

### Task 2b.4 (B): The report

**`canonical/reports/learning_tool_activity_and_use.yaml`**

**Filters:**
- primary and comparison date ranges;
- role: `course_role_description`, single-select, listed in `filters_ignored` on the course page;
- tool: `tool_name`, single-select.

**Visuals:**
- **User Activity:**
  - minutes heat map: daily minutes by `activity_date`, `day_of_week` and `hour_group`, then `average_by` over
    `day_of_week` and `hour_group`;
  - users heat map: the same with `people`;
  - minutes by date (line);
  - distinct users (KPI vs comparison);
  - users by role (top 5 plus Other, donut);
  - users by date (line);
  - total minutes (KPI vs comparison);
  - two help texts.
- **Course Activity:** courses using vs not using tools (donut). It has two queries:
  - courses overlapping the range, from `courses.v1` with the live and top-level filters;
  - courses with tool activity in the range.

  These feed `part_of_whole`.

**Verification:** the report validates, every query runs live, and the role filter is ignored only on the course page.
