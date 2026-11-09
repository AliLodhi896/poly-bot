"use client";

import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { useSnapshot, type SnapshotState } from "@/lib/useSnapshot";
import { StatusBar } from "@/components/StatusBar";
import { StartupLoader } from "@/components/screens/StartupLoader";
import { Section } from "@/components/Section";
import { DataTable, type Column } from "@/components/DataTable";
import { QuestionCell } from "@/components/QuestionCell";
import { AmbientVisuals } from "@/components/AmbientVisuals";
import { AnimatedDashboardCard } from "@/components/ui/AnimatedDashboardCard";
import { Pnl, Side, Yes } from "@/components/Value";
import { useControl, type ControlState } from "@/lib/useControl";
import { LaunchScreen } from "@/components/screens/LaunchScreen";
import { NeonButton } from "@/components/ui/NeonButton";
import { fmt, money, num, price, signedMoney, str } from "@/lib/format";

const quoteCols: Column[] = [
  { key: "q", header: "Market", render: (r) => <QuestionCell q={r.question} /> },
  {
    key: "out",
    header: "Outcome",
    render: (r) => <span className="text-muted">{str(r.outcome)}</span>,
  },
  {
    key: "quote",
    header: "Quote",
    render: (r) => (
      <span className="flex items-center gap-1.5">
        <Side side={r.side} />
        <span className="tnum">{price(r.price)}</span>
        <span className="text-faint">×</span>
        <span className="tnum text-muted">{fmt(r.size)}</span>
      </span>
    ),
  },
  {
    key: "state",
    header: "State",
    render: (r) => <span className="text-muted">{str(r.lifecycle_state ?? r.status)}</span>,
  },
  { key: "mid", header: "Mid", align: "right", render: (r) => price(r.midpoint) },
  {
    key: "dist",
    header: "Dist / Max",
    align: "right",
    render: (r) => (
      <span className="text-muted">
        {price(r.distance_from_midpoint)} / {price(r.max_spread)}
      </span>
    ),
  },
  {
    key: "sc",
    header: "Scoring",
    align: "right",
    render: (r) => <Yes on={r.locally_scoring} />,
  },
  {
    key: "age",
    header: "Age",
    align: "right",
    render: (r) => <span className="text-faint">{fmt(r.age_seconds)}s</span>,
  },
];

const marketCols: Column[] = [
  { key: "q", header: "Market", render: (r) => <QuestionCell q={r.question} /> },
  {
    key: "rate",
    header: "Rate / Day",
    align: "right",
    render: (r) => <span className="text-gold">{money(r.rewards_rate_per_day, 2)}</span>,
  },
  { key: "min", header: "Min Size", align: "right", render: (r) => fmt(r.rewards_min_size) },
  { key: "spread", header: "Max Spr", align: "right", render: (r) => price(r.rewards_max_spread) },
  { key: "tick", header: "Tick", align: "right", render: (r) => price(r.tick_size) },
  {
    key: "quotes",
    header: "Score / Open",
    align: "right",
    render: (r) => (
      <span>
        <span className="text-profit">{fmt(r.scoring_quotes)}</span>
        <span className="text-faint"> / </span>
        <span className="text-muted">{fmt(r.open_quotes)}</span>
      </span>
    ),
  },
  { key: "vol", header: "Vol 24h", align: "right", render: (r) => money(r.volume_24hr, 0) },
];

const inventoryCols: Column[] = [
  { key: "q", header: "Market", render: (r) => <QuestionCell q={r.question} /> },
  {
    key: "outcome",
    header: "Outcome",
    render: (r) => <span className="text-muted">{str(r.outcome)}</span>,
  },
  { key: "state", header: "State", render: (r) => <span className="text-muted">{str(r.state)}</span> },
  { key: "shares", header: "Shares", align: "right", render: (r) => fmt(r.shares, 2) },
  { key: "avg", header: "Avg", align: "right", render: (r) => price(r.avg_price) },
  { key: "mark", header: "Mark", align: "right", render: (r) => price(r.mark_price) },
  {
    key: "risk",
    header: "Risk PnL",
    align: "right",
    render: (r) => (
      <Pnl value={r.risk_pnl ?? r.liquidation_pnl} render={(v) => signedMoney(v)} />
    ),
  },
  {
    key: "stop",
    header: "To Stop",
    align: "right",
    render: (r) => <Pnl value={r.loss_to_stop} render={(v) => signedMoney(v)} />,
  },
];

