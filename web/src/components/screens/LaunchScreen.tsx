"use client";

import { useEffect, useMemo, useState } from "react";
import { motion } from "motion/react";
import { Check, ChevronDown, ChevronUp, ChevronsUpDown, RefreshCw, Search } from "lucide-react";
import type { ControlState } from "@/lib/useControl";
import type { LaunchConfig, MarketRow } from "@/lib/types";
import { useMarkets, type MarketParams } from "@/lib/useMarkets";
import { LaserFlow } from "@/components/ui/LaserFlow";
import { SpotlightCard } from "@/components/ui/SpotlightCard";
import { StardustButton } from "@/components/ui/StardustButton";
import { Highlight, RiseWord } from "@/components/ui/HighlightText";
import { SliderBase, SliderWithOutput } from "@/components/ui/Slider";
import { InfoTip } from "@/components/ui/InfoTip";
import { cn } from "@/lib/utils";
import { estimateDailyReward, money } from "@/lib/format";

const CATEGORIES: { key: string; label: string }[] = [
  { key: "weather", label: "Weather" },
  { key: "finance", label: "Finance" },
  { key: "sports", label: "Sports" },
  { key: "politics", label: "Politics" },
  { key: "tech", label: "Tech" },
  { key: "other", label: "Other" },
];

const GRADES = ["A", "B", "C"];

const GRADE_TONE: Record<string, string> = {
  A: "text-profit",
  B: "text-warn",
  C: "text-loss",
};

const GATE_LABELS: Record<string, string> = {
  gap_risk: "gap risk",
  live_sports: "live",
  near_resolution: "closing",
  volatile: "volatile",
  volatile_1d: "1d move",
  extreme_price: "extreme",
  one_sided: "one-sided",
  thin: "thin",
};

const MIN_SHARE_OPTIONS: { label: string; value: number | null }[] = [
  { label: "Any size", value: null },
  { label: "≤ 20", value: 20 },
  { label: "≤ 50", value: 50 },
  { label: "≤ 100", value: 100 },
  { label: "≤ 250", value: 250 },
  { label: "≤ 500", value: 500 },
];

// Minimum runway before resolution (value in HOURS). Filters out markets close to resolving —
// the risky convergence tail. For the ~2-day weather markets, "> 1 day" is the catch-all preset.
const RESOLVE_OPTIONS: { label: string; value: number | null }[] = [
  { label: "Any", value: null },
  { label: "> 1 day", value: 24 },
  { label: "> 2 days", value: 48 },
  { label: "> 4 days", value: 96 },
  { label: "> 7 days", value: 168 },
  { label: "> 30 days", value: 720 },
];

const SIZE_MIN = 20;
const SIZE_MAX = 1000;
const SIZE_STEP = 10;
const PAGE_SIZE = 24;

const clampSize = (n: number) =>
  Math.min(SIZE_MAX, Math.max(SIZE_MIN, Math.round(n / SIZE_STEP) * SIZE_STEP));

const hoursLabel = (hours: number | null) => {
  if (hours == null) return "n/a";
  if (hours >= 48) return `${Math.round(hours / 24)}d`;
  if (hours >= 1) return `${Math.round(hours)}h`;
  return "<1h";
};
const centsLabel = (value: number) => `${Math.round(value * 100)}c`;
const agoLabel = (ts: number) => {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
};

const GLASS_PILL_ON = {
  background: "linear-gradient(180deg, rgba(91,140,255,0.24), rgba(91,140,255,0.08))",
  boxShadow:
    "inset 0 1px 0 rgba(255,255,255,0.16), 0 0 0 1px rgba(91,140,255,0.42), 0 8px 20px -10px rgba(91,140,255,0.55)",
} as const;
const GLASS_PILL_OFF = {
  background: "rgba(255,255,255,0.028)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.05), 0 0 0 1px rgba(255,255,255,0.06)",
} as const;
const GLASS_FIELD = {
  background: "rgba(255,255,255,0.028)",
  boxShadow: "inset 0 1px 0 rgba(255,255,255,0.05), 0 0 0 1px rgba(255,255,255,0.07)",
} as const;

