"""FastAPI application for the generated Shariah holdings datasets."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from .api_models import (
    FundHoldingsPage, FundsResponse, Health, Metadata, OverlapResponse,
    StockDetail, StocksPage,
)
from .models import TICKER_RE
from .repository import DataUnavailable, FUNDS, HoldingsRepository, Snapshot

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
ASSETS = Path(__file__).parent / "assets"
STRICT_CSP = ("default-src 'self'; style-src 'self'; object-src 'none'; "
              "frame-ancestors 'none'; base-uri 'self'")
DOCS_CSP = (STRICT_CSP + "; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https://fastapi.tiangolo.com")


def _repository(request: Request) -> HoldingsRepository:
    return request.app.state.repository


def _snapshot(request: Request, repository: Annotated[HoldingsRepository, Depends(_repository)]) -> Snapshot:
    snapshot = repository.load()
    request.state.generation = snapshot.generation
    return snapshot


def _meta(snapshot: Snapshot) -> dict[str, str]:
    return {"generation": snapshot.generation, "checked_at": snapshot.checked_at}


def _normalize_fund(fund: str, *, not_found: bool = False) -> str:
    if not fund.isascii():
        status = 404 if not_found else 422
        raise HTTPException(status_code=status, detail=f"Unsupported fund: {fund}")
    normalized = fund.upper()
    if fund != fund.strip() or normalized not in FUNDS:
        status = 404 if not_found else 422
        raise HTTPException(status_code=status, detail=f"Unsupported fund: {fund}")
    return normalized


def _holding(row: dict[str, str]) -> dict[str, str | None]:
    return {key: (value if value != "" else None) for key, value in row.items()}


def _stock_items(snapshot: Snapshot) -> list[dict[str, Any]]:
    by_symbol = snapshot.holdings_by_symbol
    return [
        {
            "symbol": row["symbol"],
            "name": row["name"],
            "source": row["source"],
            "membership": sorted(item["fund"] for item in by_symbol[row["symbol"]]),
        }
        for row in snapshot.allowlist
    ]


def _page(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size),
    }


def _overlap(snapshot: Snapshot) -> dict[str, Any]:
    sets = {
        fund: {row["symbol"] for row in snapshot.holdings_by_fund[fund]}
        for fund in FUNDS
    }
    hlal, mnzl, spus = sets["HLAL"], sets["MNZL"], sets["SPUS"]
    triple = spus & hlal & mnzl
    exact = {
        "SPUS": spus - hlal - mnzl,
        "HLAL": hlal - spus - mnzl,
        "MNZL": mnzl - spus - hlal,
        "SPUS_HLAL": (spus & hlal) - mnzl,
        "SPUS_MNZL": (spus & mnzl) - hlal,
        "HLAL_MNZL": (hlal & mnzl) - spus,
        "SPUS_HLAL_MNZL": triple,
    }
    totals = {fund: len(sets[fund]) for fund in ("SPUS", "HLAL", "MNZL")}
    return {
        "fund_totals": totals,
        "etf_totals": totals,
        "union": len(spus | hlal | mnzl),
        "exactly_one": sum(len(exact[fund]) for fund in ("SPUS", "HLAL", "MNZL")),
        "at_least_two": len((spus & hlal) | (spus & mnzl) | (hlal & mnzl)),
        "pairwise_including_triple": {
            "SPUS_HLAL": len(spus & hlal),
            "SPUS_MNZL": len(spus & mnzl),
            "HLAL_MNZL": len(hlal & mnzl),
            "SPUS_HLAL_MNZL": len(triple),
        },
        "exact_only": {name: len(symbols) for name, symbols in exact.items()},
        **_meta(snapshot),
    }


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    """Create an app with an injectable authoritative data directory."""

    configured = data_dir or os.environ.get("SHARIAH_DATA_DIR")
    directory = Path(configured) if configured else Path(__file__).resolve().parents[2] / "data"
    app = FastAPI(
        title="Shariah Holdings API",
        description="Read-only validated holdings for SPUS, HLAL, and MNZL.",
        version="0.1.0",
    )
    app.state.repository = HoldingsRepository(directory)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    @app.middleware("http")
    async def security_and_cache_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        csp = DOCS_CSP if request.url.path in {"/docs", "/docs/oauth2-redirect", "/redoc"} else STRICT_CSP
        response.headers["Content-Security-Policy"] = csp
        # Starlette adds the simple-request CORS header regardless of method.
        # Do not advertise non-GET responses as cross-origin-readable.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            for header in ("Access-Control-Allow-Origin", "Access-Control-Allow-Credentials"):
                if header in response.headers:
                    del response.headers[header]
        generation = getattr(request.state, "generation", None)
        if response.status_code == 503:
            response.headers["Cache-Control"] = "no-store"
        elif generation and response.status_code == 200 and request.method == "GET":
            resource = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            suffix = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:12]
            etag = f'"{generation}-{suffix}"'
            if request.headers.get("if-none-match") == etag:
                headers = {
                    "ETag": etag,
                    "Cache-Control": "public, max-age=300, must-revalidate",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": csp,
                }
                if request.headers.get("origin"):
                    headers["Access-Control-Allow-Origin"] = "*"
                response = Response(status_code=304, headers=headers)
            else:
                response.headers["ETag"] = etag
                response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
        return response

    @app.exception_handler(DataUnavailable)
    async def unavailable(_: Request, __: DataUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "Authoritative data is unavailable"})

    @app.get("/", include_in_schema=False)
    def homepage(request: Request, snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        overlap = _overlap(snapshot)
        funds = {item["fund"]: item for item in snapshot.metadata["funds"]}
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={"snapshot": snapshot, "overlap": overlap, "funds": funds},
        )

    @app.get("/assets/site.css", include_in_schema=False)
    def site_stylesheet():
        return FileResponse(ASSETS / "site.css", media_type="text/css")

    @app.get("/health", tags=["service"], response_model=Health)
    def health(snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        return {"status": "ok", **_meta(snapshot)}

    @app.get("/metadata", tags=["service"], response_model=Metadata)
    def metadata(snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        return {**snapshot.metadata, "generation": snapshot.generation}

    @app.get("/stocks", tags=["stocks"], response_model=StocksPage)
    def stocks(
        snapshot: Annotated[Snapshot, Depends(_snapshot)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        search: Annotated[str | None, Query(max_length=100)] = None,
        fund: Annotated[str | None, Query(max_length=10)] = None,
    ):
        items = _stock_items(snapshot)
        if search:
            needle = search.strip().casefold()
            items = [item for item in items if needle in item["symbol"].casefold() or needle in item["name"].casefold()]
        if fund:
            normalized = _normalize_fund(fund)
            items = [item for item in items if normalized in item["membership"]]
        return {**_page(items, page, page_size), **_meta(snapshot)}

    @app.get("/stocks/{symbol}", tags=["stocks"], response_model=StockDetail)
    def stock(symbol: str, snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        if not symbol.isascii():
            raise HTTPException(status_code=404, detail="Stock not found")
        normalized = symbol.upper()
        if symbol != symbol.strip() or not TICKER_RE.fullmatch(normalized):
            raise HTTPException(status_code=404, detail="Stock not found")
        allow = snapshot.allowlist_by_symbol.get(normalized)
        if allow is None:
            raise HTTPException(status_code=404, detail="Stock not found")
        positions = sorted(
            (_holding(row) for row in snapshot.holdings_by_symbol[normalized]),
            key=lambda row: str(row["fund"]),
        )
        return {
            "symbol": allow["symbol"],
            "name": allow["name"],
            "source": allow["source"],
            "membership": [row["fund"] for row in positions],
            "funds": positions,
            **_meta(snapshot),
        }

    @app.get("/funds", tags=["funds"], response_model=FundsResponse)
    def funds(snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        items = [
            {
                "fund": item["fund"],
                "holding_count": item["holding_count"],
                "holdings_date": item["holdings_date"],
                "source_url": item["source_url"],
            }
            for item in snapshot.metadata["funds"]
        ]
        return {"items": sorted(items, key=lambda item: item["fund"]), **_meta(snapshot)}

    @app.get("/funds/{fund}/holdings", tags=["funds"], response_model=FundHoldingsPage)
    def fund_holdings(
        fund: str,
        snapshot: Annotated[Snapshot, Depends(_snapshot)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        search: Annotated[str | None, Query(max_length=100)] = None,
    ):
        normalized = _normalize_fund(fund, not_found=True)
        items = [_holding(row) for row in snapshot.holdings_by_fund[normalized]]
        if search:
            needle = search.strip().casefold()
            items = [item for item in items if needle in str(item["symbol"]).casefold() or needle in str(item["name"]).casefold()]
        return {"fund": normalized, **_page(items, page, page_size), **_meta(snapshot)}

    @app.get("/overlap", tags=["stocks"], response_model=OverlapResponse)
    def overlap(snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        return _overlap(snapshot)

    @app.get("/downloads/shariah-list.csv", tags=["downloads"])
    def shariah_csv(snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        return Response(
            snapshot.allowlist_csv,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="shariah-list.csv"'},
        )

    @app.get("/downloads/holdings.csv", tags=["downloads"])
    def holdings_csv(snapshot: Annotated[Snapshot, Depends(_snapshot)]):
        return Response(
            snapshot.holdings_csv,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="holdings.csv"'},
        )

    return app


app = create_app()
