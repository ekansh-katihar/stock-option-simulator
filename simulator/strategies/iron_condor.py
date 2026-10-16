"""Four-leg templates and expiry risk; independent of UI and pricing source."""

from simulator.data.option_pricer import OptionRight
from simulator.engine.portfolio import LegAction


# A reverse condor can reuse the same roles with each BUY/SELL reversed.
CONDOR_ROLES = {
    "lower_put": (OptionRight.PUT, LegAction.BUY),
    "upper_put": (OptionRight.PUT, LegAction.SELL),
    "lower_call": (OptionRight.CALL, LegAction.SELL),
    "upper_call": (OptionRight.CALL, LegAction.BUY),
}


def validate_condor(contracts):
    if set(contracts) != set(CONDOR_ROLES):
        raise ValueError("Select all four condor legs.")
    ordered = [contracts[role] for role in CONDOR_ROLES]
    if any(c.right != CONDOR_ROLES[role][0] for role, c in contracts.items()):
        raise ValueError("Condor requires two puts and two calls.")
    if len({c.expiry for c in ordered}) != 1:
        raise ValueError("All four legs must have the same expiry.")
    if any(a.strike >= b.strike for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Strikes must increase: lower put < upper put < lower call < upper call.")


def expiry_risk(contracts, quantity, credit):
    """Dollar amounts; credit includes entry costs, excludes future close fees."""
    lp, sp, sc, lc = [contracts[r] for r in CONDOR_ROLES]
    multiplier = 100 * quantity
    width = max(sp.strike - lp.strike, lc.strike - sc.strike) * multiplier
    narrow_width = min(sp.strike - lp.strike, lc.strike - sc.strike) * multiplier
    # Individual rolls can turn the remaining position into a net debit.
    # There are no conventional two breakevens in that case.
    has_breakevens = 0 <= credit < narrow_width
    return {
        "entry_credit": credit,
        "max_profit": credit,
        "max_loss": width - credit,
        "lower_breakeven": sp.strike - credit / multiplier if has_breakevens else None,
        "upper_breakeven": sc.strike + credit / multiplier if has_breakevens else None,
        "expiry": lp.expiry,
    }
