# Standard reports on the semantic layer — design

Date: 2026-10-09. Status: draft for review.
Companion: [`2026-10-09-standard-reports-gap-analysis.md`](2026-10-09-standard-reports-gap-analysis.md), the build
inventory: what each report needs from the semantic layer, as a ranked list of pieces P1–P25.

## 1. Goal

Recreate Illuminate's standard reports in the POC's Reporting section, with every number coming from the semantic
layer. The reports show the same pages, visuals, filters and data points as the shipped QuickSight reports, in the
POC's own visual style. Any visual can be pinned to the dashboard as a card.

### Scope

The 14 customer-facing reports defined in `illuminate-code/bbdata-quicksight-envs` (prod us-east-1 baseline):

| Area | Reports |
|---|---|
| Learning | Student Engagement; Student Performance & Grades; Social & Collaborative Engagement; Student Summary; Student Summary (Reach) |
| Teaching | Instructional Practices; Assessment & Grades; Course Summary; AI Design Assistant Adoption |
| Leading | Learning Tool Activity & Use; Collaboration Session Activity; Course Administration; Learning Platform Adoption; Learning Tools Adoption |

They hold 35 pages and 315 visuals: 267 data visuals and 48 help-text panels.

Out of scope:
- `V_RA_CREDIT_BURNDOWN`, which is internal and reads `CREDIT_MGMT`.
- The five Reach visuals built on `ACTIVITY_RISK` (the engagement score). Neither source is reachable from the POC. Those visuals render as "Not available in this environment".
- Row-level scoping by institutional-hierarchy node, which arrives with user management (§6).

### Success criteria

1. Every in-scope data visual renders from one or more compiled semantic contracts, with no SQL in the frontend.
2. Each report's filter bar applies to every visual on the report.
3. A test compiles every visual of every report definition against the canonical catalog, so a definition change
   cannot silently break a report.
4. Numbers come from the semantic layer's definitions.
5. Every number has an info button that shows how it is calculated (§3.2).
6. Every governed visual can be pinned as a dashboard card.

## 2. Decisions

| Decision | Ruling |
|---|---|
| Definitions | **The semantic layer is the source of truth.** It already corrects many bbd-analytics and QuickSight defects. When a report needs a data point the layer lacks (a median, a band, a role-scoped count), we add it to the layer with correct maths, and never copy QuickSight's maths. |
| Identity (PII) | **Viewers** see counts only: PII may be filtered and counted, never selected. **Admins, Authors and Developers** see everything, including roster tables. Details are in §6. |
| Fidelity | Same content in our style: the same pages, visuals, filters and data points, drawn with the POC's components. We don't copy QuickSight's grid. |
| Date defaults | The current term, where a report has a Term filter. Otherwise the last 30 days. Users can change either. |
| Help-text panels | Kept, as text visuals carrying the QuickSight English copy, with the wording corrected where our definitions differ. |
| What users see | Reports, and nothing about how they were built. No product surface refers to QuickSight, bbd-analytics or this build inventory: not report text, not visual titles, not measure or metric descriptions (which appear in Info and the chat catalog), not errors. Descriptions say what a number is. |
| What we document | What is built, in PRs and code comments. Nothing compares our numbers with another product's. |

## 3. Architecture

### 3.1 Report definitions (backend)

Reports are data, not code. Each report is a YAML file in `canonical/reports/<slug>.yaml` in the backend repo, beside
the datasets and metrics it reads. Keeping them there means the definition tests run against the catalog in the same
suite.

```yaml
id: report.leading_learning_platform_adoption.v1
title: Learning Platform Adoption
area: leading                    # learning | teaching | leading
description: …
filters:                         # the filter bar; applied to every visual unless a visual opts out
  - id: term
    label: Term
    dimension: dataset.courses.v1:term_name
    control: multi_select
    default: current_term
  - id: dates
    label: Date range
    control: date_range
    default: last_30_days
    time_dimension: { default: session_start, by_dataset: { … } }
pages:
  - title: LMS Activity
    visuals:
      - id: active_users
        type: kpi                # kpi | bar | line | combo | pie | table | pivot | heatmap | histogram | scatter | treemap | text
        title: Active users
        queries:                 # one or more contracts; the filter bar is merged into each
          current: { measures: [dataset.lms_sessions.v1:users] }
          comparison: { measures: [dataset.lms_sessions.v1:users], time_range: comparison_period }
        transform: { kind: period_over_period, value: current, baseline: comparison }
        encode: { value: users, format: number }
```

