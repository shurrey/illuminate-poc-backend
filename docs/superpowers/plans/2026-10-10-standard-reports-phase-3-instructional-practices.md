# Standard reports, Phase 3: Instructional Practices. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Instructional Practices: five pages on how instructors support learning.
- **Class Size:** students per instructor, per course.
- **Course Access:** how often and how recently instructors access their courses.
- **Course Design and Organization:** course item counts by group and type.
- **Learning Tools Engagement:** instructor interactions and contributions in learning tools.
- **Virtual Classroom Engagement:** Collaborate sessions and recordings per course.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`TEACHING_INSTRUCTIONAL_PRACTICES`.

## Global constraints

As in Phase 2a.

## Approach

Most visuals are statistics over per-course values: the median, minimum and maximum class size, a distribution
of courses by bin, and node averages of per-course values. Each page therefore gets a course-grain dataset (a
course × type grain where a page breaks down by type). The statistics are then plain aggregates over rows, and
histogram bins are SQL dimensions. Course attributes (term, duration, primary-node hierarchy, number, name) come
from `courses.v1` through the course key.

## Review focus

1. Every per-course statistic is over courses (or course × type rows), never over enrollments or events.
2. Class size divides by instructors, and a course with none uses 1.
3. Instructor access frequency is the share of the course's elapsed days with instructor activity, capped at 100%.
4. The bins cover every value with no gaps (0–5, 5–10, … for access; tens for class size).
5. The Collaborate numbers per course match the Collaboration Session Activity report's definitions.

---

### Task 3.1 (B): `course_teaching_summary`, one row per course

It is built on `course_role_activity` (pivoted on role) and Collaborate media.

**Class size:**
- instructors, students and their available counterparts;
- `class_size` = CEIL(students / max(instructors, 1));
- `class_size_bin` in tens.

**Course access:**
- instructor active days over elapsed course days, as `pct_instructor_access` (0–100);
- `access_bin` in 5-point steps;
- instructor and student minutes per person and their ratio;
- `instructor_recency`: Recently active (within 7 days), Previously active, or Inactive.

**Collaborate:**
- sessions, session hours, average session hours, recordings and recording hours, and weekly averages;
- the share of students attending;
- `has_collab`.

**Tests first:** compiled grain, the class-size divisor guard, access capping and bins.

### Task 3.2 (B): Course items

- **`course_item_totals`**, one row per course: total items, items per group (Assessment, Content, Tool, Other)
  and the share accessed.
- **`course_item_types`**, one row per course × item type: its group and its item count.
- **Tests first:** the group mapping and the grain.

### Task 3.3 (B): `course_tool_instructor_engagement`, one row per course × tool type

- **Columns:** instructor interactions and contributions (instructor submissions), and their weekly averages.
- **A course-level label:** both, tool activity only, contributions only, or none.
- **Tests first:** the label logic and the grain.

### Task 3.4 (B): The report

- **Filters:** creation date range, term (current term), duration, ih1–4 on `courses.v1`, and course.
- **Pages:**
  - per-course tables;
  - minimum, median and maximum KPIs;
  - distributions by bin;
  - child-node comparisons (`child_of`);
  - donuts.

  A choice control switches total vs weekly average where the source has one.
