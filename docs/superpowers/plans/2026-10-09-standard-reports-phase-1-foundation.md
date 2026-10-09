# Standard reports, Phase 1: Foundation. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the standard reports share:
- Info on every number;
- roles and identity;
- cross-dataset filters;
- report definitions and their endpoints;
- the renderer;
- the core client transforms;
- the course, grading and platform definitions the first reports need.

**Architecture:**
- **Reports** are YAML in `canonical/reports/`, validated against the catalog. The backend merges filter-bar values
  into each visual's contracts and runs them through the existing compile→guard→execute path.
- **The frontend** renders definitions and applies pure transforms over results.
- **Info** is built from the catalog plus the contract and provenance that actually ran.

**Tech stack:** Python 3.11, FastAPI, pydantic v2, sqlglot 26.0.0, moto (B); Next 16, React 19, TypeScript, Recharts,
and Vitest, which is new (F).

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`

## Global constraints

- One task = one PR, branched from `main`, self-merged after tests pass. Never merge `main` into a PR branch.
- **PR descriptions:** start with `**Claude:**`, end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`, and
  describe only what is built.
- **Commits:** end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Comments:** no narrative, no ticket or requirement IDs, docstrings ≤ 4 lines. No product surface mentions QuickSight
  or bbd-analytics; descriptions say what a number is.
- **Tests:** never call AWS or Bedrock. Run the backend tests with
  `$S/venv311/bin/python -m pytest -p no:cacheprovider -o addopts="" -q`. Frontend: `npx tsc --noEmit`, `npm run lint`
  (no new warnings), `npm test` (from 1.2), and `npm run build`.
- **Never commit** `illuminate-poc/tsconfig.tsbuildinfo`.
- **Deploying to the test account** (`AWS_PROFILE=App_Dashbaoard_Developer`, us-east-1): after any API deploy, redeploy
  the frontend stack (CORS).

## Review focus

1. **Semi-join filters on a dataset with several shared entities.** The filter must use the entity that is primary
   in the target, for example `person_course` rather than `course` for SIS attributes. Otherwise it filters whole
   courses.
2. **Viewer requests for a report visual whose only dimensions are identity.** The response must say "requires
   Author access", never return a partial roster.
3. **Filter-bar values for a dimension a visual's dataset can't reach.** That visual ignores that filter, and the
   response says which filters were ignored. The rest still apply.
4. **No term spans today (between terms).** Current term falls back to the most recently ended term.
5. **Info for ratio measures and metrics with default filters.** It shows numerator, denominator and every condition.

---

### Task 1.1 (B): Catalog exposes measure expressions and filter conditions

**Files:**
- Modify: `semantic_layer/catalog_view.py`
- Test: `tests/test_catalog_view.py`, new if absent. Otherwise add to the existing catalog-view test file found with
  `grep -l public_catalog tests/`.

**Interfaces:**
- Produces: in each catalog dataset, `measures[].expr` (str | null; null for ratios) and
  `filters[].sql` (str). These come from the catalog passed in, so tenant overrides are already applied.

- [ ] **Step 1: Write the failing tests**

```python
from semantic_layer.catalog import load_catalog
from semantic_layer.catalog_view import public_catalog
from semantic_layer.overlays import Overlay, apply_overlays


def _dataset(view, ds_id):
    return next(d for d in view["datasets"] if d["id"] == ds_id)


def test_measures_carry_their_expression_and_filters_their_condition():
    grades = _dataset(public_catalog(load_catalog()), "dataset.student_grade.v1")
    measure = next(m for m in grades["measures"] if m["name"] == "average_grade_percentage")
    assert measure["expr"] == "GRADE_PERCENTAGE"
    ratio = next(m for m in grades["measures"] if m["agg"] == "ratio")
    assert ratio["expr"] is None
    assert all(isinstance(f["sql"], str) and f["sql"] for f in grades["filters"])


def test_expressions_reflect_the_tenants_overrides():
    target = "measure:dataset.student_grade.v1:average_grade_percentage"
    catalog = apply_overlays(load_catalog(), [Overlay(target=target, expr="ROUND(GRADE_PERCENTAGE, 0)", version=1)])
    measure = next(m for m in _dataset(public_catalog(catalog), "dataset.student_grade.v1")["measures"]
                   if m["name"] == "average_grade_percentage")
    assert measure["expr"] == "ROUND(GRADE_PERCENTAGE, 0)"
```

