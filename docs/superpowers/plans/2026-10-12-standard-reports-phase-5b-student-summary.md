# Standard reports, Phase 5b: Student Summary. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Student Summary: one chosen student across three pages.
- **Overview:** courses, an access heat map, submission status, and hours vs peers.
- **Engagement and Performance:** timeliness per course, grade vs peers, the per-course table, and the
  per-assessment table.
- **Activity Log:** events by type, by week, and the log itself.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEARNING_STUDENT_SUMMARY`.

## Review focus

1. Every visual is about the chosen student, and the report runs nothing until a student is chosen.
2. Peer figures are the averages of the other enrollments in the same course (window columns), never the
   institution.
3. The whole report is identity: every visual requires it, so Viewers get "unavailable".

### Task 5b.1 (B): Datasets

- **`persons`** gains `person_id_name` ("<institution id> - <first> <last>", PII).
- **`student_enrollments`** and **`student_grade`** gain course-average (peer) columns.
- **New `student_grade_items`:** one row per student enrollment × gradebook column, with the item, type, due and
  attempt times, normalized score, status, and the course's average score on that item.

### Task 5b.2 (B): The report

- **Required filter:** the student (`persons.person_id_name`).
- **Other filters:** course, term, and an activity date range (activity log and tool activity only).
