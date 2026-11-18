"""Estimate a lot's True Value (TV) from its sample, using the market formula.

The formula and its thresholds come from GET /v1/calendar -> "rules".
"""
from collections.abc import Callable


# Assumed sample error in percentage points, matching the RFQ safety margins.
# These are starting estimates for appraisal, not measured error distributions.
DEFECT_UNCERTAINTY_PP = 0.4
MOISTURE_UNCERTAINTY_PP = 0.2


def grade(rules: dict, defect_pct: float) -> tuple[str, float]:
    for step in rules["defect_steps"]:
        if defect_pct <= step["max_defect_pct"]:
            return step["grade"], step["step"]
    last = rules["defect_steps"][-1]
    return last["grade"], last["step"]


def moisture_factor(rules: dict, m: float) -> float:
    mo = rules["moisture"]
    if m <= mo["ref"]:
        return 1.0
    if m <= mo["hard_limit"]:
        return (1 - m / 100) / (1 - mo["ref"] / 100) - mo["penalty_per_pp"] * (m - mo["ref"])
    return mo["hard_factor"]


def screen_factor(rules: dict, screen: int) -> float:
    factors = {int(k): v for k, v in rules["screen_factors"].items()}
    codes = sorted(factors)
    return factors[min(max(int(screen), codes[0]), codes[-1])]


def grade_factor(rules: dict, defect_pct: float) -> float:
    return (1 - defect_pct / 100) * grade(rules, defect_pct)[1]


def true_value(rules: dict, sample: dict) -> float:
    """Market TV formula for a specific quality, in baht per kg."""
    d, m = sample["defect_pct"], sample["moisture_pct"]
    return (rules["p0"] * grade_factor(rules, d) * moisture_factor(rules, m)
            * screen_factor(rules, sample["screen"]))


def defect_cutoffs(rules: dict) -> list[float]:
    # The last grade already covers everything above its listed maximum.
    return [step["max_defect_pct"] for step in rules["defect_steps"][:-1]]


def mean_factor(reading: float, margin: float, cutoffs: list[float], factor: Callable[[float], float]) -> float:
    lo, hi = max(0.0, reading - margin), min(100.0, reading + margin)
    inside = sorted({cutoff for cutoff in cutoffs if lo < cutoff < hi})
    if not inside:
        return factor(reading)
    # Uniform error within the interval. Each piece is linear, so evaluating
    # its midpoint gives the exact mean for that piece.
    edges = [lo, *inside, hi]
    return sum((b - a) * factor((a + b) / 2) for a, b in zip(edges, edges[1:])) / (hi - lo)


def estimated_value(rules: dict, sample: dict) -> float:
    """Blend the formula across grade and moisture boundaries when a sample could cross them."""
    moisture = rules["moisture"]
    g = mean_factor(sample["defect_pct"], DEFECT_UNCERTAINTY_PP, defect_cutoffs(rules),
                    lambda d: grade_factor(rules, d))
    m = mean_factor(sample["moisture_pct"], MOISTURE_UNCERTAINTY_PP,
                    [moisture["ref"], moisture["hard_limit"]], lambda m: moisture_factor(rules, m))
    return rules["p0"] * g * m * screen_factor(rules, sample["screen"])


def estimate_lots(rules: dict, lots: list[dict]) -> dict[str, float]:
    return {lot["lot_id"]: round(estimated_value(rules, lot["sample"]), 2) for lot in lots}


def appraisal_items(rules: dict, lots: list[dict], tv: dict[str, float]) -> list[dict]:
    items = []
    cutoffs = defect_cutoffs(rules)
    for lot in lots:
        lot_id, defect = lot["lot_id"], lot["sample"]["defect_pct"]
        item = {"lot_id": lot_id, "est_value_per_kg": tv[lot_id]}
        if not any(defect - DEFECT_UNCERTAINTY_PP < cutoff < defect + DEFECT_UNCERTAINTY_PP
                   for cutoff in cutoffs):
            item["grade"] = grade(rules, defect)[0]
        items.append(item)
    return items