If `apply_overlays` has a different signature, match it as used in `lambda_handler._apply_each`.

- [ ] **Step 2: Run it.** Expect FAIL with `KeyError: 'expr'`.
- [ ] **Step 3: Implement.**
  - Add `"expr"` to `_MEASURE_FIELDS`.
  - Change filters to `{"name", "description", "sql"}`.
  - Docstring: "The catalog as callers see it: public datasets and metrics. Measures carry their expression and
    filters their condition, so a caller can show how a number is calculated; base SQL stays private."
- [ ] **Step 4: Run the full suite.** Expect PASS. If a test asserts that SQL is absent from the catalog, update it
  to check only `base_sql`.
- [ ] **Step 5: Ship.** Use `ship.sh`. After merge, deploy the API, then the frontend stack.

### Task 1.2 (F): Vitest, `describeQuery`, and Info on dashboard cards

**Files:**
- Create:
  - `vitest.config.ts`
  - `src/lib/describeQuery.ts`
  - `src/lib/describeQuery.test.ts`
  - `src/components/MetricInfo.tsx`
- Modify:
  - `package.json` (devDeps `vitest`, script `"test": "vitest run"`)
  - `src/types/semantic.ts` (add `expr?: string | null` to `CatalogMeasure`, and `sql: string` to the dataset
    filters)
  - `src/components/LiveKPICard.tsx` and `src/components/CardModals.tsx` (Info uses `MetricInfo`)

**Interfaces:**
- Produces:

```ts
export interface CalculationLine { label: string; detail: string; code?: string }
export interface QueryDescription {
  title: string;                // metric display name or measure name
  description: string;
  calculation: CalculationLine[]; // agg in words + expression; ratio → numerator and denominator lines
  dataset: { name: string; grain: string; description: string };
  filters: CalculationLine[];   // contract filters, metric default filters (with sql), time range
  transform?: string;           // words, filled in by 1.6/1.7
  overrides: string[];          // provenance.overlays
}
export function describeQuery(contract: QueryContract, catalog: SemanticCatalog, provenance?: Provenance,
                              transform?: string): QueryDescription[];   // one entry per metric/measure
```

  Aggregation words: count → "Count of", count_distinct → "Number of distinct", sum → "Total of",
  avg → "Average of", median → "Median of", min → "Lowest", max → "Highest".

- [ ] **Step 1:** `npm install -D vitest`. Add `vitest.config.ts`, using the `@` alias from `tsconfig` and the node
  environment. Add the `test` script.
- [ ] **Step 2: Write the failing tests** in `describeQuery.test.ts`, using a small hand-built `SemanticCatalog`
  fixture:
  - a metric whose measure is `avg GRADE_PERCENTAGE` with default filter `graded` (`sql: "GRADED_IND = 1"`)
    describes as "Average of" with code `GRADE_PERCENTAGE`, and lists the filter with its SQL;
  - a ratio measure gives two calculation lines, numerator and denominator, each with its own expression;
  - contract filters and a time range appear in `filters`;
  - `provenance.overlays` appear in `overrides`.
- [ ] **Step 3: Run** `npm test`. Expect FAIL: the module is missing.
- [ ] **Step 4: Implement** `describeQuery`. Then build `MetricInfo({ descriptions })`, which renders each
  description in the spec §3.2 order and shows code in monospace. Replace the card info modal's body with it,
  calling `describeQuery(card.contract, catalog, result.provenance)`.
- [ ] **Step 5:** `npm test`, `tsc`, `lint`, `build`. Expect PASS. Ship, deploy the frontend, and check a card's
  Info live.

### Task 1.3 (B): Roles and identity

