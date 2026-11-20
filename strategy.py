"""Decide this round's offers.  <- ไฟล์หลักที่ทีมควรแก้

Every round:
  1. rfq_markups()  a price (markup x our TV) for each RFQ's buyer, learned from how buyers behave
  2. candidates()   (lot, RFQ) pairs allowed by the sales policy below
  3. llm_plan()     the LLM picks offers from those candidates (optional, gateway only in the competition)
  4. rule_plan()    used instead when the LLM is off, too slow, or answers nonsense
  5. guard()        every offer, from the LLM or the rules, is checked before it is sent

Sales policy
  - Buyer priority: SPECIALTY > COMMERCIAL > INSTANT, while covering every compatible RFQ when capacity allows.
  - Round 1, COMMERCIAL: ask a little above TV, kept under the ceiling that past games suggest.
  - Later rounds: each buyer's price moves with what happened last round (our results and how the other
    sellers fared in market_summary), so the beans sell.
  - INSTANT starts at TV (markup 1.00), steps down by 0.02 after OUTBID, and respects the buyer's known average paid price.
  - Offer about 70 % of available stock through the first half, release the rest in the second half.

price_per_kg = markup × our TV estimate. A markup of 1.00 asks exactly what we think the lot is worth.
"""
import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field

BAG = 60
# Spec margin: 0 = follow the market's spec exactly (a lot whose sample passes the RFQ limit is offered).
# (was 0.4 / 0.2: a 6.8 % defect lot never went to a ≤7 % COMMERCIAL buyer and ended in salvage.)
# A wrong guess only costs SPEC_MISMATCH once: spec_mismatched() then skips that spec for the lot.
SPEC_MARGIN_DEFECT = 0.0              # percentage points of defects
SPEC_MARGIN_MOISTURE = 0.0            # percentage points of moisture
MIN_MARKUP, MAX_MARKUP = 0.85, 1.20   # unsold beans go to salvage at 0.80 × TV, so never go near that
LLM_MIN_MARKUP = 0.89                 # keep LLM discretionary discounts above 89% of our TV estimate

TYPE_RANK = {"SPECIALTY": 0, "COMMERCIAL": 1, "INSTANT": 2}
PREMIUM_TYPES = ("SPECIALTY", "COMMERCIAL")
# Where each buyer type starts in round 1, and how far later rounds may move it.
# competition 8001: SPECIALTY winners sold at x0.95-1.04 (avg ~x0.98), COMMERCIAL x0.94-1.07: start near those
BASE_MARKUP = {"SPECIALTY": 1.07, "COMMERCIAL": 1.03, "INSTANT": 1.00}
SPECIALTY_OVER_COMMERCIAL = 0.03      # SPECIALTY asks 3 % more than the best COMMERCIAL price, until it OUTBIDs us twice
BOUNDS = {"SPECIALTY": (1.00, 1.20), "COMMERCIAL": (0.98, 1.12), "INSTANT": (0.90, 1.00)}
STEP_REPEAT_OUTBID = -0.01            # SPECIALTY/COMMERCIAL: OUTBID 2+ rounds in a row with no sale -> 1 % lower per round
REPEAT_OUTBID_ROUNDS = 2
URGENCY_NO_SOLD_FLOOR = 0.5           # above this urgency the "it just sold there" floor no longer holds the price up
# INSTANT buyers cap in BAHT/kg, not as a share of TV: a high-TV lot at x0.94 (113.7 ฿) was ABOVE_LIMIT while the
# market's top INSTANT sale stayed ~110-120 ฿. Per lot: price <= baht cap (from ABOVE_LIMIT and market top sales).
INSTANT_BAHT_MARGIN = 0.015           # ask 1.5 % under the lowest baht price this buyer rejected as ABOVE_LIMIT
INSTANT_MIN_MARKUP = 0.82             # a baht cap may push a lot down to 0.82 x TV (salvage is 0.80); below that skip
FIRST_ROUND_COMMERCIAL_FLOOR = 1.01   # round 1: always ask above TV from COMMERCIAL buyers
FINAL_ROUND_OVER_BEST_SOLD = 0.02     # final round: at most 2 % above the best markup that sold this game
HISTORY_WEIGHT = 0.3                  # past games are a behaviour hint, not a rule

# How a buyer's markup moves after a round (in markup units, 0.01 = 1 % of TV)
STEP_ACCEPTED_SPARE_DEMAND = 0.012    # sold everything and the buyer still wanted more -> ask more
STEP_ACCEPTED = 0.006                 # sold everything, buyer was full -> nudge up
# OUTBID / PARTIAL: do NOT lower the price automatically. OUTBID affects price learning, not early offer volume.
STEP_PARTIAL = 0.0
STEP_OUTBID = 0.0
STEP_INSTANT_OUTBID = -0.02          # lower INSTANT by 2% after an OUTBID, then recheck against its paid-price floor
# Sell-out mode. The Round Capacity (≈1.5 × an even share per round) is a hard market limit, so to sell every bag
# the offers must SELL more often when stock is behind schedule. need = bags left ÷ (capacity bags × rounds left)
# is the fill rate still required; above URGENT_FILL we are behind: urgency 0..1 grows with it.
URGENT_FILL = 0.5
# Lots that few buyers accept (narrow spec fit, or the buyer that takes them is often absent) get fewer chances
# to sell: offer all of them whenever an outlet is here, give them capacity and RFQ room first.
# Competitors' sold prices (market_summary) converted to markups of our comparable lots: see market_view()
MARKET_RECENT = 2                     # look at the last 2 rounds with sales for each buyer
MARKET_MIN_ROUNDS = 1                 # need at least this many rounds of data before using it for prices
MARKET_SANE_GAP = 0.05                # competitors' average is used as a floor only within 5 % of what the buyer paid us
MARKET_HEADROOM = 0.02                # never ask more than 2 % above the highest price that sold with that buyer
MAX_RFQ_SHARE = 0.4                  # one RFQ gets at most ~40 % of the round's bags (spread the risk of OUTBID)
SPREAD_DECAY = 0.7                    # each bag we already put on an RFQ makes it 30 % less attractive for the next lot
NARROW_MAX_SHARE = 0.6                # a narrow lot takes at most 60 % of the round's capacity
NARROW_OUTLET = 0.8                   # chance per round that some buyer for the lot is present, below = narrow
PRESENCE_PRIOR_N = 2.0                # past games count as 2 rounds of evidence about a buyer's presence
SOLD_FLOOR_GAP = 0.01                 # stay within 1 % of the best markup that sold to that buyer type this game
FILL_PRIOR_BAGS = 4.0                 # early in a game, assume a 50 % fill worth 4 bags of evidence
URGENCY_MAX_CUT = 0.06                # at full urgency ask up to 6 % less (still never below the type floor)
FINAL_UNDER_LOST = 0.01               # final round only: go 1 % under the price we lost at (salvage pays 0.80)
STEP_ABOVE_LIMIT = -0.025             # over the ceiling of ANOTHER buyer of this type (applied at half strength)
ABOVE_LIMIT_MARGIN = 0.015            # this buyer said ABOVE_LIMIT at x: next time ask at most x − 1.5 %
# SPECIALTY often appears only in a few festival rounds: find its ceiling faster (bigger step down)
ABOVE_LIMIT_MARGIN_BY_TYPE = {"SPECIALTY": 0.025}
STEP_TIGHT_MARKET = 0.0               # (was −0.008) no price cut on indirect hints; ABOVE_LIMIT handles ceilings
SPILLOVER = 0.5                       # lessons from one buyer apply at half strength to the same type
# Do not undercut too far: OUTBID/PARTIAL cuts stop this far below the round-1 start (ABOVE_LIMIT still caps)
MAX_CUT = {"SPECIALTY": 0.08, "COMMERCIAL": 0.04, "INSTANT": 0.10}

# Competition by round: other sellers run out of beans late in the game, so fewer offers per RFQ and buyers
# left short. markup += COMPETITION_K × (1 − expected offers per RFQ this round / usual offers per RFQ)
COMPETITION_K = 0.08
COMPETITION_ADJ = (0.0, 0.03)         # crowded rounds: no cut (hold volume instead); quiet rounds add up to 3 %
SCARCE_FILL = 0.7                     # buyers expected to get < 70 % of what they want -> +SCARCE_BONUS
SCARCE_BONUS = 0.01

# Chance to sell per buyer (see win_chances): prior strength, how much past games count, and the smallest
# chance worth splitting a lot onto as a second RFQ
WIN_PRIOR = 2.0
WIN_HISTORY_WEIGHT = 0.3
WIN_HISTORY_MAX_N = 3.0
MIN_SPLIT_P_WIN = 0.2

OFFER_SHARE = 0.7                     # offer 70 % of each lot's available bags per round
# Market Calendar festival (more SPECIALTY demand later, e.g. R6–R8): lots passing a SPECIALTY spec keep
# 1 bag per festival round for it, never more than half the lot, and only while we are on schedule
FESTIVAL_HOLD_PER_ROUND = 1
FESTIVAL_HOLD_MAX_SHARE = 0.5
# A lot whose sample fails every spec: still try INSTANT at 0.82 × TV. Buyers check the TRUE quality, not the
# noisy sample, so it may pass; if not we only get SPEC_MISMATCH and keep the beans (salvage pays 0.80 anyway).
LONGSHOT_MARKUP = 0.89              # do not dump uncertain/no-spec lots at 0.82 x an appraisal that may be wrong


@dataclass
class Context:
    rules: dict                       # GET /v1/calendar -> rules
    calendar: list                    # GET /v1/calendar -> calendar
    tv: dict                          # lot_id -> our TV estimate (baht/kg)
    rng: random.Random
    memory: object = None
    llm: object = None
    seconds_left: float = 999         # time left in this round when the state was read
    notes: list = field(default_factory=list)
    markups: dict | None = None       # rfq_id -> markup the rules chose this round
    comp: dict | None = None          # buyer_type -> competition outlook for this round
    wins: dict | None = None          # rfq_id -> chance an offer to it sells
    commercial_ref: float | None = None   # best COMMERCIAL markup this round (SPECIALTY is priced above it)
    demoted: set = field(default_factory=set)   # SPECIALTY buyers whose ceiling proved no better than COMMERCIAL
    urg: float | None = None          # sell-out urgency 0..1 for this round
    outlets: dict | None = None       # lot_id -> chance per round that a buyer for it is present
    market: dict | None = None        # buyer -> competitors' sold markups (market_view)
    llm_views: dict = field(default_factory=dict)   # buyer -> the LLM's short Thai note
    lot_target: dict | None = None    # lot_id -> bags the rules give it this round (caps the LLM too)
    baht_caps: dict | None = None     # INSTANT buyer -> highest baht/kg worth asking this round (instant_baht_caps)
    llm_io: dict | None = None        # prompt + answer of this round's LLM call (logged to logs/llm.jsonl)


