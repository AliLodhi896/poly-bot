"""Reward-market scanner.

Ranks the live Polymarket reward pool and returns the best markets to farm,
using the same scoring the live engine uses (see ``RewardEngine._book_competitor_q``
and ``RewardEngine._projected_daily_reward``). Kept as a standalone, reusable
module so the live bot can later import :func:`scan_reward_markets` to drive a
continuous market-rotation watcher.

Scoring recap:

* ``q_self``   = ``distance_score(max_spread, 0.5*max_spread) * size_shares``
* ``competitor`` = distance-weighted resting size within ``max_spread`` of the
  midpoint, summed over *both* token books, ignoring dust (< ``min_size``).
* ``share``    = ``min(share_cap, q_self / (q_self + competitor*haircut))``
* ``projected``= ``rate_per_day * share``
* ``score``    = ``projected * sweep * imbalance * activity * resolution * category * recent-vol``

Ranking is **led by reward** (``projected``, which already prices in competition via the share
term). **Sweep-resistance** — the protective resting size between the midpoint and our quote,
i.e. the shares a single adverse order must eat before it fills us — is a *soft* modifier in a
compressed ``[floor, 1.0]`` band, not the lead signal: it nudges, and badges thin books
``sweepable``, but never annihilates a high-reward market. (A thin book only bleeds when price
moves through it, and that is the volatility gate's job; on a calm low-competition market a thin
book is the best farm — big share, no fills.) The category factor penalises only headline-gap
markets (whose books evaporate on news, which sweep cannot see); every other category is neutral
so the best earners rise on merit. The dashboard keeps high-min-size markets visible and
annotates whether the current UI size is sufficient; one-day price change is no longer a gate.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

from polybot.api.clob import ClobClient
from polybot.api.gamma import GammaClient
from polybot.config import Settings
from polybot.feed.books import FastBook
from polybot.models import Book, Market
from polybot.rewards.models import RewardPair
from polybot.rewards.planner import RewardQuotePlanner

# Low-toxicity, mean-reverting markets we prefer for liquidity farming.
WEATHER_KEYWORDS: tuple[str, ...] = (
    "temperature",
    "weather",
    "°c",
    "°f",
    "degrees",
    "rain",
    "snow",
    "highest temperature",
    "lowest temperature",
    "high in ",
    "low in ",
)

# Defaults for explicit user exclusions. Keep empty-ish by default for discovery; the user can
# still add keywords in env when a whole market family should be hidden.
DEFAULT_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "esport",
    "csgo",
    "cs2",
    "league of legends",
    "dota",
    "valorant",
    "counter-strike",
    "roster",
)

# Display categories. A market is assigned the FIRST category whose metadata/keywords match,
# in this priority order: weather, finance, sports, politics, tech, else "other".
CATEGORY_ORDER: tuple[str, ...] = ("weather", "finance", "sports", "politics", "tech")

# Finance / crypto: daily asset up/down and crypto-price markets. Mean-reverting and not driven by
# a single headline (unlike token launches, which are gap-risk — see GAP_RISK_KEYWORDS), so they
# are a relatively safe farm and get their own bucket instead of hiding in "other"/"tech".
FINANCE_KEYWORDS: tuple[str, ...] = (
    "bitcoin", "ethereum", "crypto", "blockchain", "hyperliquid", "btc", "eth", "solana",
    "dogecoin", "xrp", "up or down", "close higher", "close lower",
)

SPORTS_KEYWORDS: tuple[str, ...] = (
    " vs ", " vs. ", "ufc", "nascar", "nba", "nfl", "mlb", "nhl", "soccer", "football",
    "basketball", "baseball", "hockey", "tennis", "golf", "f1 ", "grand prix", "premier league",
    "la liga", "serie a", "bundesliga", "champions league", "world cup", "super bowl",
    "playoff", "championship", "match", "fight", "boxing", "olympic", "wimbledon", "cricket",
)
POLITICS_KEYWORDS: tuple[str, ...] = (
    "election", "president", "senate", "congress", "governor", " mayor", "parliament",
    "nominee", "primary", "ballot", "referendum", "prime minister", "candidate", "by-election",
    "impeach", "vote", "poll", "cabinet", "minister", "chancellor", "shutdown", "tariff",
    # Geopolitics / world affairs belong in Politics (short bare tokens like "war"/"iran" match on
    # word boundaries via _kw_match, so they don't hit "reward"/"warriors"/"chairman").
    "geopolitic", "ceasefire", "war", "military", "invasion", "invade", "airspace", "sanctions",
    "nato", "iran", "israel", "gaza", "palestin", "ukraine", "russia", "hamas", "hezbollah",
    "houthi", "missile", "annex", "coup", "regime", "diplomat", "treaty",
)
TECH_KEYWORDS: tuple[str, ...] = (
    "ai", "gpt", "openai", "anthropic", "llm", "model", "chip", "semiconductor",
    "nvidia", "apple", "google", "microsoft", "tesla", "spacex", "ipo", "startup", "software",
    "app store", "iphone",
)

# Headline-gap markets: a single news item reprices them through resting quotes
# and the resulting inventory cannot be hedged. Heavily penalised, not excluded.
GAP_RISK_KEYWORDS: tuple[str, ...] = (
    "join",
    "stay at",
    " sign",
    "transfer",
    " loan",
    "win the",
    "by-election",
    "nominee",
    "elected",
    "president",
    "governor",
    " mayor",
    "election",
    "primary",
    "referendum",
    "merger",
    "acquisition",
    "acquire",
    # Political appointments reprice on a single announcement (and can slip the politics category
    # when a finance/economy tag mislabels them, e.g. "next Chancellor of the Exchequer").
    "chancellor",
    "prime minister",
    "next president",
    # Single-announcement sports props (drafts, awards): one headline reprices them through the
    # book instantly, so resting quotes get swept before they can be pulled — same gap pattern.
    "draft",
    "drafted",
    "overall pick",
    "1st pick",
    "first pick",
    "to be named",
    # Token launches / FDV: a discrete listing event reprices through the book in one move and the
    # resulting inventory cannot be hedged — same gap pattern, kept out of the Finance bucket.
    "launch a token",
    "token launch",
    "fdv",
    "fully diluted",
    # Geopolitics / conflict: a single headline (strike, ceasefire, capture) reprices the whole
    # book and the makers ahead of us pull, so the depth buffer cannot protect.
    "strike on",
    "strikes on",
    "invade",
    "invasion",
    "ceasefire",
    "captures",
    "capture ",
    "annex",
    "missile",
    "withdraw from",
    "withdrawal",
    "sanctions",
    "strait of",
    "hormuz",
    # Macro / central-bank decisions: a scheduled announcement gaps the market in one print.
    "rate hike",
    "rate cut",
    "rate decision",
    "interest rate",
    "fomc",
    # Political exits/appointments beyond elections.
    "leave office",
    "resign",
    "step down",
    "removed as",
    # Scheduled product releases (model/app launches) gap on the announcement.
    "be released",
    "released by",
    "release date",
    # Esports match outcomes are live/headline events (no game_start_time, so the live-sports gate
    # may miss them); exclude by title.
    "esport",
    "csgo",
    "cs2",
    "dota",
    "valorant",
    "counter-strike",
)

MICRO_PROP_KEYWORDS: tuple[str, ...] = (
    "announcers say",
    "commentators say",
    "broadcast say",
)


@dataclass(frozen=True)
class EventCategoryMetadata:
    """Small normalized slice of Gamma event metadata used by the dashboard scanner."""

    category: str | None = None
    tags: tuple[str, ...] = ()
    sport: str | None = None
    series_slug: str | None = None
    title: str | None = None


def _slug_text(value: object) -> str:
    return str(value or "").strip().lower()


def _extract_tag_texts(raw_tags: object) -> tuple[str, ...]:
    tags: list[str] = []
    for tag in raw_tags if isinstance(raw_tags, list) else []:
        if isinstance(tag, str):
            tags.append(tag)
        elif isinstance(tag, dict):
            for key in ("slug", "label", "id"):
                value = tag.get(key)
                if value not in (None, ""):
                    tags.append(str(value))
    return tuple(tags)


def _event_metadata_from_gamma(event: dict[str, Any] | None) -> EventCategoryMetadata:
    if not event:
        return EventCategoryMetadata()
    sport = event.get("sport")
    sport_text: str | None
    if isinstance(sport, dict):
        sport_text = str(sport.get("sport") or sport.get("slug") or sport.get("id") or "")
    elif sport is not None:
        sport_text = str(sport)
    else:
        sport_text = None
    return EventCategoryMetadata(
        category=str(event.get("category")) if event.get("category") not in (None, "") else None,
        tags=_extract_tag_texts(event.get("tags")),
        sport=sport_text or None,
        series_slug=(
            str(event.get("seriesSlug"))
            if event.get("seriesSlug") not in (None, "")
            else None
        ),
        title=str(event.get("title")) if event.get("title") not in (None, "") else None,
    )


@dataclass(frozen=True)
class CategoryResolver:
    """Classify reward markets using Gamma metadata first, keyword fallback second."""

    metadata_by_slug: dict[str, EventCategoryMetadata] = field(default_factory=dict)

    @classmethod
    async def build(
        cls,
        settings: Settings,
        pairs: list[RewardPair],
        *,
        metadata_event_limit: int,
    ) -> CategoryResolver:
        """Fetch every reward market's event from Gamma and read its Polymarket tags.

        Polymarket carries the authoritative category on the EVENT's tags (``politics``,
        ``geopolitics``, ``crypto``, ``sports`` …), so classification is metadata-first with the
        keyword lists only as a fallback. We resolve the exact set of event slugs we farm (a few
        hundred, deduped, richest-first under a safety cap) concurrently — the ``/events`` listing
        is ordered by volume and buries low-volume reward events, so fetching by slug is both
        cheaper and complete. Best-effort and cached (see ``_build_resolver``): on error we return
        whatever resolved and fall back to keyword classing for the rest.
        """
        if metadata_event_limit <= 0:
            return cls()
        slugs: list[str] = []
        seen: set[str] = set()
        for pair in sorted(pairs, key=lambda p: p.rewards_rate_per_day, reverse=True):
            slug = pair.event_slug
            if not slug or slug in seen:
                continue
            seen.add(slug)
            slugs.append(slug)
            if len(slugs) >= metadata_event_limit:
                break
        if not slugs:
            return cls()

        metadata: dict[str, EventCategoryMetadata] = {}
        semaphore = asyncio.Semaphore(12)
        try:
            async with GammaClient(settings) as gamma:

                async def fetch(slug: str) -> None:
                    async with semaphore:
                        event = await gamma.get_event_by_slug(slug)
                    if event:
                        metadata[slug] = _event_metadata_from_gamma(event)

                await asyncio.gather(*(fetch(slug) for slug in slugs))
        except Exception:  # noqa: BLE001 - category enrichment is best-effort, never fatal.
            return cls(metadata)
        return cls(metadata)

    def classify(self, pair: RewardPair) -> str:
        meta = self.metadata_by_slug.get(pair.event_slug or "")
        return classify_category(
            pair.question,
            event_slug=pair.event_slug,
            market_slug=pair.market_slug,
            event_title=pair.event_title or (meta.title if meta else None),
            category=pair.category or (meta.category if meta else None),
            tags=(*pair.tags, *(meta.tags if meta else ())),
            sport=pair.sport or (meta.sport if meta else None),
            series_id=pair.series_id or (meta.series_slug if meta else None),
        )


def distance_score(max_spread: float, distance: float) -> float:
    """Reward weight of an order ``distance`` away from the midpoint.

    Mirrors ``RewardQuotePlanner._distance_score``: orders at the midpoint score
    1.0, orders at the edge of ``max_spread`` score ~0.
    """
    if max_spread <= 0 or distance > max_spread:
        return 0.0
    return max(0.0, (max_spread - distance) / max_spread) ** 2


@dataclass(frozen=True)
class ScanConfig:
    """Tunables for a scan. Defaults mirror the live engine where one exists."""

    top: int = 5
    max_min_size: float = 0.0  # optional legacy affordability gate; 0 = show all min sizes
    min_midpoint: float = 0.10  # reject near-0/1 extremes (inventory skew)
    min_hours: float = 12.0  # need runway before resolution
    max_one_day_change: float = 0.25  # volatility/toxicity gate (0 = disabled)
    min_top_depth: float = 200.0  # need a real two-sided book, not a dead one
    size_shares: float = 20.0  # our intended per-quote size, for the share estimate
    entry_depth_multiplier: float = 1.0
    # Depth-based placement: when on, farmability mirrors the live planner (an in-zone tick whose
    # resting depth-ahead clears the floor buffer) instead of the top-of-book proxy below.
    depth_based: bool = False
    quote_distance_fraction: float = 0.5
    competitor_haircut: float = 1.0
    share_cap: float = 0.02
    # Flat discount mapping the optimistic model estimate to realised payouts (see config note).
    # Constant → does not change ranking, only the displayed $ magnitude. 1.0 = raw model.
    reward_realism_factor: float = 1.0
    # Latent-competition floor: an empty book (competition ~0) is not a ~100%-share goldmine, so
    # floor competition before computing share. 0 = off (raw ScanConfig); from_settings sets it.
    competition_floor: float = 0.0
    # --- sweep-resistance: a SOFT fill-risk modifier (reward leads the ranking) ---
    # Protective resting shares between the midpoint and our quote price = the size a single
    # adverse order must eat before it reaches us. The factor ranges ``[sweep_factor_floor, 1.0]``
    # and saturates at ``target = max(sweep_target_base, sweep_target_size_mult * desired_size)``;
    # the target scales with our quote size because protection only has to dwarf what we risk. A
    # ``sweepable`` badge is raised below ``sweep_floor_mult * desired_size``.
    sweep_target_base: float = 120.0
    sweep_target_size_mult: float = 4.0
    sweep_factor_floor: float = 0.45
    sweep_floor_mult: float = 1.0
    # Book imbalance (protective bid vs ask within the quote band). Beyond this skew the mid is
    # likely to drift into the thin side and take us; soft penalty + ``imbalanced`` badge. 0 = off.
    imbalance_warn: float = 0.80
    exclude_keywords: tuple[str, ...] = DEFAULT_EXCLUDE_KEYWORDS
    gap_risk_keywords: tuple[str, ...] = GAP_RISK_KEYWORDS
    pool_limit: int = 1000  # how many reward markets to pull from the pool
    hydrate_limit: int = 260  # how many top-by-rate candidates to fetch books for
    metadata_event_limit: int = 6000  # max Gamma /events paged in the bulk category sweep
    # Activity is a *liveness floor*, not a reward signal: 24h trade volume does not enter the
    # reward formula, and low-volume markets tend to have lower competition (higher share). Full
    # credit at/above `activity_floor_volume`; a near-dead book bottoms at `activity_min_factor`.
    activity_floor_volume: float = 2_000.0
    activity_min_factor: float = 0.60
    volatility_10m_warn: float = 0.06
    volatility_10m_reject: float = 0.12
    volatility_2m_warn: float = 0.02
    volatility_2m_reject: float = 0.03
    # --- intraday-volatility gate (prices-history) — drops "jumpy" markets whose fast moves /
    # sweeps beat the bot's cancel latency and cause adverse fills (the calm markets that farmed
    # 0-fill had tiny ranges). 0 = disabled. Checked only for the top score contenders.
    volatility_interval: str = "6h"
    max_recent_step: float = 0.025  # reject if any consecutive move exceeds this (~quote dist)
    max_recent_range: float = 0.06  # reject if the window's max-min swing exceeds this

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: object) -> ScanConfig:
        """Build a config seeded from the bot's live settings, then apply overrides."""
        base = dict(
            min_midpoint=settings.reward_mm_min_midpoint,
            min_hours=settings.reward_mm_min_hours_to_resolution or cls.min_hours,
            max_one_day_change=settings.reward_mm_max_one_day_change or cls.max_one_day_change,
            min_top_depth=settings.reward_mm_min_top_depth_shares,
            size_shares=settings.reward_mm_size_shares,
            entry_depth_multiplier=settings.reward_mm_entry_depth_multiplier,
            depth_based=settings.reward_mm_depth_based_placement,
            quote_distance_fraction=settings.reward_mm_quote_distance_fraction,
            competitor_haircut=settings.reward_mm_competitor_haircut_base,
            share_cap=settings.reward_mm_liquidity_share_cap_base,
            volatility_10m_warn=settings.reward_mm_scanner_volatility_10m_warn,
            volatility_10m_reject=settings.reward_mm_scanner_volatility_10m_reject,
            volatility_2m_warn=settings.reward_mm_scanner_volatility_2m_warn,
            volatility_2m_reject=settings.reward_mm_scanner_volatility_2m_reject,
            metadata_event_limit=settings.reward_mm_scanner_metadata_event_limit,
            sweep_target_base=settings.reward_mm_scanner_sweep_target_base,
            sweep_target_size_mult=settings.reward_mm_scanner_sweep_target_size_mult,
            sweep_factor_floor=settings.reward_mm_scanner_sweep_factor_floor,
            sweep_floor_mult=settings.reward_mm_scanner_sweep_floor_mult,
            imbalance_warn=settings.reward_mm_scanner_imbalance_warn,
            reward_realism_factor=settings.reward_mm_scanner_reward_realism_factor,
            competition_floor=settings.reward_mm_scanner_competition_floor,
        )
        max_min = settings.reward_mm_max_min_size_shares
        if max_min > 0:
            base["max_min_size"] = max_min
        extra = settings.reward_mm_exclude_keyword_set
        if extra:
            base["exclude_keywords"] = tuple({*DEFAULT_EXCLUDE_KEYWORDS, *extra})
        base.update(overrides)  # type: ignore[arg-type]
        return cls(**base)  # type: ignore[arg-type]


