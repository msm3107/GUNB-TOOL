# Core Issue Tracker Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task with TDD and the repository's mandatory independent PR review.

**Goal:** Implement PR4 of projectplan.md: record customer feedback and expose an explicitly unverified data-quality label.

**Architecture:** SQLite v17 adds issue_reports and a storage-owned requires-verification flag on investments. A framework-independent IssueTracker validates and atomically records feedback only for cases delivered to that customer. Exports contain only the general label, never feedback descriptions or reporter identities.

**Tech Stack:** Python >=3.10, standard library, SQLite/WAL, existing openpyxl and pytest.

**Spec:** projectplan.md, pillar 3 and PR4 (core/issue_tracker.py).

## Global Constraints

- Keep /core independent from FastAPI, Starlette and Jinja2; retain the current engine without bot runtime.
- Use existing backup-before-migration and transactional versioning; no network or file generation inside report write transactions.
- Four issue types: zly_telefon, to_nie_inwestor_tylko_projektant, inwestycja_juz_stoi, inna_uwaga.
- Descriptions are normalized plain text, up to 1000 input characters; inna_uwaga requires nonblank text. HTML is not trusted markup.
- Report submission requires a completed delivery to the requesting customer, across that customer's orders.
- One report per customer/case/type; retries return the original report without changing its description or consuming quota.
- At most 20 new reports per rolling hour per customer, checked within the same short write transaction.
- Feedback means “Zgłoszona uwaga — wymaga weryfikacji”, not a verified error or an automatic change to registry status.
- Import/upsert cannot set or clear the quality flag. Resolution/moderation and HTTP routes remain later work.

## Review Focus

- A different customer or an undelivered case must not gain report access by guessing identifiers.
- Concurrent connections must not exceed the quota or produce duplicate reports.
- Invalid data and failed writes must leave both reports and the lead flag unchanged.
- Reimport, schema upgrades and migration packages must preserve quality state and existing B2B delivery memory.
- Untrusted descriptions must stay private and must not be interpreted as HTML, SQL or spreadsheet formulas.

### Task 1: Schema and storage-owned quality flag

**Files:** core/storage.py, core/models.py, tests/test_storage.py, tests/test_migration.py.

**Interfaces:** Investment.wymaga_weryfikacji: bool = False; Investment.data_quality_label: str | None. SQLite issue_reports(issue_id, customer_id, id_sprawy, typ, opis, zgloszono_utc), unique customer/case/type, indexed customer/time and case. Both foreign keys preserve the audit by restricting deletion.

- [x] Add failing v16→v17 migration/backup/constraint tests and import-preservation tests.
- [x] Run targeted tests and confirm the missing schema/flag failure.
- [x] Add v17 migration, model field/property and storage-owned field handling; update tests that pinned the old current version.
- [x] Verify storage and migration tests, then commit.

### Task 2: Report validation, authorization and atomic limits

**Files:** core/issue_tracker.py, tests/test_issue_tracker.py.

**Interfaces:** IssueType enum; frozen IssueReport; IssueNotAllowed(PermissionError), IssueRateLimitExceeded(RuntimeError). IssueTracker(repository).report_issue(customer_id: int, id_sprawy: str, issue_type: IssueType | str, description: str = "") -> IssueReport.

- [ ] Add failing tests for all four types, sanitization/limits, authorization across orders/customers, duplicate retries, quota boundary, concurrency and transactional rollback.
- [ ] Confirm RED, implement report_issue using parameterized SQL and repository.transaction(), then confirm GREEN.
- [ ] Verify no source status change or private report leakage, then commit.

### Task 3: Label in exports and end-to-end preservation

**Files:** core/export_package.py, tests/test_issue_tracker.py, tests/test_migration.py, README.md.

**Interfaces:** XLSX/CSV “Jakość danych” column consumes Investment.data_quality_label; it does not include the report body or customer ID.

- [ ] Add failing tests for the label in XLSX and CSV and issue state in export/import packages.
- [ ] Implement the label column and document the current API, defaults and unverified meaning.
- [ ] Run the full suite, Ruff for changed feature/test files, compileall, dependency ASCII and diff checks.
- [ ] Commit, push and open the next PR. Request independent review of the complete BASE..HEAD and check CI for the exact HEAD; fix P0/P1 and fix or explicitly justify P2.
- [ ] Save the report under ignored .local-review, update the PR description, and leave merge to the user.