function SortHeader({
  label,
  field,
  sort,
  onSort,
}: {
  label: string;
  field: string;
  sort: { key: string; desc: boolean };
  onSort: (k: string) => void;
}) {
  const active = sort.key === field;
  return (
    <button
      onClick={() => onSort(field)}
      className={cn(
        "ml-auto flex items-center gap-1 text-[10.5px] font-medium uppercase tracking-wider transition-colors",
        active ? "text-accent-soft" : "text-faint hover:text-muted",
      )}
    >
      {label}
      {active ? (
        sort.desc ? <ChevronDown size={12} /> : <ChevronUp size={12} />
      ) : (
        <ChevronsUpDown size={11} className="opacity-50" />
      )}
    </button>
  );
}

function Chip({
  active,
  onClick,
  children,
  count,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  count?: number;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      style={active ? GLASS_PILL_ON : GLASS_PILL_OFF}
      className={cn(
        "flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[12px] transition-all duration-200",
        active ? "text-foreground" : "text-muted hover:text-foreground",
      )}
    >
      {children}
      {count != null && count > 0 && <span className="tnum text-[10px] text-faint">{count}</span>}
    </button>
  );
}

function PriceCell({ mid, approx }: { mid: number | null; approx?: boolean }) {
  if (mid == null) return <span className="text-faint">—</span>;
  // Approximate = from Gamma metadata (no order-book pass yet), so render it muted with a tilde.
  if (approx) {
    return (
      <span className="tnum text-[11px] text-faint" title="From market metadata — book not fetched yet">
        ~{Math.round(mid * 100)} · {Math.round((1 - mid) * 100)}
      </span>
    );
  }
  return (
    <span className="tnum text-[11px]">
      <span className="text-profit">{Math.round(mid * 100)}</span>
      <span className="text-faint"> · </span>
      <span className="text-loss">{Math.round((1 - mid) * 100)}</span>
    </span>
  );
}

