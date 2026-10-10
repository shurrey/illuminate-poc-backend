# Standard reports: roadmap

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`
**Build inventory:** `docs/superpowers/specs/2026-10-09-standard-reports-gap-analysis.md` (P-numbers)
**Repos:** B = this repo (`illuminate-conversational-intelligence`), F = `illuminate-poc`.

One unit is one PR, branched from `main` and self-merged after its tests pass. Each phase gets its own detailed
plan when it starts, written against the code the earlier phases actually produced.

## Phase 1: Foundation. Detailed plan: `2026-10-09-standard-reports-phase-1-foundation.md`

| Unit | Repo | Scope |
|---|---|---|
| 1.1 | B | The public catalog exposes each measure's `expr` and each filter's `sql`, with tenant overrides applied |
| 1.2 | F | Vitest; `describeQuery` (how a number is calculated); the Info panel on dashboard cards |
| 1.3 | B | Roles (Admin / Author / Developer / Viewer); identity selectable for the first three; identity queries logged |
| 1.4 | B | P1: cross-dataset filters as a semi-join |
| 1.5 | B | `terms` dataset; report schema, loader and validation; the `/api/v1/reports` endpoints, including `run` |
| 1.6 | F | Report renderer: list, filter bar, pages, and the KPI / bar / line / pie / table / text visuals with Info |
| 1.7 | F | Client transforms: period_over_period, percent_of_total, unpivot, top_n_other |
| 1.8 | B | P2: course-grain dimensions on `courses.v1` |
| 1.9 | B | P9: grading and platform definitions |

Phase 1 is complete: B #109–#117, F #28–#33. After the whole-phase review, fixes landed for:
- report results outliving sign-out;
- semi-join scope;
- filter reachability, value types and validation;
- cleared filters reverting to their defaults.

Deferred minors from that review:
- `/run` ignores `truncated` in the UI, and the default limit (100) can cut long daily lines;
- `_current_terms` caps at 1,000 terms with no order;
- `course_weeks` can be negative;
- filter options fail silently on datasets needing a time range, and on identity dimensions for Viewers;
- `top_n_other` mishandles a bad `n`;
- Info omits ignored filters and repeats titles on multi-query visuals;
- a test that PII dimension names stay unique;
- the pool can briefly exceed its limit.

## Phase 2: Quick reports

The first unit is **comparison periods**. A visual query declares `period: comparison` (the previous period of
equal length), and the date-range filter shifts it accordingly. Without it, period-over-period KPIs show 0%.

**Done:**
- **Learning Platform Adoption.** Plan `2026-10-10-standard-reports-phase-2-platform-adoption.md`; B #120–#126, F #34–#37.
- **Learning Tool Activity & Use.** Plan `2026-10-10-standard-reports-phase-2b-tool-activity.md`; B #128–#132, F #38–#39.
- **Assessment & Grades.** Plan `2026-10-10-standard-reports-phase-2c-assessment-grades.md`; B #135–#137, #139–#140,
  F #40–#43.

- **Collaboration Session Activity.** Plan `2026-10-10-standard-reports-phase-2d-collaboration-sessions.md`; B #138,
  #142, #144.
- **Course Administration.** Plan `2026-10-10-standard-reports-phase-2e-course-administration.md`; B #143, #145–#146,
  F #44. Adds `child_of` grouping by the child nodes of the selected hierarchy node, and search in long filter lists.

All five were reviewed, fixed and checked live.

**In progress:** AI Design Assistant Adoption, plan `2026-10-10-standard-reports-phase-2f-ai-design-assistant.md`;
B #147–#149.

Deferred minors:
- a cleared comparison range runs all-time;
- active users and courses filter on first-activity date, not overlap;
- any hierarchy level narrows course data to reportable courses;
- recents aren't recorded from Quick Access or deep links;
- hierarchy options are stale while reloading;
- heat maps hide weekdays with no data, and a missing cell looks like zero;
- `course_tool_use` mixes local activity hours with UTC submission times;
- validation misses bad `time_overlap` refs and per-query `filters_ignored` typos;
- the course help-text title says "available";
- the role filter shrinks the role pie instead of highlighting;
- `average_by`'s Info text uses raw names;
- Assessment & Grades:
  - every change to the grading-time number reruns every visual;
  - a blank or negative grading time is accepted;
  - non-finite numbers return 500;
  - the primary-node join uses `ANY_VALUE`;
  - hierarchy filters match any node while grouping uses the primary one;
  - `courses.v1` level synonyms differ from `course_filters_ih`;
  - an empty current term has no hint to clear it.
- Collaboration Session Activity:
  - event queries scan all of `COLLAB_EVENTS`;
  - day charts keep the oldest 400 days on long ranges;
  - session-uid uniqueness isn't enforced;
  - the instance timezone uses `ANY_VALUE`.
- Course Administration:
  - a malformed `child_of` raises instead of being reported;
  - courses attached directly to the chosen node are labelled '-';
  - depth counts only leading filters;
  - every query re-aggregates enrollments and items.

Each report is one PR in B (its definition) and is verified live in F. Datasets and definitions it needs land first,
in their own PRs.

| Report | Needs first |
|---|---|
| Learning Platform Adoption | 1.7 weekday_divisor transform; Ally and SafeAssign definitions as found |
| Learning Tool Activity & Use | `day_of_week` / `hour_group_3h` on CTA (or P20) |
| Assessment & Grades | threshold transform; `has_due_date` scoping (1.9) |
| Collaboration Session Activity | `collab_sessions`, `collab_events` (P5) |
| Course Administration | `course_readiness` (P12); P21 paging |
| AI Design Assistant Adoption | `course_item_ai_usage` (P13); ih_child transform |
| Learning Tools Adoption | measure_switch transform; P4 interim (primary IH node) |

The first report's PR in F removes the mock reports (`mockReports`, `mockChartData`, `ReportChartArea`).

## Phase 3: Instructional Practices

P4 (group by IH node) · P10 (CRA instructor definitions) · `course_items` (P11) · `course_groups` (P16) ·
`collab_course_media` (P17) · bin transform · the report.

## Phase 4: Student Engagement, Social & Collaborative, Course Summary

`course_enrollments` (P8) · P19 (two-stage aggregation) · `course_access_by_slot` (P14) ·
`enrollment_daily_grade_activity` (P18) · the per-dataset definitions for CSA, SCM, SA, SOC, CSSA, CITA, SITA,
`activity_log` and `lms_course_logins` · combine transform · the three reports.

## Phase 5: Student-level reports

`persons` (P6) · `student_grade_items` (P15) · P20 (hour / weekday grains) · P21 (paging) · P24 (percentiles) ·
Student Performance & Grades · Student Summary · Student Summary (Reach). Reach's engagement-score visuals render as
not available.

## Deferred

- IH-node scoping for Authors and Viewers, which arrives with user management.
- P25 cumulative totals and the credit burndown, both out of scope.
