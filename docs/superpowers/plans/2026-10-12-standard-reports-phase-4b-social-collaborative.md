# Standard reports, Phase 4b: Social & Collaborative Engagement. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Social & Collaborative Engagement, two pages.
- **Learning Tools Engagement:** student enrollments' interactions and contributions in social tools
  (discussions, blogs, journals, wikis …) by tool, node, course and student.
- **Virtual Classroom Engagement:** student participation in the course's Collaborate sessions.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEARNING_SOCIAL_AND_COLLABORATIVE_ENGAGEMENT`.

## Review focus

1. Every statistic is per student enrollment, or per enrollment × tool for the tool table.
2. Engagement: Participating (contributed), Reviewing (interacted only), or Inactive. It covers every student
   enrollment.
3. Collaborate participation is the share of the course's session time the student attended (0–100). Students
   with no session time are Inactive, not 0% active.
4. Viewers never receive student identity.

### Task 4b.1 (B): Enrollment-grain roll-ups

- **`student_social_engagement`:** from `social_interactions_by_type`, student rows of social types only.
  Interactions, contributions, items available and used, the engagement label, and an interactions bin.
- **`student_collab_participation`:** from `collab_session_student_activity`. Sessions held and attended, minutes,
  participation % and a 10-point bin, and the status (Participated, Did not participate, or No sessions).
- **Per-row statistics:** `social_interactions_by_type` gains per enrollment × type median, minimum and maximum.

### Task 4b.2 (B): The report

- **Filters:** as in Student Engagement.
- **Node visuals:** use `child_of`.
- **Per-student tables:** take names from `persons`.