@dataclass
class ScoredMarket:
    condition_id: str
    question: str
    is_weather: bool
    mid: float
    rate_per_day: float
    min_size: float
    max_spread: float  # probability units, e.g. 0.045
    competition: float  # distance-weighted competitor liquidity
    top_depth: float  # raw shares at best bid+ask across both books
    hours_left: float | None
    one_day_change: float
    volume_24hr: float
    projected_reward: float  # $/day to us
    share: float
    risk_factor: float  # fill-risk multiplier in (0, 1]
    risk_tier: str  # weather | event | gap-risk
    category: str  # weather | sports | politics | tech | other (display grouping)
    score: float  # ranking key = projected_reward * risk_factor
    token_a: str
    token_b: str
    required_size: float = 0.0  # min shares required for reward scoring
    quote_size: float = 0.0  # size used for scanner reward/depth estimates
    size_eligible: bool = True
    depth_a: float = 0.0
    depth_b: float = 0.0
    depth_ratio: float = 0.0
    depth_factor: float = 1.0
    sweep_shares: float = 0.0  # protective resting size between mid and our quote (worst side)
    sweep_factor: float = 1.0  # graded fill-risk multiplier from sweep_shares, in (0, 1]
    depth_buffer_shares: float = 0.0  # depth mode: worst-leg depth-ahead at the live depth tick
    protect_bid: float = 0.0
    protect_ask: float = 0.0
    imbalance: float = 0.5  # protect_bid / (protect_bid + protect_ask); 0.5 = balanced
    imbalance_factor: float = 1.0
    activity_factor: float = 1.0
    resolution_factor: float = 1.0
    category_factor: float = 1.0
    volatility_factor: float = 1.0
    volatility: float = 0.0  # legacy recent max-min price range (0 = not measured)
    max_step: float = 0.0  # legacy largest recent consecutive move (fast-jump proxy)
    volatility_10m: float = 0.0
    volatility_2m: float = 0.0
    max_step_2m: float = 0.0
    hard_reject: bool = False
    flags: list[str] = field(default_factory=list)


