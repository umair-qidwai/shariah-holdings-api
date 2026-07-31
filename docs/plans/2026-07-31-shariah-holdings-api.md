# Shariah Holdings API Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Publish validated SPUS, HLAL, and MNZL holdings plus their deduplicated stock union through a public FastAPI service, refreshed by GitHub Actions and deployable to Vercel.

**Architecture:** A pure-Python refresh module downloads issuer data, normalizes all three schemas, validates each complete dataset, and writes deterministic CSV/JSON metadata atomically. A read-only FastAPI application loads those generated files, serves query endpoints and downloads, and renders a small server-side homepage. GitHub Actions runs tests and a weekday refresh, committing only when generated data changes; Vercel deploys the FastAPI app from the repository.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest, httpx, Playwright fallback for MNZL, GitHub Actions, Vercel Python runtime.

---

## Acceptance criteria

- The final allowlist is the exact deduplicated ticker union of valid positive-value equities from SPUS, HLAL, and MNZL.
- The detailed holdings CSV has one row per fund/ticker position and retains fund, ticker, name, CUSIP when available, weight, shares, market value when available, holdings date, and source URL.
- Cash, money-market rows, malformed identifiers, and zero-value residual entries do not enter the stock union.
- No generated file is replaced unless all three fund sources pass date, schema, row-count, and total-weight checks.
- The API provides health, metadata, stock lookup/search, fund holdings, overlap statistics, CSV downloads, OpenAPI docs, and a minimal responsive homepage.
- API responses expose source and as-of metadata and use pagination for list endpoints.
- The scheduled workflow runs after the U.S. close on weekdays, supports manual dispatch, runs tests first, refreshes, and commits only when files changed.
- A failed source or validation leaves the previous data untouched and fails the workflow.
- The Vercel deployment uses no writable local persistence and serves committed generated data.

## Task 1: Scaffold project and data models

**Files:** `pyproject.toml`, `requirements.txt`, `src/shariah_holdings/models.py`, `tests/test_models.py`, `.gitignore`

1. Write failing tests for normalized holdings and source metadata validation.
2. Run the focused tests and verify expected failures.
3. Implement only the models needed by the tests.
4. Run focused and full tests.

## Task 2: Implement deterministic normalization and merge

**Files:** `src/shariah_holdings/refresh.py`, `tests/test_refresh.py`, `tests/fixtures/*`

1. Add small official-schema fixtures for SPUS/HLAL and MNZL.
2. Write failing tests for schema parsing, exclusions, dates, weights, duplicate rejection, union deduplication, fund membership, and atomic fail-closed writes.
3. Implement the parsers and output builder.
4. Verify deterministic output and all edge cases.

## Task 3: Implement source acquisition

**Files:** `src/shariah_holdings/sources.py`, `scripts/refresh.py`, `tests/test_sources.py`

1. Write failing tests around HTTP download retries and injected MNZL acquisition.
2. Implement direct official feeds for SPUS and HLAL.
3. Implement MNZL acquisition with direct WordPress content extraction when available and a Playwright browser fallback; support a dated local official export for reproducible/manual recovery.
4. Ensure source acquisition never silently reuses stale/partial data during a successful refresh.

## Task 4: Build FastAPI and homepage

**Files:** `api/index.py`, `src/shariah_holdings/api.py`, `src/shariah_holdings/repository.py`, `src/shariah_holdings/templates/index.html`, `tests/test_api.py`

1. Write failing API tests for `/health`, `/metadata`, `/stocks`, `/stocks/{symbol}`, `/funds`, `/funds/{fund}/holdings`, `/overlap`, downloads, 404s, filters, and pagination.
2. Implement a read-only repository over generated files.
3. Implement API routes and cache/source metadata.
4. Add the minimal responsive homepage with counts, overlaps, refresh time, data-source explanation, docs link, and download links.
5. Run API and full tests.

## Task 5: Add generated datasets and deployment automation

**Files:** `data/*`, `.github/workflows/test.yml`, `.github/workflows/refresh.yml`, `vercel.json`, `README.md`, `LICENSE`

1. Seed data using the already validated July 31, 2026 official exports.
2. Configure Vercel routing to the FastAPI ASGI app.
3. Add CI for pushes and pull requests.
4. Add a weekday post-close UTC schedule and `workflow_dispatch`; grant only `contents: write`; commit only on `git diff`.
5. Document local development, endpoints, source caveats, fail-closed behavior, GitHub/Vercel setup, and custom-domain setup.

## Task 6: Integration verification and review

1. Run formatting/static checks if configured and the complete test suite.
2. Run the refresh against live SPUS/HLAL plus the dated official MNZL fixture and compare counts with the validated source state.
3. Start FastAPI locally and probe all public endpoints.
4. Review workflow permissions, untrusted CSV handling, path traversal, response sizes, cache behavior, and accidental secret inclusion.
5. Verify `git diff`, commit, push, inspect GitHub Actions, and verify the remote repository contents.