**Files:**
- Create: `roles.py` (repo root, next to `lambda_handler.py`); `tests/test_roles.py`
- Modify:
  - `semantic_layer/compiler.py`: `compile_query(..., allow_identity: bool = False)`; the PII selection check is
    skipped when it is true
  - `semantic_layer/catalog_view.py`: `public_catalog(catalog, allow_identity=False)`; `selectable` is true for PII
    dimensions when allowed
  - `lambda_handler.py`:
    - `_compile_contract` and the catalog endpoint pass `allow_identity=role_allows_identity(user)`;
    - log `identity_query sub=<sub> contract=<json>` when a compiled query selects a PII dimension
  - `cdk/lib/base/auth.ts`: groups `illuminate-authors` and `illuminate-developers`
  - `cdk/test/check_template.py`: assert the two groups exist
  - `semantic_layer/chat_tools.py`: unchanged. It always compiles with the default `allow_identity=False`.

**Interfaces:**

```python
# roles.py
Role = Literal["admin", "author", "developer", "viewer"]
GROUP_ROLES = {"illuminate-admins": "admin", "illuminate-authors": "author", "illuminate-developers": "developer"}
def role_of(user: dict) -> Role: ...            # highest of the user's groups; no group -> "viewer"
def role_allows_identity(user: dict) -> bool:   # admin, author, developer
```

- [ ] **Step 1: Write the failing tests.**
  - `role_of` maps each group, and maps no groups to viewer.
  - `compile_query` with a PII dimension still raises by default, and compiles with `allow_identity=True`.
  - `POST /semantic/query` with a PII dimension returns 400 for a viewer, and 200 plus an `identity_query` log record
    for an author (`caplog`). Stub the warehouse as in `tests/test_semantic_query_endpoint.py`.
  - The catalog marks a PII dimension `selectable: true` for an admin and `false` for a viewer.
  - `ChatTools` `query_semantic` with a PII dimension errors, whatever the role.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement** as above. The PII check in `_group_query` becomes
  `if target.is_pii(dim.column) and not allow_identity: raise ...`. Thread `allow_identity` from `compile_query` to
  `_group_query`.
- [ ] **Step 4: Verify.** Run the full suite, CDK `tsc`, synth of the base stack, and `check_template.py`.
- [ ] **Step 5: Ship.**
  - Deploy base, then API, then frontend.
  - Live check, as `poctest@illuminate.com` (an admin): a query selecting a PII dimension works and logs
    `identity_query`.

### Task 1.4 (B): Cross-dataset filters (semi-join)

**Files:**
- Modify: `semantic_layer/compiler.py`
- Test: `tests/test_semantic_compiler.py`

**Interfaces:**
- **Semantics.** A contract filter, but not a dimension or time_range, may name a dimension of any dataset that
  shares an entity with the base and is not reachable by the many-to-one join. It compiles to
  `b.<base column> IN (SELECT <target column> FROM <target cte> WHERE <condition>)`.
- **Choosing the entity.** Prefer one that is `primary` in the target; otherwise use the first entity they share, in
  the base's order.
- **When it applies.** Only when `_resolve` would raise "is not complete", "would multiply" or "cannot be reached"
  for that filter **and** a shared entity exists.
- **Provenance.** The target's dataset id is added to the datasets read, so its CTE is emitted.

- [ ] **Step 1: Write the failing tests.**
  - A filter `dataset.course_filters_ih.v1:ih_level_1 eq "Nursing"` on a `dataset.course_student_activity.v1` measure
    compiles. Its SQL contains `IN (SELECT` and `DS_COURSE_FILTERS_IH_V1`. Its row source is not multiplied: the base
    `FROM` has no join to the IH CTE.
  - A filter on `dataset.sis_enrollment_attributes.v1:<a program dimension>`, applied to a dataset carrying both
    `course` and `person_course`, uses the person_course column.
  - The same IH reference used as a **dimension** still raises the existing "is not complete" error.
  - A filter on a dataset sharing no entity still raises "cannot be reached".
  - **Live smoke:** compile the first test's contract with `$S/smoke.py` and run it through illuminate-mcp. It returns
    rows.
