# Standard reports, Phase 2g: Learning Tools Adoption. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship Learning Tools Adoption: two pages, with a key-metric selector (users, courses, tools, minutes)
that drives several visuals.

**Spec:** `docs/superpowers/specs/2026-10-09-standard-reports-design.md`. The source is in the inventory, section
`LEADING_LEARNING_TOOLS_ADOPTION`.

## Global constraints

As in Phase 2a.

## Review focus

1. Switching the key metric changes the measure of the visuals that follow it, and only those, without breaking
   their encode.
2. A choice value that isn't one of the options is rejected (400), never compiled.
3. The comparison page's comparison queries use only the comparison range.
4. Grouping by node uses the primary node and the child of the chosen node.

---

### Task 2g.1 (B): Choice controls and `measure_from`

- **`ReportFilter.control: "choice"`** has `options: [labels]` and a `default` that is one of them. It is not a
  filter: `merged_contract` skips it, and `resolve_defaults` returns `[default]`.
- **A query's `measure_from: {filter, measures: {label: ref}, as}`** puts the chosen option's measure first.
  `order_by` on `as` sorts by it, and `/run` returns it as `as`. An unknown value raises `ReportValueError`.
- **Validation:**
  - the choice's options are non-empty and its default is one of them;
  - `measure_from` names a choice filter and maps every option;
  - each measure compiles.
- **Tests first:** chosen and default measure, alias ordering and rename, a bad value raising, and validation.

### Task 2g.2 (F): Choice control

A choice filter renders a select of its static options with no "All". The URL state and reset behave like other
filters. Tested with Vitest.

### Task 2g.3 (B): The report

**Filters:**
- primary and comparison date ranges on activity date (last 30 days and previous 30 days);
- term (current term);
- ih1–4 on `courses.v1`;
- role, course and tool;
- key metric: User count, Course count, Tool count, Time spent on tools (minutes).

**Overview:**
- users by role (donut);
- most used tools by the key metric (top 20, horizontal bars);
- the key metric by child node;
- the key metric by week;
- help text.

**Comparison:**
- the key metric by week, for each range;
- users and courses using tools, vs the comparison range;
- item coverage by type ("Are tools being accessed?");
- the courses using tools the most (table).

The per-user table waits for the persons dataset in Phase 5.
