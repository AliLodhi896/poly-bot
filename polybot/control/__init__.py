"""Control plane: launch/stop the live reward bot as a subprocess from the web UI.

This is orchestration only — it spawns the existing `reward-mm-live` CLI with chosen
parameters and a market whitelist (env). It never touches the trading/quoting/hedging logic.
"""
