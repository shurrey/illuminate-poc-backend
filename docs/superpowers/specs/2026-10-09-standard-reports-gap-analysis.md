# Standard reports: build inventory

Date: 2026-10-09. Companion to [`2026-10-09-standard-reports-design.md`](2026-10-09-standard-reports-design.md).

**Inputs:**
- QuickSight prod us-east-1 analyses in `illuminate-code/bbdata-quicksight-envs`: 15 reports, 315 visuals.
- This repo's semantic layer as of #106: 38 datasets (35 public), 19 metrics, `semantic_layer/contract.py` and
  `compiler.py`.
- Semantics checked against the QuickSight dataset SQL and `bbdata-bbd-analytics`.

**Abbreviations:**
- CSA `course_student_activity` · SCM `student_course_minutes` · SA `student_assignments` · SG `student_grade`
- CRA `course_role_activity` · GRT `grade_response_time` · CTA `course_tool_activity`
- CITA `course_item_tool_activity` · SITA `student_item_tool_activity` · SOC `social_interactions_by_type`
- CSSA `collab_session_student_activity`

## 0. What blocks reports today

| Constraint | Effect |
|---|---|
| Cross-dataset dimensions resolve only through many-to-one joins to a `complete: true` dataset. Only `courses.v1` qualifies. | IH levels, course duration, creation date, modality and the SIS attributes can't filter other datasets. That breaks the filter bar on 11 reports. |
| No HAVING, windows, nested aggregation, percentiles beyond median, period comparison, or hour/weekday grains. Ratios only within one dataset. | QuickSight LOD fields, histograms of per-entity values, and peer comparisons. |
| PII is filterable and countable, never selectable. `PERSON_ID` and `PERSON_COURSE_ID` are PII-listed. | Roster tables. The single-student filter exists only on CSA (`person_email`) and `active_students` (`student_email`). |
| `limit` ≤ 1000. | Long tables, and binning over more than 1,000 entities. |
| `activity_log` requires a time range and holds one year. | "All activity" options. |

## 1. Summary

Precedence for classifying a visual: NEEDS-DATASET > NEEDS-CAPABILITY > NEEDS-DEFINITION > READY.

| Report | READY | DEF | DATASET | CAP | Insight |
|---|---|---|---|---|---|
| LEARNING_STUDENT_ENGAGEMENT | 1 | 5 | 4 | 15 | 3 |
| LEARNING_STUDENT_PERFORMANCE_AND_GRADES | 0 | 0 | 0 | 12 | 2 |
| TEACHING_INSTRUCTIONAL_PRACTICES | 0 | 8 | 12 | 15 | 7 |
| TEACHING_ASSESSMENT_GRADES | 2 | 2 | 0 | 4 | 1 |
| TEACHING_COURSE_SUMMARY | 1 | 10 | 14 | 13 | 1 |
| LEADING_LEARNING_TOOL_ACTIVITY_AND_USE | 2 | 0 | 0 | 6 | 2 |
| LEADING_COLLABORATION_SESSION_ACTIVITY | 0 | 0 | 38 | 0 | 11 |
| LEADING_COURSE_ADMINISTRATION | 0 | 0 | 5 | 0 | 0 |
| LEADING_LEARNING_PLATFORM_ADOPTION | 2 | 2 | 0 | 14 | 9 |
| LEARNING_SOCIAL_AND_COLLABORATIVE_ENGAGEMENT | 0 | 1 | 0 | 15 | 2 |
| LEARNING_STUDENT_SUMMARY | 0 | 7 | 1 | 12 | 4 |
| LEADING_LEARNING_TOOLS_ADOPTION | 2 | 1 | 0 | 12 | 4 |
| LEARNING_STUDENT_SUMMARY_REACH | 0 | 9 | 7 | 3 | 0 |
| TEACHING_AI_DESIGN_ASSISTANT_ADOPTION | 0 | 0 | 9 | 0 | 2 |
| V_RA_CREDIT_BURNDOWN (out of scope) | 0 | 0 | 1 | 0 | 0 |
| **Total (315)** | **10** | **45** | **91** | **121** | **48** |

About 60 of the CAP visuals need only client transforms. 38 of the DATASET visuals are Collaboration's, which two
small datasets unblock. 5 visuals are blocked by unreachable sources: `ACTIVITY_RISK` and `CREDIT_MGMT`.

## 2. Missing pieces

### Top 10

