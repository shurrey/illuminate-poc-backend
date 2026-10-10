# Standard reports, Phase 2c: Assessment & Grades. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Assessment & Grades, 9 visuals in all:
- a response-time histogram;
- a hierarchy-node comparison;
- a per-course table;
- grading by submission type;
- inside vs outside the expected grading time (a donut, and a stacked bar by node);
- ungraded count and share.

All of these turn on a user-set "Expected grading time (days)" threshold.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. Source: the inventory, section `TEACHING_ASSESSMENT_GRADES`.

## Review focus

1. "Inside the grading time" is graded within K days. Ungraded submissions count as outside, as in the source.
2. Changing K changes every threshold visual, and nothing else.
3. Grouping by node uses each course's primary node only, so it never multiplies submissions.
4. The per-course table's "% graded within the expected time" is graded-within-K ÷ graded, per course.

### Task 2c.1 (B): Number controls, per-query parameter filters, primary-node levels, a response-days bucket

- **Number control.** `ReportFilter.control: "number"`, with a numeric `default`. A query's
  `param_filters: [{dimension, op, param}]` turns into a contract filter using the parameter's value.
- **Primary-node levels.** `courses.v1` gains `ih_level_1` to `ih_level_4` for the course's primary hierarchy node
  (`INSTITUTION_HIERARCHY_COURSE.PRIMARY_IND`). Level 1 is "No hierarchy node" when there is none; deeper levels are
  "-".
- **Bucket.** `grade_response_time` gains `response_days_bucket` ("0" … "90", "91+").
- **Tests first:**
  - a parameter filter takes the URL value or the default;
  - an unknown `param` fails validation;
  - the new dimensions compile.

### Task 2c.2 (F): The number control and the `join` transform

- **FilterBar** renders `number` filters as a numeric input.
- **`join`** `{queries: [names], on: [cols], ratios?: {name: [numerator col, denominator col]}}` merges the queries'
  rows on the key columns, keeps every column, and adds the ratio columns. The denominator 0 gives null.
- **Vitest tests first.**

### Task 2c.3 (B): The report

`canonical/reports/assessment_grades.yaml`.

**Filters:**
- date range on course start, as an overlap;
- term, course duration, hierarchy levels 1–4 (`course_filters_ih`, cascading), item type, course ID;
- expected grading time (default 21).

**Visuals:** the histogram, node × term comparison table, per-course table (`join`), grading by item type, the
inside/outside donut (`part_of_whole`), the stacked bar by level-1 node (`side_by_side` of inside and outside), the two
KPIs, and help text.

**Verification:** the report validates and every query runs live; changing K moves only the threshold visuals.
