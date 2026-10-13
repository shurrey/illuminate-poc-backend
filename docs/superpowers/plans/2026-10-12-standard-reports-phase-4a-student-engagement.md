# Standard reports, Phase 4a: Student Engagement. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Student Engagement, three pages.
- **Course Access:** access frequency, active vs inactive, recency, distributions, per-course and per-student
  tables.
- **Engagement with Course Content:** interactions per enrollment, by item type, per course, per student.
- **Engagement with Assessments:** on-time, late, overdue and upcoming submissions by type, node, course and
  student.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEARNING_STUDENT_ENGAGEMENT`.

## Global constraints

As in Phase 2a. Student names, emails and ids are identity: Admin, Author and Developer see them, Viewers never
do.

## Review focus

1. Active vs inactive covers every student enrollment in reported courses, including those with no activity.
2. Statistics are per enrollment, never per activity row.
3. Viewers never receive a student's name, email or id; a per-student table is unavailable to them.
4. Recency uses the last access: within 7 days, earlier, or inactive (under 5 minutes in total).

---

### Task 4a.1 (B): `student_enrollments`

One row per student enrollment (not deleted) in reported courses (`course_filters`), left-joined to
`course_student_activity`.

**Columns:**
- the student's name, email and id (PII);
- minutes, accesses, interactions and submissions;
- access frequency (0–100) and a 5-point `access_bin`;
- `engaged` (Active when the student has 5 minutes or more, else Inactive);
- `recency` (Recently active, Previously active, Inactive);
- an interactions bin;
- content items reviewed and completed, against the course's content items.

**Measures:** enrollments and students, the median, minimum and maximum of access and interactions, recently
active enrollments, and hours per week.

**Tests first:** the grain, the left join keeping inactive enrollments, the bins, and the identity columns being
PII.

### Task 4a.2 (B): The report

- **Filters:** creation date range, term (current term), duration, delivery method, ih1–4 on `courses.v1`, and
  course.
- **Visuals:**
  - access: from `student_enrollments`;
  - content: from `student_item_tool_activity` (per enrollment and type) and `student_enrollments`;
  - assessments: from `student_assignments`, with on-time, late, overdue and upcoming unpivoted for the donut.
- **Node visuals:** use `child_of`.