def spec_ok(sample: dict, spec: dict) -> bool:
    """Does the lot's sample pass this spec, as the market states it (SPEC_MARGIN_* = 0)? Buyers check the TRUE
    quality, so a sample near the limit may still get SPEC_MISMATCH; spec_mismatched() stops repeating that."""
    return (sample["defect_pct"] <= spec["max_defect_pct"] - SPEC_MARGIN_DEFECT
            and sample["moisture_pct"] <= spec["max_moisture_pct"] - SPEC_MARGIN_MOISTURE
            and (spec["min_screen"] is None or sample["screen"] >= spec["min_screen"]))


def rounds_left(state: dict, ctx: Context) -> int:
    rnd = state["round"]
    total = rnd.get("of") or ctx.rules["timeline"]["rounds"]
    return total if rnd["kind"] == "practice" else total - rnd["number"] + 1


def round_weight(ctx: Context, n: int) -> float:
    """How much selling round n is worth, from the Market Calendar's demand_weight (1.0 = normal).
    Capped at 1.0: a busy round (festival) still only takes one Round Capacity of our bags, but a quiet round
    (near close, final) sells fewer of them, so it counts as less than a full round."""
    for seg in ctx.calendar or []:
        if n in _calendar_rounds(seg):
            w = seg.get("demand_weight")
            return min(1.0, max(0.1, float(w))) if isinstance(w, (int, float)) else 1.0
    return 1.0


def demand_rounds(state: dict, ctx: Context) -> tuple[float, float]:
    """(selling rounds left including this one, selling rounds in the whole game), each weighted by round_weight.
    Plain round counts when the calendar gives no demand_weight."""
    rnd = state["round"]
    total = rnd.get("of") or ctx.rules["timeline"]["rounds"]
    whole = sum(round_weight(ctx, n) for n in range(1, total + 1))
    if rnd["kind"] == "practice":
        return whole, whole
    return sum(round_weight(ctx, n) for n in range(rnd["number"], total + 1)), whole


def is_final_round(state: dict, ctx: Context) -> bool:
    return state["round"]["kind"] != "practice" and rounds_left(state, ctx) <= 1


def is_first_round(state: dict) -> bool:
    return state["round"]["kind"] != "practice" and state["round"]["number"] == 1


# ---------------------------------------------------------------- pricing

def history_markup(memory: object, buyer: str, buyer_type: str) -> float:
    """Round-1 starting markup: the type default, nudged by how this buyer behaved in past games."""
    base = BASE_MARKUP.get(buyer_type, 1.0)
    if buyer_type == "INSTANT":
        return base                   # INSTANT: start at TV, then step down until it sells
    behaviour = memory.buyer_behavior(buyer) if memory is not None and hasattr(memory, "buyer_behavior") else {}
    accepted, above = behaviour.get("accepted_median"), behaviour.get("above_limit_median")
    estimate = None
    if accepted is not None and above is not None:
        estimate = min(accepted + 0.01, above - 0.01)
    elif accepted is not None:
        estimate = accepted + 0.01
    elif above is not None:
        estimate = above - 0.02
    if estimate is None:
        estimate = base
    else:
        estimate = (1 - HISTORY_WEIGHT) * base + HISTORY_WEIGHT * estimate

    # A buyer's ABOVE_LIMIT result is an explicit ceiling signal. Keep this cap
    # across games too, so a restart/new game does not repeat a price already
    # rejected by this exact buyer. Use the lowest rejected markup as the
    # conservative bound; current-game evidence is enforced again in buyer_markup.
    history = getattr(memory, "buyer_history", {}).get(buyer, {}) if memory is not None else {}
    rejected = [o.get("markup") for o in history.get("observations", [])
                if o.get("status") == "ABOVE_LIMIT" and isinstance(o.get("markup"), (int, float))]
    if rejected:
        margin = ABOVE_LIMIT_MARGIN_BY_TYPE.get(buyer_type, ABOVE_LIMIT_MARGIN)
        estimate = min(estimate, min(rejected) - margin)
    return estimate


def _round_number(round_id) -> int | None:
    try:
        return int(str(round_id).lstrip("Rr"))
    except ValueError:
        return None


def _round_lessons(memory: object) -> list[dict]:
    """Per finished round of this game: {buyer: [(status, our markup)]} plus market demand per buyer."""
    out = []
    for result in getattr(memory, "rounds", []):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(memory, "sent", {}).get(result.get("round_id"), [])}
        ours: dict[str, list] = {}
        types: dict[str, str] = {}
        for offer in result.get("my_offers", []):
            s = sent.get((offer.get("rfq_id"), offer.get("lot_id")))
            if not s or not isinstance(s.get("markup"), (int, float)) or s.get("spec_longshot"):
                continue
            ours.setdefault(s["buyer"], []).append((offer.get("status", ""), s["markup"]))
            types[s["buyer"]] = s.get("buyer_type")
        demand: dict[str, list] = {}               # buyer -> [wanted kg, bought kg, offers, rfqs]
        for row in result.get("market_summary", []):
            if row.get("buyer"):
                d = demand.setdefault(row["buyer"], [0, 0, 0, 0])
                d[0] += row.get("wanted_kg", 0) or 0
                d[1] += row.get("bought_kg", 0) or 0
                d[2] += row.get("offers", 0) or 0
                d[3] += 1
                types.setdefault(row["buyer"], row.get("buyer_type"))
        out.append({"ours": ours, "demand": demand, "types": types})
    return out


def competition_outlook(state: dict, ctx: Context) -> dict[str, dict]:
    """How crowded this round should be for each buyer type, from other sellers' behaviour.

    History (all games): offers per RFQ and how much buyers got, by round number. Late rounds usually have
    fewer offers (sellers run out) and buyers left short -> we can ask more. This game's last round tells us
    whether today's market is busier or quieter than usual (trend)."""
    if ctx.comp is not None:
        return ctx.comp
    memory = ctx.memory
    hist: dict[str, dict[int, list]] = {}         # type -> round no. -> [offers, rfqs, wanted, bought]
    for profile in getattr(memory, "buyer_history", {}).values():
        t = profile.get("buyer_type")
        for row in profile.get("market_rounds", []):
            n = _round_number(row.get("round_id"))
            if t and n:
                h = hist.setdefault(t, {}).setdefault(n, [0, 0, 0, 0])
                h[0] += row.get("offers", 0) or 0
                h[1] += 1
                h[2] += row.get("wanted_kg", 0) or 0
                h[3] += row.get("bought_kg", 0) or 0
    live: dict[str, dict[int, list]] = {}         # this game: type -> round no. -> [offers, rfqs]
    for result in getattr(memory, "rounds", []):
        n = _round_number(result.get("round_id"))
        for row in result.get("market_summary", []):
            if n and row.get("buyer_type"):
                v = live.setdefault(row["buyer_type"], {}).setdefault(n, [0, 0])
                v[0] += row.get("offers", 0) or 0
                v[1] += 1

    n_now = state["round"]["number"]
    out = {}
    for t in {r["buyer_type"] for r in state["rfqs"]}:
        by = hist.get(t, {})
        offers, rfqs = sum(v[0] for v in by.values()), sum(v[1] for v in by.values())
        if rfqs < 5:
            out[t] = {"adj": 0.0, "baseline": None, "expected": None, "fill": None}
            continue
        baseline = offers / rfqs
        now = by.get(n_now)
        expected = now[0] / now[1] if now and now[1] else baseline
        fill = now[3] / now[2] if now and now[2] else None
        trend = 1.0
        if live.get(t):
            last = max(live[t])
            lv = live[t][last][0] / max(1, live[t][last][1])
            ref = by.get(last)
            hv = ref[0] / ref[1] if ref and ref[1] else baseline
            trend = min(1.4, max(0.7, lv / hv)) if hv > 0 else 1.0
        expected *= trend
        adj = COMPETITION_K * (1 - expected / baseline) if baseline > 0 else 0.0
        adj = min(max(adj, COMPETITION_ADJ[0]), COMPETITION_ADJ[1])
        if fill is not None and fill < SCARCE_FILL:
            adj += SCARCE_BONUS
        out[t] = {"adj": round(adj, 4), "baseline": round(baseline, 1), "expected": round(expected, 1),
                  "fill": round(fill, 2) if fill is not None else None, "trend": round(trend, 2)}
    ctx.comp = out
    ctx.notes.append("competition: " + "; ".join(
        f"{t} expect {c['expected']} offers/RFQ vs usual {c['baseline']}, buyers get {c['fill']} -> {c['adj']:+}"
        for t, c in out.items()))
    return out


def _step_for(outcomes: list, demand: list | None) -> tuple[float, float | None]:
    """(markup change, ceiling seen) for one buyer after one round of our own offers."""
    statuses = [s for s, _ in outcomes]
    above = [m for s, m in outcomes if s == "ABOVE_LIMIT"]
    if above:
        return STEP_ABOVE_LIMIT, min(above)
    if "OUTBID" in statuses:
        return STEP_OUTBID, None
    if "PARTIAL" in statuses:
        return STEP_PARTIAL, None
    if statuses and all(s == "ACCEPTED" for s in statuses):
        spare = demand is not None and demand[1] < demand[0]
        return (STEP_ACCEPTED_SPARE_DEMAND if spare else STEP_ACCEPTED), None
    return 0.0, None                  # SPEC_MISMATCH / CANCELLED say nothing about price


def market_view(state: dict, ctx: Context) -> dict[str, dict]:
    """buyer -> what OTHER sellers sold at with that buyer in this game, as markups of our comparable lots.

    market_summary gives the baht/kg range of the offers that sold on each RFQ (no team names). Dividing by the
    TV of our lots that pass the same spec turns it into a markup we can compare with our own ask. On competition
    day all teams get the same grades, so this is close; in the sandbox lots differ, so it is rougher.
    Rows where only our own offers sold are skipped (that would be comparing us with ourselves)."""
    if ctx.market is not None:
        return ctx.market
    memory = ctx.memory
    specs = {b: v["spec"] for b, v in getattr(memory, "buyer_specs", {}).items()}
    for r in state["rfqs"]:
        specs[r["buyer"]] = r["spec"]
    lots = state["lots"]
    rows: dict[str, list] = {}
    for result in getattr(memory, "rounds", []):
        ours = {}
        for o in result.get("my_offers", []):
            ours[o.get("rfq_id")] = ours.get(o.get("rfq_id"), 0) + (o.get("filled_kg") or 0)
        for row in result.get("market_summary", []):
            b = row.get("buyer")
            if not b or row.get("price_avg") is None or (row.get("bought_kg") or 0) <= ours.get(row.get("rfq_id"), 0):
                continue
            spec = specs.get(b)
            ok_lots = [l for l in lots if l["lot_id"] in ctx.tv and spec and spec_ok(l["sample"], spec)]
            if row.get("buyer_type") == "INSTANT":
                # INSTANT buys the low grades: compare with our lots that would really go there, not our best ones
                low = [l for l in ok_lots if not premium_capable(l, state, ctx)]
                ok_lots = low or ok_lots
            ref = [ctx.tv[l["lot_id"]] for l in ok_lots]
            if not ref:
                ref = list(ctx.tv.values())
            tv = statistics.median(ref) if ref else None
            if not tv:
                continue
            rows.setdefault(b, []).append({"round": result.get("round_id"), "offers": row.get("offers", 0),
                                           "min": row["price_min"] / tv, "avg": row["price_avg"] / tv,
                                           "max": row["price_max"] / tv})
    out = {}
    for b, rs in rows.items():
        recent = rs[-MARKET_RECENT:]                    # the latest rounds say most about today's competitors
        out[b] = {"rounds": len(rs), "offers": round(sum(r["offers"] for r in recent) / len(recent), 1),
                  "min": round(min(r["min"] for r in recent), 3), "avg": round(sum(r["avg"] for r in recent) / len(recent), 3),
                  "max": round(max(r["max"] for r in recent), 3),
                  "trend": round(recent[-1]["avg"] - recent[0]["avg"], 3) if len(recent) > 1 else 0.0}
    ctx.market = out
    if out:
        ctx.notes.append("competitors sold at: " + "; ".join(
            f"{b} x{v['min']:.2f}-{v['max']:.2f} (avg {v['avg']:.2f}, {v['offers']} offers/RFQ)" for b, v in out.items()))
    return out