- [ ] **Step 2: Run.** Expect FAIL with "is not complete".
- [ ] **Step 3: Implement.**
  - Add `_semi_join(base, ref, catalog) -> tuple[Dataset, DatasetDimension, str, str] | None`.
  - In `_group_query`'s filter loop, on `CompileError` from `_resolve` try `_semi_join`. If it finds a path, append
    `("semi", f, target, dim, mine, theirs)`; otherwise re-raise.
  - Build the condition with `exp.In(this=exp.column(mine, table=alias[base.id]),
    query=exp.select(exp.column(theirs)).from_(cte_name(target.id)).where(_condition(col, f)))`.
  - Return the semi targets among the read dataset ids, but not among `joined`.
- [ ] **Step 4:** Full suite, plus the live smoke. Expect PASS.
- [ ] **Step 5:** Ship and deploy the API, then the frontend stack.

### Task 1.5 (B): Terms, report definitions, and report endpoints

**Files:**
- Create:
  - `canonical/datasets/course/terms.yaml`: `dataset.terms.v1`, one row per `CDM_LMS.TERM`, primary entity `term`.
    Dimensions `term_name`, `term_start` and `term_end` (time); measure `terms`.
  - `semantic_layer/reports.py`
  - `canonical/reports/.gitkeep`
  - `tests/fixtures/reports/sample.yaml`
  - `tests/test_reports.py`, `tests/test_report_endpoints.py`
- Modify: `lambda_handler.py` (endpoints); `cdk/lib/api/lambda-proxy.ts`. The Lambda bundle already copies
  `canonical/`; confirm that `reports/` is included.

**Interfaces:**

```python
class ReportFilter(BaseModel):            # extra="forbid"
    id: str; label: str
    control: Literal["multi_select", "select", "date_range"]
    dimension: Optional[str] = None        # required unless date_range
    time_dimension: Optional[str] = None   # date_range: default time dimension ref
    default: Optional[Union[Literal["current_term", "last_30_days"], list]] = None
class VisualQuery(QueryContract-compatible dict)   # parsed as QueryContract; may add `time_dimension: str`
class Visual(BaseModel):
    id: str; type: Literal["kpi","bar","line","combo","pie","table","pivot","heatmap","histogram","scatter","treemap","text"]
    title: str; text: str = ""             # text visuals only
    queries: dict[str, dict] = {}          # name -> contract (+ optional time_dimension)
    transform: Optional[dict] = None       # {kind: ..., ...}; validated against TRANSFORM_KINDS
    encode: dict[str, Any] = {}
    filters_ignored: list[str] = []        # filter ids this visual opts out of
class Page(BaseModel): title: str; visuals: list[Visual]
class Report(BaseModel): id: str; title: str; area: Literal["learning","teaching","leading"]; description: str
                         filters: list[ReportFilter] = []; pages: list[Page]

def load_reports(root: Path = REPORTS_DIR) -> dict[str, Report]
def merged_contract(report, visual, query_name, values: dict[str, Any], catalog) -> tuple[QueryContract, list[str]]
    # values: filter id -> list of values (select) or {"start","end"} (date_range)
    # returns the contract with filter-bar filters merged, and the filter ids it had to ignore because its
    # dataset can't reach the dimension (tried by compiling; a CompileError naming the dimension means ignore)
def validate_report(report, catalog, database) -> list[str]   # problems; compiles every query with sample values
def resolve_defaults(report, today: date, terms: list[dict]) -> dict[str, Any]
    # current_term: terms with start <= today <= end, else the latest ended; last_30_days: today-30..today
```

**Endpoints:**
- `GET /api/v1/reports` → `{reports: [{id, title, area, description}]}`.
- `GET /api/v1/reports/{id}` → the definition plus `defaults`. The current term comes from a compiled
  `dataset.terms.v1` query, cached per instance for an hour.
- `POST /api/v1/reports/{id}/run` with `{visual, query, values}` → `{columns, rows, sql, provenance, contract,
  ignored_filters, truncated}`, or `{unavailable: "Requires Author access"}` when a viewer asks for a query whose
  dimensions are all identity.
  - For a viewer, identity dimensions are dropped from table queries before compiling.
  - It compiles via the same helper as `/semantic/query`, with the caller's tenant catalog and `allow_identity`.
- `TRANSFORM_KINDS = {"period_over_period", "percent_of_total", "unpivot", "top_n_other"}`. Phase 2 adds more as
  reports need them.