export function LaunchScreen({ control }: { control: ControlState }) {
  // Launch settings (the bot's quote size lives here ONLY — it no longer filters/re-ranks the list).
  const [size, setSize] = useState(20);
  const [maxLoss, setMaxLoss] = useState(0.5);
  const [hours, setHours] = useState(0);
  const [haltOnStop, setHaltOnStop] = useState(false);
  const [arbClose, setArbClose] = useState(true);

  // Selection basket — keyed by condition_id, holds the full row so a market stays selected even
  // after it scrolls off the current page / filter.
  const [basket, setBasket] = useState<Record<string, MarketRow>>({});
  const [confirming, setConfirming] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  // Filters / sort / pagination.
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [categories, setCategories] = useState<string[]>([]);
  const [grades, setGrades] = useState<string[]>([]);
  const [includeRisky, setIncludeRisky] = useState(false);
  const [maxShares, setMaxShares] = useState<number | null>(null);
  const [minHours, setMinHours] = useState<number | null>(null);
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: "reward", desc: true });
  const [page, setPage] = useState(1);

  useEffect(() => {
    const t = setTimeout(() => {
      setSearch(searchInput.trim());
      setPage(1);
    }, 300);
    return () => clearTimeout(t);
  }, [searchInput]);

  const params: MarketParams = {
    page,
    pageSize: PAGE_SIZE,
    search,
    categories,
    grades,
    includeRisky,
    maxShares,
    minHours,
    sort: sort.key,
    descending: sort.desc,
  };
  const { data, loading, error, refresh } = useMarkets(params);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const facets = data?.facets ?? {};
  const warming = !!data?.warming;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const chosen = useMemo(() => Object.values(basket), [basket]);
  const selectedIds = useMemo(() => new Set(Object.keys(basket)), [basket]);
  const undersized = chosen.filter((m) => m.min_shares > size);

  // Position-aware daily earnings estimate for the SELECTED basket only (a handful of markets), so
  // it recomputes instantly as the size slider moves — never over all ~20k indexed markets. Uses
  // the per-market competitor liquidity + the formula constants the index ships in `estimate`.
  const estCfg = data?.estimate;
  const estById = useMemo(() => {
    const m = new Map<string, number>();
    if (estCfg) {
      for (const row of chosen) {
        m.set(row.condition_id, estimateDailyReward(row.rate_per_day, row.competitor_q, size, estCfg));
      }
    }
    return m;
  }, [chosen, size, estCfg]);
  const estDaily = useMemo(
    () => chosen.reduce((s, row) => s + (estById.get(row.condition_id) ?? 0), 0),
    [chosen, estById],
  );
  const maxEst = Math.max(...chosen.map((row) => estById.get(row.condition_id) ?? 0), 1e-9);

  // Paper is the default: a start is never blocked. `gate_problems` only explains what is
  // still missing before the bot would place REAL orders.
  const isLive = control.status?.mode === "live";
  const gateProblems = control.status?.gate_problems ?? [];

  const config: LaunchConfig = {
    markets: chosen.length || 1,
    size,
    max_loss_usdc: maxLoss,
    seconds: hours > 0 ? hours * 3600 : null,
    market_ids: chosen.map((m) => m.condition_id),
    halt_on_stop_loss: haltOnStop,
    arb_close_after_fill: arbClose,
  };

  const toggle = (row: MarketRow) => {
    setStartError(null);
    setBasket((prev) => {
      const next = { ...prev };
      if (next[row.condition_id]) delete next[row.condition_id];
      else next[row.condition_id] = row;
      return next;
    });
  };

  const toggleCategory = (key: string) => {
    setPage(1);
    setCategories((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]));
  };
  const toggleGrade = (g: string) => {
    setPage(1);
    setGrades((prev) => (prev.includes(g) ? prev.filter((k) => k !== g) : [...prev, g]));
  };
  const onSort = (key: string) => {
    setPage(1);
    setSort((prev) => (prev.key === key ? { key, desc: !prev.desc } : { key, desc: true }));
  };

  const confirmStart = async () => {
    if (undersized.length) {
      const required = Math.max(...undersized.map((m) => m.min_shares));
      setStartError(`A selected market needs ≥ ${required.toFixed(0)} shares — raise position size.`);
      setConfirming(false);
      return;
    }
    const ok = await control.start(config);
    if (ok) setConfirming(false);
  };

  return (
    <div className="relative min-h-screen overflow-hidden bg-base">
      {/* contrasting laser beam on the right — a tall column that rises from behind the cards */}
      <div
        aria-hidden
        className="pointer-events-none absolute right-0 z-0 h-[1040px] w-[58%] max-w-[880px]"
        style={{
          top: "-210px",
          maskImage: "linear-gradient(180deg, black 0%, black 68%, transparent 96%)",
          WebkitMaskImage: "linear-gradient(180deg, black 0%, black 68%, transparent 96%)",
        }}
      >
        <LaserFlow
          color="#5b8cff"
          horizontalBeamOffset={0.04}
          verticalBeamOffset={0.0}
          verticalSizing={24.0}
          horizontalSizing={0.95}
          fogIntensity={0.9}
          fogScale={0.32}
          wispSpeed={12}
          wispIntensity={6}
          flowSpeed={0.35}
        />
      </div>

      <div className="relative z-10 mx-auto max-w-[1280px] px-5 py-12">
        {/* hero — heading on the left; the laser (behind) fills the right and reaches the cards */}
        <motion.section
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
          className="mb-10 max-w-xl pt-4"
        >
          <p className="eyebrow mb-3">Automated Polymarket liquidity rewards</p>
          <h1 className="font-display text-[clamp(32px,4.8vw,54px)] font-medium leading-[1.04] tracking-[-0.03em] text-foreground">
            <RiseWord delay={0.15}>Browse</RiseWord> <Highlight delay={0.5}>reward markets</Highlight>
          </h1>
          <p className="mt-4 max-w-md text-[14px] leading-relaxed text-muted">
            Pick any markets to farm, then set risk limits and launch your bot.
          </p>
          <button
            onClick={refresh}
            style={GLASS_PILL_OFF}
            className="mt-6 inline-flex items-center gap-2 rounded-full px-3.5 py-2 text-[11px] text-muted transition-colors hover:text-foreground"
          >
            <RefreshCw size={12} className={cn((loading || warming) && "animate-spin")} />
            {warming ? "Warming…" : `Updated ${agoLabel(data?.updated_at ?? 0)}`}
          </button>
        </motion.section>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          {/* ---- markets browser ---- */}
          <motion.div
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.06, ease: [0.16, 1, 0.3, 1] }}
          >
            <SpotlightCard className="p-5" spot="rgba(120,150,255,0.10)">
              {/* filter bar */}
              <div className="space-y-3.5 pb-4">
                <div className="relative">
                  <Search
                    size={15}
                    className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint"
                  />
                  <input
                    value={searchInput}
                    onChange={(e) => setSearchInput(e.target.value)}
                    placeholder="Search markets"
                    style={GLASS_FIELD}
                    className="w-full rounded-xl py-2.5 pl-10 pr-3 text-[13px] text-foreground outline-none transition-shadow placeholder:text-faint focus:shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_0_0_1px_rgba(91,140,255,0.5)]"
                  />
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Chip active={categories.length === 0} onClick={() => setCategories([])}>
                    All
                  </Chip>
                  {CATEGORIES.map((c) => (
                    <Chip
                      key={c.key}
                      active={categories.includes(c.key)}
                      onClick={() => toggleCategory(c.key)}
                      count={facets[c.key]}
                    >
                      {c.label}
                    </Chip>
                  ))}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10.5px] uppercase tracking-wider text-faint">Grade</span>
                    {GRADES.map((g) => (
                      <Chip key={g} active={grades.includes(g)} onClick={() => toggleGrade(g)}>
                        <span className={cn("font-semibold", GRADE_TONE[g])}>{g}</span>
                      </Chip>
                    ))}
                  </div>
                  <div className="flex items-center gap-3">
                    <select
                      value={minHours == null ? "null" : String(minHours)}
                      onChange={(e) => {
                        setPage(1);
                        const v = e.target.value;
                        setMinHours(v === "null" ? null : Number(v));
                      }}
                      style={GLASS_FIELD}
                      className="rounded-lg px-2.5 py-1.5 text-[12px] text-muted outline-none focus:text-foreground"
                      aria-label="Minimum time to resolution filter"
                    >
                      {RESOLVE_OPTIONS.map((o) => (
                        <option key={o.label} value={o.value == null ? "null" : String(o.value)}>
                          Resolves: {o.label}
                        </option>
                      ))}
                    </select>
                    <select
                      value={maxShares == null ? "null" : String(maxShares)}
                      onChange={(e) => {
                        setPage(1);
                        const v = e.target.value;
                        setMaxShares(v === "null" ? null : Number(v));
                      }}
                      style={GLASS_FIELD}
                      className="rounded-lg px-2.5 py-1.5 text-[12px] text-muted outline-none focus:text-foreground"
                      aria-label="Min shares filter"
                    >
                      {MIN_SHARE_OPTIONS.map((o) => (
                        <option key={o.label} value={o.value == null ? "null" : String(o.value)}>
                          Min shares: {o.label}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={includeRisky}
                      onClick={() => {
                        setPage(1);
                        setIncludeRisky((v) => !v);
                      }}
                      className="flex items-center gap-2 text-[12px] text-muted transition-colors hover:text-foreground"
                    >
                      <span
                        className={cn(
                          "flex h-4 w-7 shrink-0 items-center rounded-full px-0.5 transition-colors",
                          includeRisky ? "bg-accent shadow-[0_0_10px_1px_rgba(91,140,255,0.6)]" : "bg-line-strong",
                        )}
                      >
                        <span
                          className={cn(
                            "h-3 w-3 rounded-full bg-[#e9ecf5] transition-transform",
                            includeRisky && "translate-x-3",
                          )}
                        />
                      </span>
                      Show risky
                    </button>
                  </div>
                </div>
              </div>

              {/* table header */}
              <div className="grid grid-cols-[20px_1fr_84px_104px_72px_84px] items-center gap-3 border-t border-white/[0.06] px-3 pb-2 pt-3 text-[10.5px] uppercase tracking-wider text-faint">
                <span />
                <span>Market</span>
                <div className="flex">
                  <SortHeader label="Min sh" field="min_shares" sort={sort} onSort={onSort} />
                </div>
                <div className="flex">
                  <SortHeader label="Reward" field="reward" sort={sort} onSort={onSort} />
                </div>
                <span className="text-right">Grade</span>
                <span className="text-right">Y · N¢</span>
              </div>

              {/* rows */}
              <div className="scroll-thin max-h-[520px] space-y-0.5 overflow-auto pr-1">
                {loading && items.length === 0 ? (
                  Array.from({ length: 8 }).map((_, i) => (
                    <div key={i} className="shimmer h-[52px] rounded-xl" />
                  ))
                ) : warming && items.length === 0 ? (
                  <div className="space-y-1.5 py-14 text-center">
                    <p className="flex items-center justify-center gap-2 text-[13px] text-accent-soft">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
                      Building the market index…
                    </p>
                    <p className="text-[11px] text-faint">
                      Loading the full reward pool once (about 2–3 min). It then stays fresh and this
                      list updates automatically.
                    </p>
                  </div>
                ) : error && items.length === 0 ? (
                  <p className="py-14 text-center text-[12px] text-loss">{error}</p>
                ) : items.length === 0 ? (
                  <p className="py-14 text-center text-[12px] text-faint">
                    No markets match these filters.
                  </p>
                ) : (
                  items.map((m) => {
                    const on = selectedIds.has(m.condition_id);
                    const warn = m.risk_label.startsWith("⚠");
                    const needsMore = m.min_shares > size;
                    return (
                      <button
                        key={m.condition_id}
                        onClick={() => toggle(m)}
                        style={{
                          animation: "fade-rise .3s ease both",
                          ...(on
                            ? {
                                background:
                                  "linear-gradient(180deg, rgba(91,140,255,0.14), rgba(91,140,255,0.04))",
                                boxShadow:
                                  "inset 3px 0 0 0 rgba(91,140,255,0.9), inset 0 1px 0 rgba(255,255,255,0.06), 0 10px 26px -18px rgba(91,140,255,0.6)",
                              }
                            : {}),
                        }}
                        className={cn(
                          "grid w-full grid-cols-[20px_1fr_84px_104px_72px_84px] items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors",
                          !on && "hover:bg-white/[0.035]",
                        )}
                      >
                        <span
                          className={cn(
                            "flex h-3.5 w-3.5 items-center justify-center rounded-[5px] transition-colors",
                            on ? "bg-accent" : "bg-white/[0.04] shadow-[inset_0_0_0_1px_rgba(255,255,255,0.14)]",
                          )}
                        >
                          {on && <Check size={10} className="text-[#06070a]" strokeWidth={3} />}
                        </span>
                        <div className="min-w-0">
                          <p className="truncate text-[13px] text-foreground">{m.question}</p>
                          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10.5px] text-faint">
                            <span
                              className="rounded px-1.5 py-0.5 uppercase tracking-wide text-muted"
                              style={{ background: "rgba(255,255,255,0.05)" }}
                            >
                              {m.category}
                            </span>
                            {!m.safe &&
                              m.gate_reasons.slice(0, 2).map((r) => (
                                <span
                                  key={r}
                                  className="rounded px-1.5 py-0.5 text-loss"
                                  style={{ background: "rgba(241,111,127,0.12)" }}
                                >
                                  {GATE_LABELS[r] ?? r}
                                </span>
                              ))}
                            <span className={cn(warn ? "text-warn" : "text-profit")}>{m.risk_label}</span>
                            {m.enriched && <span className="tnum">sweep {m.sweep_lots.toFixed(0)}×</span>}
                            <span className="tnum">
                              {m.vol_known ? `vol ${centsLabel(m.volatility_10m)}` : "vol ?"}
                            </span>
                            <span className="tnum">left {hoursLabel(m.hours_left)}</span>
                          </div>
                        </div>
                        <span
                          className={cn(
                            "tnum text-right text-[13px]",
                            needsMore ? "text-warn" : "text-muted",
                          )}
                        >
                          {m.min_shares.toFixed(0)}
                        </span>
                        <span className="tnum text-right text-[13px] text-foreground">
                          {money(m.rate_per_day, 0)}
                          <span className="text-[10px] text-faint">/d</span>
                        </span>
                        <span
                          className={cn(
                            "text-right text-[12px] font-semibold uppercase tnum",
                            GRADE_TONE[m.grade] ?? "text-faint",
                          )}
                        >
                          {m.grade || "—"}
                          {m.grade && (
                            <span className="text-[10px] font-normal text-faint"> {Math.round(m.safety)}</span>
                          )}
                        </span>
                        <span className="text-right">
                          <PriceCell mid={m.mid} approx={!m.enriched} />
                        </span>
                      </button>
                    );
                  })
                )}
              </div>

              {/* pagination */}
              <div className="mt-1 flex items-center justify-between border-t border-white/[0.06] pt-3.5 text-[12px] text-faint">
                <span className="tnum">
                  {total} market{total === 1 ? "" : "s"}
                  {!includeRisky && " · safe only"}
                </span>
                <div className="flex items-center gap-2">
                  <button
                    disabled={page <= 1}
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    style={GLASS_PILL_OFF}
                    className="rounded-lg px-2.5 py-1 transition-colors enabled:hover:text-foreground disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <span className="tnum text-muted">
                    {page} / {pages}
                  </span>
                  <button
                    disabled={page >= pages}
                    onClick={() => setPage((p) => Math.min(pages, p + 1))}
                    style={GLASS_PILL_OFF}
                    className="rounded-lg px-2.5 py-1 transition-colors enabled:hover:text-foreground disabled:opacity-40"
                  >
                    Next
                  </button>
                </div>
              </div>
            </SpotlightCard>
          </motion.div>

          {/* ---- launch panel ---- */}
          <motion.div
            initial={{ opacity: 0, y: 14 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.12, ease: [0.16, 1, 0.3, 1] }}
            className="lg:sticky lg:top-6 lg:self-start"
          >
            <SpotlightCard className="space-y-6 p-6">
              <div className="flex items-center justify-between border-b border-white/[0.06] pb-3">
                <h2 className="eyebrow text-muted">Launch</h2>
                <span className="tnum text-[11px] text-faint">{chosen.length} selected</span>
              </div>

              <div className="space-y-3">
                <div className="flex items-baseline justify-between">
                  <span className="flex items-center gap-1.5">
                    <label className="eyebrow" htmlFor="position-size">
                      Position size
                    </label>
                    <InfoTip
                      label="Position size"
                      text="Shares quoted per order on each side. A launch setting — it doesn’t filter the list."
                    />
                  </span>
                  <div className="flex items-baseline gap-1.5">
                    <input
                      id="position-size"
                      type="number"
                      inputMode="numeric"
                      min={SIZE_MIN}
                      max={SIZE_MAX}
                      step={SIZE_STEP}
                      value={size}
                      onChange={(e) => {
                        const n = Number(e.target.value);
                        if (Number.isFinite(n)) setSize(n);
                      }}
                      onBlur={() => setSize((s) => clampSize(s))}
                      style={GLASS_FIELD}
                      className="w-16 rounded-lg px-2 py-1 text-right tnum text-[15px] font-medium text-foreground outline-none"
                    />
                    <span className="text-[12px] text-faint">sh</span>
                  </div>
                </div>
                <SliderBase
                  value={[Math.min(SIZE_MAX, Math.max(SIZE_MIN, size))]}
                  min={SIZE_MIN}
                  max={SIZE_MAX}
                  step={SIZE_STEP}
                  onValueChange={(v) => setSize(v[0])}
                  aria-label="Position size"
                />
              </div>

              <SliderWithOutput
                label="Max loss / position"
                value={maxLoss}
                onValueChange={setMaxLoss}
                min={0.1}
                max={30}
                step={0.1}
                format={(n) => money(n, 2)}
                info="On breach the bot cancels that market’s orders and alerts — it never auto-sells."
              />
              <SliderWithOutput
                label="Run duration"
                value={hours}
                onValueChange={setHours}
                min={0}
                max={48}
                step={1}
                info="How long the bot farms before stopping on its own. ∞ = runs until you stop it."
                format={(n) => (n === 0 ? "∞ unlimited" : `${n} h`)}
              />
              <div className="space-y-4">
                <div
                  className="flex items-center justify-between gap-3 rounded-xl px-3.5 py-3 transition-all"
                  style={arbClose ? GLASS_PILL_ON : GLASS_FIELD}
                >
                  <span className="flex items-center gap-1.5">
                    <span className="text-[12.5px] text-foreground">Instant arbitrage on fill</span>
                    <InfoTip
                      label="Instant arbitrage on fill"
                      text="On a fill the bot completes the set for a locked arb when it can. Off leaves the filled leg to be managed on its own."
                    />
                  </span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={arbClose}
                    aria-label="Instant arbitrage on fill"
                    onClick={() => setArbClose((v) => !v)}
                    className={cn(
                      "flex h-4 w-7 shrink-0 items-center rounded-full px-0.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                      arbClose ? "bg-accent" : "bg-line-strong",
                    )}
                  >
                    <span
                      className={cn(
                        "h-3 w-3 rounded-full bg-[#e9ecf5] transition-transform",
                        arbClose && "translate-x-3",
                      )}
                    />
                  </button>
                </div>

                <div
                  className="flex items-center justify-between gap-3 rounded-xl px-3.5 py-3 transition-all"
                  style={haltOnStop ? GLASS_PILL_ON : GLASS_FIELD}
                >
                  <span className="flex items-center gap-1.5">
                    <span className="text-[12.5px] text-foreground">Halt market on stop-loss</span>
                    <InfoTip
                      label="Halt market on stop-loss"
                      text="A stopped-out market goes dark until restart; other markets keep farming."
                    />
                  </span>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={haltOnStop}
                    aria-label="Halt market on stop-loss"
                    onClick={() => setHaltOnStop((v) => !v)}
                    className={cn(
                      "flex h-4 w-7 shrink-0 items-center rounded-full px-0.5 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                      haltOnStop ? "bg-accent" : "bg-line-strong",
                    )}
                  >
                    <span
                      className={cn(
                        "h-3 w-3 rounded-full bg-[#e9ecf5] transition-transform",
                        haltOnStop && "translate-x-3",
                      )}
                    />
                  </button>
                </div>
              </div>

              {/* estimated reward for the selected markets at the current position size */}
              <div className="rounded-xl p-3.5" style={GLASS_FIELD}>
                <div className="flex items-baseline justify-between">
                  <span className="flex items-center gap-1.5">
                    <span className="eyebrow">Est. reward</span>
                    <InfoTip
                      label="Est. reward"
                      text="Your projected share of each market’s reward pool at the current position size, given on-book competition — not the full pool."
                    />
                  </span>
                  <span className="tnum text-[15px] font-medium text-foreground">
                    {chosen.length === 0 || !estCfg ? "—" : money(estDaily, 2)}
                    <span className="text-[11px] text-faint">/day</span>
                  </span>
                </div>
                {chosen.length === 0 ? (
                  <p className="mt-1.5 text-[11px] text-faint">None selected — the bot auto-picks the top market.</p>
                ) : (
                  <>
                    <div className="mt-2.5 flex h-8 items-end gap-1">
                      {chosen.slice(0, 16).map((m) => {
                        const est = estById.get(m.condition_id) ?? 0;
                        return (
                          <div
                            key={m.condition_id}
                            className="flex-1 rounded-sm"
                            style={{
                              height: `${Math.max(8, (est / maxEst) * 100)}%`,
                              background:
                                "linear-gradient(180deg, rgba(120,150,255,0.9), rgba(91,140,255,0.4))",
                            }}
                            title={`${m.question} · est. ${money(est, 2)}/day (pool ${money(m.rate_per_day, 0)}/day)`}
                          />
                        );
                      })}
                    </div>
                  </>
                )}
              </div>

              {/* gates + start */}
              <div className="space-y-2">
                {isLive ? (
                  <div className="text-[12.5px]">
                    <p className="flex items-center gap-2 text-profit">
                      <span className="h-1.5 w-1.5 rounded-full bg-profit shadow-[0_0_8px_1px_rgba(114,214,162,0.6)]" />{" "}
                      Live mode — real orders, real funds
                    </p>
                    {control.status?.dry_run_fresh === false && (
                      <p className="mt-1 pl-3.5 text-[12px] text-faint">
                        Starting will first verify the live wiring with one tiny order (placed
                        and cancelled). This takes a few seconds.
                      </p>
                    )}
                  </div>
                ) : (
                  <div className="text-[12px] text-muted">
                    <p className="mb-1 flex items-center gap-2 font-medium text-foreground">
                      <span className="h-1.5 w-1.5 rounded-full bg-muted" />
                      Paper mode — simulated fills, no orders sent
                    </p>
                    <p className="mb-1 text-faint">Still missing for live trading:</p>
                    <ul className="list-disc space-y-0.5 pl-4 text-faint">
                      {gateProblems.map((g) => (
                        <li key={g}>{g}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {(control.error || startError) && (
                  <p className="text-[12px] text-loss">{startError || control.error}</p>
                )}
                {undersized.length > 0 && (
                  <p className="text-[12px] text-warn">
                    {undersized.length} selected market(s) need a larger position size.
                  </p>
                )}
              </div>

              {confirming ? (
                <div className="space-y-3">
                  <p className="text-center text-[12.5px] text-foreground">
                    {isLive ? (
                      <>
                        Launch with <span className="text-accent-soft">real funds</span>?
                      </>
                    ) : (
                      <>
                        Launch a <span className="text-accent-soft">paper</span> run?
                      </>
                    )}
                  </p>
                  <StardustButton
                    onClick={confirmStart}
                    disabled={undersized.length > 0}
                    busy={control.busy}
                  >
                    {control.busy ? "Starting…" : "Confirm launch"}
                  </StardustButton>
                  <button
                    onClick={() => setConfirming(false)}
                    className="w-full rounded-full py-2 text-[12px] text-faint transition-colors hover:text-muted"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <StardustButton
                  onClick={() => setConfirming(true)}
                  disabled={undersized.length > 0}
                  busy={control.busy}
                >
                  {isLive ? "Start farming" : "Start paper run"}
                </StardustButton>
              )}
            </SpotlightCard>
          </motion.div>
        </div>
      </div>
    </div>
  );
}