def _is_weather(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in WEATHER_KEYWORDS)


def _kw_match(haystack: str, keyword: str) -> bool:
    """Substring match, but short bare alpha tokens (nfl, nba, gpt, vote, poll, …) match on
    word boundaries — so e.g. ``nfl`` does not match ``i-nfl-ation`` nor ``poll`` ``pollution``.
    """
    token = keyword.strip()
    if " " not in keyword and token.isalpha() and len(token) <= 4:
        return re.search(rf"\b{re.escape(token)}\b", haystack) is not None
    return keyword in haystack


def _any_kw(haystack: str, keywords: tuple[str, ...]) -> bool:
    return any(_kw_match(haystack, kw) for kw in keywords)


def classify_category(
    question: str,
    event_slug: str | None = None,
    *,
    market_slug: str | None = None,
    event_title: str | None = None,
    category: str | None = None,
    tags: tuple[str, ...] = (),
    sport: str | None = None,
    series_id: str | None = None,
) -> str:
    """Assign a market to a display category by metadata first, keyword fallback second."""
    parts = [
        question,
        event_slug or "",
        market_slug or "",
        event_title or "",
        category or "",
        sport or "",
        series_id or "",
        " ".join(tags),
    ]
    haystack = " ".join(parts).lower()
    explicit = {
        _slug_text(category),
        _slug_text(sport),
        *{_slug_text(tag) for tag in tags},
    }
    # Metadata-first: Polymarket tags the EVENT with an authoritative top-level category. These sets
    # are the real Gamma tag slugs (verified against /events); order encodes priority so a market
    # carrying several tags lands in the most specific single bucket.
    if explicit & {"weather", "climate", "temperature", "hurricane"}:
        return "weather"
    if explicit & {
        "finance",
        "finance-updown",
        "crypto",
        "pyth-finance",
        "up-or-down",
        "daily-close",
        "pre-market",
        "business",
        "economy",
        "economics",
        "macro",
        "stocks",
        "earnings",
        "etf",
        "altcoins",
        "bitcoin",
        "ethereum",
        "fed",
        "inflation",
        "interest-rates",
    }:
        return "finance"
    if explicit & {
        "sports",
        "sport",
        "soccer",
        "football",
        "fifa-world-cup",
        "2026-fifa-world-cup",
        "fifwc",
        "soccer-fifwc",
        "basketball",
        "baseball",
        "tennis",
        "cricket",
        "hockey",
        "ufc",
        "nfl",
        "nba",
        "mlb",
        "nhl",
        "golf",
        "boxing",
        "mma",
        "f1",
        "formula-1",
        "nascar",
        "olympics",
        "premier-league",
        "la-liga",
        "champions-league",
        "esports",
        "chess",
    }:
        return "sports"
    if explicit & {
        "politics",
        "elections",
        "election",
        "world-elections",
        "geopolitics",
        "world",
        "international-affairs",
        "foreign-policy",
        "nato",
        "federal-government",
        "house-races",
        "us-current-affairs",
        "trump-presidency",
        "immigration",
        "supreme-court",
    }:
        return "politics"
    if explicit & {
        "technology",
        "tech",
        "ai",
        "artificial-intelligence",
        "openai",
        "anthropic",
        "claude",
        "nvidia",
        "science",
        "space",
        "spacex",
        "robotics",
    }:
        return "tech"
    if _any_kw(haystack, WEATHER_KEYWORDS):
        return "weather"
    if _any_kw(haystack, FINANCE_KEYWORDS):
        return "finance"
    if _any_kw(haystack, SPORTS_KEYWORDS):
        return "sports"
    if _any_kw(haystack, POLITICS_KEYWORDS):
        return "politics"
    if _any_kw(haystack, TECH_KEYWORDS):
        return "tech"
    return "other"