- [ ] **Step 1: Write the failing tests** (`test_reports.py`).
  - The fixture report loads.
  - `validate_report` returns `[]` for it, and names the visual and query for a broken measure.
  - `merged_contract` adds an `in` filter for a term selection, and a time_range on the query's `time_dimension` for
    a date range.
  - A filter whose dimension the dataset can't reach comes back in `ignored`.
  - `resolve_defaults` picks the spanning term, or the latest ended one between terms.
  - Every file in `canonical/reports/` validates against `load_catalog()`. With an empty dir, the test is
    parametrised over nothing.
- [ ] **Step 2: Write the endpoint tests** (`test_report_endpoints.py`), with the warehouse stubbed:
  - 401 without a token;
  - list, get with defaults, and run;
  - a viewer gets `unavailable` for an identity-only query, and has identity columns dropped from a mixed table
    query;
  - an author gets identity.
- [ ] **Step 3: Run.** Expect FAIL.
- [ ] **Step 4: Implement.** Smoke the `terms` dataset live via illuminate-mcp before relying on it.
- [ ] **Step 5:** Full suite, then ship and deploy (API, then frontend stack).

### Task 1.6 (F): Report renderer

**Files:**
- Create:
  - `src/services/reportsApi.ts`
  - `src/types/reports.ts`
  - `src/app/reporting/report/page.tsx` (client page reading `?id=`; static export can't pre-render unknown ids)
  - `src/components/reports/{ReportView,FilterBar,VisualCard,KpiVisual,BarVisual,LineVisual,PieVisual,TableVisual,TextVisual}.tsx`
  - `src/hooks/useReportVisual.ts`
- Modify: `src/app/reporting/page.tsx` lists API reports, grouped by area, above the existing mock list. The mock
  list goes with the first real report.

**Interfaces:**
- **Consumes:** the 1.5 endpoints, 1.2 `describeQuery` and `MetricInfo`, and 1.7 `applyTransform`. Until 1.7 lands,
  a visual with a transform shows "Coming soon".
- **Filter state:** in the URL query (`?id=…&f.term=…&f.dates=2026-01-01..2026-03-31`).
- **Visual runs:** `useReportVisual(reportId, visual, values)` runs every query of the visual through `/run` in
  parallel, at most 6 at a time across the page, caches by `(report, visual, values)`, and exposes
  `{results, error, retry}`.
- **Visual cards:** `VisualCard` wraps every visual with its title, Info (`MetricInfo`, from each query's returned
  `contract` and `provenance`), View SQL, and Pin as card (disabled for ungoverned or transform-only visuals). It also
  shows a muted line when some filters were ignored, and renders `unavailable` as a muted message.
- **Charts:** reuse the Recharts setup in `src/components/chat/ChartRenderer.tsx`. Tables use `ResultTable`.

- [ ] **Step 1:** Types and API client, checked by `tsc`.
- [ ] **Step 2:** `useReportVisual` and its request pool. Unit test the pool (at most 6 concurrent) with Vitest.
- [ ] **Step 3:** `FilterBar`: multi-select for terms, whose values are fetched with `/semantic/query` (the
  `terms` measure by `dataset.terms.v1:term_name`); a date range; reset to defaults.
- [ ] **Step 4:** The visual components and `ReportView` (page tabs, responsive grid).
- [ ] **Step 5:** `npm test`, `tsc`, `lint`, `build`, then ship. It is verified live with the first Phase 2 report.

### Task 1.7 (F): Client transforms

**Files:**
- Create: `src/reports/transforms.ts`, `src/reports/transforms.test.ts`
- Modify: `VisualCard`, which applies the transform and passes its words to `describeQuery`

**Interfaces:**

```ts
type Rows = Record<string, unknown>[];
type Results = Record<string, { columns: string[]; rows: Rows }>;
export function applyTransform(t: { kind: string; [k: string]: unknown }, results: Results): { columns: string[]; rows: Rows; words: string };
// period_over_period {value, baseline, field}: one row {value, baseline, change_pct} (null when baseline is 0 or missing)
// percent_of_total {query, field, by?}: adds `<field>_pct`, share within `by` group or overall
// unpivot {query, fields: [{field, label}]}: rows {category: label, value}
// top_n_other {query, field, label, n}: top n rows by field, the rest summed into label "Other"
```

- [ ] **Step 1: Write the failing tests,** at least 3 per kind. Include: a zero baseline gives null; percentages sum
  to 100 within each group; unpivot keeps the order of `fields`; top_n with fewer than n rows adds no "Other".
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement** the transforms, and each kind's `words` (for example, "% change vs the comparison
  period").
- [ ] **Step 4:** `npm test`, `tsc`, `lint`, `build`, then ship.

### Task 1.8 (B): Course-grain dimensions on `courses.v1`

**Files:**
- Modify: `canonical/datasets/course/courses.yaml`
- Test: `tests/test_semantic_definitions.py`, which already compiles every definition; add targeted asserts.

**Add:**
- **Base columns, copying the expressions in `course_filters_ih.yaml`:**
  - `COURSE_DURATION`: `IFF(COALESCE(co.END_TIME, te.END_TIME) IS NOT NULL, 'Fixed', 'Continuous')`;
  - `COURSE_CREATION_DATE`: `IFF(YEAR(co.CREATED_TIME) < 1970, TO_DATE('1970-01-01'), co.CREATED_TIME::DATE)`;
  - `DELIVERY_METHOD`: the course's SIS delivery method, joined as in lines 69–79 there, `'-'` when absent.
- **Course weeks:** `COURSE_WEEKS`, computed as `CEIL(DATEDIFF(day, start, LEAST(end, CURRENT_DATE())) / 7)` over the
  existing start and end.
- **Dimensions:**
  - `course_duration`, `delivery_method` (synonyms: modality, delivery mode), `course_weeks` (numeric);
  - `course_creation_date` (time, grains day to year).
- **Steps:**
  1. Write the test first: a contract grouping `dataset.course_student_activity.v1` students by
     `dataset.courses.v1:course_duration` and `delivery_method` compiles.
  2. Smoke the new base SQL live, then ship.

### Task 1.9 (B): Grading and platform definitions

**Files:**
- Modify: `grades/grade_response_time.yaml`, `platform/lms_sessions.yaml`, `collaborate/collab_sessions_by_slot.yaml`

**Add:**
- **GRT:**
  - **dimensions:**
    - `has_due_date` (boolean, `DUE_TIME IS NOT NULL`);
    - `due_time` (time, day to year);
    - `gradebook_name`: add `gb.TITLE AS GRADEBOOK_NAME` to the base. Check the column name against the CDM
      dictionary snapshot; if it's PII-flagged, add a `pii_exempt` reason.
    - `response_days_capped` (numeric, `LEAST(RESPONSE_DAYS, 91)`);
  - **measures:** `ungraded_attempts` (count_distinct `IFF(GRADED_IND = 0, GRADE_ID, NULL)`),
    `min_response_days`, `max_response_days`, `share_ungraded` (ratio).
- **`lms_sessions`:** dimension `session_end_date` (time, `LAST_ACCESSED_DATE`).
- **`collab_sessions_by_slot`:** measure `session_slots` (count `*`), "Session-slots: one per session per two-hour
  slot it ran in".
- **Steps:**
  1. Write the tests first: each new field compiles in a contract, and `has_due_date` filters.
  2. Smoke GRT live, then ship.

---

## Self-review

- **Spec coverage:**
  - §3.1 → 1.5; §3.2 → 1.2 and 1.6; §3.3 → 1.7 (core kinds; the rest come with the Phase 2 reports, see the ledger
    ruling); §4 Catalog for Info → 1.1.
  - §4 P1 → 1.4, P2 → 1.8, P9 → 1.9; §5 → 1.5; §6 → 1.3.
  - The P4, P19, P20, P21 and later datasets are Phases 2–5, per the roadmap.
- **Types:**
  - `allow_identity` is threaded the same way in 1.3 and 1.5.
  - `describeQuery`'s signature matches its uses in 1.6 and 1.7.
  - `TRANSFORM_KINDS` matches 1.7's kinds.
