# Standard reports, Phase 2e: Course Administration. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Course Administration, one page and five visuals:
- the proportion of courses ready to start (donut);
- the actions needed to get courses ready (table of readiness-measure combinations);
- readiness by the child nodes of the selected hierarchy node (stacked percentage bars);
- readiness measures by node and term (table);
- the courses that are or are not ready (table).

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEADING_COURSE_ADMINISTRATION`.

## Global constraints

As in Phase 2a.

## Definitions

A course is **ready to start** when all three of these hold:
- at least one instructor is enrolled (roles I or O);
- at least one student is enrolled;
- at least one course item has been created or modified since the course was created.

Availability to students is shown alongside but is not part of readiness. Deleted courses and enrollments are
excluded.

## Review focus

1. A course appears once: readiness counts never multiply by enrollments or items.
2. "Child nodes" follow the hierarchy filter. No level chosen groups by level 1, level 1 chosen groups by level
   2, and so on, with level 4 the floor.
3. A course with no items, or no enrollments, is "Not ready" and shows zero counts, never null.
4. The per-course table caps at 1,000 rows and says so. The course filter finds any course.

---

### Task 2e.1 (B): `course_readiness` dataset

One row per undeleted course, with a foreign `course` entity, so the term, duration, creation date, hierarchy
levels, number and name come from `courses.v1`.

**Columns:**
- instructor, student and available-student counts;
- item and new-or-updated item counts, and the new-or-updated share;
- Yes/No flags: `instructor_enrolled`, `students_enrolled`, `items_updated`, `available`;
- `readiness` (Ready / Not ready).

**Measures:**
- `courses`, `ready_courses`;
- `pct_ready` and `pct_not_ready` (0–100 over courses);
- maxima of the counts and the share, for per-course rows.

**Tests first:**
- it compiles;
- grouping by `readiness` with `courses.v1` dimensions joins without fan-out (a LEFT JOIN on the course primary
  key);
- zero counts use `ZEROIFNULL`/`NVL`.

### Task 2e.2 (B): Child-node grouping

- A query may set `child_of: {filters: [ih1..ih4], dimensions: [l1..l4], as: node}`. The query is grouped by
  `dimensions[k]`, where k is the number of leading `filters` with a value (at most the last index).
- `/run` renames that column to `as`, so the encode stays stable.
- Validation: the filters exist, the dimensions resolve, and the two lists are the same length.
- **Tests first:**
  - no level gives l1;
  - ih1 set gives l2;
  - ih1 to ih4 set gives l4;
  - the rename is applied.

### Task 2e.3 (B): The report

**Filters:**
- course creation date range, with no default;
- term (current term);
- course duration;
- ih1–4 on `courses.v1` primary-node levels;
- course (multi-select on `course_number`).

**Visuals:** as in the goal. The node bars use `child_of`, with `pct_ready` and `pct_not_ready` stacked.

**Verification:** the report validates, and every query runs live.