def _risk_tier(question: str, is_weather: bool, cfg: ScanConfig, *, category: str = "") -> str:
    """Classify fill-risk tier for the score's category weight.

    Politics markets (elections, appointments, nominations) are treated as gap-risk wholesale:
    their books reprice on a single headline and the resulting inventory cannot be hedged, which
    sweep-resistance — a snapshot of current depth — cannot detect. Keyword matching catches the
    rest (transfers, M&A) regardless of category.
    """
    if is_weather:
        return "weather"
    if category == "politics":
        return "gap-risk"
    q = question.lower()
    if any(kw in q for kw in cfg.gap_risk_keywords):
        return "gap-risk"
    return "event"


def _resolution_factor(hours_left: float | None) -> float:
    """Risk multiplier for time-to-resolution.

    The scanner keeps close-to-resolution markets visible, but heavily discounts them because
    convergence/news/live-game movement is usually where adverse fills happen.
    """
    if hours_left is None:
        return 0.70
    if hours_left <= 0:
        return 0.05
    if hours_left < 1:
        return 0.12
    if hours_left < 6:
        return 0.25
    if hours_left < 24:
        return 0.55
    if hours_left < 72:
        return 0.85
    return 1.0


def _category_factor(category: str, tier: str) -> float:
    """Penalise only what sweep-resistance cannot see.

    Headline-gap markets (transfers/elections/M&A) can show a deep book *now* yet have every
    maker pull and reprice the instant news lands — sweep_shares measures protection against a
    single order, not book evaporation. Every other category is left neutral so the genuinely
    best earners rise on their own merits rather than a blanket category bias.
    """
    if tier == "gap-risk":
        return 0.30
    return 1.0


def _activity_factor(volume_24hr: float, cfg: ScanConfig) -> float:
    """Liveness floor only — NOT a reward signal.

    Reward depends on resting liquidity, not trade volume (verified: volume never enters
    ``projected_daily_reward``). Low-volume reward markets tend to have *lower* competition and so
    a *higher* share, making them better farms, not worse; high volume mostly means more taker
    flow and more adverse-fill risk. So this only mildly discounts a near-dead book (where the
    reward may be stale / the mid unreliable) and treats every genuinely active market equally.
    """
    full = max(1.0, cfg.activity_floor_volume)
    volume = max(0.0, volume_24hr)
    if volume >= full:
        return 1.0
    return cfg.activity_min_factor + (1.0 - cfg.activity_min_factor) * (volume / full)


