"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { MarketsResponse } from "./types";

export interface MarketParams {
  page: number;
  pageSize: number;
  search: string;
  categories: string[];
  grades: string[];
  includeRisky: boolean;
  maxShares: number | null; // show markets whose min-shares requirement is at most this
  minHours: number | null; // show markets with at least this many hours of runway before resolution
  sort: string; // reward | min_shares | safety | hours_left | volume
  descending: boolean;
}

const REFRESH_MS = 60_000;

function buildQuery(p: MarketParams): string {
  const q = new URLSearchParams();
  q.set("page", String(p.page));
  q.set("page_size", String(p.pageSize));
  if (p.search) q.set("search", p.search);
  if (p.categories.length) q.set("category", p.categories.join(","));
  if (p.grades.length) q.set("grade", p.grades.join(","));
  if (p.includeRisky) q.set("include_risky", "true");
  if (p.maxShares != null) q.set("max_shares", String(p.maxShares));
  if (p.minHours != null) q.set("min_hours", String(p.minHours));
  q.set("sort", p.sort);
  q.set("descending", String(p.descending));
  return q.toString();
}

/** Fetch the materialized market index for the given filter/sort/page, auto-refreshing every 60s
 * (and faster while the first full index load is still warming). The list is size-independent, so
 * the bot's quote size is NOT a parameter here. */
export function useMarkets(params: MarketParams) {
  const [data, setData] = useState<MarketsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const qs = buildQuery(params);

  const load = useCallback(
    async (silent = false) => {
      if (!silent) setLoading(true);
      try {
        const res = await fetch(`/api/markets?${qs}`, { cache: "no-store" });
        const json = (await res.json()) as MarketsResponse;
        if (mounted.current) {
          setData(json);
          setError(json.error ?? null);
        }
      } catch {
        if (mounted.current) setError("market index unavailable");
      } finally {
        if (mounted.current) setLoading(false);
      }
    },
    [qs],
  );

  useEffect(() => {
    mounted.current = true;
    const first = setTimeout(() => load(), 0);
    const id = setInterval(() => load(true), REFRESH_MS);
    return () => {
      mounted.current = false;
      clearTimeout(first);
      clearInterval(id);
    };
  }, [load]);

  // While the first full index load is warming, poll faster so the table fills in soon.
  useEffect(() => {
    if (!data?.warming) return;
    const t = setTimeout(() => load(true), 8000);
    return () => clearTimeout(t);
  }, [data?.warming, load]);

  return { data, loading, error, refresh: () => load(true) };
}
