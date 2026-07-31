# Shariah Holdings API

A read-only FastAPI service for validated holdings of **SPUS**, **HLAL**, and **MNZL**, plus their deduplicated ticker union. Generated snapshots are committed to Git so deployments need no database or writable filesystem.

Seeded snapshot (official exports dated 2026-07-31):

- SPUS: 216 equity holdings
- HLAL: 211 equity holdings
- MNZL: 487 equity holdings
- Union: 531 unique symbols

## API

Interactive OpenAPI documentation is available at `/docs`; the schema is at `/openapi.json`.

- `GET /` — small HTML status page
- `GET /health` — service status, generation, and check time
- `GET /metadata` — source URLs, holdings dates, row counts, and exclusions
- `GET /stocks` — paginated union; accepts `page`, `page_size`, `search`, and `fund`
- `GET /stocks/{symbol}` — one symbol and all fund positions
- `GET /funds` — supported funds and snapshot metadata
- `GET /funds/{fund}/holdings` — paginated fund holdings; accepts `page`, `page_size`, and `search`
- `GET /overlap` — pairwise/triple overlap counts
- `GET /downloads/shariah-list.csv` — deduplicated allowlist
- `GET /downloads/holdings.csv` — normalized fund positions

List responses default to 50 items and accept at most 100 per page. Successful generation-backed GET responses use ETags and a five-minute revalidation cache policy.

## Local development

Python 3.12 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test,browser]"
python -m playwright install chromium
python -m pytest
python scripts/verify_generation.py --data-dir data
uvicorn shariah_holdings.api:app --reload
```

Then open <http://127.0.0.1:8000/>, <http://127.0.0.1:8000/docs>, or, for example:

```bash
curl http://127.0.0.1:8000/health
curl 'http://127.0.0.1:8000/stocks?fund=SPUS&search=apple'
```

To validate a refresh without publishing it:

```bash
python -m shariah_holdings.cli \
  --mnzl-csv sources/mnzl-official-holdings-2026-07-31.csv \
  --dry-run
```

A successful non-dry refresh atomically publishes a content-addressed generation under `data/generations/` and updates `data/current.json` last. All three funds must pass schema, source-row, holding-count, total-weight, identifier, and freshness checks; otherwise the prior generation remains authoritative. SPUS and HLAL default to a 7-day maximum age. MNZL defaults to 120 days because it is published quarterly; `--max-age-days` and `--mnzl-max-age-days` configure these independently.

## MNZL limitation and manual update

SPUS and HLAL expose direct official CSV feeds. MNZL does **not reliably expose a stable public CSV URL or a machine-readable holdings as-of date**. The refresher therefore tries, in order:

1. a discoverable official MNZL download;
2. a Playwright Chromium download control;
3. only in the scheduled workflow, the latest dated official CSV staged in `sources/`.

A live MNZL result is accepted only when its date is issuer-labelled or manually asserted with `--mnzl-as-of YYYY-MM-DD`. A date conflict fails closed. The staged fallback is used only after live acquisition fails and retains the date in its filename; it is never silently relabelled. The workflow's fallback is currently `sources/mnzl-official-holdings-2026-07-31.csv`, so MNZL remains at that exact snapshot until a newer official export is reviewed and committed. Because the MNZL freshness ceiling is 120 days, the workflow fails closed rather than serving that fallback after it becomes too old; a newer reviewed official export is then required.

To update MNZL manually:

1. Download the complete CSV from the official Manzil Funds holdings control.
2. Confirm the issuer's exact holdings date and preserve the official file contents.
3. Name it `sources/mnzl-official-holdings-YYYY-MM-DD.csv`.
4. Run the dry-run command above with that path, run the full tests, and commit the reviewed source export. The next workflow run will select the lexicographically latest dated file as fallback.

For an undated live export that you have independently verified against an issuer-labelled date, use `--mnzl-as-of`; do not use that option to guess or advance a date.

## GitHub Actions refresh

`.github/workflows/refresh.yml` runs at **23:30 UTC Monday-Friday** (18:30 EST / 19:30 EDT) and supports manual `workflow_dispatch`. It:

1. checks out the branch and installs Python, test/browser extras, and Playwright Chromium with system dependencies;
2. runs the complete test suite;
3. selects the latest staged dated MNZL export;
4. refreshes SPUS and HLAL live, tries MNZL live first, and uses the staged MNZL only on live failure;
5. reloads and verifies the generated snapshot and count floors;
6. commits and pushes `data/` only when its generated contents changed.

Any download, validation, test, or verification failure stops the workflow without changing committed data. A data commit triggers the normal Vercel Git deployment. The workflow has only `contents: write`; repository branch protection must permit the GitHub Actions bot to push if automatic commits are desired.

## Deploy to Vercel

1. Push the repository to GitHub (local setup in this repository intentionally does not push for you).
2. In Vercel, choose **Add New → Project**, import the GitHub repository, and leave the repository root as the project root.
3. Vercel reads `vercel.json`, builds `api/index.py` with the Python runtime, and routes all requests to the FastAPI ASGI app. No build command, output directory, environment variable, database, or writable persistence is required.
4. Deploy, then check `/health`, `/metadata`, `/docs`, and both CSV downloads on the assigned Vercel URL.
5. In **Project Settings → Domains**, add `shariahdata.umairqidwai.com`. At the DNS provider for `umairqidwai.com`, create the CNAME record Vercel displays (typically pointing the subdomain to `cname.vercel-dns.com`), remove conflicting records, and wait for Vercel's DNS/SSL verification.

Every pushed code commit can produce a deployment. A scheduled refresh deploys only when the workflow creates and pushes a changed `data/` commit; unchanged generations produce no commit and therefore no refresh deployment.

Committed snapshots are deliberately simple and suitable for this dataset size. If history or repository/deployment size becomes excessive, move immutable generations to versioned object storage (for example, S3 or Cloudflare R2), publish a signed/versioned manifest, and have the API read that manifest without requiring local writes.

## Data and financial disclaimer

Data is derived from issuer-provided files and may be delayed, incomplete, changed, or erroneous. “Shariah” here means membership in one of the named funds' published holdings, not an independent screening determination, certification, recommendation, or guarantee. This project is for informational and software-development purposes only and is **not financial, investment, tax, legal, or religious advice**. Verify current holdings and screening methodology with the fund issuer and qualified advisers before making decisions.

## License

Code is available under the [MIT License](LICENSE). Issuer data may remain subject to its source's terms and is not relicensed by the MIT License.
