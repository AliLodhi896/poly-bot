import asyncio

import pytest

from polybot.api.geoblock import GeoblockResult
from polybot.config import Settings
from polybot.execution.live import Geoblocked, LiveExecutor, UnsafeOrder


def test_config_defaults_are_paper_only() -> None:
    settings = Settings(_env_file=None)
    assert settings.live_enabled is False
    assert settings.is_paper_only is True
    assert settings.database_path.name == "polybot.sqlite3"


def test_config_accepts_csv_lists_from_dotenv(tmp_path) -> None:
    """CSV settings are stored as plain strings and split by their reader properties."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "POLYBOT_REWARD_MM_EXCLUDE_KEYWORDS=esports, dota ,\n"
        "POLYBOT_GEOBLOCK_BLOCKED_COUNTRIES=us,gb\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_path)
    assert settings.reward_mm_exclude_keyword_set == {"esports", "dota"}
    assert settings.geoblock_blocked_country_set == {"US", "GB"}


def test_post_only_live_gate() -> None:
    executor = LiveExecutor(Settings(_env_file=None))
    executor.validate_maker_order("GTC", True)
    with pytest.raises(UnsafeOrder):
        executor.validate_maker_order("FOK", True)
    with pytest.raises(UnsafeOrder):
        executor.validate_maker_order("GTC", False)


def test_geoblock_live_gate_blocks_execution() -> None:
    async def blocked() -> GeoblockResult:
        return GeoblockResult(blocked=True, country="US", region="NY")

    settings = Settings(
        _env_file=None,
        live_enabled=True,
        wallet_private_key="not-a-real-key",
        wallet_funder_address="0x0000000000000000000000000000000000000000",
    )
    with pytest.raises(Geoblocked):
        asyncio.run(LiveExecutor(settings, geoblock_check=blocked).ensure_live_allowed())
