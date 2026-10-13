# Standard reports, Phase 4c: Course Summary. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Course Summary: one chosen course across four pages (Overview, Student Engagement and Performance,
Instructional Practice, Course Design), built from the existing course, enrollment, grading, tool-activity and
item datasets.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`TEACHING_COURSE_SUMMARY`.

## Review focus

1. Every query is scoped to the chosen course, and nothing is silently ignored.
2. The activity date range narrows only the activity-over-time visuals (and grading by due week). Course-level
   figures declare that they ignore it.
3. The heat maps average per weekday over the days in the range.
4. The per-person tables require identity.

### Task 4c.1 (B): The report

- **Filters:** a course (single select on `courses.v1:course_id_name`) and an activity date range.
- **Data:** one row per student enrollment gains an hours-per-week bin.