def _is_micro_prop(question: str) -> bool:
    q = question.lower()
    return any(keyword in q for keyword in MICRO_PROP_KEYWORDS)


def _depth_factor(depth_ratio: float, cfg: ScanConfig) -> float:
    target = max(1.0, cfg.entry_depth_multiplier)
    if depth_ratio < target:
        return 0.0
    return 1.0


def _volatility_factor(market: ScoredMarket, cfg: ScanConfig) -> float:
    factor = 1.0
    if cfg.volatility_10m_reject > 0 and market.volatility_10m >= cfg.volatility_10m_reject:
        market.flags.append("volatile-10m")
        market.hard_reject = True
        return 0.0
    if cfg.volatility_2m_reject > 0 and market.volatility_2m >= cfg.volatility_2m_reject:
        market.flags.append("jumpy-2m")
        market.hard_reject = True
        return 0.0
    if cfg.volatility_10m_warn > 0 and market.volatility_10m >= cfg.volatility_10m_warn:
        market.flags.append("warn-10m")
        factor *= 0.40
    if cfg.volatility_2m_warn > 0 and market.volatility_2m >= cfg.volatility_2m_warn:
        market.flags.append("warn-2m")
        factor *= 0.35
    return factor


def _finalize_score(market: ScoredMarket, cfg: ScanConfig) -> None:
    market.volatility_factor = _volatility_factor(market, cfg)
    market.risk_factor = (
        market.sweep_factor
        * market.imbalance_factor
        * market.activity_factor
        * market.resolution_factor
        * market.category_factor
        * market.volatility_factor
    )
    market.score = market.projected_reward * market.risk_factor


def _history_window_range(
    history: list[tuple[float, float]], seconds: float
) -> tuple[float, float]:
    if seconds <= 0 or len(history) < 2:
        return 0.0, 0.0
    latest = max(ts for ts, _price in history)
    prices = [price for ts, price in history if ts >= latest - seconds]
    if len(prices) < 2:
        return 0.0, 0.0
    price_range = max(prices) - min(prices)
    max_step = max(abs(prices[index] - prices[index - 1]) for index in range(1, len(prices)))
    return price_range, max_step


def _hours_left(pair: RewardPair) -> float | None:
    if pair.end_date is None:
        return None
    return (pair.end_date.timestamp() - time.time()) / 3600.0


def _adjusted_mid(book: Book, cutoff: float) -> float | None:
    """Best bid/ask midpoint ignoring dust levels (size < cutoff)."""
    bids = [lvl for lvl in book.bids if float(lvl.size) >= cutoff]
    asks = [lvl for lvl in book.asks if float(lvl.size) >= cutoff]
    if not bids or not asks:
        return None
    best_bid = max(float(lvl.price) for lvl in bids)
    best_ask = min(float(lvl.price) for lvl in asks)
    return (best_bid + best_ask) / 2.0


def _top_depth(book: Book) -> float:
    depth = 0.0
    if book.best_bid is not None:
        depth += float(book.depth_shares("bids", max_levels=1))
    if book.best_ask is not None:
        depth += float(book.depth_shares("asks", max_levels=1))
    return depth


def _protective_depth(
    book: Book, mid: float, quote_distance: float, cutoff: float
) -> tuple[float, float]:
    """Resting shares between the midpoint and our quote price, per side.

    Our maker quote rests ``quote_distance`` inside the spread from ``mid``. To fill our bid an
    adverse seller must first consume every bid resting *above* our price (between our price and
    mid); likewise for the ask. Those sums are the protective depth — the shares a single order
    must eat to reach us. Dust levels (< ``cutoff``) are not counted as protection.
    """
    lo = mid - quote_distance
    hi = mid + quote_distance
    bid = sum(
        float(lvl.size)
        for lvl in book.bids
        if float(lvl.size) >= cutoff and lo < float(lvl.price) <= mid
    )
    ask = sum(
        float(lvl.size)
        for lvl in book.asks
        if float(lvl.size) >= cutoff and mid <= float(lvl.price) < hi
    )
    return bid, ask


def _sweep_factor(sweep_shares: float, desired_size: float, cfg: ScanConfig) -> float:
    """Soft fill-risk multiplier from protective depth.

    Ranges ``[sweep_factor_floor, 1.0]`` — a compressed band so that reward (which already prices
    in competition) leads the ranking and sweep only nudges. Saturates to 1.0 once protection
    reaches ``max(sweep_target_base, mult * desired_size)``; the floor keeps a high-reward but thin
    market competitive (the user still sees the ``sweepable`` badge) instead of being annihilated.
    """
    target = max(cfg.sweep_target_base, cfg.sweep_target_size_mult * max(1.0, desired_size))
    floor = max(0.0, min(1.0, cfg.sweep_factor_floor))
    if target <= 0:
        return 1.0
    graded = max(0.0, min(1.0, sweep_shares / target))
    return floor + (1.0 - floor) * graded


def _imbalance_factor(imbalance: float, cfg: ScanConfig) -> float:
    """Soft penalty for a one-sided protective book (the mid drifts into the thin side)."""
    if cfg.imbalance_warn <= 0 or cfg.imbalance_warn >= 1:
        return 1.0
    skew = max(imbalance, 1.0 - imbalance)
    if skew <= cfg.imbalance_warn:
        return 1.0
    return 1.0 - 0.5 * (skew - cfg.imbalance_warn) / (1.0 - cfg.imbalance_warn)


def _competitor_q(books: list[Book], max_spread: float, cutoff: float) -> tuple[float, float]:
    """Replicate ``RewardEngine._book_competitor_q`` from REST books.

    Returns ``(distance_weighted_competition, raw_top_depth)`` where top_depth is
    the plain shares resting at the best bid+ask across the supplied books.
    """
    total = 0.0
    top_depth = 0.0
    for book in books:
        mid = _adjusted_mid(book, cutoff)
        if mid is None:
            continue
        top_depth += _top_depth(book)
        for lvl in (*book.bids, *book.asks):
            size = float(lvl.size)
            if size < cutoff:
                continue
            distance = abs(float(lvl.price) - mid)
            if distance > max_spread:
                continue
            total += distance_score(max_spread, distance) * size
    return total, top_depth


