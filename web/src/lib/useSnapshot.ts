"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { EMPTY_SNAPSHOT, type Snapshot } from "./types";
import { num } from "./format";

export type ConnState = "connecting" | "online" | "offline";

export interface SnapshotState {
  snapshot: Snapshot;
  conn: ConnState;
  lastUpdated: number | null;
  /** rolling history of a few scalar metrics, for sparklines */
  history: Record<string, number[]>;
  error: string | null;
}

const TRACKED_METRICS = [
  "estimated_reward_usdc",
  "reward_net_base_usdc",
  "reward_adjusted_net_pnl",
  "current_scoring_quotes",
  "current_open_quotes",
  "inventory_exposure",
  "unhedged_pnl",
] as const;

const MAX_POINTS = 60;

/**
 * Live dashboard data via short-interval polling of `/api/snapshot` (the bot's local
 * SQLite snapshot — no Polymarket load). Polling is used deliberately: short-lived
 * requests survive the Next dev proxy and never block the control daemon's graceful
 * shutdown (a long-lived stream would, which stalls Ctrl+C and the bot's order cancel).
 */
export function useSnapshot(intervalMs = 1500): SnapshotState {
  const [state, setState] = useState<SnapshotState>({
    snapshot: EMPTY_SNAPSHOT,
    conn: "connecting",
    lastUpdated: null,
    history: {},
    error: null,
  });
  const historyRef = useRef<Record<string, number[]>>({});

  const tick = useCallback(async (signal: AbortSignal) => {
    try {
      const res = await fetch("/api/snapshot", { signal, cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as Snapshot;

      const hist = historyRef.current;
      const push = (key: string, v: number | null) => {
        if (v === null) return;
        const arr = hist[key] ? [...hist[key], v] : [v];
        hist[key] = arr.length > MAX_POINTS ? arr.slice(-MAX_POINTS) : arr;
      };
      for (const key of TRACKED_METRICS) push(key, num(data.metrics?.[key]));
      const estimatedReward = num(data.metrics?.estimated_reward_usdc);
      const rewardNet =
        num(data.metrics?.reward_net_base_usdc) ?? num(data.metrics?.reward_adjusted_net_pnl);
      const realToday = num(data.real_rewards?.today_usdc);
      const tradingPnl =
        rewardNet !== null && estimatedReward !== null
          ? rewardNet - estimatedReward
          : realToday === null
            ? rewardNet
            : null;

      push("real_today", realToday);
      push("daily_pnl", realToday !== null ? realToday + (tradingPnl ?? 0) : rewardNet ?? estimatedReward);

      setState({
        snapshot: data,
        conn: "online",
        lastUpdated: Date.now(),
        history: { ...hist },
        error: null,
      });
    } catch (err) {
      if (signal.aborted) return;
      setState((prev) => ({
        ...prev,
        conn: "offline",
        error: err instanceof Error ? err.message : "request failed",
      }));
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;

    const loop = async () => {
      await tick(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(loop, intervalMs);
    };
    loop();

    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [tick, intervalMs]);

  return state;
}