def market_floor_sane(ctx: Context, buyer: str, avg: float) -> bool:
    """Use the competitors' average as a price floor only if it is close to what this buyer actually accepted from
    us (past games' median sold markup). Far off = the conversion compared different lots; ignore it."""
    memory = ctx.memory
    if memory is None or not hasattr(memory, "buyer_behavior"):
        return False
    sold = memory.buyer_behavior(buyer).get("accepted_median")
    return sold is not None and abs(avg - sold) <= MARKET_SANE_GAP


def our_last(ctx: Context, buyer: str) -> tuple[float, str] | None:
    """Our latest markup and result with this buyer in this game, e.g. (1.036, "OUTBID")."""
    for lesson in reversed(_round_lessons(ctx.memory)):
        if buyer in lesson["ours"]:
            status, mk = lesson["ours"][buyer][-1]
            return mk, status
    return None


def buyer_markup(state: dict, ctx: Context, buyer: str, buyer_type: str) -> float:
    """This round's markup for one buyer.

    1. start from past-game behaviour            4. + competition outlook for this round (quiet late
    2. replay this game's results for the buyer     rounds -> higher, crowded rounds -> a little lower)
    3. never cut more than MAX_CUT below start   5. ABOVE_LIMIT ceilings, bounds, final-round safety"""
    lo, hi = BOUNDS.get(buyer_type, (MIN_MARKUP, MAX_MARKUP))
    comp = competition_outlook(state, ctx).get(buyer_type, {})
    start = m = history_markup(ctx.memory, buyer, buyer_type)
    ceiling = None                    # lowest markup this buyer rejected as ABOVE_LIMIT in this game
    best_sold = None                  # highest markup that sold to this buyer type in this game
    recent_sold = None                # markup of the latest sale to this type, unless we lost at/below it since
    last_lost = None                  # lowest markup at which this buyer last OUTBID us (used in the final round)
    streak = 0                        # rounds in a row (latest) in which this buyer OUTBID every offer of ours
    for lesson in _round_lessons(ctx.memory):
        demand = lesson["demand"].get(buyer)
        for b, outcomes in lesson["ours"].items():
            if lesson["types"].get(b) == buyer_type:
                sold = [mk for s, mk in outcomes if s in ("ACCEPTED", "PARTIAL")]
                lost = [mk for s, mk in outcomes if s in ("OUTBID", "ABOVE_LIMIT")]
                if sold:
                    best_sold = max([best_sold or 0, *sold])
                    recent_sold = max(sold)                       # latest round with a sale to this type
                if lost and recent_sold is not None and min(lost) <= recent_sold:
                    recent_sold = None                            # lost at or below it since: no floor
        if buyer in lesson["ours"]:
            statuses = [s for s, _ in lesson["ours"][buyer]]
            streak = streak + 1 if statuses and all(s == "OUTBID" for s in statuses) else 0
            step, seen = _step_for(lesson["ours"][buyer], demand)
            if buyer_type != "INSTANT" and streak >= REPEAT_OUTBID_ROUNDS:
                step += STEP_REPEAT_OUTBID      # beaten again and again: competitors are cheaper, follow a little
            if seen is not None:      # ABOVE_LIMIT: sit just under the rejected price (no extra cut on top)
                ceiling = seen if ceiling is None else min(ceiling, seen)
                m = min(m, seen - ABOVE_LIMIT_MARGIN_BY_TYPE.get(buyer_type, ABOVE_LIMIT_MARGIN))
            else:
                lost = [mk for s, mk in lesson["ours"][buyer] if s == "OUTBID"]
                last_lost = min(lost) if lost else last_lost    # remembered, but no cut before the final round
                m += STEP_INSTANT_OUTBID if buyer_type == "INSTANT" and lost else step
        else:
            # we did not sell to this buyer: learn from same-type buyers we did sell to, at half strength
            same = [_step_for(o, lesson["demand"].get(b))[0] for b, o in lesson["ours"].items()
                    if lesson["types"].get(b) == buyer_type]
            if same:
                m += SPILLOVER * sum(same) / len(same)
            # many sellers offered, yet the buyer bought little -> its ceiling is tight. (Few offers and a
            # buyer left short means sellers ran out of beans: that is a chance to ask more, not less.)
            if demand and demand[0] > 0 and demand[3] > 0 and demand[1] / demand[0] < 0.6:
                crowded = comp.get("baseline") is None or demand[2] / demand[3] >= comp["baseline"]
                if crowded:
                    m += STEP_TIGHT_MARKET
    adj = comp.get("adj", 0.0)
    if is_first_round(state):
        adj = max(adj, 0.0)           # round 1 keeps its own rule: COMMERCIAL just under the ceiling, INSTANT at TV
    if adj > 0:
        adj *= 1 - urgency(state, ctx)     # behind schedule: selling every bag beats asking more
    m += adj                                              # quiet round -> ask more; crowded -> a little less
    m = max(m, start - MAX_CUT.get(buyer_type, 0.05))     # never undercut more than MAX_CUT in total
    if buyer_type == "SPECIALTY" and streak >= REPEAT_OUTBID_ROUNDS:
        ctx.demoted.add(buyer)        # competitors keep beating our premium price: rank and price it like COMMERCIAL
    if buyer_type == "SPECIALTY" and ctx.commercial_ref is not None and buyer not in ctx.demoted:
        floor = ctx.commercial_ref + SPECIALTY_OVER_COMMERCIAL  # SPECIALTY has the highest ceiling: ask more
        m = max(m, floor)
        if ceiling is not None and ceiling - 0.01 < floor:
            # this game proved its ceiling is below that floor: it is no better than a COMMERCIAL buyer
            ctx.demoted.add(buyer)
    if ceiling is not None:
        m = min(m, ceiling - 0.01)                        # ABOVE_LIMIT is a hard fact for this game
    if buyer_type == "INSTANT":
        m = min(m, 1.00)              # INSTANT never above TV
    if is_first_round(state) and buyer_type == "COMMERCIAL":
        lo = FIRST_ROUND_COMMERCIAL_FLOOR
    m = min(max(m, lo), hi)
    u = urgency(state, ctx)
    if u > 0 and not is_first_round(state):
        m = max(m - URGENCY_MAX_CUT * u, lo)      # behind schedule: sell more often, ceilings still respected
    mv = market_view(state, ctx).get(buyer)
    if mv and mv["rounds"] >= MARKET_MIN_ROUNDS:
        # what other sellers actually SOLD at with this buyer (converted to markups of our comparable lots)
        win_max, win_avg, win_min = mv["max"], mv["avg"], mv["min"]
        target = (win_avg + win_max) / 2                 # urgent -> aim between their average and best sale
        cap = win_max + MARKET_HEADROOM - (win_max + MARKET_HEADROOM - target) * u
        if m > cap:
            m = max(cap, lo)                      # asking far above every winning price only collects OUTBIDs
        elif (u == 0 and m < win_min and (ceiling is None or win_min < ceiling - 0.01)
              and market_floor_sane(ctx, buyer, win_avg)):
            m = min(win_min, hi)                  # even their cheapest sale beat our ask: room to raise
    if (recent_sold is not None and not is_final_round(state, ctx) and u < URGENCY_NO_SOLD_FLOOR
            and streak < REPEAT_OUTBID_ROUNDS and (ceiling is None or recent_sold < ceiling)):
        m = max(m, min(recent_sold - SOLD_FLOOR_GAP, hi))   # it just sold there: no need to go much lower
    if mv and mv["rounds"] >= MARKET_MIN_ROUNDS and not is_final_round(state, ctx):
        # competitors first: never sell below the AVERAGE price that sold with this buyer (we were often the
        # cheapest sale on the RFQ = money left on the table). Only a proven ceiling (ABOVE_LIMIT) goes lower.
        # The baht -> markup conversion is only trusted when it agrees with what this buyer really paid us in past
        # games (sandbox lots differ per team: e.g. INSTANT "sold at x1.22" is impossible and would pin our price).
        sane = market_floor_sane(ctx, buyer, mv["avg"])
        if sane:
            floor = mv["avg"] - URGENCY_MAX_CUT * u          # behind schedule: the floor gives way too
            if ceiling is not None:
                floor = min(floor, ceiling - 0.01)
            if buyer_type == "INSTANT":
                floor = min(floor, 1.00)
            m = max(m, min(floor, hi))
    if is_final_round(state, ctx):    # last chance: whatever is unsold now only gets 0.80 × TV
        if best_sold is not None:
            # stay near a price that sold; if this buyer's ceiling is known (ABOVE_LIMIT seen), do not probe at all
            m = min(m, best_sold + (0.0 if ceiling is not None else FINAL_ROUND_OVER_BEST_SOLD))
        if last_lost is not None:
            m = min(m, last_lost - FINAL_UNDER_LOST)            # just under where this buyer beat us
    if buyer_type == "INSTANT":
        # Round one is a clean x1.00 probe. Later reductions cannot go below the
        # average markup this buyer paid us, or the comparable market average.
        if is_first_round(state):
            m = 1.00
        else:
            history = getattr(ctx.memory, "buyer_history", {}).get(buyer, {}) if ctx.memory else {}
            accepted = [o["markup"] for o in history.get("observations", [])
                        if o.get("status") in ("ACCEPTED", "PARTIAL")
                        and (o.get("filled_kg") or 0) > 0 and isinstance(o.get("markup"), (int, float))]
            floors = [statistics.mean(accepted)] if accepted else []
            if mv and mv["rounds"] >= MARKET_MIN_ROUNDS and market_floor_sane(ctx, buyer, mv["avg"]):
                floors.append(mv["avg"])
            floor = min(1.00, max(floors, default=BOUNDS["INSTANT"][0]))
            if ceiling is not None:
                floor = min(floor, ceiling - 0.01)
            m = max(m, floor)
    # Apply the buyer's observed ceiling last. Earlier price floors (especially the
    # SPECIALTY premium and market-average floor) must not undo an ABOVE_LIMIT signal.
    if ceiling is not None:
        max_allowed = ceiling - ABOVE_LIMIT_MARGIN_BY_TYPE.get(buyer_type, ABOVE_LIMIT_MARGIN)
        if max_allowed < MIN_MARKUP:
            return 0.0                    # no acceptable price remains above our salvage safety floor
        m = min(m, max_allowed)
    return round(min(max(m, MIN_MARKUP), MAX_MARKUP), 4)


