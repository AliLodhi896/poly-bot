"""Safety-first reward-market selector (rebuild of scanner.py's selection logic).

The old scanner ranked by projected reward with soft risk *multipliers*, so a high-reward toxic
market could still float to the top (the run-48 NBA-draft fill is the canonical failure). This
module inverts the priority for a passive liquidity-rewards maker whose #1 goal is to NOT get
filled:

  Stage 1 — hard gates. A market that fails ANY gate is excluded outright; reward never buys back
            risk. Gates: affordability, toxic category (gap/event + live sports), time-to-
            resolution, 1-day move, two-sided book, extreme price, depth feasibility, realized
            volatility (with a conservative fallback when prices-history is unavailable).
  Stage 2 — rank the survivors by projected reward, tie-break by a 0..100 safety score.

Output is deliberately compact (a safety grade + reward + one-line risk label, with a details
dict for an optional expander) to replace the old per-market wall of flags. This module is
read-only and never touches the trading path.

Transitional note: low-level primitives (category classification, the reward model, depth
feasibility, the pool pager) are imported from ``scanner`` so the rebuild stays focused on the new
selection logic. When ``scanner.py`` is retired those move here or into a shared module.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from polybot.api.clob import ClobClient
from polybot.config import Settings
from polybot.models import Book
from polybot.rewards.models import RewardPair
from polybot.rewards.scanner import (
    GAP_RISK_KEYWORDS,
    CategoryResolver,
    ScanConfig,
    _competitor_q,
    _depth_farm_assessment,
    _fetch_pool,
    _history_window_range,
    projected_daily_reward,
)

# Safety grade cutoffs (0..100). Tunable here, not user-facing yet.
GRADE_A = 75.0
GRADE_B = 55.0

# Safety-score component weights (must sum to 1.0).
W_CALM = 0.40  # realized volatility is the #1 fill driver (gaps blow through any buffer)
W_SWEEP = 0.30  # in-zone depth a taker must eat to reach us
W_CENTRAL = 0.15  # distance from 0/1 (extreme legs are one-sided, costly on a fill)
W_RESOLUTION = 0.15  # runway before convergence/news


@dataclass(frozen=True)
class SelectorConfig:
    """Hard-gate thresholds + scoring knobs. Defaults come from Settings."""

    size_shares: float
    min_price: float = 0.10
    min_hours: float = 18.0
    min_hours_weather: float = 12.0
    max_one_day_change: float = 0.15
    vol_range_10m_max: float = 0.08
    vol_step_2m_max: float = 0.04
    sweep_target_lots: float = 20.0
    size_fit_k: float = 0.25
    sports_pre_seconds: float = 1800.0
    sports_post_seconds: float = 14400.0
    pool_limit: int = 20000
    hydrate_per_category: int = 60
    hydrate_limit: int = 600
    vol_check_per_category: int = 30
    vol_concurrency: int = 16
    vol_timeout_seconds: float = 2.0
    metadata_event_limit: int = 6000  # max Gamma /events paged in the bulk category sweep

    @classmethod
    def from_settings(
        cls, settings: Settings, *, size_shares: float | None = None
    ) -> SelectorConfig:
        return cls(
            size_shares=size_shares if size_shares is not None else settings.reward_mm_size_shares,
            min_price=settings.reward_mm_selector_min_price,
            min_hours=settings.reward_mm_selector_min_hours,
            min_hours_weather=settings.reward_mm_selector_min_hours_weather,
            max_one_day_change=settings.reward_mm_selector_max_one_day_change,
            vol_range_10m_max=settings.reward_mm_selector_vol_range_10m_max,
            vol_step_2m_max=settings.reward_mm_selector_vol_step_2m_max,
            sweep_target_lots=settings.reward_mm_selector_sweep_target_lots,
            size_fit_k=settings.reward_mm_selector_size_fit_k,
            sports_pre_seconds=settings.reward_mm_selector_sports_pre_seconds,
            sports_post_seconds=settings.reward_mm_selector_sports_post_seconds,
            pool_limit=settings.reward_mm_selector_pool_limit,
            hydrate_per_category=settings.reward_mm_selector_hydrate_per_category,
            hydrate_limit=settings.reward_mm_selector_hydrate_limit,
            vol_check_per_category=settings.reward_mm_selector_vol_check_per_category,
            vol_concurrency=settings.reward_mm_selector_vol_concurrency,
            vol_timeout_seconds=settings.reward_mm_selector_vol_timeout_seconds,
            metadata_event_limit=settings.reward_mm_scanner_metadata_event_limit,
        )


@dataclass
class SelectedMarket:
    condition_id: str
    question: str
    category: str
    rate_per_day: float
    projected_reward: float  # $/day to us
    rank_reward: float  # projected_reward x size-fit bonus; the ranking key (display: projected)
    min_size: float
    required_size: float
    size_eligible: bool
    mid: float
    safety: float  # 0..100
    grade: str  # A | B | C
    risk_label: str  # one short line for the UI
    token_a: str
    token_b: str
    # details (for an optional expander; kept out of the headline row)
    sweep_lots: float = 0.0
    volatility_10m: float = 0.0
    volatility_2m: float = 0.0
    vol_known: bool = True
    hours_left: float | None = None
    one_day_change: float = 0.0


def _hours_left(pair: RewardPair) -> float | None:
    if pair.end_date is None:
        return None
    return (pair.end_date.timestamp() - time.time()) / 3600.0


def _is_gap_risk(question: str, category: str) -> bool:
    """Headline-gap markets: a single announcement reprices through the book (drafts, elections,
    transfers, M&A). Politics is gap-risk wholesale; the rest is keyword-matched."""
    if category == "politics":
        return True
    q = question.lower()
    return any(kw in q for kw in GAP_RISK_KEYWORDS)


def _is_live_or_near_start_sports(
    pair: RewardPair, category: str, now: float, cfg: SelectorConfig
) -> bool:
    """A sports market whose game is in-play or about to start. Clockless props (game_start_time
    is None, e.g. 'announcers say X') are pre-game and allowed."""
    if category != "sports":
        return False
    start = pair.game_start_time
    if start is None:
        return False
    start_ts = start.timestamp()
    return start_ts - cfg.sports_pre_seconds <= now <= start_ts + cfg.sports_post_seconds


def _adjusted_mid(book: Book, cutoff: float) -> float | None:
    bids = [float(lvl.price) for lvl in book.bids if float(lvl.size) >= cutoff]
    asks = [float(lvl.price) for lvl in book.asks if float(lvl.size) >= cutoff]
    if not bids or not asks:
        return None
    return (max(bids) + min(asks)) / 2.0


def _in_zone_bid_depth(book: Book, mid: float, max_spread: float, cutoff: float) -> float:
    """Resting BUY-side shares inside the reward zone — the size a seller must eat (within the zone)
    before reaching the zone floor where our quote rests. This is the honest 'sweep capacity', as
    opposed to the depth at our single placement tick (which is ~floor by construction)."""
    floor = mid - max_spread
    return sum(
        float(lvl.size)
        for lvl in book.bids
        if float(lvl.size) >= cutoff and floor <= float(lvl.price) <= mid
    )


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def evaluate(
    pair: RewardPair,
    book_a: Book | None,
    book_b: Book | None,
    *,
    category: str,
    vol: tuple[float, float] | None,
    now: float,
    cfg: SelectorConfig,
    scan_cfg: ScanConfig,
) -> tuple[SelectedMarket | None, str]:
    """Apply all hard gates and, if the market survives, build a scored ``SelectedMarket``.

    Returns ``(market, "")`` on success or ``(None, reason)`` on exclusion. ``vol`` is
    ``(range_10m, step_2m)`` from prices-history, or ``None`` when history is unavailable.
    Network-free: books and volatility are passed in so this is unit-testable.
    """
    # --- cheap metadata gates ---
    if pair.rewards_rate_per_day <= 0 or not pair.rewards_max_spread:
        return None, "no_reward"
    required_size = max(pair.rewards_min_size, pair.min_order_size)
    size_eligible = cfg.size_shares + 1e-12 >= required_size
    if not size_eligible:
        return None, "too_small"
    if _is_gap_risk(pair.question, category):
        return None, "gap_risk"
    if _is_live_or_near_start_sports(pair, category, now, cfg):
        return None, "live_sports"
    hours_left = _hours_left(pair)
    # Category-aware near-resolution gate: weather converges gradually, so it uses a shorter window
    # (its higher-paying tier lives at 12-18h); every other category keeps the full window.
    min_hours = cfg.min_hours_weather if category == "weather" else cfg.min_hours
    if hours_left is not None and hours_left < min_hours:
        return None, "near_resolution"
    if cfg.max_one_day_change > 0 and abs(pair.one_day_price_change) > cfg.max_one_day_change:
        return None, "volatile_1d"

    # --- book gates ---
    if book_a is None or book_b is None:
        return None, "no_book"
    max_spread = float(pair.rewards_max_spread)
    cutoff = required_size
    mid = _adjusted_mid(book_a, cutoff)
    if mid is None:
        return None, "one_sided"
    if mid < cfg.min_price or mid > 1.0 - cfg.min_price:
        return None, "extreme_price"
    assessment = _depth_farm_assessment(pair, book_a, book_b, scan_cfg, cfg.size_shares)
    if not assessment.farmable:
        return None, "thin_for_size"

    # --- volatility gate (with conservative fallback) ---
    vol_known = vol is not None
    range_10m, step_2m = vol if vol is not None else (0.0, 0.0)
    if vol_known and (
        range_10m >= cfg.vol_range_10m_max or step_2m >= cfg.vol_step_2m_max
    ):
        return None, "volatile"

    # --- survived: score safety and reward ---
    mid_b = _adjusted_mid(book_b, cutoff) or (1.0 - mid)
    zone_a = _in_zone_bid_depth(book_a, mid, max_spread, cutoff)
    zone_b = _in_zone_bid_depth(book_b, mid_b, max_spread, cutoff)
    worst_zone = min(zone_a, zone_b)
    sweep_lots = worst_zone / required_size if required_size > 0 else 0.0

    if not vol_known:
        calm = 0.5  # unknown -> middling, never assume calm
    else:
        calm = _clamp01(
            1.0
            - 0.5 * (range_10m / cfg.vol_range_10m_max if cfg.vol_range_10m_max > 0 else 0.0)
            - 0.5 * (step_2m / cfg.vol_step_2m_max if cfg.vol_step_2m_max > 0 else 0.0)
        )
    sweep_score = _clamp01(sweep_lots / cfg.sweep_target_lots) if cfg.sweep_target_lots > 0 else 1.0
    centrality = _clamp01(1.0 - abs(mid - 0.5) / 0.5)
    resolution = 1.0 if hours_left is None else _clamp01(hours_left / 72.0)
    safety = 100.0 * (
        W_CALM * calm
        + W_SWEEP * sweep_score
        + W_CENTRAL * centrality
        + W_RESOLUTION * resolution
    )
    grade = "A" if safety >= GRADE_A else "B" if safety >= GRADE_B else "C"

    competitor, _top = _competitor_q([book_a, book_b], max_spread, cutoff)
    projected = projected_daily_reward(
        pair.rewards_rate_per_day, competitor, scan_cfg, size_shares=cfg.size_shares
    )
    # Size-fit bonus: a small, EXACT-match nudge — only markets whose reward min_size equals the
    # configured quote size get it (50->50, 100->100, …); every other tier is left at its raw
    # reward. Those exact-fit markets pay more (verified) and fully use the quote size. This only
    # reorders the ranking; the displayed projected_reward stays raw, and order WITHIN a tier is
    # preserved (uniform multiplier). 0 disables.
    exact_fit = abs(required_size - cfg.size_shares) <= 1e-9
    rank_reward = projected * (1.0 + cfg.size_fit_k) if exact_fit else projected

    risk_label = _risk_label(
        vol_known=vol_known, calm=calm, sweep_score=sweep_score,
        centrality=centrality, resolution=resolution,
    )

    market = SelectedMarket(
        condition_id=pair.condition_id,
        question=pair.question,
        category=category,
        rate_per_day=pair.rewards_rate_per_day,
        projected_reward=projected,
        rank_reward=rank_reward,
        min_size=pair.rewards_min_size,
        required_size=required_size,
        size_eligible=size_eligible,
        mid=mid,
        safety=safety,
        grade=grade,
        risk_label=risk_label,
        token_a=pair.token_a,
        token_b=pair.token_b,
        sweep_lots=sweep_lots,
        volatility_10m=range_10m,
        volatility_2m=step_2m,
        vol_known=vol_known,
        hours_left=hours_left,
        one_day_change=pair.one_day_price_change,
    )
    return market, ""


def _risk_label(
    *, vol_known: bool, calm: float, sweep_score: float, centrality: float, resolution: float
) -> str:
    """One short line surfacing the weakest safety dimension (or a positive when all are strong)."""
    if not vol_known:
        return "⚠ vol unknown"
    if calm < 0.5:
        return "⚠ choppy"
    if sweep_score < 0.4:
        return "⚠ thin zone"
    if centrality < 0.4:
        return "⚠ skewed price"
    if resolution < 0.4:
        return "near resolution"
    return "calm · deep book"


# Module-level reward-pool cache (stale-while-revalidate). Paging the full ~20k-market pool is the
# slow step (~3 min) and its composition changes slowly, so the parsed pairs are cached and the
# refresh runs as a detached background task — the request never blocks on it.
_pool_cache: dict[str, object] = {"ts": 0.0, "pairs": [], "task": None, "error": None}


async def _refresh_pool(settings: Settings) -> None:
    """Page the whole reward pool with a dedicated client and store the parsed pairs in the cache.

    Runs as a detached task (never awaited by a request), so it owns its client and swallows its
    own errors — an unretrieved task exception would otherwise spam the server log.
    """
    try:
        limit = SelectorConfig.from_settings(settings).pool_limit
        async with ClobClient(settings) as clob:
            pool = await _fetch_pool(clob, limit)
        _pool_cache["pairs"] = [
            p for m in pool if (p := RewardPair.from_market(m)) is not None
        ]
        _pool_cache["ts"] = time.time()
        _pool_cache["error"] = None
    except Exception as exc:  # noqa: BLE001 — read-only refresh; record and let the next scan retry
        _pool_cache["error"] = f"{type(exc).__name__}: {exc}"


def _ensure_pool_refresh(settings: Settings) -> None:
    """Kick a background pool refresh if none is running and the cache is stale/empty."""
    ttl = settings.reward_mm_selector_pool_cache_seconds
    if _pool_cache["pairs"] and time.time() - float(_pool_cache["ts"]) < ttl:
        return
    task = _pool_cache["task"]
    if task is None or task.done():  # type: ignore[union-attr]
        _pool_cache["error"] = None
        _pool_cache["task"] = asyncio.create_task(_refresh_pool(settings))


def pool_status(settings: Settings) -> str:
    """'ready' (have pairs), 'warming' (loading, none yet), or 'error' (last load failed, none yet).

    Reflects whether the FIRST full pool load has completed; once it has, scans are fast forever.
    """
    if _pool_cache["pairs"]:
        return "ready"
    task = _pool_cache["task"]
    if task is not None and not task.done():  # type: ignore[union-attr]
        return "warming"
    return "error" if _pool_cache["error"] else "warming"


async def _cached_pool(settings: Settings) -> list[RewardPair]:
    """Return the cached reward pool, kicking a background refresh when stale. Never blocks on the
    slow first load: on a cold cache it returns ``[]`` immediately (the caller reports 'warming')
    while the detached refresh task populates the cache for the next scan."""
    _ensure_pool_refresh(settings)
    return _pool_cache["pairs"]  # type: ignore[return-value]


async def select_markets(
    clob: ClobClient,
    *,
    settings: Settings,
    size_shares: float,
    top: int,
) -> list[SelectedMarket]:
    """Apply the safety gates to the (cached) reward pool and return survivors ranked by reward."""
    cfg = SelectorConfig.from_settings(settings, size_shares=size_shares)
    scan_cfg = ScanConfig.from_settings(settings, size_shares=size_shares, depth_based=True)
    now = time.time()

    # Whole-pool coverage via the stale-while-revalidate cache (paging is the slow step).
    pairs = await _cached_pool(settings)
    resolver = await CategoryResolver.build(
        settings, pairs, metadata_event_limit=cfg.metadata_event_limit
    )

    # Cheap-gate the whole pool (no book fetch) and group the survivors by category.
    by_category: dict[str, list[RewardPair]] = {}
    for pair in pairs:
        category = resolver.classify(pair)
        _market, reason = evaluate(
            pair, None, None, category=category, vol=None, now=now, cfg=cfg, scan_cfg=scan_cfg
        )
        # ``no_book`` means the cheap gates passed (the book is all that is missing) — a candidate.
        if reason == "no_book":
            by_category.setdefault(category, []).append(pair)

    # Stratified hydration: take each category's top-by-rate up to a per-category cap, so the safest
    # low-rate categories are not crowded out by high-rate ones. Hydration (book + volatility) is
    # the expensive step, so the total is also bounded by ``hydrate_limit``.
    shortlist: list[tuple[RewardPair, str]] = []
    for category, items in by_category.items():
        items.sort(key=lambda p: p.rewards_rate_per_day, reverse=True)
        shortlist.extend((pair, category) for pair in items[: cfg.hydrate_per_category])
    shortlist.sort(key=lambda item: item[0].rewards_rate_per_day, reverse=True)
    shortlist = shortlist[: cfg.hydrate_limit]

    token_ids = [t for pair, _ in shortlist for t in (pair.token_a, pair.token_b)]
    books = await clob.get_order_books(token_ids)

    # First pass: score every hydrated market WITHOUT volatility (book gates + safety, calm
    # unknown). All cheap/local, so it ranks the field before paying for the slow vol API.
    prelim: list[tuple[RewardPair, str, SelectedMarket]] = []
    for pair, category in shortlist:
        market, _reason = evaluate(
            pair, books.get(pair.token_a), books.get(pair.token_b),
            category=category, vol=None, now=now, cfg=cfg, scan_cfg=scan_cfg,
        )
        if market is not None:
            prelim.append((pair, category, market))

    # Volatility is the slow API: fetch it only for the top reward contenders per category. The
    # displayed top is always vol-checked; the long tail keeps a 'vol unknown' middling safety.
    contenders: dict[str, list] = {}
    for entry in prelim:
        contenders.setdefault(entry[1], []).append(entry)
    contender_pairs: list[RewardPair] = []
    for items in contenders.values():
        items.sort(key=lambda it: it[2].rank_reward, reverse=True)
        contender_pairs.extend(it[0] for it in items[: cfg.vol_check_per_category])
    vols = await _fetch_volatility(clob, contender_pairs, cfg)
    checked = {pair.token_a for pair in contender_pairs}

    # Re-score contenders with real volatility (this applies the vol gate); keep the tail as-is.
    selected: list[SelectedMarket] = []
    for pair, category, prelim_market in prelim:
        if pair.token_a not in checked:
            selected.append(prelim_market)
            continue
        market, _reason = evaluate(
            pair, books.get(pair.token_a), books.get(pair.token_b),
            category=category, vol=vols.get(pair.token_a), now=now, cfg=cfg, scan_cfg=scan_cfg,
        )
        if market is not None:
            selected.append(market)

    # All survivors are "safe enough" -> rank by size-fit-adjusted reward, tie-break by safety.
    selected.sort(key=lambda m: (m.rank_reward, m.safety), reverse=True)
    return selected[:top] if top > 0 else selected


async def _fetch_volatility(
    clob: ClobClient, pairs: list[RewardPair], cfg: SelectorConfig
) -> dict[str, tuple[float, float]]:
    """Best-effort 10m-range / 2m-step per market from prices-history, with a per-call timeout so a
    slow endpoint cannot stall the scan. Missing/timed-out -> absent (the caller treats absence as
    'vol unknown', never as calm)."""
    semaphore = asyncio.Semaphore(max(1, cfg.vol_concurrency))
    out: dict[str, tuple[float, float]] = {}

    async def fetch(pair: RewardPair) -> None:
        try:
            async with semaphore:
                history = await asyncio.wait_for(
                    clob.price_history(pair.token_a, interval="6h", fidelity=1),
                    timeout=cfg.vol_timeout_seconds,
                )
        except TimeoutError:
            return
        if not history:
            return
        range_10m, _step_10m = _history_window_range(history, 600.0)
        _range_2m, step_2m = _history_window_range(history, 120.0)
        out[pair.token_a] = (range_10m, step_2m)

    await asyncio.gather(*(fetch(pair) for pair in pairs))
    return out


async def select_by_category(
    clob: ClobClient,
    *,
    settings: Settings,
    per_category: int,
    size_shares: float,
) -> dict[str, list[SelectedMarket]]:
    """Group the safety-gated survivors by display category, each ranked by reward. Mirrors the
    old ``scan_by_category`` interface so the launch picker is a drop-in swap."""
    everything = await select_markets(
        clob, settings=settings, size_shares=size_shares, top=0
    )
    return _group_unique_by_category(everything, per_category=per_category)


def _group_unique_by_category(
    markets: list[SelectedMarket], *, per_category: int
) -> dict[str, list[SelectedMarket]]:
    """Group display markets by category, keeping one row per condition_id globally."""
    out: dict[str, list[SelectedMarket]] = {}
    seen: set[str] = set()
    for market in markets:
        if market.condition_id in seen:
            continue
        seen.add(market.condition_id)
        out.setdefault(market.category, []).append(market)
    for category in ("weather", "finance", "sports", "politics", "tech", "other"):
        out.setdefault(category, [])
    return {cat: items[:per_category] for cat, items in out.items()}