- `queries` are ordinary `QueryContract`s. The filter bar's values are merged in when the report runs.
- `transform` names one client transform from §3.3.
- `encode` maps result columns to the visual's channels: x, y, series, value, size, colour.
- **New endpoints:**
  - `GET /api/v1/reports` lists report summaries.
  - `GET /api/v1/reports/{id}` returns one definition, resolved for the caller: the current-term default is
    computed, and identity columns are dropped for Viewers (§6).
  - Visuals still run through the existing `POST /api/v1/semantic/query`, one contract at a time, so caching, the
    guard and provenance are unchanged.
- **Validation** (`semantic_layer/reports.py`, run in tests and at catalog load):
  - every query compiles with its filter bar merged;
  - every encode column exists in the result;
  - every transform's inputs exist.

### 3.2 Renderer (frontend)

`/reporting` lists the reports by area from `GET /api/v1/reports`, replacing `mockReports`. `/reporting/[id]` renders
a definition:
- the filter bar, with filter state kept in the URL so a filtered view can be shared;
- pages as tabs;
- visuals in a responsive grid.

Each visual type is one component, built on the existing Recharts and card components and on `ResultTable` for tables.
Visuals run their contracts in parallel and are cached by contract. Each visual has Info, View SQL and Pin as card.

**Info, on every number.** Every KPI, every chart and table (with one entry per series or column), and every dashboard
card has an info button. It shows how the number is calculated, in this order:
1. The metric or measure name and its description.
2. The calculation in words and in its expression: for example "Average of `GRADE_PERCENTAGE`", or for a ratio its
   numerator ÷ denominator, each with its own expression.
3. The dataset it reads, with that dataset's grain and description.
4. Every filter in force: the filter-bar values, the metric's default filters, and the visual's own filters, each with
   its condition.
5. The time range and time dimension.
6. Any client transform, in words, for example "% change vs the comparison period" or "share of total".
7. Your institution's overrides, if any applied, with their version.

Everything Info shows comes from the catalog, the visual's contracts and the query provenance, so it is always what
actually ran. View SQL shows the compiled SQL.

The mock Reporting data (`mockReports`, `mockChartData`, `ReportChartArea`) is removed once the first real report ships.

### 3.3 Client transforms

These are pure functions over contract results, kept in `src/reports/transforms.ts`, each with unit tests. A transform
belongs in the client when it rearranges or combines result rows. It belongs in the semantic layer (§4) when it needs
row-level data the result doesn't carry.

| Transform | Used for |
|---|---|
| `period_over_period` | KPI change vs a comparison range: % difference, with week-of-term/quarter/year alignment. About 34 visuals. |
| `percent_of_total` | Stacked-% bars and shares. |
| `unpivot` | Several measures turned into one category (on time / late / overdue). |
| `top_n_other` | Top N plus an "Other" bucket. |
| `measure_switch` | A control choosing which measure a visual shows (min/median/max, minutes/users). |
| `bin` | Histograms over at most 1,000 entity rows. |
| `threshold` | A user threshold splitting rows into two groups. |
| `combine` | Arithmetic across columns or across a visual's queries (ratio, difference vs peers). |
| `weekday_divisor` | Heat-map averages per weekday in the range. |
| `ih_child` | Show the next IH level below the selected node. |

## 4. Semantic-layer additions

These are the pieces the reports need, ranked and sized in the companion as P1–P25. Each is one PR, with its tests.

**Contract and compiler capabilities**
- **P1, cross-dataset filters (semi-join).** A contract may filter on a dimension of any dataset sharing an entity
  key (course, person_course, person). It compiles to `<key> IN (SELECT <key> FROM <dataset> WHERE …)`, so it can't
  fan out. This unblocks the IH, modality, duration and SIS filters on 11 reports.
- **P4, grouping by IH node.** Measures limited to `count_distinct`, `min` or `max` may be grouped through the
  course→IH bridge. The interim fallback is the course's primary node.
- **P20, hour-of-day and day-of-week grains** (`__hour_of_day`, `__day_of_week`), for heat maps.
- **P19, two-stage aggregation.** An aggregate of a per-entity aggregate, for example the median of per-course
  averages. This is expressed as `group_by_first: <entity>` on a measure, compiled as an inner GROUP BY.
- **P21, row mode with paging,** for the long tables: activity log, course admin, per-student lists. `limit` is still
  at most 1,000 per page.
- **P22 HAVING, P24 percentiles, P23 exact list match.** Each is small.

