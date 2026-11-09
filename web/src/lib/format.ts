// Formatting helpers. The backend hands back numbers, numeric strings, or null;
// every helper tolerates all three and never throws.

import type { EstimateConfig } from "./types";

/** Position-aware daily reward estimate for one market, mirroring the engine's
 * projected_daily_reward: your liquidity share of the pool, capped, minus a realism haircut.
 * competitorQ null (un-enriched market) falls back to the competition floor. */
export function estimateDailyReward(
  ratePerDay: number,
  competitorQ: number | null,
  size: number,
  cfg: EstimateConfig,
): number {
  const qSelf = cfg.q_self_per_share * size;
  if (qSelf <= 0 || !(ratePerDay > 0)) return 0;
  const compEff =
    Math.max(Math.max(0, competitorQ ?? 0), cfg.competition_floor) * cfg.competitor_haircut;
  const share =
    compEff <= 0 ? Math.min(1, cfg.share_cap) : Math.min(cfg.share_cap, qSelf / (qSelf + compEff));
  return ratePerDay * share * cfg.reward_realism_factor;
}

export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function fmt(v: unknown, digits = 0): string {
  const n = num(v);
  if (n === null) return "—";
  return n.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function money(v: unknown, digits = 3): string {
  const n = num(v);
  if (n === null) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function signedMoney(v: unknown, digits = 3): string {
  const n = num(v);
  if (n === null) return "—";
  const s = money(v, digits);
  return n > 0 ? `+${s}` : s;
}

export function price(v: unknown, digits = 4): string {
  const n = num(v);
  if (n === null) return "—";
  return n.toFixed(digits);
}

export function pct(v: unknown, digits = 1): string {
  const n = num(v);
  if (n === null) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function pnlTone(v: unknown): "profit" | "loss" | "flat" {
  const n = num(v);
  if (n === null || n === 0) return "flat";
  return n > 0 ? "profit" : "loss";
}

export function timeOf(epochSeconds: unknown): string {
  const n = num(epochSeconds);
  if (n === null || n === 0) return "—";
  return new Date(n * 1000).toLocaleTimeString("en-US", { hour12: false });
}

export function ageSeconds(epochSeconds: unknown): number | null {
  const n = num(epochSeconds);
  if (n === null || n === 0) return null;
  return Math.max(0, Date.now() / 1000 - n);
}

export function dur(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

export function str(v: unknown, fallback = "—"): string {
  if (v === null || v === undefined || v === "") return fallback;
  return String(v);
}
