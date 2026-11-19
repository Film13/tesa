"""One summary row per game in logs/game-summary.jsonl, so strategy changes can be compared game by game.

    python game_summary.py              # print the table of every game recorded so far
    python game_summary.py --backfill   # add older games rebuilt from logs/decisions.jsonl (approximate PR)

PR = (revenue + salvage_ratio × TV × kg left) ÷ (TV × kg total)
  - "pr"      the market's real PR (sandbox only: /v1/sandbox/reveal after the market closes)
  - "pr_est"  the same formula with OUR TV estimates; works on competition day too (no reveal there)
main.py writes a row automatically when a market closes. RUN_LABEL in .env (e.g. RUN_LABEL=spread all lots)
is stored with the row so you can tell which change a game was played with.
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
SUMMARY = HERE / "logs" / "game-summary.jsonl"
BAG = 60


def code_version() -> dict:
    """Which strategy played: git commit (+ "dirty" if uncommitted edits) and a hash of strategy.py itself."""
    out = {"strategy_sha": hashlib.sha1((HERE / "strategy.py").read_bytes()).hexdigest()[:8]}
    try:
        out["git"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True,
                                    text=True, timeout=5).stdout.strip() or None
        dirty = subprocess.run(["git", "status", "--porcelain", "strategy.py"], cwd=HERE, capture_output=True,
                               text=True, timeout=5).stdout.strip()
        out["git_dirty"] = bool(dirty)
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _by_type(memory) -> dict:
    """buyer_type -> offers, bags offered / sold, average markup of what sold (from this game's memory)."""
    out: dict = {}
    for result in getattr(memory, "rounds", []):
        if result.get("kind") == "practice":           # practice rounds do not count for the score
            continue
        sent = {(o["rfq_id"], o["lot_id"]): o for o in getattr(memory, "sent", {}).get(result.get("round_id"), [])}
        for o in result.get("my_offers", []):
            s = sent.get((o.get("rfq_id"), o.get("lot_id")), {})
            t = s.get("buyer_type") or "?"
            row = out.setdefault(t, {"offers": 0, "bags_offered": 0, "bags_sold": 0, "statuses": {}, "_mk": 0.0})
            row["offers"] += 1
            row["bags_offered"] += (o.get("kg") or 0) // BAG
            sold = (o.get("filled_kg") or 0) // BAG
            row["bags_sold"] += sold
            if sold and isinstance(s.get("markup"), (int, float)):
                row["_mk"] += s["markup"] * sold
            st = o.get("status", "?")
            row["statuses"][st] = row["statuses"].get(st, 0) + 1
    for row in out.values():
        mk = row.pop("_mk")
        row["avg_markup_sold"] = round(mk / row["bags_sold"], 3) if row["bags_sold"] else None
    return out


def summarize(state: dict, tv: dict, memory=None, reveal: dict | None = None, label: str | None = None) -> dict:
    """Build the summary row from the final /v1/state (phase CLOSED), our TV estimates and, in the sandbox, reveal."""
    st = state.get("standings") or {}
    salvage = state.get("salvage_ratio") or 0.8
    lots = []
    for l in state.get("lots", []):
        total, left = l.get("kg_total") or 0, l.get("kg_remaining") or 0
        lots.append({"lot_id": l["lot_id"], "tv_est": tv.get(l["lot_id"]), "kg_total": total,
                     "kg_sold": total - left, "kg_left": left})
    value = sum((x["tv_est"] or 0) * x["kg_total"] for x in lots)
    left_value = sum((x["tv_est"] or 0) * x["kg_left"] for x in lots)
    revenue = st.get("revenue_thb") or 0
    row = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "market": state.get("market"),
        "game": hashlib.sha1(repr(sorted((l["lot_id"], json.dumps(l.get("sample"), sort_keys=True))
                                         for l in state.get("lots", []))).encode()).hexdigest()[:8],
        "label": label or None,
        **code_version(),
        "revenue_thb": revenue,
        "kg_total": st.get("kg_total"), "kg_sold": st.get("kg_sold"), "kg_left": st.get("kg_remaining"),
        "sold_share": round((st.get("kg_sold") or 0) / st["kg_total"], 3) if st.get("kg_total") else None,
        "avg_price_per_kg": st.get("avg_price_per_kg"),
        "pr_est": round((revenue + salvage * left_value) / value, 4) if value else None,
        "pr_basis": "our TV estimate",
        "lots": lots,
        "by_buyer_type": _by_type(memory) if memory is not None else {},
    }
    if reveal:
        sc = reveal.get("score") or {}
        row.update({"pr": sc.get("pr"), "pr_score": sc.get("pr_score"), "appraisal_score": sc.get("appraisal_score"),
                    "total_score": sc.get("total"), "mape": sc.get("mape")})
        true_tv = {x["lot_id"]: x.get("true_value_per_kg") for x in reveal.get("lots", [])}
        for x in lots:
            x["tv_true"] = true_tv.get(x["lot_id"])
    return row


def append(row: dict) -> None:
    """Add the row; a row for the same market + game replaces the old one (e.g. the agent restarted after close)."""
    SUMMARY.parent.mkdir(exist_ok=True)
    rows = load()
    rows = [r for r in rows if not (r.get("market") == row.get("market") and r.get("game") == row.get("game"))]
    rows.append(row)
    SUMMARY.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def load() -> list[dict]:
    if not SUMMARY.exists():
        return []
    return [json.loads(line) for line in SUMMARY.read_text(encoding="utf-8").splitlines() if line.strip()]


