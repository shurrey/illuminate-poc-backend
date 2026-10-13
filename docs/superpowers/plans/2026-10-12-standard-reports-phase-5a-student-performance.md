# Standard reports, Phase 5a: Student Performance & Grades. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Student Performance & Grades: a grade distribution page, and a grade boundary page driven by a
number control (default 50%).

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEARNING_STUDENT_PERFORMANCE_AND_GRADES`.

## Review focus

1. Grade statistics cover graded enrollments only. Not-graded enrollments are counted separately.
2. The boundary applies only to the boundary visuals. At or above plus below equals graded, per node and course.
3. Per-student tables require identity.

### Task 5a.1 (B): `student_grade` gains a numeric grade value, band order, graded status and min/max.

### Task 5a.2 (B): The report. Joins of all vs at-or-above give below and the pass rate.