function DashboardView({ control, snap }: { control: ControlState; snap: SnapshotState }) {
  const { snapshot, conn } = snap;
  const { run, metrics } = snapshot;
  // The trading mode comes from the control plane, never from data availability: real-reward
  // figures can lag a fresh live run by minutes, and that must not relabel it "paper".
  const mode: "paper" | "live" = control.status?.mode === "live" ? "live" : "paper";

  const stateErrors = snapshot.state_errors.length;
  const emergencyExits = num(metrics.emergency_exits) ?? 0;
  const estimatedReward = num(metrics.estimated_reward_usdc);
  const rewardNet =
    num(metrics.reward_net_base_usdc) ?? num(metrics.reward_adjusted_net_pnl);

  const rr = snapshot.real_rewards;
  // Real-reward figures are account-wide (the wallet's payouts today), so they label the card
  // only on a live run: a paper run on a machine with live credentials must stay "paper model".
  const isReal = mode === "live" && !!(rr?.enabled && rr.today_usdc != null);
  const realRewardToday = isReal ? rr!.today_usdc : null;
  const tradingPnl =
    rewardNet !== null && estimatedReward !== null
      ? rewardNet - estimatedReward
      : isReal
        ? null
        : rewardNet;
  const dailyPnl =
    isReal && realRewardToday !== null
      ? realRewardToday + (tradingPnl ?? 0)
      : rewardNet ?? estimatedReward;
  const balance = num(rr?.balance_usdc);

  const pnlTone =
    (dailyPnl ?? 0) > 0 ? "text-profit" : (dailyPnl ?? 0) < 0 ? "text-loss" : "text-foreground";

  return (
    <div className="relative min-h-full overflow-hidden">
      <AmbientVisuals />

      <StatusBar run={run} conn={conn} mode={mode} />

      <div className="relative z-10 mx-auto mt-3 flex max-w-[1440px] items-center justify-end px-3 sm:px-5 lg:px-6">
        <NeonButton variant="ghost" size="sm" onClick={control.stop} disabled={control.busy}>
          {control.busy ? "Stopping…" : "Stop bot"}
        </NeonButton>
      </div>

      <main className="relative z-10 mx-auto max-w-[1440px] space-y-5 px-3 pb-20 pt-4 sm:px-5 lg:px-6">
        {(stateErrors > 0 || emergencyExits > 0) && (
          <motion.div
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            className="flex flex-wrap items-center gap-3 rounded-lg border border-loss/35 bg-loss/10 px-4 py-2.5 text-[13px] text-loss"
          >
            <span className="h-2 w-2 rounded-full bg-loss live-dot" />
            {stateErrors > 0 && <span>{stateErrors} inventory state error(s)</span>}
            {emergencyExits > 0 && (
              <span>· {emergencyExits} emergency exit(s) — manual action may be needed</span>
            )}
          </motion.div>
        )}

        <AnimatedDashboardCard
          primaryLabel="Daily PnL"
          primaryValue={dailyPnl}
          primaryFormat={(n) => signedMoney(n)}
          primaryTone={pnlTone}
          secondaryLabel="Account balance"
          secondaryValue={balance}
          secondaryFormat={(n) => money(n, 2)}
          badge={isReal ? "real rewards" : mode === "live" ? "live · estimates" : "paper model"}
        />

        <Section title="Open Reward Quotes" count={snapshot.quotes.length}>
          <DataTable
            columns={quoteCols}
            rows={snapshot.quotes}
            rowKey={(r, i) => str(r.quote_uid, `q${i}`)}
            empty="No resting quotes right now"
            maxHeight={430}
          />
        </Section>

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(340px,0.85fr)]">
          <Section title="Reward Markets" count={snapshot.markets.length}>
            <DataTable
              columns={marketCols}
              rows={snapshot.markets}
              rowKey={(r, i) => str(r.condition_id, `m${i}`)}
              empty="No markets selected"
              maxHeight={360}
            />
          </Section>

          <Section title="Open Inventory" count={snapshot.inventory.length}>
            <DataTable
              columns={inventoryCols}
              rows={snapshot.inventory}
              rowKey={(r, i) => str(r.position_uid, `i${i}`)}
              empty="No open inventory"
              maxHeight={360}
            />
          </Section>
        </div>
      </main>
    </div>
  );
}

// While the bot is running, hold on the startup loader until the snapshot has real data
// (a live run with markets selected) — or a grace timeout — then reveal the dashboard.
function RunningArea({ control }: { control: ControlState }) {
  const snap = useSnapshot();
  const [graceElapsed, setGraceElapsed] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setGraceElapsed(true), 30000);
    return () => clearTimeout(t);
  }, []);

  const run = snap.snapshot.run;
  const marketsWatched = num(snap.snapshot.metrics.markets_watched) ?? 0;
  const dataReady = run?.status === "running" && marketsWatched > 0;

  if (!dataReady && !graceElapsed) {
    return <StartupLoader />;
  }
  return <DashboardView control={control} snap={snap} />;
}

export default function Page() {
  const control = useControl();

  if (!control.loaded) {
    return (
      <div className="flex min-h-screen items-center justify-center text-[13px] text-faint">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-gold" />
        <span className="ml-2">Connecting…</span>
      </div>
    );
  }
  return control.status?.running ? (
    <RunningArea control={control} />
  ) : (
    <LaunchScreen control={control} />
  );
}