| # | Piece | Kind / size | Definition | Unblocks |
|---|---|---|---|---|
| P1 | Cross-dataset filter (semi-join) | SL, M | Filter on a dimension of a non-complete dataset sharing an entity: `b.<key> IN (SELECT <key> FROM <cte> WHERE …)`.<br>• Via course: `course_filters_ih` `ih_level_1..4` / `ih_node_name`, duration, creation date, delivery method.<br>• Via person_course: SIS program, grade level, retake, delivery method; activity-after-X-days. | Filter bars of 11 reports |
| P2 | `courses.v1` course-grain definitions | DEF, S | **Dimensions:**<br>• `course_duration`: `IFF(COALESCE(co.END_TIME,te.END_TIME) IS NOT NULL,'Fixed','Continuous')`<br>• `course_creation_date`, `delivery_method`, `course_weeks`, `has_instructor`, `course_status`, `description`, `enrollment_method`, `timezone`<br>**Measures:** `student_enrollments`, `teaching_staff_enrollments` (NOT IN S,G), `course_items`<br>**Also:** align `course_start` with the `course_filters` fallback | Term / Date Filter Type / Duration / Modality controls; about 20 visuals |
| P3 | Client transform kit | client, S–M | POP (≈34), PCT-TOTAL (≈14), UNPIVOT (≈10), TOPN (≈6), PARAM-MEASURE (≈35, mostly secondary), BIN (≈7), CROSS-CALC (≈20), PARAM-THRESHOLD (≈9), CAL-DIVISOR (4), IH-CHILD | ≈60 visuals, primary blocker; ≈40 secondary |
| P4 | Group by IH node | SL, M; interim S | Bridge course→IH, limited to count_distinct, min and max. Interim: the primary node's `ih_level_1..4` on `courses.v1`. | ≈30 visuals |
| P5 | `collab_sessions` + `collab_events` | 2 × S | **Sessions:** one row per `CDM_CLB.SESSION`; session_start (instance tz), minutes, attendance_count, linked_to_course, term_names, ih_nodes; sessions, rooms and minutes stats.<br>**Events:** `CDM_TLM.COLLAB_EVENTS` → session; event_type, event_group, event_time. | Collaboration 38 of 38 |
| P6 | `persons` | dataset, S | `complete: true`, primary `person`. Filter-only `person_source_id` and `person_alt_id` (`COALESCE(STAGE:student_id, STAGE:user_id, STAGE:batch_uid, SOURCE_ID)`). | Scope for Student Summary and Reach |
| P7 | Identity (roster) policy | policy, then SL M | Decided: role-gated (design §6) | ≈24 tables and the student picker |
| P8 | `course_enrollments` | dataset, M | **Rows:** every PERSON_COURSE, all roles, test users out, left-joined to activity.<br>**Activity:** cnt_days, percentage_days, interactions, hours, last access and submission, contributions (`item_group <> 'A'`), grades entered, response hours, Collaborate.<br>**Recency:** ≥5 min within 7 days; recency bucket; NTILE(4) quartile.<br>**Flags:** available, enabled, deleted, accommodation. | ≈11 visuals |
| P9 | Grading and platform definitions | DEF, S | **GRT:** `has_due_date`, `due_time`, `gradebook_name`, `response_days_capped` (`LEAST(RESPONSE_DAYS,91)`), `ungraded_attempts`, min/max response days, `share_ungraded`.<br>**lms_sessions:** `session_end_date`.<br>**collab_sessions_by_slot:** `session_slots`. | ≈17 visuals; Assessment & Grades |
| P10 | CRA instructor definitions | DEF, S–M | Role-scoped active / enrolled / minutes; class size `CEIL(S_active/NVL(NULLIF(I_active,0),1))` with stats and band; access frequency with stats and band; `activity_recency`; Collaborate shares and minutes; `contributions` | ≈18 visuals |

### Further datasets

| # | Dataset | Size | Unblocks |
|---|---|---|---|
| P11 | `course_items` (inventory) | M | Instructional Practices: Course Design; Course Summary items |
| P12 | `course_readiness` (course × IH, readiness flags, ZEROIFNULL) | M | Course Administration 5/5 |
| P13 | `course_item_ai_usage` (`COURSE_ITEM.AI_STATUS`) | M | AI Design Assistant 9/9 |
| P14 | `course_access_by_slot` (2-hour slots by role) | M | Heat maps in Course Summary, Reach and Student Summary |
| P15 | `student_grade_items` (gradebook column × enrollment) | M | Student Summary E9; Reach 19 |
| P16 | `course_groups` | S | Class size by group |
| P17 | `collab_course_media` (recordings, hours, GB) | S | Instructional Practices: Virtual Classroom |
| P18 | `enrollment_daily_grade_activity` | M | Course Summary SE1 |
| — | Blocked: ACTIVITY_BASED_RISK_SCORE, RA_CREDIT_BURNDOWN | — | Sources unreachable |

### Further capabilities

| # | Capability | Size | Unblocks |
|---|---|---|---|
| P19 | Two-stage (nested) aggregation | M–L | ≈20 visuals |
| P20 | `__hour_of_day` / `__day_of_week` grains | S | Heat maps |
| P21 | Row mode with paging | M | Long tables |
| P22 | HAVING (client row-dropping is usually acceptable) | S | 4 visuals |
| P23 | Exact list match on `ih_nodes` | S | IH precision on Platform and Collaboration |
| P24 | Percentiles | S | Performance & Grades GD7 |
| P25 | Cumulative totals | M | Burndown only (out of scope) |

### Remaining definitions, by YAML file

