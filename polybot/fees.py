from __future__ import annotations


def taker_fee_per_share(price: float, fee_rate: float) -> float:
    """Return the Polymarket taker fee for one share executed at ``price``.

    The fee is symmetric around 0.5 — it is proportional to ``price * (1 - price)`` — so it
    costs most at even odds and vanishes toward either end of the book. ``fee_rate`` comes
    from the market's own CLOB metadata, not from configuration.
    """
    if fee_rate <= 0:
        return 0.0
    return fee_rate * price * (1.0 - price)
