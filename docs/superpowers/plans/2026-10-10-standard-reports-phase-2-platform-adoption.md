# Standard reports, Phase 2a: Learning Platform Adoption. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the first standard report, Learning Platform Adoption (4 pages, 27 visuals, 9 of them help text),
and add the shared pieces it needs:
- comparison date ranges;
- filters that resolve per dataset;
- cascading hierarchy filters;
- heat maps;
- side-by-side and per-weekday calculations.

**Architecture:** The report pieces build on Phase 1.
- **Backend:** `semantic_layer/reports.py` (definitions, merge, validation) and the `/api/v1/reports` endpoints.
- **Frontend:** `src/components/reports/*` and `src/reports/transforms.ts`.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source report's details are in the
inventory (`$S/reports_inventory.md`, section `LEADING_LEARNING_PLATFORM_ADOPTION`).

## Global constraints

The same as Phase 1: one PR per task, self-merged after verification; PRs describe only what is built; the code
comment rules; no QuickSight or bbd-analytics in any product text. After an API deploy, redeploy the frontend stack.

## Review focus

1. A comparison query must use only the comparison range, never the primary one as well.
2. A per-weekday average over a range that contains no Monday must not divide by zero.
3. A hierarchy filter on a platform dataset (`ih_nodes contains`) and on a course dataset (`ih_level_n in`) must
   both apply. Neither may be reported as ignored.
4. Cascading hierarchy options: choosing level 1 limits level 2's options; clearing level 1 clears levels 2–4.

---

### Task 2.1 (B): Comparison date ranges

**Interfaces**
- A report may have several `date_range` filters.
- A visual query may set `date_filter: <filter id>`. That query then takes only that date filter. A query without
  `date_filter` takes the report's first date-range filter only.
- New filter default `previous_30_days`: the 30 days before `last_30_days`, i.e. today−60 to today−31.
- `validate_report` fails when a `date_filter` names a filter that isn't a date range.

**Tests (RED first)**, in `tests/test_reports.py`:
- With primary and comparison date filters, a query with `date_filter: comparison` gets only the comparison range.
- A query with no `date_filter` gets only the primary range.
- `resolve_defaults` gives `previous_30_days` as 2026-08-10..2026-09-09 when today is 2026-10-09.
- Validation flags `date_filter: nope`.

### Task 2.2 (B): Filters that resolve per dataset, cascading, and access modality

**Interfaces**
- `ReportFilter.dimensions: list[{ref, op}]` is the ordered alternative to `dimension` (`op` is `in` by default,
  or `contains`). A query applies the first alternative its datasets can reach. A bare `ref` resolves on the query's
  own datasets.
- `ReportFilter.depends_on: list[filter id]`. The frontend applies these parents' values when it loads this
  filter's options. On the backend, it's validated (the parents must exist).
- A `contains` alternative takes exactly one value, which is why the hierarchy filters are single-select. Given
  several values, it is ignored for that query and reported.
- `lms_sessions` gets the dimension `access_modality`: `IFF(MOBILE_IND, 'Mobile app', 'Web browser')`.

**Tests (RED first)**
- A filter with alternatives `[dataset.course_filters_ih.v1:ih_level_1, ih_nodes contains]` applies the first
  alternative to a course-keyed query, and `ih_nodes contains` to an `lms_sessions` query. Neither is ignored.
- `depends_on` naming an unknown filter is a validation problem.
- `access_modality` compiles.

### Task 2.3 (F): Heat maps, side-by-side, per-weekday averages, cascading options

**Interfaces**
- **Visual type `heatmap`,** with `encode: {x, y, value, x_order?, y_order?}`. A grid of cells shaded by value, plus a
  legend. The column and row order come from `*_order` (a list) or a numeric sort column.
- **Transform `side_by_side`** `{queries: {label: name}, on, field}`. It merges several queries' rows on `on`, giving
  one column per label. It drives "primary vs comparison" bars.
- **Transform `per_weekday_average`** `{query, field, day}`. It divides each row's `field` by the number of days
  with that weekday name in the query's applied time range, which `/run` returns in `contract.time_range`.
- `/run` results already carry the merged contract, so the transform reads the range from there.
- **FilterBar:** a filter with `depends_on` loads its options with its parents' current values as filters. Clearing
  a parent clears its dependants.

**Tests (Vitest, RED first)**
- `side_by_side` keeps rows found in only one query, with null in the other column.
- `per_weekday_average` over 2026-10-01..2026-10-14 divides Monday by 2. Over a range with no Monday, Monday's
  value is null.
- The cascade-clearing helper clears levels 2–4 when level 1 changes.

### Task 2.4 (B): The Learning Platform Adoption report

**`canonical/reports/learning_platform_adoption.yaml`** (`report.learning_platform_adoption.v1`, area `leading`)

**Filters:**
- `dates`, Primary date range: last 30 days, applied by default.
- `comparison`, Comparison date range: previous 30 days.
- `ih1` to `ih4`, Institutional hierarchy levels 1–4: single-select, alternatives
  `[dataset.course_filters_ih.v1:ih_level_n, ih_nodes contains]`, with each level `depends_on` the levels above it.

**Pages and visuals** (the source report's titles):
- **LMS Activity:**
  - roles bar (`side_by_side` sessions by `institution_role_name`, primary vs comparison);
  - two modality donuts (`lms_sessions` sessions by `access_modality`, comparison and primary);
  - two heat maps (`lms_sessions_by_slot` sessions by `day_of_week` × `slot_label`, `per_weekday_average`, comparison
    and primary);
  - active LMS courses KPI (`lms_course_logins` `courses_accessed`, period over period);
  - active LMS users KPI (`lms_sessions` `people`, period over period);
  - the help text.
- **Collaboration Tool Activity:**
  - rooms created KPI (`collab_rooms` `rooms` on `created_date`, period over period);
  - two heat maps (`collab_sessions_by_slot` `session_slots`, `per_weekday_average`);
  - two users-over-time lines (`collab_attendance_hourly` `attendees` by `day__day`);
  - Collaborate users KPI (`collab_attendance` `attendees`, period over period);
  - the three help texts.
- **Accessibility Tool Activity:**
  - alternative formats downloaded KPI (`ally_alternative_formats` `downloads`, period over period);
  - accessibility score clicks KPI (`ally_instructor_feedback` `feedback_events`, period over period);
  - downloads by format bar (`side_by_side` `downloads` by `format_type`);
  - two help texts.
- **Originality Tool Activity:**
  - originality reports KPI (`safeassign_originality_reports` `reports`, period over period);
  - originality scores KPI (`safeassign_originality_reports_basic` `reports`, period over period);
  - two help texts.

The Originality page lists `filters_ignored: [ih1, ih2, ih3, ih4]`, as the SafeAssign events carry no hierarchy.

**Verification**
- `test_every_canonical_report_validates` passes.
- Live: every visual's queries run through `/run` on the test account, with no error and no unexpected
  `ignored_filters`.

### Task 2.5 (F): Retire the mock reports

Remove `mockReports` from `/reporting` and the `[reportId]` pages, plus `mockChartData` and `ReportChartArea`.
`ReportSidebar`, `QuickAccessBar` and `DataQASearch` also read `mockReports`:
- the sidebar goes with the old detail page;
- `QuickAccessBar` favourites and recents switch to the API report list;
- `DataQASearch` keeps its suggested questions in a small local list.

The "Sample data" badge stays on What Changed and Notifications.

**Verification:** `tsc`, `lint`, `test` and `build` pass, and every link to `/reporting/...` still resolves.