def instant_baht_caps(state: dict, ctx: Context) -> dict[str, float]:
    """INSTANT buyer -> baht/kg cap for this round (this game's evidence only): what the market paid it at the top
    (avg of price_max over the recent rounds). No slide toward the average price: the average is pulled down by
    competitors' low-quality lots, and a high-TV lot then never got offered (TV 118 lot skipped 7 rounds).
    ABOVE_LIMIT prices cap only the lot that was rejected (instant_rejected): a rejection of a TV-91 lot at 91 ฿
    says nothing about a TV-118 lot of better quality."""
    if ctx.baht_caps is not None:
        return ctx.baht_caps
    memory = ctx.memory
    market: dict[str, list] = {}
    for result in getattr(memory, "rounds", []):
        rows: dict[str, list] = {}
        for row in result.get("market_summary", []):
            if row.get("buyer_type") == "INSTANT" and row.get("price_max") and row.get("price_avg"):
                rows.setdefault(row["buyer"], []).append((row["price_max"], row["price_avg"]))
        for b, rs in rows.items():
            market.setdefault(b, []).append((max(x for x, _ in rs), sum(a for _, a in rs) / len(rs)))
    out = {}
    for b in {r["buyer"] for r in state["rfqs"] if r["buyer_type"] == "INSTANT"}:
        recent = market.get(b, [])[-MARKET_RECENT:]
        if recent:
            out[b] = round(sum(x for x, _ in recent) / len(recent), 2)
    ctx.baht_caps = out
    if out:
        ctx.notes.append("INSTANT baht caps: " + ", ".join(f"{b} {c:.1f}฿" for b, c in out.items()))
    return out


def instant_rejected(ctx: Context, buyer: str, lot_id: str) -> float | None:
    """Lowest baht/kg this INSTANT buyer rejected as ABOVE_LIMIT for THIS lot this game (None = never)."""
    memory = ctx.memory
    low = None
    for result in getattr(memory, "rounds", []):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(memory, "sent", {}).get(result.get("round_id"), [])}
        for o in result.get("my_offers", []):
            s = sent.get((o.get("rfq_id"), o.get("lot_id")), {})
            if (o.get("status") == "ABOVE_LIMIT" and o.get("lot_id") == lot_id and s.get("buyer") == buyer
                    and o.get("price_per_kg")):
                low = o["price_per_kg"] if low is None else min(low, o["price_per_kg"])
    return low


def lot_markup(state: dict, ctx: Context, rfq: dict, lot_id: str, markup: float) -> float | None:
    """The markup for THIS lot at this RFQ: INSTANT buyers also get the baht cap (market top price, and below the
    price this buyer rejected for this same lot). None = even INSTANT_MIN_MARKUP × TV is over the cap; in the final
    round we still offer at INSTANT_MIN_MARKUP (unsold beans pay only salvage 0.80)."""
    if rfq["buyer_type"] != "INSTANT":
        return markup
    tv = ctx.tv.get(lot_id)
    caps = [c for c in (instant_baht_caps(state, ctx).get(rfq["buyer"]),) if c]
    rej = instant_rejected(ctx, rfq["buyer"], lot_id)
    if rej:
        caps.append(rej * (1 - INSTANT_BAHT_MARGIN))
    if not caps or not tv or markup * tv <= min(caps):
        return markup
    capped = min(caps) / tv
    if capped >= INSTANT_MIN_MARKUP:
        return round(capped, 4)
    return INSTANT_MIN_MARKUP if is_final_round(state, ctx) else None


def rfq_markups(state: dict, ctx: Context) -> dict[str, float]:
    if ctx.markups is None:
        by_buyer = {}
        # COMMERCIAL (and INSTANT) first: SPECIALTY is priced above the best COMMERCIAL markup of this round
        for r in sorted(state["rfqs"], key=lambda r: r["buyer_type"] == "SPECIALTY"):
            if r["buyer_type"] == "SPECIALTY" and ctx.commercial_ref is None:
                com = [m for b, m in by_buyer.items()
                       if any(x["buyer"] == b and x["buyer_type"] == "COMMERCIAL" for x in state["rfqs"])]
                ctx.commercial_ref = max(com) if com else BASE_MARKUP["COMMERCIAL"]
            if r["buyer"] not in by_buyer:
                by_buyer[r["buyer"]] = buyer_markup(state, ctx, r["buyer"], r["buyer_type"])
        ctx.markups = {r["rfq_id"]: by_buyer[r["buyer"]] for r in state["rfqs"]}
        ctx.notes.append("markups: " + ", ".join(f"{b} {m}" for b, m in by_buyer.items()))
        if ctx.demoted:
            ctx.notes.append(f"SPECIALTY ceiling proved low this game, ranked like COMMERCIAL: {sorted(ctx.demoted)}")
    return ctx.markups


def type_rank(c: dict, ctx: Context) -> int:
    if c["buyer_type"] == "SPECIALTY" and c["buyer"] in ctx.demoted:
        return TYPE_RANK["COMMERCIAL"]
    return TYPE_RANK.get(c["buyer_type"], 9)


# ---------------------------------------------------------------- volume and lot -> buyer policy

def achieved_fill(ctx: Context) -> float:
    """Share of offered bags that sold in this game so far (long shots left out), shrunk toward 0.5 early on."""
    offered = sold = 0
    for result in getattr(ctx.memory, "rounds", []):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(ctx.memory, "sent", {}).get(result.get("round_id"), [])}
        for o in result.get("my_offers", []):
            if sent.get((o.get("rfq_id"), o.get("lot_id")), {}).get("spec_longshot"):
                continue
            offered += (o.get("kg") or 0) // BAG
            sold += (o.get("filled_kg") or 0) // BAG
    return (sold + FILL_PRIOR_BAGS * 0.5) / (offered + FILL_PRIOR_BAGS)


