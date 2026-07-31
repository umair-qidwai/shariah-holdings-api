"""Public response contracts used for validation and OpenAPI generation."""

from pydantic import BaseModel


class Generation(BaseModel):
    generation: str
    checked_at: str


class Health(Generation):
    status: str


class Exclusion(BaseModel):
    reason: str
    row_number: int
    symbol: str


class FundMetadata(BaseModel):
    excluded_row_count: int
    exclusions: list[Exclusion]
    fund: str
    holding_count: int
    holdings_date: str
    source_row_count: int
    source_url: str


class Metadata(Generation):
    acquired_at: str
    funds: list[FundMetadata]
    unique_symbols: int


class StockSummary(BaseModel):
    symbol: str
    name: str
    source: str
    membership: list[str]


class Holding(BaseModel):
    fund: str
    symbol: str
    name: str
    cusip: str | None
    weight: str
    shares: str
    market_value: str | None
    holdings_date: str
    source_url: str


class Page(Generation):
    total: int
    page: int
    page_size: int
    pages: int


class StocksPage(Page):
    items: list[StockSummary]


class StockDetail(StockSummary, Generation):
    funds: list[Holding]


class FundSummary(BaseModel):
    fund: str
    holding_count: int
    holdings_date: str
    source_url: str


class FundsResponse(Generation):
    items: list[FundSummary]


class FundHoldingsPage(Page):
    fund: str
    items: list[Holding]


class OverlapResponse(Generation):
    fund_totals: dict[str, int]
    etf_totals: dict[str, int]
    union: int
    exactly_one: int
    at_least_two: int
    pairwise_including_triple: dict[str, int]
    exact_only: dict[str, int]