def projected_daily_reward(
    rate_per_day: float,
    competitor: float,
    cfg: ScanConfig,
    *,
    size_shares: float | None = None,
) -> float:
    """Expected $/day to us = pool rate x our liquidity share."""
    q_self = distance_score(1.0, cfg.quote_distance_fraction) * (
        cfg.size_shares if size_shares is None else size_shares
    )
    if q_self <= 1e-12:
        return 0.0
    # Floor competition: a near-empty book is not a free ~100% share (snapshot misses makers).
    competitor_effective = max(max(0.0, competitor), cfg.competition_floor)
    competitor_base = competitor_effective * cfg.competitor_haircut
    if competitor_base <= 1e-12:
        share = min(1.0, cfg.share_cap)
    else:
        share = min(cfg.share_cap, q_self / (q_self + competitor_base))
    return rate_per_day * share * cfg.reward_realism_factor


def _share(rate_per_day: float, projected: float) -> float:
    return projected / rate_per_day if rate_per_day > 0 else 0.0


async def _fetch_pool(clob: ClobClient, limit: int) -> list[Market]:
    """Page through the rewards pool ordered by rate/day until ``limit`` reached."""
    markets: list[Market] = []
    cursor: str | None = None
    while len(markets) < limit:
        page, cursor = await clob.get_rewards_markets(
            limit=min(500, limit - len(markets)),
            order_by="rate_per_day",
            position="DESC",
            next_cursor=cursor,
        )
        if not page:
            break
        markets.extend(page)
        if not cursor or cursor in ("LTE=", ""):
            break
    return markets[:limit]


def _passes_prefilter(pair: RewardPair, cfg: ScanConfig) -> bool:
    """Cheap metadata gate (no book fetch). A relaxed cfg disables the toxicity/runway gates
    (``min_hours``/``max_one_day_change``) while keeping affordability and the exclude list."""
    if pair.rewards_rate_per_day <= 0 or not pair.rewards_max_spread:
        return False
    if cfg.max_min_size > 0 and pair.rewards_min_size > cfg.max_min_size:
        return False
    haystack = f"{pair.question} {pair.event_slug or ''} {pair.market_slug or ''}".lower()
    if any(kw in haystack for kw in cfg.exclude_keywords):
        return False
    hours = _hours_left(pair)
    if cfg.min_hours > 0 and hours is not None and hours < cfg.min_hours:
        return False
    if cfg.max_one_day_change > 0 and abs(pair.one_day_price_change) > cfg.max_one_day_change:
        return False
    return True


@dataclass(frozen=True)
class DepthAssessment:
    """Result of mirroring the live depth planner on a candidate pair."""

    farmable: bool
    buffer_shares: float  # worst-leg cumulative depth-ahead at the chosen depth tick
    price_a: float | None
    price_b: float | None


def _depth_farm_assessment(
    pair: RewardPair,
    book_a: Book,
    book_b: Book,
    cfg: ScanConfig,
    desired_size: float,
) -> DepthAssessment:
    """Decide farmability the way the live engine does.

    Reuses ``RewardQuotePlanner.plan_buy`` in depth mode: a leg is farmable iff the planner finds
    an in-zone tick whose cumulative resting depth-ahead clears the floor buffer (else it returns
    ``None`` — exactly what ``RewardEngine._leg_farmable_by_depth`` checks). Reports the worst-leg
    depth-ahead at the chosen tick, i.e. the protective buffer the live quote would rest behind.
    The scanner's ``RewardPair`` carries default tick / min-order-size, so we rebuild each leg with
    the real values off its order book before planning.
    """
    planner = RewardQuotePlanner(cfg.quote_distance_fraction, depth_based=True)
    legs: list[tuple[bool, float, float | None]] = []
    for book, leg, outcome in (
        (book_a, "a", pair.outcome_a),
        (book_b, "b", pair.outcome_b),
    ):
        fast = FastBook.from_book(book)
        leg_pair = replace(
            pair, tick_size=fast.tick_size, min_order_size=float(book.min_order_size)
        )
        plan = planner.plan_buy(
            pair=leg_pair,
            book=fast,
            leg=leg,
            outcome=outcome,
            size_shares=desired_size,
        )
        if plan is None:
            legs.append((False, 0.0, None))
            continue
        buffer = float(fast.cumulative_depth_ahead("BUY", plan.price))
        legs.append((True, buffer, plan.price))
    farmable = all(ok for ok, _buf, _price in legs)
    buffer_worst = min(buf for _ok, buf, _price in legs) if farmable else 0.0
    return DepthAssessment(
        farmable=farmable,
        buffer_shares=buffer_worst,
        price_a=legs[0][2],
        price_b=legs[1][2],
    )


