# Standard reports, Phase 5c: Student Summary (Reach). Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Student Summary (Reach), a one-page advising view of one student:
- projected grade KPIs and courses below 70%;
- overdue assessments;
- content engagement;
- timeliness;
- grade vs peers;
- weekly time;
- an access heat map;
- course and assessment details.

It reuses the Student Summary datasets. Engagement scores are not in the data, so a note replaces those three
visuals.

## Review focus

1. Every visual is about the chosen student and requires identity.
2. "Projected final grade" is the student's current grade per course.
3. The engagement-score note is the only place the missing data shows.

### Task 5c.1 (B): The report