**Datasets** (each its own PR, under the existing dataset schema)
- `collab_sessions` and `collab_events`.
- `course_enrollments`: every enrollment, kept even with no activity.
- `persons`.
- `course_items`.
- `course_readiness`.
- `course_item_ai_usage`.
- `course_access_by_slot`.
- `student_grade_items`.
- `course_groups`.
- `collab_course_media`.
- `enrollment_daily_grade_activity`.

**Catalog for Info** (in the foundation phase): the public catalog adds each measure's expression (`expr`, with the
tenant's override applied) and each filter's condition (`sql`). Today only the admin overlay endpoint returns these.

**Definitions** (grouped into small PRs by YAML file)
- Course-grain dimensions on `courses.v1` (P2).
- Grading and platform definitions (P9).
- Instructor-scoped measures (P10).
- The per-dataset measures and dimensions listed in the companion's §2, "Remaining definitions".

Each new measure's description says what it measures, in user terms (§2). Examples:
- `students_active_last_7_days` keeps its definition, and gains a sibling, `share_recently_active_5min`, for the
  report's data point.
- "Instructor" data points use `teaching_staff` (all non-student roles), a named role group beside `instructor`.

## 5. Current term

`GET /api/v1/reports/{id}` resolves `default: current_term` to the term (or terms) whose start ≤ today ≤ end, read
from a small `terms` dataset over `CDM_LMS.TERM`. If no term spans today, it uses the most recent term that ended. This
lookup is cached per Lambda instance for an hour.

## 6. Roles and identity

Cognito groups map to Illuminate roles:
- `illuminate-admins` → Admin;
- `illuminate-authors` → Author;
- `illuminate-developers` → Developer;
- no group → Viewer.

The compiler gets the caller's role:
- For **Admin, Author and Developer**, dimensions over PII columns are selectable. Roster tables, named students and
  the Student Summary picker all work. Every compiled query that selects a PII dimension is logged with the caller's
  `sub`, the contract and the time.
- For **Viewers**, nothing changes: PII may be filtered and counted, never selected. A report delivered to a Viewer
  drops identity columns from tables, and a visual that is identity-only shows "Requires Author access".

The freehand SQL guard (chat `execute_sql`) is unchanged for every role. Identity only flows through governed
contracts.

**Deferred to user management:** for Authors and Viewers, scoping every query to their assigned IH nodes, compiled
as a P1 semi-join on the node set taken from the user's profile. The design leaves room for it: the role and node set
travel together in the caller context the compiler receives.

## 7. Build order

1. **Foundation**, about 7 PRs:
   - report definition schema, validation and endpoints, with one trivial report;
   - catalog expressions and filter conditions, plus the Info panel (shared by report visuals and dashboard cards);
   - the renderer and filter bar;
   - the client transform kit;
   - P1 cross-dataset filters;
   - P2 course dimensions;
   - P9 grading and platform definitions;
   - roles and identity (§6).
2. **Quick reports**, one PR each plus their dataset or definition PRs: Learning Platform Adoption; Learning Tool
   Activity & Use; Assessment & Grades; Collaboration Session Activity (needs `collab_sessions`/`collab_events`);
   Course Administration (`course_readiness`); AI Design Assistant Adoption (`course_item_ai_usage`); Learning Tools
   Adoption.
3. **Instructional Practices:** P4, P10, `course_items`, `course_groups`, `collab_course_media`.
4. **Student Engagement, Social & Collaborative, Course Summary:** `course_enrollments`, P19, `course_access_by_slot`,
   `enrollment_daily_grade_activity`, plus per-dataset definitions.
5. **Student-level reports:** Student Performance & Grades, Student Summary and Reach, with `persons`,
   `student_grade_items`, P20 and P21.

Each report PR lands once every one of its visuals compiles and renders against the test account. Mock reports are
removed in the first report PR.

## 8. Testing

- **Backend.**
  - Every report definition is validated and every visual compiled: one parametrised test per report.
  - New capabilities get compiler tests.
  - New datasets get the existing definition tests, plus a live smoke run through `illuminate-mcp` before merge.
- **Frontend.**
  - Unit tests for the transforms, the first frontend unit tests. This adds Vitest to the POC.
  - Type checks for definitions, which come from the endpoint.
  - A manual check of each report on the test deployment.

## 9. Risks

- **Warehouse load.** A report page can fire 20+ contracts at once. Visuals run in parallel up to 6 at a time, and
  results are cached by contract. If Snowflake queues, KPI pairs (current and comparison) should be batched into one
  query.
- **P19 and P1 complexity.** These are the two compiler changes with real fan-out risk. Each lands with tests that
  pin row counts against hand-checked fixtures.
- **Identity logging.** The log is CloudWatch only in the POC. Retention and review belong to user management.