def _score_pair(
    pair: RewardPair,
    books: dict[str, Book],
    cfg: ScanConfig,
    *,
    category: str | None = None,
) -> ScoredMarket | None:
    """Score a hydrated pair against ``cfg``; returns None if a book-quality gate rejects it."""
    book_a = books.get(pair.token_a)
    book_b = books.get(pair.token_b)
    if book_a is None or book_b is None:
        return None
    max_spread = float(pair.rewards_max_spread or 0.0)
    required_size = max(pair.rewards_min_size, pair.min_order_size)
    desired_size = max(cfg.size_shares, required_size)
    cutoff = required_size  # engine dust cutoff defaults to min reward/order size
    mid = _adjusted_mid(book_a, cutoff)
    if mid is None:
        return None
    if cfg.min_midpoint > 0 and (mid < cfg.min_midpoint or mid > 1.0 - cfg.min_midpoint):
        return None  # extreme price -> heavy one-sided inventory

    # Depth mode mirrors the live planner: a market is only farmable if both legs have an in-zone
    # tick whose resting depth-ahead clears the floor buffer for our size. Drop the rest (the live
    # engine's plan_buy would return None there too) and record the real protective buffer.
    depth_buffer = 0.0
    if cfg.depth_based:
        assessment = _depth_farm_assessment(pair, book_a, book_b, cfg, desired_size)
        if not assessment.farmable:
            return None
        depth_buffer = assessment.buffer_shares

    competition, top_depth = _competitor_q([book_a, book_b], max_spread, cutoff)
    depth_a = _top_depth(book_a)
    depth_b = _top_depth(book_b)
    # The top-of-book depth gate is the wrong granularity for depth placement (which farms the
    # whole reward zone, not just the best level), so it only applies in non-depth mode.
    if not cfg.depth_based and cfg.min_top_depth > 0 and min(depth_a, depth_b) < cfg.min_top_depth:
        return None  # dead/illiquid book
    if cfg.depth_based:
        # Feasibility above already confirmed an in-zone tick holds the buffer for our size, so the
        # achievable quote size is our configured size (no top-of-book cap).
        estimated_size = cfg.size_shares if cfg.size_shares + 1e-12 >= required_size else 0.0
    else:
        depth_cap = min(depth_a, depth_b) / max(1.0, cfg.entry_depth_multiplier)
        estimated_size = min(desired_size, depth_cap) if depth_cap >= required_size else 0.0
    quote_size = estimated_size if estimated_size > 0 else desired_size

    # Sweep-resistance: shares an adverse order must eat to reach our quote (worst side). In depth
    # mode this is the planner's actual depth-ahead at the chosen tick; otherwise the price-band
    # proxy. The bid/ask split is kept either way for the imbalance signal.
    quote_distance = cfg.quote_distance_fraction * max_spread
    protect_bid, protect_ask = _protective_depth(book_a, mid, quote_distance, cutoff)
    sweep_shares = depth_buffer if cfg.depth_based else min(protect_bid, protect_ask)
    total_protect = protect_bid + protect_ask
    imbalance = protect_bid / total_protect if total_protect > 0 else 0.5

    projected = projected_daily_reward(
        pair.rewards_rate_per_day,
        competition,
        cfg,
        size_shares=estimated_size,
    )
    share = _share(pair.rewards_rate_per_day, projected)
    category = category or classify_category(
        pair.question,
        event_slug=pair.event_slug,
        market_slug=pair.market_slug,
        event_title=pair.event_title,
        category=pair.category,
        tags=pair.tags,
        sport=pair.sport,
        series_id=pair.series_id,
    )
    is_weather = category == "weather"
    hours_left = _hours_left(pair)
    tier = _risk_tier(pair.question, is_weather, cfg, category=category)
    size_eligible = cfg.size_shares + 1e-12 >= required_size
    depth_ratio = min(depth_a, depth_b) / desired_size if desired_size > 0 else 0.0

    flags: list[str] = [tier]
    if cfg.depth_based:
        flags.append("depth-farmable")
    if competition <= 1e-9:
        flags.append("empty-book")  # capped share but no flow — treat with care
    if 0.0 < (hours_left or 1e9) < 24:
        flags.append("<24h")
    if hours_left is not None and hours_left < 6:
        flags.append("near-resolution")
    if not size_eligible:
        flags.append("requires-size")
    if not cfg.depth_based and depth_ratio < max(1.0, cfg.entry_depth_multiplier):
        flags.append("thin")  # top-of-book proxy; depth mode uses the buffer feasibility instead
    if estimated_size <= 0:
        flags.append("depth-below-min")
    if sweep_shares < cfg.sweep_floor_mult * desired_size:
        flags.append("sweepable")  # one order can clear our quote
    if cfg.imbalance_warn > 0 and max(imbalance, 1.0 - imbalance) > cfg.imbalance_warn:
        flags.append("imbalanced")
    if pair.volume_24hr < cfg.activity_floor_volume:
        flags.append("low-volume")
    # Micro-props ("announcers say X") keep a flat price until the broadcast event, so fill risk
    # for a passive LP is low — softened from a near-veto to a mild discount for the binary
    # resolution snap. Still badged so the user can see what it is.
    micro_prop_factor = 0.60 if _is_micro_prop(pair.question) else 1.0
    if micro_prop_factor < 1.0:
        flags.append("micro-prop")

    market = ScoredMarket(
        condition_id=pair.condition_id,
        question=pair.question,
        is_weather=is_weather,
        mid=mid,
        rate_per_day=pair.rewards_rate_per_day,
        min_size=pair.rewards_min_size,
        max_spread=max_spread,
        competition=competition,
        top_depth=top_depth,
        hours_left=hours_left,
        one_day_change=pair.one_day_price_change,
        volume_24hr=pair.volume_24hr,
        projected_reward=projected,
        share=share,
        risk_factor=1.0,
        risk_tier=tier,
        category=category,
        score=projected,
        token_a=pair.token_a,
        token_b=pair.token_b,
        required_size=required_size,
        quote_size=quote_size,
        size_eligible=size_eligible,
        depth_a=depth_a,
        depth_b=depth_b,
        depth_ratio=depth_ratio,
        depth_factor=_depth_factor(depth_ratio, cfg),
        sweep_shares=sweep_shares,
        sweep_factor=_sweep_factor(sweep_shares, desired_size, cfg),
        depth_buffer_shares=depth_buffer,
        protect_bid=protect_bid,
        protect_ask=protect_ask,
        imbalance=imbalance,
        imbalance_factor=_imbalance_factor(imbalance, cfg),
        activity_factor=_activity_factor(pair.volume_24hr, cfg),
        resolution_factor=_resolution_factor(hours_left),
        category_factor=_category_factor(category, tier) * micro_prop_factor,
        flags=flags,
    )
    _finalize_score(market, cfg)
    return market


async def scan_reward_markets(
    clob: ClobClient,
    cfg: ScanConfig | None = None,
    *,
    settings: Settings | None = None,
) -> list[ScoredMarket]:
    """Return the reward markets that survive the risk filters, ranked best-first."""
    if cfg is None:
        cfg = ScanConfig.from_settings(settings) if settings is not None else ScanConfig()

    pool = await _fetch_pool(clob, cfg.pool_limit)
    candidates = [
        pair
        for market in pool
        if (pair := RewardPair.from_market(market)) is not None and _passes_prefilter(pair, cfg)
    ]
    resolver = (
        await CategoryResolver.build(
            settings,
            candidates,
            metadata_event_limit=cfg.metadata_event_limit,
        )
        if settings is not None
        else CategoryResolver()
    )
    # Keep the richest-by-rate slice before paying for book fetches.
    candidates.sort(key=lambda p: p.rewards_rate_per_day, reverse=True)
    candidates = candidates[: cfg.hydrate_limit]

    token_ids = [t for p in candidates for t in (p.token_a, p.token_b)]
    books = await clob.get_order_books(token_ids)

    scored = [
        sm
        for p in candidates
        if (sm := _score_pair(p, books, cfg, category=resolver.classify(p))) is not None
    ]
    scored.sort(key=lambda m: m.score, reverse=True)
    scored = await _apply_recent_volatility_gate(clob, scored, cfg)
    return scored[: cfg.top] if cfg.top > 0 else scored