def urgency(state: dict, ctx: Context) -> float:
    """0 = on schedule, 1 = far behind: the bags left need more than the remaining Round Capacity can carry
    at a normal fill rate. Without a Round Capacity (market sets none) we use the pace the market would set:
    1.5 × an even share of our stock per round, so prices still come down when stock is not selling."""
    if ctx.urg is not None:
        return ctx.urg
    cap = state.get("round_capacity_kg")
    if not cap:
        total = state["round"].get("of") or ctx.rules["timeline"]["rounds"]
        stock = sum(l.get("kg_total") or l["kg_available"] for l in state["lots"]) // BAG
        cap = max(1, math.ceil(1.5 * stock / max(1, total))) * BAG
    u = 0.0
    if cap and state["round"]["kind"] != "practice":
        # lots no buyer accepts wait until the rest is sold, so they do not count toward the sell-out pace
        lots = [l for l in state["lots"] if not unsellable(l, state, ctx)] or state["lots"]
        bags = sum(l["kg_available"] // BAG for l in lots)
        # quiet rounds near the close count as less than a full round (calendar demand_weight)
        left = demand_rounds(state, ctx)[0]
        need = bags / max(1.0, cap // BAG * left)
        fill = achieved_fill(ctx)
        # behind only if we must sell more often than we actually do (selling 90 % while 62 % is needed = fine)
        bar = max(URGENT_FILL, fill)
        u = min(1.0, max(0.0, (need - bar) / (1 - bar))) if bar < 1 else 0.0
        if u > 0:
            ctx.notes.append(f"sell-out mode: {bags} bags left, {cap // BAG} bags/round x {left:.1f} demand-weighted "
                             f"rounds -> must sell {need:.0%} of offers, we sell {fill:.0%}, urgency {u:.2f}")
    ctx.urg = u
    return u


def premium_lots(state: dict, ctx: Context) -> set[str]:
    """Lots whose best buyer this round is asked ABOVE our appraisal (markup > 1.00), not a long shot."""
    markups = rfq_markups(state, ctx)
    out = set()
    for lot in state["lots"]:
        for r in state["rfqs"]:
            if markups.get(r["rfq_id"], 0) > 1.0 and spec_ok(lot["sample"], r["spec"])                     and not spec_mismatched(ctx, lot["lot_id"], r["spec"]):
                out.add(lot["lot_id"])
                break
    return out


def sell_budget(state: dict, ctx: Context) -> dict[str, int]:
    """Offer 70 % of each lot in the first half of the game's DEMAND, then release the rest, reaching 100 % by
    the final round. Progress is weighted by the calendar's demand_weight, so with quiet rounds at the end
    (R13–R15 "near close") the stock is released earlier. Before a SPECIALTY festival, a few bags of lots that
    pass a SPECIALTY spec are kept back for it (specialty_reserve)."""
    final = is_final_round(state, ctx)
    u = urgency(state, ctx)
    left, whole = demand_rounds(state, ctx)
    first = round_weight(ctx, state["round"].get("number") or 1)
    progress = 0.0 if state["round"]["kind"] == "practice" or whole <= first else (whole - left) / (whole - first)
    reserve = specialty_reserve(state, ctx)
    out = {}
    for lot in state["lots"]:
        bags = lot["kg_available"] // BAG
        # First half: put up 70% of available stock for sale. Second half:
        # progressively release the remaining 30%, reaching 100% by the final round.
        share = 1.0 if final or u >= 0.5 else (
            OFFER_SHARE if progress <= 0.5 else OFFER_SHARE + (1 - OFFER_SHARE) * ((progress - 0.5) * 2)
        )
        if lot["lot_id"] in narrow_now(state, ctx):
            share = 1.0                       # few buyers take this lot: clear it while one is here
        n = min(bags, max(1, math.ceil(bags * share))) if bags else 0
        if reserve.get(lot["lot_id"]):
            n = min(n, bags - reserve[lot["lot_id"]])     # may be 0: the lot waits for the festival this round
        out[lot["lot_id"]] = max(0, n) * BAG
    return out


def _calendar_rounds(segment: dict) -> range:
    text = str(segment.get("rounds", ""))
    try:
        a, _, b = text.partition("-")
        return range(int(a), int(b or a) + 1)
    except ValueError:
        return range(0)


def _specialty_share(ctx: Context, n: int) -> float:
    for seg in ctx.calendar or []:
        if n in _calendar_rounds(seg):
            return float(seg.get("specialty_share") or 0)
    return 0.0


def specialty_rounds_ahead(state: dict, ctx: Context) -> list[int]:
    """Later rounds where the Market Calendar promises MORE SPECIALTY demand than this round (a festival,
    e.g. R6–R8 "เทศกาลกาแฟ"). Empty during the festival itself and when the calendar is flat."""
    if state["round"]["kind"] == "practice":
        return []
    n = state["round"]["number"]
    total = state["round"].get("of") or ctx.rules["timeline"]["rounds"]
    now = _specialty_share(ctx, n)
    return [r for r in range(n + 1, total + 1) if _specialty_share(ctx, r) > now]


def specialty_reserve(state: dict, ctx: Context) -> dict[str, int]:
    """lot_id -> bags kept back this round for the SPECIALTY festival later in the calendar.

    Only lots that pass a SPECIALTY spec already seen this game (not rejected by it as SPEC_MISMATCH), only
    FESTIVAL_HOLD_PER_ROUND bag per festival round per lot, at most FESTIVAL_HOLD_MAX_SHARE of the lot, and
    nothing when we are behind schedule (urgency ≥ 0.5): selling out matters more than the festival premium."""
    ahead = specialty_rounds_ahead(state, ctx)
    if not ahead or is_final_round(state, ctx) or urgency(state, ctx) >= URGENCY_NO_SOLD_FLOOR:
        return {}
    specs = list(getattr(ctx.memory, "specialty_specs", []) or [])
    specs += [r["spec"] for r in state["rfqs"] if r["buyer_type"] == "SPECIALTY"]
    here = [r["spec"] for r in state["rfqs"] if r["buyer_type"] == "SPECIALTY"]
    out = {}
    for lot in sorted(state["lots"], key=lambda l: -ctx.tv[l["lot_id"]]):
        bags = lot["kg_available"] // BAG
        fits = [s for s in specs if spec_ok(lot["sample"], s) and not spec_mismatched(ctx, lot["lot_id"], s)]
        if bags < 2 or not fits:
            continue
        if any(s in here for s in fits):
            continue                          # its SPECIALTY buyer is here now: sell to it, no need to wait
        out[lot["lot_id"]] = min(len(ahead) * FESTIVAL_HOLD_PER_ROUND, math.floor(bags * FESTIVAL_HOLD_MAX_SHARE))
    out = {i: n for i, n in out.items() if n > 0}
    if out:
        ctx.notes.append(f"festival in rounds {ahead[0]}-{ahead[-1]}: keep back for SPECIALTY " +
                         ", ".join(f"{i} {n} bags" for i, n in out.items()))
    return out


def lots_outbid_last_round(ctx: Context) -> set[str]:
    rounds = getattr(ctx.memory, "rounds", [])
    if not rounds:
        return set()
    return {o["lot_id"] for o in rounds[-1].get("my_offers", [])
            if o.get("status") == "OUTBID" and not (o.get("filled_kg") or 0)}


def premium_capable(lot: dict, state: dict, ctx: Context) -> bool:
    """Could this lot ever go to a SPECIALTY/COMMERCIAL buyer? (specs seen this round or earlier this game)"""
    specs = [r["spec"] for r in state["rfqs"] if r["buyer_type"] in PREMIUM_TYPES]
    if ctx.memory is not None and hasattr(ctx.memory, "premium_specs"):
        specs += ctx.memory.premium_specs
    # a spec that already rejected this lot's TRUE quality (SPEC_MISMATCH) does not count
    return any(spec_ok(lot["sample"], s) and not spec_mismatched(ctx, lot["lot_id"], s) for s in specs)


def spec_mismatched(ctx: Context, lot_id: str, spec: dict) -> bool:
    """Will this spec reject the lot? True when a spec at least as LOOSE as this one (every limit equal or looser)
    already said SPEC_MISMATCH to this lot this game: the true quality failed the looser spec, so it fails this
    stricter one too. A looser spec than the one that failed is still worth a try.
    Results whose spec we did not record (left over from an older run of the same sandbox game) are ignored."""
    memory = ctx.memory
    for result in getattr(memory, "rounds", []):
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(memory, "sent", {}).get(result.get("round_id"), [])}
        for o in result.get("my_offers", []):
            if o.get("lot_id") == lot_id and o.get("status") == "SPEC_MISMATCH":
                failed = sent.get((o.get("rfq_id"), lot_id), {}).get("spec")
                if failed and _at_least_as_strict(spec, failed):
                    return True
    return False


def _at_least_as_strict(spec: dict, other: dict) -> bool:
    """Every limit of spec is equal to or stricter than the same limit of other."""
    return (spec["max_defect_pct"] <= other["max_defect_pct"]
            and spec["max_moisture_pct"] <= other["max_moisture_pct"]
            and (spec.get("min_screen") or 0) >= (other.get("min_screen") or 0))


def win_chances(state: dict, ctx: Context) -> dict[str, float]:
    """rfq_id -> chance an offer to it sells (0.05–0.95), from how each buyer behaved toward us and others.

    - this game: what happened to our offers to this buyer (ACCEPTED 1, PARTIAL 0.5, OUTBID 0), last round counts double
    - past games: the same, weighted lightly (at most WIN_HISTORY_MAX_N offers' worth)
    - crowding: buyers that usually draw many offers per RFQ (e.g. 16 on one INSTANT RFQ) are harder to win
    - RFQ size: a bigger RFQ has more room
    ABOVE_LIMIT and SPEC_MISMATCH are left out: price and quality are handled elsewhere."""
    if ctx.wins is not None:
        return ctx.wins
    memory = ctx.memory
    comp = competition_outlook(state, ctx)
    lessons = _round_lessons(memory)
    score = {"ACCEPTED": 1.0, "PARTIAL": 0.5, "OUTBID": 0.0}
    by_buyer = {}
    for buyer, buyer_type in {(r["buyer"], r["buyer_type"]) for r in state["rfqs"]}:
        # crowding: this buyer's usual offers per RFQ vs its type's usual
        rows = getattr(memory, "buyer_history", {}).get(buyer, {}).get("market_rounds", [])
        offers = [row.get("offers", 0) or 0 for row in rows]
        baseline = comp.get(buyer_type, {}).get("baseline")
        crowd = 1.0
        if offers and baseline:
            crowd = min(1.4, max(0.6, (baseline / max(0.5, sum(offers) / len(offers))) ** 0.5))
        s, n = WIN_PRIOR * 0.5 * crowd, WIN_PRIOR
        obs = [o for o in getattr(memory, "buyer_history", {}).get(buyer, {}).get("observations", [])
               if o.get("status") in score]
        if obs:
            w = min(WIN_HISTORY_WEIGHT, WIN_HISTORY_MAX_N / len(obs))
            s += w * sum(score[o["status"]] for o in obs)
            n += w * len(obs)
        for i, lesson in enumerate(lessons):
            w = 2.0 if i == len(lessons) - 1 else 1.0
            for status, _ in lesson["ours"].get(buyer, []):
                if status in score:
                    s += w * score[status]
                    n += w
        by_buyer[buyer] = s / n
    bags_by_type = {}
    for r in state["rfqs"]:
        bags_by_type.setdefault(r["buyer_type"], []).append(r["bags"])
    out = {}
    for r in state["rfqs"]:
        avg = sum(bags_by_type[r["buyer_type"]]) / len(bags_by_type[r["buyer_type"]])
        size = min(1.15, max(0.85, (r["bags"] / avg) ** 0.3)) if avg else 1.0
        out[r["rfq_id"]] = round(min(0.95, max(0.05, by_buyer[r["buyer"]] * size)), 3)
    ctx.wins = out
    ctx.notes.append("win chance: " + ", ".join(f"{b} {p:.2f}" for b, p in by_buyer.items()))
    return out


def unsellable(lot: dict, state: dict, ctx: Context) -> bool:
    """The lot passes no spec we know: not this round's RFQs, not any buyer seen this game (SPEC_MISMATCH counts
    as failing)."""
    specs = [r["spec"] for r in state["rfqs"]]
    specs += [v["spec"] for v in getattr(ctx.memory, "buyer_specs", {}).values() if v.get("spec")]
    return not any(spec_ok(lot["sample"], s) and not spec_mismatched(ctx, lot["lot_id"], s) for s in specs)


def sellable_left(state: dict, ctx: Context) -> bool:
    """Is any bag of a lot that some buyer accepts still in stock?"""
    return any(l["kg_available"] >= BAG and not unsellable(l, state, ctx) for l in state["lots"])


def candidates(state: dict, ctx: Context) -> list[dict]:
    """(lot, RFQ) pairs the sales policy allows this round: best buyer type first, then the best
    expected price (markup × chance to sell)."""
    budget = sell_budget(state, ctx)
    markups = rfq_markups(state, ctx)
    wins = win_chances(state, ctx)
    out = []
    for lot in state["lots"]:
        if budget[lot["lot_id"]] < BAG:
            continue
        passes = [r for r in state["rfqs"] if spec_ok(lot["sample"], r["spec"])]
        # skip specs that already said SPEC_MISMATCH to this lot this game: its true quality fails them,
        # offering again only wastes round capacity (e.g. sample 6.x % defect, true 7.8 % vs a 7 % limit)
        ok = [r for r in passes if not spec_mismatched(ctx, lot["lot_id"], r["spec"])]
        if len(ok) < len(passes):
            ctx.notes.append(f"{lot['lot_id']} skips specs that gave SPEC_MISMATCH before")
        # Keep lots with a known SPECIALTY/COMMERCIAL outlet for those buyers.
        # INSTANT is a fallback only when this lot has no viable premium outlet
        # among current or previously observed premium specs.
        instant_only = not premium_capable(lot, state, ctx)
        allowed = [r for r in ok if r["buyer_type"] != "INSTANT" or instant_only]
        if len(allowed) < len(ok):
            ctx.notes.append(f"{lot['lot_id']} skips INSTANT: premium buyer can accept this lot")
        longshot = not passes                                # the sample fails every spec this round
        if longshot:
            # Probe an INSTANT-only lot early instead of waiting until every other lot is gone.
            # A failed-spec sample can be noisy; early feedback gives later rounds time to find another outlet.
            # Keep the premium-buyer rule: never divert a lot that a premium buyer is known to accept.
            if not instant_only:
                continue
            allowed = [r for r in state["rfqs"] if r["buyer_type"] == "INSTANT"
                       and not spec_mismatched(ctx, lot["lot_id"], r["spec"])]
            if allowed:
                probe_markup = 1.00 if is_first_round(state) else LONGSHOT_MARKUP
                ctx.notes.append(f"{lot['lot_id']} fails current specs: early INSTANT-only probe at x{probe_markup:.2f}")
        for r in allowed:
            mk = (1.00 if is_first_round(state) else lot_markup(
                state, ctx, r, lot["lot_id"], LONGSHOT_MARKUP
            )) if longshot else lot_markup(state, ctx, r, lot["lot_id"], markups[r["rfq_id"]])
            if mk is None:
                ctx.notes.append(f"{lot['lot_id']} skips {r['rfq_id']}: even x{INSTANT_MIN_MARKUP} is over "
                                 f"{r['buyer']}'s baht cap")
                continue
            if longshot and not is_first_round(state) and mk < LONGSHOT_MARKUP:
                ctx.notes.append(f"{lot['lot_id']} skips {r['rfq_id']}: INSTANT cap is below the long-shot floor")
                continue
            if not longshot and mk < MIN_MARKUP:
                ctx.notes.append(f"{lot['lot_id']} skips {r['rfq_id']}: buyer ceiling is below the minimum viable markup")
                continue
            out.append({"lot_id": lot["lot_id"], "rfq_id": r["rfq_id"], "buyer": r["buyer"],
                        "buyer_type": r["buyer_type"], "rfq_bags": r["bags"],
                        "max_bags": min(budget[lot["lot_id"]], r["kg"]) // BAG,
                        "tv": ctx.tv[lot["lot_id"]],
                        "markup": mk,
                        "p_win": wins[r["rfq_id"]], "longshot": longshot, "tie": ctx.rng.random()})
    # best type first, then expected price (markup × chance to sell); a small random tie-break so we do not
    # chase exactly the same RFQ as every other team
    out.sort(key=lambda c: (type_rank(c, ctx), -(c["markup"] * c["p_win"]) - 0.01 * c["tie"]))
    return out


def offer_gain(c: dict, ctx: Context) -> float:
    """Expected baht/kg gained over salvage by offering one bag on this candidate: (markup − 0.80) × TV × chance."""
    salvage = ctx.rules.get("salvage_ratio", 0.8)
    return (c["markup"] - salvage) * c["tv"] * c.get("p_win", 0.5)


def buyer_presence(ctx: Context) -> dict[str, float]:
    """Chance that each buyer posts an RFQ in a given round: this game's rounds, with past games as a prior."""
    memory = ctx.memory
    seen_rounds = [r for r in getattr(memory, "rounds", []) if r.get("market_summary")]
    game_count: dict[str, int] = {}
    for r in seen_rounds:
        for b in {row.get("buyer") for row in r["market_summary"] if row.get("buyer")}:
            game_count[b] = game_count.get(b, 0) + 1
    all_rounds, hist_count = set(), {}
    for b, profile in getattr(memory, "buyer_history", {}).items():
        keys = {":".join(str(row.get("id", "")).split(":")[:2]) for row in profile.get("market_rounds", [])}
        hist_count[b] = len(keys)
        all_rounds |= keys
    out = {}
    for b in set(game_count) | set(hist_count) | set(getattr(memory, "buyer_specs", {})):
        prior = hist_count[b] / len(all_rounds) if b in hist_count and all_rounds else 0.7   # unknown buyer: 0.7
        out[b] = (game_count.get(b, 0) + PRESENCE_PRIOR_N * prior) / (len(seen_rounds) + PRESENCE_PRIOR_N)
    return out


def outlet_here(lot: dict, state: dict, ctx: Context) -> bool:
    """Is a buyer whose spec this lot passes (and that has not rejected it) posting an RFQ this round?"""
    return any(spec_ok(lot["sample"], r["spec"]) and not spec_mismatched(ctx, lot["lot_id"], r["spec"])
               for r in state["rfqs"])


def narrow_now(state: dict, ctx: Context) -> dict[str, float]:
    """Narrow lots whose buyer IS here this round -> their outlet chance. Only then do they jump the queue;
    when their buyer is absent they must not use capacity on long shots."""
    if not getattr(ctx.memory, "rounds", []):
        return {}                             # round 1: we have not seen which buyers come yet, so nothing is narrow
    outlets = lot_outlets(state, ctx)
    failed = lots_unsold_last_round(ctx)      # it was offered last round and sold nothing: no priority this round
    return {l["lot_id"]: outlets[l["lot_id"]] for l in state["lots"]
            if outlets.get(l["lot_id"], 1.0) < NARROW_OUTLET and outlet_here(l, state, ctx)
            and l["lot_id"] not in failed}


def lots_unsold_last_round(ctx: Context) -> set[str]:
    rounds = getattr(ctx.memory, "rounds", [])
    if not rounds:
        return set()
    offered, sold = set(), set()
    for o in rounds[-1].get("my_offers", []):
        offered.add(o["lot_id"])
        if o.get("filled_kg"):
            sold.add(o["lot_id"])
    return offered - sold


def lot_outlets(state: dict, ctx: Context) -> dict[str, float]:
    """lot_id -> chance per round that at least one buyer whose spec this lot passes is in the market.

    A lot that only one, rarely-seen buyer accepts gets few chances to sell, so it must be cleared whenever
    that buyer shows up. Specs that already gave this lot SPEC_MISMATCH do not count."""
    if ctx.outlets is not None:
        return ctx.outlets
    presence = buyer_presence(ctx)
    specs = {b: v["spec"] for b, v in getattr(ctx.memory, "buyer_specs", {}).items()}
    for r in state["rfqs"]:
        specs[r["buyer"]] = r["spec"]
    out = {}
    for lot in state["lots"]:
        miss = 1.0
        for b, spec in specs.items():
            if spec_ok(lot["sample"], spec) and not spec_mismatched(ctx, lot["lot_id"], spec):
                miss *= 1 - min(0.99, presence.get(b, 0.7))
        out[lot["lot_id"]] = round(1 - miss, 3)
    ctx.outlets = out
    narrow = {i: p for i, p in out.items() if p < NARROW_OUTLET}
    if narrow:
        ctx.notes.append("few buyers take these lots, clear them first: " + ", ".join(f"{i} {p}" for i, p in narrow.items()))
    return out


def capacity_split(state: dict, ctx: Context, by_lot: dict, wanted: dict) -> dict[str, int]:
    """Bags per lot this round under the Round Capacity (at most ceil(1.5 × total bags ÷ rounds) bags on offer).

    1. pace: every lot first gets its share of what is left: remaining bags ÷ the rounds left in which a buyer
       for it is likely present (narrow lots, accepted by few or rarely-seen buyers, get a bigger share and go
       first), so no lot is starved and everything can still be offered before the market closes
    2. the spare capacity (the extra 0.5×) goes to the lots whose best offer gains most over salvage:
       (markup − 0.80) × TV × chance to sell; a 0.82 long shot or a crowded RFQ comes last."""
    # Until an INSTANT-only long-shot lot has actually sold, use one bag to test
    # whether its noisy sample is misleading. If it sells, later rounds may scale up.
    sold_lots = {o.get("lot_id") for rnd in getattr(ctx.memory, "rounds", [])
                 for o in rnd.get("my_offers", []) if (o.get("filled_kg") or 0) >= BAG}
    room = {i: sum({c["rfq_id"]: c["rfq_bags"] for c in opts}.values()) for i, opts in by_lot.items()}
    for i, opts in by_lot.items():
        if opts and all(c.get("longshot") for c in opts) and i not in sold_lots:
            room[i] = min(room[i], 1)
    wanted = {i: min(wanted.get(i, 0), room.get(i, 0)) for i in by_lot}

    cap = state.get("capacity_left_kg")
    if cap is None or sum(wanted.get(i, 0) for i in by_lot) <= cap // BAG:
        ctx.lot_target = {i: wanted.get(i, 0) for i in by_lot}
        return ctx.lot_target                                   # capacity does not bind: offer all we want
    cap_bags = cap // BAG
    left = demand_rounds(state, ctx)[0]       # quiet rounds near the close count less -> faster pace now
    avail = {l["lot_id"]: l["kg_available"] // BAG for l in state["lots"]}
    best_gain = {i: max(offer_gain(c, ctx) for c in opts) for i, opts in by_lot.items()}
    outlets = lot_outlets(state, ctx)
    narrow = narrow_now(state, ctx)
    # narrow lots whose buyer is here first (fewest chances left to sell), the rest by gain
    order = sorted(by_lot, key=lambda i: (narrow.get(i, NARROW_OUTLET), -best_gain[i]))
    out = {i: 0 for i in by_lot}

    def pace_of(i: str) -> int:
        chances = max(1.0, left * (outlets.get(i, 1.0) if i in narrow else 1.0))   # rounds with a buyer for it
        pace = min(wanted.get(i, 0), math.ceil(avail.get(i, 0) / chances))
        if i in narrow:
            pace = min(pace, max(1, math.floor(cap // BAG * NARROW_MAX_SHARE)))   # leave room for other lots
        return pace

    def give(i: str, bags: int) -> None:
        nonlocal cap_bags
        n = max(0, min(bags, wanted.get(i, 0) - out[i], cap_bags))
        out[i] += n
        cap_bags -= n

    def round_robin(limit) -> None:
        """One bag per lot per pass, in `order`, until every lot reaches limit(i) or capacity runs out."""
        moved = True
        while moved and cap_bags > 0:
            moved = False
            for i in order:
                if out[i] < min(limit(i), wanted.get(i, 0)) and cap_bags > 0:
                    give(i, 1)
                    moved = True

    # Offer EVERY lot every round (price discovery on each lot, no lot sold alone for many rounds while the others
    # wait for the end). Order: narrow lots whose buyer is here, then the most bags still to clear (backlog),
    # then gain. When capacity is smaller than the number of lots, rotate with the round number so a lot left
    # out this round is first in line next round.
    order = sorted(by_lot, key=lambda i: (narrow.get(i, NARROW_OUTLET), -avail.get(i, 0), -best_gain[i]))
    if cap_bags < len(order):
        shift = (state["round"].get("number") or 0) % len(order)
        order = order[shift:] + order[:shift]
        order.sort(key=lambda i: narrow.get(i, NARROW_OUTLET))   # narrow lots whose buyer is here stay first
    round_robin(lambda i: 1)                                     # 0. one bag for every lot
    round_robin(pace_of)                                         # 1. each lot's pace share, one bag at a time
    round_robin(lambda i: wanted.get(i, 0))                      # 2. spare capacity, still spread over lots
    ctx.notes.append("capacity " + str(cap // BAG) + " bags: " + ", ".join(f"{i} {n}" for i, n in out.items() if n))
    ctx.lot_target = out
    return out


def rule_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict]:
    """Best lots to the best buyers. Each lot is split over the 2 most promising RFQs of its best type, with
    more bags to the RFQ more likely to buy (split by chance to sell); any remainder goes wherever room is left."""
    budget = sell_budget(state, ctx)
    rfq_left = {r["rfq_id"]: r["bags"] for r in state["rfqs"]}
    by_lot: dict[str, list] = {}
    for c in cands:
        by_lot.setdefault(c["lot_id"], []).append(c)
    plan = []

    def give(lot_id: str, c: dict, bags: int) -> int:
        bags = min(bags, rfq_left[c["rfq_id"]])
        if bags <= 0:
            return 0
        existing = next((p for p in plan if p["lot_id"] == lot_id and p["rfq_id"] == c["rfq_id"]), None)
        if existing:
            existing["bags"] += bags
        else:
            plan.append({"lot_id": lot_id, "rfq_id": c["rfq_id"], "bags": bags, "markup": c["markup"],
                         "longshot": c.get("longshot", False)})
        rfq_left[c["rfq_id"]] -= bags
        return bags

    target = capacity_split(state, ctx, by_lot, {i: b // BAG for i, b in budget.items()})
    target_left = dict(target)
    narrow = narrow_now(state, ctx)

    # Give each compatible RFQ one bag first, subject to stock, RFQ size and round capacity.
    # The bag comes from the lot with the most bags still unplaced, so the RFQs are shared out over ALL lots
    # instead of the single best lot taking every one.
    covered = set()
    for r in sorted(state["rfqs"], key=lambda row: (TYPE_RANK.get(row["buyer_type"], 99), row["rfq_id"])):
        choices = [c for c in cands if c["rfq_id"] == r["rfq_id"]
                   and target_left.get(c["lot_id"], 0) > 0 and rfq_left.get(c["rfq_id"], 0) > 0]
        if not choices:
            continue
        c = max(choices, key=lambda x: (target_left[x["lot_id"]], x["markup"] * x["p_win"], -ctx.tv[x["lot_id"]]))
        if give(c["lot_id"], c, 1):
            target_left[c["lot_id"]] -= 1
            covered.add(r["rfq_id"])
    if covered:
        ctx.notes.append("RFQ coverage: " + ", ".join(sorted(covered)))

    # narrow lots first: a lot any buyer takes must not use up the room of the one RFQ a narrow lot can use
    for lot_id in sorted(by_lot, key=lambda i: (narrow.get(i, NARROW_OUTLET), -ctx.tv[i])):
        # best type first; within a type prefer RFQs we have not loaded yet, so bags spread over every RFQ
        # whose spec fits instead of piling onto one (one OUTBID would then lose everything)
        used = {r["rfq_id"]: r["bags"] - rfq_left[r["rfq_id"]] for r in state["rfqs"]}
        if is_final_round(state, ctx):
            # At close there is no value in saving stock for a future premium buyer.
            # Rank outlets by expected value over salvage, while retaining a small
            # preference for premium buyers when expected returns are nearly equal.
            options = sorted(by_lot[lot_id], key=lambda c: (
                -offer_gain(c, ctx) * SPREAD_DECAY ** used.get(c["rfq_id"], 0),
                type_rank(c, ctx), c["rfq_id"]))
        else:
            options = sorted(by_lot[lot_id], key=lambda c: (type_rank(c, ctx),
                             -c["markup"] * c["p_win"] * SPREAD_DECAY ** used.get(c["rfq_id"], 0)))
        bags_left = target_left.get(lot_id, 0)
        best_rank = type_rank(options[0], ctx)
        top = [c for c in options if (is_final_round(state, ctx) or type_rank(c, ctx) == best_rank)
               and rfq_left[c["rfq_id"]] > 0][:2]
        if len(top) == 2 and bags_left >= 2 and top[1]["p_win"] >= MIN_SPLIT_P_WIN:
            p1, p2 = top[0]["p_win"], top[1]["p_win"]
            first = min(bags_left - 1, max(1, round(bags_left * p1 / (p1 + p2))))
            bags_left -= give(lot_id, top[0], first)
            bags_left -= give(lot_id, top[1], bags_left)
        elif top:
            bags_left -= give(lot_id, top[0], bags_left)
        for c in options:                                    # remainder: next best RFQs with room
            if bags_left <= 0:
                break
            bags_left -= give(lot_id, c, bags_left)
    return plan


# ---------------------------------------------------------------- LLM (event gateway only in the competition)

SYSTEM = """You are the sales agent of a coffee farm selling green robusta beans in a sealed-offer market.
Each round buyers post RFQs. You choose which RFQs to offer your lots to and how many 60 kg bags.
price = markup x the lot's True Value estimate (tv). Buyers rank offers by price / true value, cheapest first,
and reject offers above their hidden ceiling. Unsold beans are worth only 0.80 x tv at the end.
Policy: prioritize SPECIALTY, then COMMERCIAL, then INSTANT, but cover each compatible RFQ when capacity permits.
Each candidate has the markup our pricing rules chose; you may go lower, never higher. Treat 0.89 x TV as a
soft floor for your own discounts: do not choose below 0.89 unless that candidate's supplied rule markup is
already below 0.89 (for example, a buyer ceiling or INSTANT baht cap). Start from our_last for that buyer when
available. Compare its status and markup with the highest and average competitor prices that actually sold to
that buyer. After OUTBID, adjust our previous price toward the observed competitor sold range, using the highest
sold price as a reference; make only a measured change and never exceed the candidate markup. Do not treat the
competitor maximum as the buyer's hidden ceiling. If our offer sold, do not cut just because the competitor range
is lower. If there is no competitor data, keep close to our rule candidate and our previous price.
p_win is the estimated chance an offer to that RFQ sells: put more bags where p_win is higher.
Volume: each round there is a hard cap on bags (Round Capacity). Use all of it when stock is left; the target
bag count in the message is what our rules would offer. Never leave capacity unused while bags remain.
Narrow lots (few buyers accept them, listed in the message) must be offered first, all bags, while their buyer is here.
Sell-out urgency 0..1: above 0 we are behind schedule; the higher it is, the more you should favour high p_win
and offer every bag, even at a slightly lower markup. At 0, keep the markups and do not undercut.
OUTBID is not a reason to lower premium-buyer prices unless urgency is above 0. INSTANT starts at x1.00 and steps down by 0.02 after OUTBID, but stays above the buyer's known average paid price when available. ABOVE_LIMIT means that buyer's ceiling is lower.
Also fill "buyers": one short note per buyer in the message's buyer table, in THAI, so the team can read it:
situation (e.g. แพ้ราคา / ขายได้ / เกินเพดาน / ยังไม่เคยเสนอ), our last markup, the markup competitors won at,
and the action this round (e.g. ลดเป็น x1.00 / คงราคา / ขึ้นราคา / ย้ายโควตาไปร้านอื่น), each under 120 characters.
Answer with one JSON object only, in the requested schema. Keep reason under 200 characters. Use supplied facts only.
"""

SCHEMA = {"type": "object", "required": ["offers", "reason", "buyers"], "properties": {
    "offers": {"type": "array", "items": {"type": "object", "required": ["candidate", "bags", "markup"], "properties": {
        "candidate": {"type": "integer"}, "bags": {"type": "integer"}, "markup": {"type": "number"}}}},
    "reason": {"type": "string", "maxLength": 300},   # short: a long reason can run past max_tokens and cut the JSON
    "buyers": {"type": "array", "items": {"type": "object", "required": ["buyer", "situation", "action"], "properties": {
        "buyer": {"type": "string"}, "situation": {"type": "string", "maxLength": 60},
        "our_markup": {"type": "number"}, "winning_markup": {"type": "number"},
        "action": {"type": "string", "maxLength": 120}}}}}}


def buyer_table(state: dict, ctx: Context) -> list[dict]:
    """One row per buyer with an RFQ now: type, our markup this round, our last result, competitors' sold range."""
    markups = rfq_markups(state, ctx)
    market = market_view(state, ctx)
    rows, seen = [], set()
    for r in state["rfqs"]:
        b = r["buyer"]
        if b in seen:
            continue
        seen.add(b)
        last = our_last(ctx, b)
        mv = market.get(b)
        rows.append({"buyer": b, "type": r["buyer_type"], "our_markup_now": markups[r["rfq_id"]],
                     "our_last": {"markup": last[0], "status": last[1]} if last else None,
                     "competitors_sold": {"min_markup": mv["min"], "max_markup": mv["max"],
                                          "avg_markup": mv["avg"], "offers_per_rfq": mv["offers"]}
                     if mv else None})
    return rows


def llm_plan(state: dict, ctx: Context, cands: list[dict]) -> list[dict] | None:
    rnd = state["round"]
    target = sum(sell_budget(state, ctx).values()) // BAG
    if state["capacity_left_kg"] is not None:
        target = min(target, state["capacity_left_kg"] // BAG)
    time_for_llm = ctx.seconds_left - 5          # keep 5 s to send the offers before the round closes
    if time_for_llm < 3:
        ctx.notes.append("no time for the LLM this round")
        return None
    table = [{"candidate": i, **{k: c[k] for k in ("lot_id", "buyer", "buyer_type", "rfq_bags", "max_bags", "tv", "markup", "p_win")}}
             for i, c in enumerate(cands)]
    u = urgency(state, ctx)
    narrow = narrow_now(state, ctx)
    stock = {l["lot_id"]: l["kg_available"] // BAG for l in state["lots"] if l["kg_available"] >= BAG}
    user = "\n".join([
        f"Round {rnd['round_id']} ({rnd['kind']}), market calendar: {rnd.get('calendar', '?')}. Rounds left: {rounds_left(state, ctx)}.",
        f"Round Capacity: you may offer at most {state['capacity_left_kg'] // BAG if state['capacity_left_kg'] is not None else 'any'} bags in total this round.",
        f"Target: offer about {target} bags in total this round.",
        f"Bags left per lot: {json.dumps(stock)}.",
        f"Sell-out urgency: {u:.2f} (0 = on schedule, 1 = far behind: offer every bag, favour high p_win).",
        ("Narrow lots, buyer present now - offer these first, all bags: "
         + ", ".join(f"{i} (chance per round a buyer for it is present {p})" for i, p in narrow.items()))
        if narrow else "Narrow lots: none this round.",
        "Buyer table (this game; markup = price / our comparable TV):",
        json.dumps(buyer_table(state, ctx), ensure_ascii=False),
        "Recent results:", ctx.memory.summary() if ctx.memory else "none",
        "Candidates (lot x RFQ, already spec-checked and filtered by the sales policy):",
        json.dumps(table, ensure_ascii=False),
        f"Pick offers. Use each candidate at most once; bags <= max_bags; markup <= candidate markup. Do not set markup below {LLM_MIN_MARKUP:.2f} unless that candidate's markup is already lower.",
        "For each buyer, compare our_last markup/status with competitors_sold.max_markup and avg_markup; use our_last as the starting point and adjust based on the observed outcome.",
    ])
    t0 = time.time()
    answer = ctx.llm.ask_json(SYSTEM, user, SCHEMA, timeout=time_for_llm)
    # main.py writes this to logs/llm.jsonl: the exact prompt and answer of every LLM call
    ctx.llm_io = {"round": rnd["round_id"], "system": SYSTEM, "user": user, "answer": answer,
                  "error": ctx.llm.last_error, "seconds": round(time.time() - t0, 1)}
    if answer is None or not isinstance(answer.get("offers"), list):
        ctx.notes.append(f"llm failed: {ctx.llm.last_error}")
        return None
    plan = []
    for o in answer["offers"]:
        try:
            i, bags, markup = int(o["candidate"]), int(o["bags"]), float(o["markup"])
            if i < 0 or not math.isfinite(markup):          # cands[-1] is valid Python; NaN/inf slip past min/max
                raise ValueError("bad candidate or markup")
            c = cands[i]
            # The LLM may make measured discounts, but cannot push below 0.89 x TV
            # unless the deterministic rule candidate itself is already below that.
            if is_first_round(state) and c["buyer_type"] == "INSTANT":
                markup = c["markup"]      # round-one INSTANT price is the appraisal; do not discount yet
            else:
                markup = min(c["markup"], max(markup, min(LLM_MIN_MARKUP, c["markup"])))
            plan.append({"lot_id": c["lot_id"], "rfq_id": c["rfq_id"], "bags": bags, "markup": markup,
                         "longshot": c.get("longshot", False)})
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            ctx.notes.append(f"llm bad offer ignored: {o}")
    ctx.notes.append(f"llm reason: {str(answer.get('reason'))[:200]}")
    for b in answer.get("buyers") or []:
        try:
            ours = f" เราเสนอ x{float(b['our_markup']):.3f}" if b.get("our_markup") is not None else ""
            wins = f" คู่แข่งชนะที่ x{float(b['winning_markup']):.3f}" if b.get("winning_markup") is not None else ""
            ctx.llm_views[str(b["buyer"])] = f"{b['situation']}{ours}{wins} -> {b['action']}"
        except (KeyError, TypeError, ValueError):
            pass
    return plan


# ---------------------------------------------------------------- safety net

def guard(state: dict, ctx: Context, plan: list[dict]) -> list[dict]:
    """Turn a plan into offers the market will accept. Never trust the LLM's numbers as they are."""
    lots = {l["lot_id"]: l for l in state["lots"]}
    rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
    markups = rfq_markups(state, ctx)
    room = state["capacity_left_kg"] if state["capacity_left_kg"] is not None else 10**9
    lot_left = {i: l["kg_available"] for i, l in lots.items()}
    rfq_left = {i: r["kg"] for i, r in rfqs.items()}
    offers, seen = [], set()
    for p in plan:
        lot, rfq = lots.get(p["lot_id"]), rfqs.get(p["rfq_id"])
        # long shot: an INSTANT-only lot that fails every current spec can be probed early at its rule price.
        longshot = (p.get("longshot") and lot is not None and rfq is not None and rfq["buyer_type"] == "INSTANT"
                    and not any(spec_ok(lot["sample"], r["spec"]) for r in state["rfqs"]))
        if (p["lot_id"], p["rfq_id"]) in seen and lot is not None and rfq is not None:
            # same lot × RFQ again (e.g. the LLM asked 3 bags, the rules 8): keep the first offer's price and top
            # its volume up to this plan's amount instead of throwing the extra bags away
            first = next(o for o in offers if o["lot_id"] == p["lot_id"] and o["rfq_id"] == p["rfq_id"])
            extra = min(p["bags"] * BAG - first["kg"], lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
            if extra >= BAG:
                first["kg"] += extra
                lot_left[lot["lot_id"]] -= extra
                rfq_left[rfq["rfq_id"]] -= extra
                room -= extra
            continue
        if lot is None or rfq is None or not (longshot or spec_ok(lot["sample"], rfq["spec"])):
            ctx.notes.append(f"guard dropped {p}")
            continue
        if rfq["buyer_type"] == "INSTANT" and premium_capable(lot, state, ctx):
            ctx.notes.append(f"guard dropped {lot['lot_id']} -> {rfq['rfq_id']}: premium outlet exists")
            continue
        kg = min(p["bags"] * BAG, lot_left[lot["lot_id"]], rfq_left[rfq["rfq_id"]], room) // BAG * BAG
        if kg < BAG:
            continue
        if longshot:
            markup = 1.00 if is_first_round(state) else max(LONGSHOT_MARKUP, p.get("markup", LONGSHOT_MARKUP))
            if not is_first_round(state):
                capped = lot_markup(state, ctx, rfq, lot["lot_id"], markup)
                if capped is None or capped < LONGSHOT_MARKUP:
                    ctx.notes.append(f"guard dropped {p['lot_id']} -> {p['rfq_id']}: INSTANT cap below long-shot floor")
                    continue
                markup = capped
        elif is_first_round(state) and rfq["buyer_type"] == "INSTANT":
            markup = 1.00                 # first-round probe: exactly our appraisal, without an early discount
        else:
            markup = min(max(p["markup"], MIN_MARKUP), MAX_MARKUP, markups[rfq["rfq_id"]])
            capped = lot_markup(state, ctx, rfq, lot["lot_id"], markup)   # INSTANT baht cap per lot
            if capped is None:
                ctx.notes.append(f"guard dropped {p['lot_id']} -> {p['rfq_id']}: over the INSTANT baht cap")
                continue
            markup = capped
        offers.append({"rfq_id": rfq["rfq_id"], "lot_id": lot["lot_id"], "kg": kg,
                       "price_per_kg": round(markup * ctx.tv[lot["lot_id"]], 2)})
        seen.add((p["lot_id"], p["rfq_id"]))
        lot_left[lot["lot_id"]] -= kg
        rfq_left[rfq["rfq_id"]] -= kg
        room -= kg
    return offers


STATUS_TH = {"ACCEPTED": "ขายได้หมด", "PARTIAL": "ขายได้บางส่วน", "OUTBID": "แพ้ราคาคู่แข่ง",
             "ABOVE_LIMIT": "เกินเพดานร้าน", "SPEC_MISMATCH": "สเปกจริงไม่ผ่าน", "CANCELLED": "ยกเลิก"}


def buyer_report(state: dict, ctx: Context, offers: list[dict], cands: list[dict] | None = None) -> None:
    """One readable Thai line per buyer, every round (with or without the LLM), into ctx.notes as 'view: ...':
    what happened last time, what competitors sold at, what we do now and why. The LLM's note is appended."""
    markups = rfq_markups(state, ctx)
    market = market_view(state, ctx)
    seen = set()
    for r in state["rfqs"]:
        b = r["buyer"]
        if b in seen:
            continue
        seen.add(b)
        mine = [o for o in offers if any(x["rfq_id"] == o["rfq_id"] and x["buyer"] == b for x in state["rfqs"])]
        n_bags = sum(o["kg"] // BAG for o in mine)
        # the markup actually sent (the LLM may have asked less than the rules); else the rules' markup
        now = (sum(o["price_per_kg"] / ctx.tv[o["lot_id"]] * o["kg"] for o in mine) / sum(o["kg"] for o in mine)
               if mine else markups[r["rfq_id"]])
        last = our_last(ctx, b)
        mv = market.get(b)
        before = f"รอบก่อน{STATUS_TH.get(last[1], last[1])} ที่ x{last[0]:.3f}" if last else "ยังไม่เคยเสนอร้านนี้"
        comp = (f"คู่แข่งขายได้ x{mv['min']:.2f}-{mv['max']:.2f} เฉลี่ย {mv['avg']:.2f} ({mv['offers']:g} ข้อเสนอ/RFQ)"
                if mv else "ยังไม่มีข้อมูลราคาคู่แข่ง")
        if last and now < last[0] - 0.002:
            move = f"ลดเป็น x{now:.3f}"
        elif last and now > last[0] + 0.002:
            move = f"ขึ้นเป็น x{now:.3f}"
        else:
            move = f"ตั้ง x{now:.3f}"
        can = cands is None or any(c["buyer"] == b for c in cands)
        if n_bags:
            plan = f"{move}, เสนอ {n_bags} กระสอบ"
        elif not can:
            plan = "ไม่ได้เสนอ: ไม่มีล็อตที่ผ่านสเปกร้านนี้ (หรือเก็บไว้ให้ร้านที่ดีกว่า)"
        else:
            plan = f"{move} แต่ไม่ได้เสนอ (โควตารอบนี้ใช้กับร้านที่คุ้มกว่า)"
        line = f"view: {b} [{r['buyer_type']}] {before} · {comp} · รอบนี้ {plan}"
        if b in ctx.llm_views:
            line += f" · LLM: {ctx.llm_views[b]}"
        ctx.notes.append(line)


def limit_llm_plan(state: dict, ctx: Context, plan: list[dict], used: list[dict] | None = None) -> list[dict]:
    """Keep the LLM inside the rules' volume plan: each lot at most the bags the rules give it this round, each
    RFQ at most MAX_RFQ_SHARE of the round's bags. The LLM still picks which RFQs and the price; the rules fill
    the rest. Stops it from piling every bag of one lot onto one RFQ."""
    target = dict(ctx.lot_target or {})
    for u in used or []:                       # bags already given by narrow reservation / lot coverage
        target[u["lot_id"]] = target.get(u["lot_id"], 0) - u["bags"]
    cap = state.get("capacity_left_kg")
    total = cap // BAG if cap is not None else sum(target.values()) or 10**6
    per_rfq = max(1, math.ceil(total * MAX_RFQ_SHARE))
    on_rfq: dict[str, int] = {}
    out = []
    for p in plan:
        bags = min(p["bags"], target.get(p["lot_id"], 0), per_rfq - on_rfq.get(p["rfq_id"], 0))
        if bags <= 0:
            ctx.notes.append(f"llm offer trimmed away (spread rule): {p['lot_id']} -> {p['rfq_id']} {p['bags']} bags")
            continue
        if bags < p["bags"]:
            ctx.notes.append(f"llm offer trimmed {p['bags']} -> {bags} bags: {p['lot_id']} -> {p['rfq_id']}")
        out.append({**p, "bags": bags})
        target[p["lot_id"]] = target.get(p["lot_id"], 0) - bags
        on_rfq[p["rfq_id"]] = on_rfq.get(p["rfq_id"], 0) + bags
    return out


def decide(state: dict, ctx: Context) -> tuple[list[dict], str]:
    """Returns (offers, source) where source is "llm" or "rules"."""
    if ctx.memory is not None and hasattr(ctx.memory, "remember_specs"):
        ctx.memory.remember_specs(state["rfqs"])
    cands = candidates(state, ctx)
    if not cands:
        return [], "none"
    plan = llm_plan(state, ctx, cands) if ctx.llm else None
    if plan:
        rules = rule_plan(state, ctx, cands)
        # Rules own the quantity and lot allocation: they cover the eligible lots and fill
        # the feasible round capacity. The LLM may tune prices for the same lot/RFQ pairs,
        # but must not change the volume plan (which previously caused unused capacity).
        llm_prices = {(p["lot_id"], p["rfq_id"]): p["markup"] for p in plan}
        volume_plan = []
        for p in rules:
            markup = min(p["markup"], llm_prices.get((p["lot_id"], p["rfq_id"]), p["markup"]))
            volume_plan.append({**p, "markup": markup})
        offers = guard(state, ctx, volume_plan)
        n_llm = sum((p["lot_id"], p["rfq_id"]) in llm_prices for p in volume_plan)
        ctx.notes.append(f"LLM price overlay on {n_llm}/{len(volume_plan)} rule volume allocations")
        buyer_report(state, ctx, offers, cands)
        return offers, f"llm+rules ({n_llm} price overlays; rules volume)"
    offers = guard(state, ctx, rule_plan(state, ctx, cands))
    buyer_report(state, ctx, offers, cands)
    return offers, "rules"
