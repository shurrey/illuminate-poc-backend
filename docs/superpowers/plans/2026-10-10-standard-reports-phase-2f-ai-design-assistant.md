# Standard reports, Phase 2f: AI Design Assistant Adoption. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship AI Design Assistant Adoption, one page:
- KPIs for the last completed month vs the month before it: courses using the AI Design Assistant, instructors
  using it, and course items created with it;
- course items created with AI by week, with the share of items created with AI;
- the share of courses using AI (donut);
- AI vs not-AI items by item type (stacked percentage bars);
- courses using AI vs not by child hierarchy node (stacked percentage bars);
- per-course and per-instructor tables (instructors: Admin, Author and Developer only).

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`TEACHING_AI_DESIGN_ASSISTANT_ADOPTION`.

## Global constraints

As in Phase 2a.

## Findings that shape the design

- On the test tenant, `COURSE_ITEM.AI_STATUS` is 'Y' (created with AI), 'N' or null. 5,289 items across 710
  courses and 565 creators are 'Y', and the data is current to October 2026. The source also treats 'P' as AI.
- `COURSE_ITEM.PERSON_ID` is the creator. It is null for every 'N' item.

## Review focus

1. "Courses using AI" counts a course once, whichever of its items used AI.
2. The month KPIs compare whole calendar months: the last completed month and the one before it, whatever the
   report's date range.
3. Instructor identity is never returned to Viewers.
4. Items with a null AI status count as created without AI.

---

### Task 2f.1 (B): `course_item_ai_usage` dataset and month defaults

**Dataset:** one row per undeleted course item, with:
- the course (foreign key to `courses.v1`);
- its type name (from `item_tool_map`, else the type);
- its local creation date;
- `ai_used` (Yes when AI_STATUS is Y or P) and `available`;
- `course_uses_ai` (Yes when any of the course's items used AI);
- the creator's id, name and email (PII).

**Measures:** items, AI items, `pct_ai_items` (0–100), courses, courses using AI, AI creators, and available and
unavailable AI items.

**Defaults:** the date-range filter gains `last_month` (the last completed calendar month) and `month_before_last`.

**Tests first:**
- compile, and identity columns are PII;
- `resolve_defaults` on 2026-10-10 gives 2026-09-01..2026-09-30 and 2026-08-01..2026-08-31, and on 2026-01-15 gives
  2025-12 and 2025-11.

### Task 2f.2 (B): The report

**Filters:**
- item creation date range (no default);
- KPI month and its comparison month (`last_month`, `month_before_last`);
- term (current term);
- duration;
- ih1–4 on `courses.v1`;
- course;
- item type.

**Visuals:** as in the goal. KPIs use period_over_period on the two month filters; the node bars use `child_of`.

**Verification:** the report validates, every query runs live, and a Viewer gets "unavailable" for the instructor
table.