- **CSA:**
  - measures: min/max percent activity; first/last access; `content_items_started` (renames `content_items_reviewed`); unopened; tracked and its ratios; access decile; contributions;
  - dimensions: percent-activity band, hours-per-week band, access recency, participation recency, days-active-after-start.
- **SCM:** min/max/median over minutes, interactions and per-week values; value dimensions; `content_activity_status`.
- **SA:**
  - measures: `share_upcoming`; available and interacted items; submissions, enrollments, courses; students with submissions or interactions and their shares; per-student ratios; last submission; next due;
  - dimension: `type_name`.
- **SG:**
  - value and band dimensions for final, avg, auto and SIS scores; `final_graded`;
  - min/max/median/avg per score, including `avg_final_normalized_score` and `min_final_normalized_score`;
  - `courses_below_projected_70`, `graded_positive_enrollments`, `courses`, grade decile;
  - dimensions `active` and `course_has_grades`.
- **SOC:**
  - enrollment counts and shares; items interacted and submitted, with shares; per-item stats and weekly twins;
  - value dimensions; `enrollment_active`; days from start to last access; `item_group`; role-scoped submissions.
- **CSSA:** enrollments, enrollments who attended, attended, `enrollment_active`, days from start to last access.
- **lms_course_logins:** join PERSON_COURSE for role; status flags; login hour; interactions, hours, enrollments; person key; test-user exclusion.
- **activity_log:** `item_group`, `course_role`, `event_id`, `item`, `seconds`, `submission_status`; person key.
- **CTA:** courses, interactions, `activity_recency`, `item_group`, `day_of_week`, `hour_group_3h`.
- **CITA:** `item_group`, `social`, tool_count, course items, available items, max accessed.
- **SITA:** `item_group`, `interactions_per_week`, person key.
- **Other:**
  - `course_catalog`: total and completed days.
  - `active_students`: accommodation, person available.
  - `course_filters_all`: `course_deleted`.
  - `sis_enrollment_attributes`: `program_name_version`.
  - `ally_alternative_formats`: person key.

## 3. Filter bar controls

| Control | Mapping | Status |
|---|---|---|
| Term | `courses.v1:term_name` | Ready |
| Course ID / Name | `courses.v1:course_number`, `course_name`, `course_id_name` | Ready |
| Date range + Date Filter Type | Client picks the time dimension: `course_start` · P2 creation date · P13 item creation | Partial |
| Course Duration, Modality | P2 (or P1) | Missing |
| IH Level 1–4 / exact depth | P1 to `course_filters_ih`; exact depth is `eq` on the next level | Missing |
| Program / Grade Level / Retake | P1 on person_course + `program_name_version` | Missing |
| Include Unavailable Enrollments | `active eq 1` where present; definitions elsewhere | Partial |
| Exclude no activity after X days | `days_active_after_start` + P1 | Missing |
| Contributing grades only | `SA:counts_towards_grade` | Ready |
| Course Role, Tool / item type | existing role / type / tool dimensions | Ready |
| Primary / comparison ranges, measure switches, thresholds | P3 | Client |
| Student picker | Filter via P6; values via P7 | Missing |
| Time zone | P2 | Missing |

## 4. Per-report notes

The full per-visual classification, with contracts for READY visuals and blockers for the rest, is in the analysis
transcript summarised here. For each report:

- **Learning Platform Adoption.** Mostly client work: POP over `lms_sessions`, `lms_course_logins`,
  `collab_attendance_hourly`, Ally and SafeAssign. Needs P9 (`session_end_date`, `session_slots`) and the weekday divisor.
- **Learning Tool Activity & Use.** Minutes and users by date are ready on CTA. The heat maps need `day_of_week` /
  `hour_group_3h` (or P20). KPIs need POP, and the role pie needs top-N.
- **Assessment & Grades.** All contracts take `has_due_date`. The not-graded KPIs are ready on GRT. The response-time
  bars and type stats need P9. The IH views need P4, and thresholds and percentages are client work.
- **Collaboration Session Activity.** Every visual needs P5. The KPIs also need POP.
- **Course Administration.** Every visual needs P12. V1 also needs P21.
- **AI Design Assistant Adoption.** Every visual needs P13. Some need POP, PCT-TOTAL or IH-CHILD.
- **Learning Tools Adoption.** The role donuts are ready on CTA. The rest needs POP, PARAM-MEASURE, P4 or P19.
- **Instructional Practices.** Needs P10, P11, P16, P17 and P4. KPIs need role-scoped stats.
- **Student Engagement.** Needs P8, the CSA/SCM/SA definitions, P4 and P19.
- **Social & Collaborative.** Needs the SOC and CSSA definitions, P19 and P4.
- **Course Summary.** Scoping to one course works today. Needs P8, P11, P14, P16, P17, P18 and GRT definitions.
- **Student Performance & Grades.** Needs the SG definitions, P4, thresholds, P24 and identity.
- **Student Summary and Reach.** Need P6, identity, P14, P15, P20 and P21. Reach's engagement score is out of scope.
