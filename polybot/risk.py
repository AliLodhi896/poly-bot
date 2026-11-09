from __future__ import annotations

from polybot.config import Settings


class RiskEngine:
    """The hard stop that sits outside the strategy: the kill switch.

    Position and exposure limits are not here. They are the operator's per-run choices —
    which markets, what position size, what max loss — set on the launch screen and passed
    into the engine. A second, invisible set of aggregate caps would only mean the bot
    quietly does something other than what was asked.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def kill_switch_active(self) -> bool:
        return self.settings.kill_switch or self.settings.kill_switch_file.exists()