def one_line(r: dict) -> str:
    pr = f"PR {r['pr']:.4f}" if r.get("pr") is not None else "PR -     "
    est = f"PR(est) {r['pr_est']:.4f}" if r.get("pr_est") is not None else "PR(est) -     "
    share = f"{r['sold_share']:.0%}" if r.get("sold_share") is not None else "-"
    avg = f"{r['avg_price_per_kg']:.1f}" if r.get("avg_price_per_kg") else "-"
    ver = (r.get("git") or "?") + ("*" if r.get("git_dirty") else "")
    return (f"{r.get('time', '')[:16]}  {r.get('market', ''):<11} {pr}  {est}  ขายได้ {share:>4} "
            f"เหลือ {r.get('kg_left') or 0:>4} kg  ฿/kg {avg:>6}  {ver:<9} {r.get('label') or ''}")


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sold_by_round(tv: dict) -> dict | None:
    """round number -> lot_id -> kg sold, from the memory file of the game with these TV estimates (real rounds)."""
    for p in (HERE / "logs").glob("memory-*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        seen = {o["lot_id"]: o.get("tv_estimate") for offers in d.get("sent", {}).values() for o in offers}
        if not seen or any(tv.get(i) != v for i, v in seen.items()):
            continue
        out: dict = {}
        for r in d.get("rounds", []):
            if r.get("kind") == "practice":
                continue
            n = int(str(r.get("round_id", "R0")).lstrip("Rr") or 0)
            for o in r.get("my_offers", []):
                if o.get("filled_kg"):
                    out.setdefault(n, {})
                    out[n][o["lot_id"]] = out[n].get(o["lot_id"], 0) + o["filled_kg"]
        return out
    return None


def _lot_totals(start: str, end: str, tv: dict, sold: dict) -> dict | None:
    """lot_id -> kg_total: bags left in the game's earliest LLM prompt + what sold in the rounds before it."""
    for row in _jsonl(HERE / "logs" / "llm.jsonl"):
        if not (start <= row.get("time", "") <= end) or not str(row.get("round", "")).startswith("R"):
            continue
        line = next((x for x in str(row.get("user", "")).splitlines() if x.startswith("Bags left per lot:")), None)
        if not line:
            continue
        left = json.loads(line.split(":", 1)[1].strip().rstrip("."))
        n = int(row["round"][1:])
        before = {}
        for k, by_lot in sold.items():
            if k < n:
                for i, kg in by_lot.items():
                    before[i] = before.get(i, 0) + kg
        return {i: left.get(i, 0) * BAG + before.get(i, 0) for i in tv}
    return None


def backfill() -> int:
    """Rebuild rows for games already played, from new_game / market_closed pairs in decisions.jsonl.
    Per-lot stock comes from the earliest LLM prompt of the game (logs/llm.jsonl) and per-lot sales from its
    memory file. When those are missing or do not add up to the market's totals, the row has pr_est = None."""
    events = _jsonl(HERE / "logs" / "decisions.jsonl")
    have = {(r.get("market"), r.get("game")) for r in load()}
    added, seen, game = 0, set(), None
    for e in events:
        if e.get("event") == "new_game":
            game = e
        elif e.get("event") == "market_closed" and game:
            st, tv = e.get("standings") or {}, game.get("tv") or {}
            key = json.dumps([sorted(tv.items()), st], sort_keys=True)
            gid = "bf-" + hashlib.sha1(key.encode()).hexdigest()[:6]
            if key in seen or (game.get("market"), gid) in have or not st.get("kg_total") or not tv:
                continue
            seen.add(key)
            row = {"time": e["time"], "market": game.get("market"), "game": gid, "label": "backfill",
                   "revenue_thb": st["revenue_thb"], "kg_total": st["kg_total"], "kg_sold": st["kg_sold"],
                   "kg_left": st["kg_remaining"], "sold_share": round(st["kg_sold"] / st["kg_total"], 3),
                   "avg_price_per_kg": st.get("avg_price_per_kg"), "pr_est": None,
                   "pr_basis": "not enough per-lot data in the logs", "tv_est": tv}
            sold = _sold_by_round(tv)
            totals = _lot_totals(game["time"], e["time"], tv, sold) if sold is not None else None
            sold_lot = {}
            for by_lot in (sold or {}).values():
                for i, kg in by_lot.items():
                    sold_lot[i] = sold_lot.get(i, 0) + kg
            if totals and sum(totals.values()) == st["kg_total"] and sum(sold_lot.values()) == st["kg_sold"]:
                value = sum(tv[i] * kg for i, kg in totals.items())
                left_value = sum(tv[i] * (kg - sold_lot.get(i, 0)) for i, kg in totals.items())
                row["pr_est"] = round((st["revenue_thb"] + 0.8 * left_value) / value, 4)
                row["pr_basis"] = "our TV estimate (per-lot stock rebuilt from logs)"
                row["lots"] = [{"lot_id": i, "tv_est": tv[i], "kg_total": kg, "kg_sold": sold_lot.get(i, 0),
                                "kg_left": kg - sold_lot.get(i, 0)} for i, kg in sorted(totals.items())]
            append(row)
            added += 1
    return added


def main() -> None:
    if "--backfill" in sys.argv:
        print(f"backfill: added {backfill()} games")
    rows = sorted(load(), key=lambda r: r.get("time", ""))
    if not rows:
        print("no games recorded yet:", SUMMARY)
        return
    for r in rows:
        print(one_line(r))
    for market in sorted({r.get("market") for r in rows}):
        prs = [r.get("pr") if r.get("pr") is not None else r.get("pr_est") for r in rows if r.get("market") == market]
        prs = [p for p in prs if p is not None]
        if prs:
            print(f"\n{market}: {len(prs)} games, PR avg {sum(prs) / len(prs):.4f}, min {min(prs):.4f}, max {max(prs):.4f}"
                  " (real PR where revealed, else estimate)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