async def _apply_volatility_gate(
    clob: ClobClient, scored: list[ScoredMarket], cfg: ScanConfig
) -> list[ScoredMarket]:
    """Drop the jumpiest markets via prices-history.

    Only the top score contenders are checked (limits prices-history calls). A market with no
    history is KEPT (don't penalise missing data). Markets are annotated with their measured
    ``volatility``/``max_step`` either way. Returns the survivors re-sorted by score.
    """
    if cfg.max_recent_step <= 0 and cfg.max_recent_range <= 0:
        return scored
    contenders = scored[: max(cfg.top * 4, 20)] if cfg.top > 0 else scored
    semaphore = asyncio.Semaphore(10)

    async def gate(market: ScoredMarket) -> ScoredMarket | None:
        async with semaphore:
            result = await clob.price_volatility(market.token_a, interval=cfg.volatility_interval)
        if result is None:
            return market  # no data → keep
        price_range, max_step = result
        market.volatility = price_range
        market.max_step = max_step
        if cfg.max_recent_step > 0 and max_step > cfg.max_recent_step:
            market.flags.append("jumpy")
            return None
        if cfg.max_recent_range > 0 and price_range > cfg.max_recent_range:
            market.flags.append("volatile")
            return None
        return market

    kept = [m for m in await asyncio.gather(*(gate(m) for m in contenders)) if m is not None]
    kept.sort(key=lambda m: m.score, reverse=True)
    return kept


async def _apply_recent_volatility_gate(
    clob: ClobClient,
    scored: list[ScoredMarket],
    cfg: ScanConfig,
) -> list[ScoredMarket]:
    """Annotate and remove markets that moved too much in the last 10m/2m."""
    if cfg.volatility_10m_reject <= 0 and cfg.volatility_2m_reject <= 0:
        return scored
    contenders = scored[: max(cfg.top * 8, 40)] if cfg.top > 0 else scored
    semaphore = asyncio.Semaphore(10)

    async def gate(market: ScoredMarket) -> ScoredMarket | None:
        async with semaphore:
            history = await clob.price_history(market.token_a, interval=cfg.volatility_interval)
        if not history:
            market.flags.append("missing-history")
            market.volatility_factor *= 0.90
            market.risk_factor *= 0.90
            market.score = market.projected_reward * market.risk_factor
            return market
        market.volatility_10m, _step_10m = _history_window_range(history, 600.0)
        market.volatility_2m, market.max_step_2m = _history_window_range(history, 120.0)
        market.volatility = market.volatility_10m
        market.max_step = market.max_step_2m
        _finalize_score(market, cfg)
        return None if market.hard_reject else market

    checked = [m for m in await asyncio.gather(*(gate(m) for m in contenders)) if m is not None]
    unchecked = scored[len(contenders):]
    kept = [*checked, *unchecked]
    kept.sort(key=lambda m: m.score, reverse=True)
    return kept


def relaxed_config(settings: Settings) -> ScanConfig:
    """Discovery gates for non-weather categories (sports/politics/tech/other).

    The bot already trusts human-pinned markets and skips its own toxicity filters, so the
    picker may surface these; we only drop the weather-tuned runway/volatility/extreme gates
    while keeping affordability (``max_min_size``), a real-book floor, and the exclude list.
    Ranking still uses the fill-risk score, so the genuinely best farm markets rise first.
    """
    weather = ScanConfig.from_settings(settings)
    return ScanConfig.from_settings(
        settings,
        min_hours=0.0,
        max_one_day_change=0.0,
        min_midpoint=min(weather.min_midpoint, 0.03),
        min_top_depth=0.0,
        max_min_size=0.0,
    )


def _category_shortlist(pairs: list[RewardPair], limit: int) -> list[RewardPair]:
    """Pick a bounded per-category universe without starving high-volume safe markets."""
    if limit <= 0:
        return []
    selected: dict[str, RewardPair] = {}

    def add(items: list[RewardPair]) -> None:
        for pair in items:
            selected.setdefault(pair.condition_id, pair)
            if len(selected) >= limit:
                return

    per_slice = max(1, limit // 2)
    rate_sorted = sorted(pairs, key=lambda p: p.rewards_rate_per_day, reverse=True)
    volume_sorted = sorted(pairs, key=lambda p: p.volume_24hr, reverse=True)
    rate_slice = rate_sorted[:per_slice]
    volume_slice = volume_sorted[:per_slice]
    add(rate_slice)
    add(volume_slice)
    add(rate_sorted)
    return list(selected.values())


async def scan_by_category(
    clob: ClobClient,
    *,
    settings: Settings,
    per_category: int = 10,
    hydrate_per_category: int = 40,
    size_shares: float | None = None,
) -> dict[str, list[ScoredMarket]]:
    """Scan the reward pool per category with the same size-aware risk model for all groups."""
    base_cfg = relaxed_config(settings)
    if size_shares is not None:
        safe_size = max(1.0, float(size_shares))
        base_cfg = replace(base_cfg, size_shares=safe_size, max_min_size=safe_size)
    scan_cfg = replace(base_cfg, top=per_category)

    pool = await _fetch_pool(clob, scan_cfg.pool_limit)
    pairs = [
        pair
        for market in pool
        if (pair := RewardPair.from_market(market)) is not None
        and _passes_prefilter(pair, scan_cfg)
    ]
    resolver = await CategoryResolver.build(
        settings,
        pairs,
        metadata_event_limit=scan_cfg.metadata_event_limit,
    )

    buckets: dict[str, list[RewardPair]] = {}
    for pair in pairs:
        category = resolver.classify(pair)
        buckets.setdefault(category, []).append(pair)

    selected: list[RewardPair] = []
    selected_categories: dict[str, str] = {}
    shortlist_limit = max(hydrate_per_category * 2, per_category * 6, 20)
    for category, category_pairs in buckets.items():
        for pair in _category_shortlist(category_pairs, shortlist_limit):
            selected.append(pair)
            selected_categories[pair.condition_id] = category

    token_ids = [t for p in selected for t in (p.token_a, p.token_b)]
    books = await clob.get_order_books(token_ids)

    out: dict[str, list[ScoredMarket]] = {}
    for pair in selected:
        category = selected_categories.get(pair.condition_id, resolver.classify(pair))
        scored = _score_pair(pair, books, scan_cfg, category=category)
        if scored is not None:
            out.setdefault(category, []).append(scored)
    for category, items in out.items():
        items.sort(key=lambda m: m.score, reverse=True)
        # The recent-volatility gate still hard-rejects genuinely toxic (fast-moving) markets —
        # that is real adverse selection, not just a thin book. Everything else is ranked, not
        # dropped: the sweep/imbalance factors already demote thin and one-sided books down the
        # list, and the UI badges them, so a sparse category still fills toward `per_category`
        # with honest risk labels instead of showing an empty panel.
        gated = await _apply_recent_volatility_gate(clob, items, scan_cfg)
        out[category] = gated[:per_category]
    for category in (*CATEGORY_ORDER, "other"):
        out.setdefault(category, [])
    return out
