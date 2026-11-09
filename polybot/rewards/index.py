"""Materialized, size-independent reward-market index for the browsable markets list.

The launch picker used to run a heavy, *size-coupled* scan on every request
(``select_by_category``): it paged the pool, hydrated hundreds of books and re-ranked them for
the user's chosen quote size, all behind a 60s ``(limit, size)`` result cache. That does not scale
to a multi-user platform where every distinct size is a fresh hydration.

This module flips the model for the new paginated/filterable markets browser:

* **Annotate, never drop.** Every reward market becomes a row carrying a safety ``grade`` and a
  ``safe`` flag + ``gate_reasons`` (``gap_risk``/``live_sports``/``near_resolution``/``volatile``/
  ``extreme_price``/``one_sided``/``thin``), instead of being hard-excluded. The UI shows safe rows
  by default and reveals the risky ones behind a toggle.
* **Size-independent rows.** All the network-heavy features (books, volatility, sweep depth, mid,
  category) and the safety score are computed **once** at the market's own ``min_shares``
  (``required_size``). The user's bot quote size is a *launch* setting only and never re-ranks the
  list, so the index needs no ``size`` parameter and no per-size cache.
* **Materialized + stale-while-revalidate.** A detached refresh task maintains the rows for the
  whole eligible universe (cheap-gated full coverage + tiered book/volatility enrichment for the
  top-by-rate slice). Requests serve from memory and filter/sort/paginate locally — cheap.

Each row also carries the fields a *local* trading agent needs to start farming a chosen market
without re-running discovery (``token_a``/``token_b``, ``tick_size``, ``min_order_size``,
``rewards_max_spread``, ``rate_per_day``).

Read-only: this never places, cancels or influences orders.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from polybot.api.clob import ClobClient
from polybot.config import Settings
from polybot.models import Book
from polybot.rewards.models import RewardPair
from polybot.rewards.scanner import (
    CategoryResolver,
    ScanConfig,
    _competitor_q,
    _depth_farm_assessment,
    _fetch_pool,
    distance_score,
)
from polybot.rewards.selector import (
    GRADE_A,
    GRADE_B,
    W_CALM,
    W_CENTRAL,
    W_RESOLUTION,
    W_SWEEP,
    SelectorConfig,
    _adjusted_mid,
    _clamp01,
    _fetch_volatility,
    _hours_left,
    _in_zone_bid_depth,
    _is_gap_risk,
    _is_live_or_near_start_sports,
    _risk_label,
)

# Gate reasons that mark a market "risky" — hidden behind the UI's "show risky" toggle. Everything
# else (e.g. a missing book for an un-enriched tail row) leaves the market visible-by-default.
HARD_GATE_REASONS: frozenset[str] = frozenset(
    {
        "gap_risk",
        "live_sports",
        "near_resolution",
        "volatile_1d",
        "volatile",
        "extreme_price",
        "one_sided",
        "thin",
    }
)

DISPLAY_CATEGORIES: tuple[str, ...] = (
    "weather",
    "finance",
    "sports",
    "politics",
    "tech",
    "other",
)

# How many top-by-rate markets get full book + volatility enrichment per refresh. The rest stay in
# the list with cheap (metadata-only) signals and a pending grade; reward/min-shares sorting still
# works for them because those are metadata fields.
_HYDRATE_CAP = 800
_VOL_CHECK_CAP = 200


@dataclass
class IndexedMarket:
    """One size-independent row of the materialized market index."""

    condition_id: str
    question: str
    category: str
    rate_per_day: float  # the market's daily reward pool — the "Reward" column (size-independent)
    min_shares: float  # required_size = max(rewards_min_size, min_order_size)
    max_spread: float
    mid: float | None
    grade: str  # A | B | C | "" when not yet enriched
    safety: float  # 0..100
    safe: bool  # passes every hard gate (else shown only with the risky toggle on)
    gate_reasons: list[str]
    risk_label: str
    sweep_lots: float
    # Distance-weighted competitor liquidity in the reward zone (both legs), for the position-aware
    # earnings estimate. None on un-enriched tail rows (no book pass); the UI then falls back to the
    # competition floor.
    competitor_q: float | None
    volatility_10m: float
    volatility_2m: float
    vol_known: bool
    hours_left: float | None
    one_day_change: float
    volume_24hr: float
    enriched: bool  # whether a book/volatility pass was applied (else metadata-only)
    # --- fields a local trading agent needs to start farming this market ---
    token_a: str
    token_b: str
    outcome_a: str
    outcome_b: str
    tick_size: str
    min_order_size: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "question": self.question,
            "category": self.category,
            "rate_per_day": self.rate_per_day,
            "min_shares": self.min_shares,
            "max_spread": self.max_spread,
            "mid": self.mid,
            "grade": self.grade,
            "safety": self.safety,
            "safe": self.safe,
            "gate_reasons": self.gate_reasons,
            "risk_label": self.risk_label,
            "sweep_lots": self.sweep_lots,
            "competitor_q": self.competitor_q,
            "volatility_10m": self.volatility_10m,
            "volatility_2m": self.volatility_2m,
            "vol_known": self.vol_known,
            "hours_left": self.hours_left,
            "one_day_change": self.one_day_change,
            "volume_24hr": self.volume_24hr,
            "enriched": self.enriched,
            "token_a": self.token_a,
            "token_b": self.token_b,
            "outcome_a": self.outcome_a,
            "outcome_b": self.outcome_b,
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
        }


def assess_market(
    pair: RewardPair,
    book_a: Book | None,
    book_b: Book | None,
    *,
    category: str,
    vol: tuple[float, float] | None,
    now: float,
    cfg: SelectorConfig,
    scan_cfg: ScanConfig,
) -> IndexedMarket:
    """Build a size-independent row, annotating risk instead of dropping the market.

    Mirrors ``selector.evaluate``'s gates and safety scoring, but: (a) never returns ``None`` —
    every gate failure is appended to ``gate_reasons``; (b) uses the market's own ``required_size``
    as the reference size for depth feasibility and sweep, so the row does not depend on any user
    quote size. ``book_a``/``book_b``/``vol`` may be ``None`` for an un-enriched tail row, in which
    case only the cheap metadata gates apply and the grade stays provisional.
    """
    required_size = max(pair.rewards_min_size, pair.min_order_size)
    max_spread = float(pair.rewards_max_spread or 0.0)
    hours_left = _hours_left(pair)
    one_day = pair.one_day_price_change
    reasons: list[str] = []

    # --- cheap, size-independent metadata gates (apply with or without books) ---
    if _is_gap_risk(pair.question, category):
        reasons.append("gap_risk")
    if _is_live_or_near_start_sports(pair, category, now, cfg):
        reasons.append("live_sports")
    min_hours = cfg.min_hours_weather if category == "weather" else cfg.min_hours
    if hours_left is not None and hours_left < min_hours:
        reasons.append("near_resolution")
    if cfg.max_one_day_change > 0 and abs(one_day) > cfg.max_one_day_change:
        reasons.append("volatile_1d")

    mid: float | None = None
    sweep_lots = 0.0
    competitor_q: float | None = None
    vol_known = False
    range_10m = step_2m = 0.0
    enriched = book_a is not None and book_b is not None

    if enriched:
        cutoff = required_size
        mid = _adjusted_mid(book_a, cutoff)  # type: ignore[arg-type]
        if mid is None:
            reasons.append("one_sided")
        else:
            if mid < cfg.min_price or mid > 1.0 - cfg.min_price:
                reasons.append("extreme_price")
            assessment = _depth_farm_assessment(pair, book_a, book_b, scan_cfg, required_size)  # type: ignore[arg-type]
            if not assessment.farmable:
                reasons.append("thin")
            mid_b = _adjusted_mid(book_b, cutoff) or (1.0 - mid)  # type: ignore[arg-type]
            zone_a = _in_zone_bid_depth(book_a, mid, max_spread, cutoff)  # type: ignore[arg-type]
            zone_b = _in_zone_bid_depth(book_b, mid_b, max_spread, cutoff)  # type: ignore[arg-type]
            sweep_lots = min(zone_a, zone_b) / required_size if required_size > 0 else 0.0
            # Competitor liquidity for the earnings estimate (same measure the scanner uses).
            competitor_q, _top = _competitor_q([book_a, book_b], max_spread, cutoff)  # type: ignore[list-item]
        if vol is not None:
            vol_known = True
            range_10m, step_2m = vol
            if range_10m >= cfg.vol_range_10m_max or step_2m >= cfg.vol_step_2m_max:
                reasons.append("volatile")

    # --- safety score (size-independent: sweep is measured at required_size) ---
    if not vol_known:
        calm = 0.5  # unknown -> middling, never assume calm
    else:
        calm = _clamp01(
            1.0
            - 0.5 * (range_10m / cfg.vol_range_10m_max if cfg.vol_range_10m_max > 0 else 0.0)
            - 0.5 * (step_2m / cfg.vol_step_2m_max if cfg.vol_step_2m_max > 0 else 0.0)
        )
    sweep_score = _clamp01(sweep_lots / cfg.sweep_target_lots) if cfg.sweep_target_lots > 0 else 1.0
    centrality = _clamp01(1.0 - abs((mid if mid is not None else 0.5) - 0.5) / 0.5)
    resolution = 1.0 if hours_left is None else _clamp01(hours_left / 72.0)
    safety = 100.0 * (
        W_CALM * calm + W_SWEEP * sweep_score + W_CENTRAL * centrality + W_RESOLUTION * resolution
    )
    grade = "A" if safety >= GRADE_A else "B" if safety >= GRADE_B else "C"
    risk_label = _risk_label(
        vol_known=vol_known,
        calm=calm,
        sweep_score=sweep_score,
        centrality=centrality,
        resolution=resolution,
    )
    safe = not (set(reasons) & HARD_GATE_REASONS)

    # Display-only fallback: un-enriched tail rows (and one-sided books) have no book midpoint,
    # but Gamma metadata usually carries outcome prices. Gates, safety and centrality above are
    # computed strictly from the book mid — this only fills the Y·N column in the browser, where
    # the UI renders non-enriched prices as approximate.
    display_mid = mid
    if display_mid is None:
        meta = pair.metadata_price_a
        if meta is not None and 0.0 < meta < 1.0:
            display_mid = meta

    return IndexedMarket(
        condition_id=pair.condition_id,
        question=pair.question,
        category=category,
        rate_per_day=pair.rewards_rate_per_day,
        min_shares=required_size,
        max_spread=max_spread,
        mid=display_mid,
        grade=grade,
        safety=safety,
        safe=safe,
        gate_reasons=reasons,
        risk_label=risk_label,
        sweep_lots=sweep_lots,
        competitor_q=competitor_q,
        volatility_10m=range_10m,
        volatility_2m=step_2m,
        vol_known=vol_known,
        hours_left=hours_left,
        one_day_change=one_day,
        volume_24hr=pair.volume_24hr,
        enriched=enriched,
        token_a=pair.token_a,
        token_b=pair.token_b,
        outcome_a=pair.outcome_a,
        outcome_b=pair.outcome_b,
        tick_size=str(pair.tick_size),
        min_order_size=float(pair.min_order_size),
    )


# --- module-level materialized index (stale-while-revalidate, shared across all users) ---
_index_cache: dict[str, Any] = {"ts": 0.0, "rows": [], "task": None, "error": None}
# Gamma category metadata is slow and changes rarely; cache it far longer than the market rows.
_metadata_cache: dict[str, Any] = {"ts": 0.0, "by_slug": {}}
_METADATA_TTL = 3600.0


async def _build_resolver(
    settings: Settings, pairs: list[RewardPair], cfg: SelectorConfig
) -> CategoryResolver:
    """Category resolver backed by a long-lived Gamma-metadata cache (rebuilt only when stale)."""
    now = time.time()
    if _metadata_cache["by_slug"] and now - float(_metadata_cache["ts"]) < _METADATA_TTL:
        return CategoryResolver(dict(_metadata_cache["by_slug"]))
    resolver = await CategoryResolver.build(
        settings, pairs, metadata_event_limit=cfg.metadata_event_limit
    )
    if resolver.metadata_by_slug:
        _metadata_cache["by_slug"] = dict(resolver.metadata_by_slug)
        _metadata_cache["ts"] = now
    return resolver


def _eligible(pair: RewardPair, scan_cfg: ScanConfig) -> bool:
    """Cheap universe filter: a real reward market, not an explicitly-excluded family."""
    if pair.rewards_rate_per_day <= 0 or not pair.rewards_max_spread:
        return False
    haystack = f"{pair.question} {pair.event_slug or ''} {pair.market_slug or ''}".lower()
    return not any(kw in haystack for kw in scan_cfg.exclude_keywords)


async def _refresh_index(settings: Settings) -> None:
    """Page the pool, enrich the top-by-rate slice with books/volatility, and materialize rows.

    Detached task: owns its client and swallows its own errors so an unretrieved task exception
    cannot spam the server log. Full coverage is cheap (metadata only); only the richest-by-rate
    ``_HYDRATE_CAP`` markets pay for books, and the top ``_VOL_CHECK_CAP`` of those for volatility.
    """
    try:
        cfg = SelectorConfig.from_settings(settings)
        scan_cfg = ScanConfig.from_settings(settings, depth_based=True)
        now = time.time()
        async with ClobClient(settings) as clob:
            pool = await _fetch_pool(clob, cfg.pool_limit)
            pairs = [
                p
                for m in pool
                if (p := RewardPair.from_market(m)) is not None and _eligible(p, scan_cfg)
            ]
            resolver = await _build_resolver(settings, pairs, cfg)

            # Enrich the richest-by-rate slice; the long tail stays metadata-only.
            by_rate = sorted(pairs, key=lambda p: p.rewards_rate_per_day, reverse=True)
            shortlist = by_rate[:_HYDRATE_CAP]
            token_ids = [t for p in shortlist for t in (p.token_a, p.token_b)]
            books = await clob.get_order_books(token_ids) if token_ids else {}
            vols = await _fetch_volatility(clob, shortlist[:_VOL_CHECK_CAP], cfg)

        rows = [
            assess_market(
                pair,
                books.get(pair.token_a),
                books.get(pair.token_b),
                category=resolver.classify(pair),
                vol=vols.get(pair.token_a),
                now=now,
                cfg=cfg,
                scan_cfg=scan_cfg,
            )
            for pair in pairs
        ]
        _index_cache["rows"] = rows
        _index_cache["ts"] = time.time()
        _index_cache["error"] = None
    except Exception as exc:  # noqa: BLE001 — read-only refresh; record and let the next call retry
        _index_cache["error"] = f"{type(exc).__name__}: {exc}"


def ensure_index_refresh(settings: Settings) -> None:
    """Kick a background index refresh if none is running and the materialized rows are stale."""
    ttl = settings.reward_mm_selector_pool_cache_seconds
    if _index_cache["rows"] and time.time() - float(_index_cache["ts"]) < ttl:
        return
    task = _index_cache["task"]
    if task is None or task.done():
        _index_cache["error"] = None
        _index_cache["task"] = asyncio.create_task(_refresh_index(settings))


def index_status(settings: Settings) -> str:
    """'ready' (have rows), 'warming' (first load running, none yet), or 'error' (load failed)."""
    if _index_cache["rows"]:
        return "ready"
    task = _index_cache["task"]
    if task is not None and not task.done():
        return "error" if _index_cache["error"] else "warming"
    return "error" if _index_cache["error"] else "warming"


# --- query: filter / sort / paginate over the materialized rows (all in-memory, cheap) ---

_SORT_KEYS = {
    "reward": lambda m: m.rate_per_day,
    "min_shares": lambda m: m.min_shares,
    "safety": lambda m: m.safety,
    "hours_left": lambda m: (m.hours_left if m.hours_left is not None else 1e9),
    "volume": lambda m: m.volume_24hr,
}


@dataclass(frozen=True)
class MarketQuery:
    """Filter / sort / pagination spec for the materialized index."""

    page: int = 1
    page_size: int = 24
    search: str = ""
    categories: frozenset[str] = field(default_factory=frozenset)
    grades: frozenset[str] = field(default_factory=frozenset)
    include_risky: bool = False
    min_shares: float | None = None  # market's required_size >= this
    max_shares: float | None = None  # market's required_size <= this
    min_hours: float | None = None  # market's hours_left >= this (min runway before resolution)
    sort: str = "reward"
    descending: bool = True


def _matches(row: IndexedMarket, q: MarketQuery, needle: str) -> bool:
    if not q.include_risky and not row.safe:
        return False
    if q.categories and row.category not in q.categories:
        return False
    if q.grades and row.grade not in q.grades:
        return False
    if q.min_shares is not None and row.min_shares < q.min_shares:
        return False
    if q.max_shares is not None and row.min_shares > q.max_shares:
        return False
    # Minimum runway before resolution. A row with no end date (hours_left is None) has unbounded
    # runway — the same "far future" treatment the hours_left sort key uses — so it passes.
    if q.min_hours is not None and row.hours_left is not None and row.hours_left < q.min_hours:
        return False
    if needle and needle not in row.question.lower():
        return False
    return True


def query_index(q: MarketQuery) -> dict[str, Any]:
    """Return one page of rows plus totals and category facets for the current filter set.

    ``facets`` count the safe-or-risky-respecting matches per category *ignoring* the category
    filter itself, so the UI's category chips can show how many markets each would reveal.
    """
    rows: list[IndexedMarket] = _index_cache["rows"]
    needle = q.search.strip().lower()

    matched = [m for m in rows if _matches(m, q, needle)]
    key = _SORT_KEYS.get(q.sort, _SORT_KEYS["reward"])
    matched.sort(key=key, reverse=q.descending)

    # Category facets: same filters but ignoring the category selection.
    facet_q = MarketQuery(
        include_risky=q.include_risky,
        grades=q.grades,
        min_shares=q.min_shares,
        max_shares=q.max_shares,
        min_hours=q.min_hours,
        search=q.search,
    )
    facets: dict[str, int] = {cat: 0 for cat in DISPLAY_CATEGORIES}
    for m in rows:
        if _matches(m, facet_q, needle):
            facets[m.category] = facets.get(m.category, 0) + 1

    total = len(matched)
    page = max(1, q.page)
    page_size = max(1, q.page_size)
    start = (page - 1) * page_size
    items = matched[start : start + page_size]
    return {
        "items": [m.to_dict() for m in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "facets": facets,
        "updated_at": _index_cache["ts"],
    }


def _parse_csv_set(value: str | None, allowed: frozenset[str] | set[str]) -> frozenset[str]:
    """Parse a comma-separated filter param into the subset of allowed values."""
    if not value:
        return frozenset()
    return frozenset(part for part in (p.strip() for p in value.split(",")) if part in allowed)


def markets_page(
    settings: Settings,
    *,
    page: int = 1,
    page_size: int = 24,
    search: str = "",
    category: str = "",
    grade: str = "",
    include_risky: bool = False,
    min_shares: float | None = None,
    max_shares: float | None = None,
    min_hours: float | None = None,
    sort: str = "reward",
    descending: bool = True,
) -> dict[str, Any]:
    """Endpoint helper: kick the SWR refresh, run the query, and annotate warming/error status.

    Thin and framework-agnostic so the platform and single-tenant dashboard share one code path.
    """
    ensure_index_refresh(settings)
    status = index_status(settings)
    query = MarketQuery(
        page=page,
        page_size=min(max(int(page_size), 1), 100),
        search=search or "",
        categories=_parse_csv_set(category, frozenset(DISPLAY_CATEGORIES)),
        grades=_parse_csv_set(grade, frozenset({"A", "B", "C"})),
        include_risky=bool(include_risky),
        min_shares=min_shares,
        max_shares=max_shares,
        min_hours=min_hours,
        sort=sort if sort in _SORT_KEYS else "reward",
        descending=bool(descending),
    )
    data = query_index(query)
    data["status"] = status
    # 'warming' only while the FIRST full load is still running (no rows yet); a filter that simply
    # matches nothing on a ready index is an empty page, not warming.
    data["warming"] = status == "warming"
    data["error"] = "market index unavailable" if status == "error" else None
    # Constants for the UI's position-aware earnings estimate (single source of truth = engine
    # config). The UI computes, per selected market: q_self = q_self_per_share * size; share =
    # min(share_cap, q_self / (q_self + max(competitor_q, competition_floor) * competitor_haircut));
    # est/day = rate_per_day * share * reward_realism_factor.
    scan_cfg = ScanConfig.from_settings(settings, depth_based=True)
    data["estimate"] = {
        "q_self_per_share": distance_score(1.0, scan_cfg.quote_distance_fraction),
        "competitor_haircut": scan_cfg.competitor_haircut,
        "share_cap": scan_cfg.share_cap,
        "competition_floor": scan_cfg.competition_floor,
        "reward_realism_factor": scan_cfg.reward_realism_factor,
    }
    return data
