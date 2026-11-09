// Shape of GET /api/snapshot (reward mode) as served by polybot/dashboard/server.py.
// Row tables are intentionally loose (Record) — the backend returns wide, evolving
// column sets and the UI reads only the fields it renders.

export type Row = Record<string, unknown>;

export interface RewardRun {
  id: number;
  started_at: number;
  ended_at: number | null;
  status: string; // running | cancelled | completed | ...
  requested_markets: number;
  size_shares: number;
  config_json: string;
}

export interface RealRewards {
  enabled: boolean;
  today_usdc: number | null;
  live_rate_per_day: number | null;
  balance_usdc?: number | null;
  by_market: Array<{
    condition_id: string;
    pct: number;
    rate_per_day: number;
    usdc_per_day: number;
  }>;
  date: string | null;
  error: string | null;
}

export interface Snapshot {
  run: RewardRun | null;
  metrics: Record<string, number | string | null>;
  markets: Row[];
  quotes: Row[];
  fills: Row[];
  inventory: Row[];
  matched_bundles: Row[];
  state_errors: Row[];
  closures: Row[];
  events: Row[];
  real_rewards?: RealRewards;
}

export interface ControlStatus {
  running: boolean;
  pid: number | null;
  params: Record<string, unknown> | null;
  started_at: number | null;
  /** What a start would do now — and, while running, what it actually did. */
  mode: "paper" | "live";
  /** False ⇒ a live start re-verifies the wiring first (one tiny order, then cancelled). */
  dry_run_fresh: boolean;
  /** Live configuration only. When false the bot still starts, in paper mode. */
  gates_ok: boolean;
  gate_problems: string[];
  exit_code: number | null;
  last_log: string;
}

/** Safety-first selector output: a grade + reward + one risk line, with raw signals in `details`. */
export interface TopMarket {
  condition_id: string;
  question: string;
  category: string;
  grade: string; // A | B | C
  safety: number; // 0..100
  risk_label: string; // one short line
  projected_reward: number;
  rate_per_day: number;
  min_size: number;
  required_size: number;
  size_eligible: boolean;
  mid: number;
  details: {
    sweep_lots: number;
    volatility_10m: number;
    volatility_2m: number;
    vol_known: boolean;
    hours_left: number | null;
    one_day_change: number;
  };
}

/** `/api/markets/top` groups markets by display category (weather/sports/politics/tech/other). */
export interface TopMarketsResponse {
  by_category: Record<string, TopMarket[]>;
  error: string | null;
  /** True while the first full reward-pool load is still running in the background. */
  warming?: boolean;
}

/** One row of the size-independent materialized market index (GET /api/markets). */
export interface MarketRow {
  condition_id: string;
  question: string;
  category: string;
  rate_per_day: number; // the market's daily reward pool ($/day) — the "Reward" column
  min_shares: number; // required_size = max(rewards_min_size, min_order_size)
  max_spread: number;
  mid: number | null;
  grade: string; // A | B | C | "" when not yet enriched
  safety: number; // 0..100
  safe: boolean; // passes every hard gate (else hidden unless "show risky" is on)
  gate_reasons: string[];
  risk_label: string;
  sweep_lots: number;
  volatility_10m: number;
  volatility_2m: number;
  vol_known: boolean;
  hours_left: number | null;
  one_day_change: number;
  volume_24hr: number;
  enriched: boolean;
  // fields a local trading agent needs to start farming this market
  token_a: string;
  token_b: string;
  outcome_a: string;
  outcome_b: string;
  tick_size: string;
  min_order_size: number;
  // Distance-weighted competitor liquidity in the reward zone; null on un-enriched tail rows.
  competitor_q: number | null;
}

/** Constants for the position-aware earnings estimate (single source of truth = engine config). */
export interface EstimateConfig {
  q_self_per_share: number;
  competitor_haircut: number;
  share_cap: number;
  competition_floor: number;
  reward_realism_factor: number;
}

/** Paginated, filterable response of GET /api/markets. */
export interface MarketsResponse {
  items: MarketRow[];
  total: number;
  page: number;
  page_size: number;
  facets: Record<string, number>; // markets per category for the current filter set
  updated_at: number;
  status: string; // ready | warming | error
  warming: boolean;
  error: string | null;
  estimate?: EstimateConfig;
}

export interface LaunchConfig {
  markets: number;
  size: number;
  max_loss_usdc: number;
  max_exposure_usdc?: number;
  max_position_shares?: number;
  seconds: number | null;
  market_ids: string[];
  halt_on_stop_loss: boolean;
  arb_close_after_fill: boolean;
}

export const EMPTY_SNAPSHOT: Snapshot = {
  run: null,
  metrics: {},
  markets: [],
  quotes: [],
  fills: [],
  inventory: [],
  matched_bundles: [],
  state_errors: [],
  closures: [],
  events: [],
};
