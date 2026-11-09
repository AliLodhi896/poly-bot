"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ControlStatus, LaunchConfig } from "./types";

export interface ControlState {
  status: ControlStatus | null;
  loaded: boolean;
  busy: boolean;
  error: string | null;
  start: (config: LaunchConfig) => Promise<boolean>;
  stop: () => Promise<void>;
  refresh: () => Promise<void>;
}

export function useControl(intervalMs = 3000): ControlState {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/control/status", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as ControlStatus;
      if (mounted.current) {
        setStatus(data);
        setLoaded(true);
      }
    } catch {
      if (mounted.current) setLoaded(true); // control API absent → treat as idle
    }
  }, []);

  const start = useCallback(
    async (config: LaunchConfig) => {
      setBusy(true);
      setError(null);
      try {
        const res = await fetch("/api/control/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(config),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        setStatus(data as ControlStatus);
        return true;
      } catch (err) {
        setError(err instanceof Error ? err.message : "failed to start");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const stop = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/control/stop", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.ok) setStatus(data as ControlStatus);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to stop");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const first = setTimeout(refresh, 0);
    const id = setInterval(refresh, intervalMs);
    return () => {
      mounted.current = false;
      clearTimeout(first);
      clearInterval(id);
    };
  }, [refresh, intervalMs]);

  return { status, loaded, busy, error, start, stop, refresh };
}
