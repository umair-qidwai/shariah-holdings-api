# Shariah Holdings API

A read-only FastAPI service for validated holdings of **SPUS**, **HLAL**, and **MNZL**, plus their deduplicated ticker union. Generated snapshots are committed to Git, so deployments need no database or writable filesystem.

Current snapshot (official exports dated 2026-07-31): SPUS 216 · HLAL 211 · MNZL 487 · 531 unique symbols.

## API

Interactive OpenAPI docs at `/docs` (schema at `/openapi.json`).

- `GET /` — HTML status page
- `GET /health` — status, generation, check time
- `GET /metadata` — source URLs, holdings dates, row counts, exclusions
- `GET /stocks` — paginated union; `page`, `page_size`, `search`, `fund`
- `GET /stocks/{symbol}` — one symbol with all fund positions
- `GET /funds` — supported funds and snapshot metadata
- `GET /funds/{fund}/holdings` — paginated fund holdings; `page`, `page_size`, `search`
- `GET /overlap` — pairwise/triple overlap counts
- `GET /downloads/shariah-list.csv` — deduplicated allowlist
- `GET /downloads/holdings.csv` — normalized fund positions

List responses default to 50 items (max 100 per page). Generation-backed GET responses use ETags and a five-minute revalidation cache.

## Local development

Python 3.12 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test,browser]"
python -m playwright install chromium
python -m pytest
python scripts/verify_generation.py --data-dir data
uvicorn shariah_holdings.api:app --reload
```

Then open <http://127.0.0.1:8000/> or <http://127.0.0.1:8000/docs>.

To validate a refresh without publishing it:

```bash
python -m shariah_holdings.cli \
  --mnzl-csv sources/mnzl-official-holdings-2026-07-31.csv \
  --dry-run
```

A real refresh serializes publication with a file lock, atomically writes a content-addressed generation under `data/generations/`, and updates `data/current.json` last. If normalized holdings are unchanged, the refresh is a byte-for-byte no-op. All three funds must pass schema, row-count, total-weight, identifier, and freshness checks; otherwise the prior generation remains authoritative. SPUS and HLAL default to a 7-day maximum age; MNZL defaults to 120 days because it publishes quarterly (`--max-age-days` and `--mnzl-max-age-days` configure these independently).

## MNZL limitation and manual update

SPUS and HLAL expose direct official CSV feeds. MNZL does not reliably expose a stable public CSV URL or machine-readable as-of date, so the refresher tries, in order: a discoverable official download, a Playwright Chromium download control, and (scheduled workflow only) the latest dated export staged in `sources/`.

A live MNZL result is accepted only when its date is issuer-labelled or manually asserted with `--mnzl-as-of YYYY-MM-DD`; a conflict fails closed. The staged fallback (`sources/mnzl-official-holdings-2026-07-31.csv`) is used only after live acquisition fails and keeps the date in its filename — it is never silently relabelled. Because the MNZL freshness ceiling is 120 days, the workflow fails closed rather than serving that fallback once it becomes too old; a newer reviewed export is then required.

To update MNZL manually:

1. Download the complete CSV from the official Manzil Funds holdings control.
2. Confirm the issuer's exact holdings date and preserve the official file contents.
3. Name it `sources/mnzl-official-holdings-YYYY-MM-DD.csv`.
4. Run the dry-run command above, run the full tests, and commit the reviewed export. The next workflow run selects the lexicographically latest dated file as fallback.

For an undated live export verified against an issuer-labelled date, use `--mnzl-as-of`; do not use that option to guess or advance a date.

## GitHub Actions refresh

`.github/workflows/refresh.yml` runs at **23:30 UTC Monday–Friday** (18:30 EST / 19:30 EDT) and supports manual `workflow_dispatch`. It:

1. validates in a read-only job using full-SHA-pinned Actions, non-persisted checkout credentials, and exact reviewed dependency constraints;
2. runs the complete test suite, selects the latest staged dated MNZL export, and performs the fail-closed refresh;
3. verifies snapshot/count floors and uploads a checksummed generated-data artifact;
4. uses a separate minimal `contents: write` job to verify the artifact and ensure the branch has not moved;
5. commits only actual `data/` changes; only the final push step sees the ephemeral token.

Any download, validation, test, or verification failure stops the workflow without changing committed data. A data commit triggers the normal Vercel Git deployment. Repository branch protection must permit the GitHub Actions bot to push if automatic commits are desired.

## Deploy to Vercel

1. Push the repository to GitHub (local setup in this repository intentionally does not push for you).
2. In Vercel: **Add New → Project**, import the repository, and leave the repository root as the project root.
3. Vercel reads `vercel.json`, builds `api/index.py` with the Python runtime, and routes all requests to the FastAPI ASGI app. No build command, output directory, environment variable, database, or writable persistence is required.
4. Deploy, then check `/health`, `/metadata`, `/docs`, and both CSV downloads on the assigned Vercel URL.
5. In **Project Settings → Domains**, add `shariahdata.umairqidwai.com`. At the DNS provider for `umairqidwai.com`, create the CNAME record Vercel displays (typically `cname.vercel-dns.com`), remove conflicting records, and wait for Vercel's DNS/SSL verification.

Every pushed code commit can produce a deployment; a scheduled refresh deploys only when it produces a changed `data/` commit.

## Data and financial disclaimer

Data is derived from issuer-provided files and may be delayed, incomplete, changed, or erroneous. “Shariah” here means membership in one of the named funds' published holdings, not an independent screening determination, certification, recommendation, or guarantee. This project is for informational and software-development purposes only and is **not financial, investment, tax, legal, or religious advice**. Verify current holdings and screening methodology with the fund issuer and qualified advisers before making decisions.

## License

Code is available under the [MIT License](LICENSE). Issuer data may remain subject to its source's terms and is not relicensed by the MIT License.
