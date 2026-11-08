"""Order repricing. EVE only accepts order prices with at most 4 significant digits (and at
least 0.01 ISK resolution), so "competitor minus 0.01" is usually rejected. These helpers find
the closest valid price that beats a competitor."""
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

SIG_DIGITS = 4
MIN_TICK = Decimal("0.01")


def tick(price: Decimal) -> Decimal:
    """Smallest allowed price step at this price's magnitude."""
    exponent = price.adjusted()  # floor(log10(price))
    return max(MIN_TICK, Decimal(1).scaleb(exponent - SIG_DIGITS + 1))


def is_valid(price) -> bool:
    p = Decimal(str(price))
    return p > 0 and p % tick(p) == 0


def _snap(p: Decimal, rounding) -> Decimal:
    t = tick(p)
    return (p / t).to_integral_value(rounding=rounding) * t


def beat_sell(competitor) -> float:
    """Highest valid price strictly below a competing sell order."""
    c = Decimal(str(competitor))
    candidate = _snap(c, ROUND_FLOOR)
    if candidate >= c:
        candidate = c - tick(c)
    # Crossing below a power of ten makes the steps finer (1,000 -> 999.9), so use the finer step.
    if candidate > 0 and tick(candidate) < tick(c):
        candidate = _snap(c - tick(candidate), ROUND_FLOOR)
    return float(max(candidate, MIN_TICK))


def beat_buy(competitor) -> float:
    """Lowest valid price strictly above a competing buy order."""
    c = Decimal(str(competitor))
    candidate = _snap(c, ROUND_CEILING)
    if candidate <= c:
        candidate = c + tick(c)
    return float(_snap(candidate, ROUND_CEILING))


def suggestion(order) -> float | None:
    """Price to set so this order is the best again, or None if it isn't undercut."""
    if not order["undercut"] or order["competitor_price"] is None:
        return None
    return beat_buy(order["competitor_price"]) if order["is_buy"] else beat_sell(order["competitor_price"])
