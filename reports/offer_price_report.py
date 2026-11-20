"""Create a slide-ready price/appraisal view for the five highest-real-PR games."""
import csv
import datetime as dt
import json
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
OUT = ROOT / "reports"
TOP_GAMES = 5
TYPE_ORDER = ["SPECIALTY", "COMMERCIAL", "INSTANT"]
TYPE_LABELS = {"SPECIALTY": "SPECIALTY", "COMMERCIAL": "COMMERCIAL", "INSTANT": "INSTANT"}
TYPE_COLORS = {"SPECIALTY": "#58D6A7", "COMMERCIAL": "#F6B85E", "INSTANT": "#86A9FF"}


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_memory(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def memory_tv_map(memory: dict) -> dict[str, float]:
    return {o["lot_id"]: o["tv_estimate"]
            for offers in memory.get("sent", {}).values() for o in offers
            if o.get("lot_id") and isinstance(o.get("tv_estimate"), (int, float))}


def select_top_games(limit: int = TOP_GAMES) -> list[dict]:
    """Select actual sandbox PR rows and attach their memory files by exact lot TV estimates."""
    summaries = [r for r in load_jsonl(LOGS / "game-summary.jsonl")
                 if r.get("market") == "sandbox" and isinstance(r.get("pr"), (int, float))
                 and r.get("lots")]
    summaries.sort(key=lambda r: r["pr"], reverse=True)
    memories = []
    for path in (LOGS).glob("memory-*.json"):
        memory = load_memory(path)
        if memory:
            memories.append((path, memory, memory_tv_map(memory),
                             dt.datetime.fromtimestamp(path.stat().st_mtime)))

    selected, used = [], set()
    for summary in summaries:
        target_tv = {x["lot_id"]: x.get("tv_est") for x in summary["lots"] if x.get("tv_est") is not None}
        when = dt.datetime.fromisoformat(summary["time"])
        ranked = []
        for path, memory, observed_tv, modified in memories:
            if path in used:
                continue
            matches = sum(1 for lot_id, value in observed_tv.items()
                          if lot_id in target_tv and abs(value - target_tv[lot_id]) < 0.005)
            coverage = matches / max(1, len(target_tv))
            age = abs((modified - when).total_seconds())
            ranked.append((coverage, -age, path, memory, matches))
        if not ranked:
            break
        coverage, _, path, memory, matches = max(ranked, key=lambda x: (x[0], x[1]))
        if coverage < 0.5:
            continue
        used.add(path)
        selected.append({**summary, "memory_path": path, "memory": memory,
                         "tv_match_count": matches, "tv_coverage": coverage})
        if len(selected) >= limit:
            break
    return selected


def collect_offers(games: list[dict]) -> list[dict]:
    """Collect accepted market offers from real rounds of the selected games only."""
    rows, seen = [], set()
    for game in games:
        memory = game["memory"]
        results = {r.get("round_id"): r for r in memory.get("rounds", [])}
        for round_id, submitted in memory.get("sent", {}).items():
            result = results.get(round_id)
            if result and result.get("kind") == "practice":
                continue
            actual = result.get("my_offers", []) if result else []
            actual_ids = {o.get("offer_id") for o in actual if o.get("offer_id")}
            actual_tuples = {(o.get("rfq_id"), o.get("lot_id"), o.get("price_per_kg"), o.get("kg"))
                             for o in actual}
            for offer in submitted:
                has_api_id = bool(offer.get("offer_id"))
                tuple_key = (offer.get("rfq_id"), offer.get("lot_id"),
                             offer.get("price_per_kg"), offer.get("kg"))
                confirmed = (offer.get("offer_id") in actual_ids if has_api_id else tuple_key in actual_tuples)
                if not (has_api_id or confirmed):
                    continue
                tv, price, buyer_type = offer.get("tv_estimate"), offer.get("price_per_kg"), offer.get("buyer_type")
                if not (isinstance(tv, (int, float)) and tv > 0 and isinstance(price, (int, float))
                        and buyer_type in TYPE_ORDER):
                    continue
                key = offer.get("offer_id") or (game["game"], round_id, offer.get("rfq_id"),
                                                  offer.get("lot_id"), price, offer.get("kg"))
                if key in seen:
                    continue
                seen.add(key)
                matched_result = next((o for o in actual
                                       if (offer.get("offer_id") and o.get("offer_id") == offer.get("offer_id"))
                                       or (not offer.get("offer_id") and
                                           (o.get("rfq_id"), o.get("lot_id"), o.get("price_per_kg"), o.get("kg")) == tuple_key)),
                                      None)
                rows.append({
                    "game_id": game["game"], "game_time": game["time"], "game_pr": game["pr"],
                    "round_id": round_id, "buyer": offer.get("buyer", ""), "buyer_type": buyer_type,
                    "lot_id": offer.get("lot_id", ""), "rfq_id": offer.get("rfq_id", ""),
                    "offer_id": offer.get("offer_id", ""), "kg": offer.get("kg"),
                    "price_per_kg": price, "appraised_tv_per_kg": tv,
                    "price_pct_of_appraisal": 100 * price / tv,
                    "result_status": (matched_result or {}).get("status", offer.get("status", "OPEN")),
                })
    return rows


def rounded_box(ax, bounds, color, alpha=1.0, edge=None, lw=0):
    # These tiny helper axes are only used to draw figure-level rounded panels
    # and progress bars. Hide their default 0..1 ticks and spines.
    ax.set_axis_off()
    x, y, w, h = bounds
    patch = FancyBboxPatch((x, y), w, h,
                           boxstyle="round,pad=0.012,rounding_size=0.018",
                           transform=ax.transAxes, facecolor=color, edgecolor=edge or color,
                           linewidth=lw, alpha=alpha, clip_on=False, zorder=-1)
    ax.add_patch(patch)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    games = select_top_games()
    if len(games) < TOP_GAMES:
        raise SystemExit(f"Need {TOP_GAMES} completed sandbox games with real PR and matching offer logs; found {len(games)}")
    rows = collect_offers(games)
    if not rows:
        raise SystemExit("No confirmed real-round offers found for the selected games")

    csv_path = OUT / "offer_price_data.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    plt.rcParams.update({"font.family": "Leelawadee UI", "font.size": 12,
                         "axes.unicode_minus": False, "svg.fonttype": "none"})
    bg, panel, ink, muted = "#101D19", "#192923", "#F3F5EF", "#A8B8AE"
    grid = "#34463E"
    fig = plt.figure(figsize=(16, 9), facecolor=bg)
    fig.text(0.065, 0.94, "TESA TOP GUN RALLY 2026     /     PERFORMANCE SPOTLIGHT",
             color="#58D6A7", fontsize=11, fontweight="bold")
    fig.text(0.065, 0.875, "เกมที่ PR สูง ตั้งราคาอย่างไร?", color=ink, fontsize=29, fontweight="bold")
    fig.text(0.065, 0.827,
             "ราคาเสนอเทียบ TV ประเมิน  ·  เฉพาะ 5 เกมที่มี PR จริงสูงสุด  ·  เฉพาะข้อเสนอในรอบจริง",
             color=muted, fontsize=13)

    # Left chart panel
    rounded_box(fig.add_axes([0.055, 0.17, 0.615, 0.60], frameon=False), (0, 0, 1, 1), panel)
    ax = fig.add_axes([0.085, 0.245, 0.55, 0.435], facecolor=panel)
    datasets = [[r["price_pct_of_appraisal"] for r in rows if r["buyer_type"] == typ]
                for typ in TYPE_ORDER]
    positions = np.arange(1, len(TYPE_ORDER) + 1)
    bp = ax.boxplot(datasets, positions=positions, widths=0.40, patch_artist=True,
                    showfliers=False, whis=(5, 95),
                    medianprops={"color": ink, "linewidth": 2.4},
                    whiskerprops={"color": "#83958A", "linewidth": 1.2},
                    capprops={"color": "#83958A", "linewidth": 1.2})
    rng = np.random.default_rng(2026)
    for pos, data, typ, box in zip(positions, datasets, TYPE_ORDER, bp["boxes"]):
        box.set_facecolor(TYPE_COLORS[typ])
        box.set_edgecolor(TYPE_COLORS[typ])
        box.set_alpha(0.22)
        box.set_linewidth(1.8)
        ax.scatter(rng.normal(pos, 0.065, len(data)), data, s=24, color=TYPE_COLORS[typ],
                   alpha=0.48, linewidths=0, zorder=3)
        median = float(np.median(data))
        ax.text(pos, median - 1.6, f"{median:.1f}%", ha="center", va="top",
                color=TYPE_COLORS[typ], fontsize=12, fontweight="bold")
        ax.text(pos, 119.1, f"n = {len(data)}", ha="center", va="bottom",
                color=muted, fontsize=10, fontweight="bold")

    ax.axhline(100, color=ink, linewidth=2, zorder=2)
    ax.axhline(89, color="#E7A94F", linewidth=1.4, linestyle=(0, (5, 4)), alpha=0.9, zorder=1)
    ax.text(3.48, 100.65, "100%  ราคาประเมิน", color=ink, fontsize=10, ha="right", fontweight="bold")
    ax.text(3.48, 89.55, "89%  ขอบส่วนลด LLM", color="#E7A94F", fontsize=9, ha="right")
    ax.set_xlim(0.55, 3.55)
    ax.set_ylim(84, 122)
    ax.set_xticks(positions, TYPE_ORDER)
    ax.set_yticks([85, 90, 100, 110, 120], ["85%", "90%", "100%", "110%", "120%"])
    ax.set_ylabel("ราคาเสนอ ÷ ราคาประเมิน", color=muted, labelpad=10)
    ax.tick_params(axis="x", length=0, pad=12, colors=ink, labelsize=11)
    ax.tick_params(axis="y", length=0, colors=muted, labelsize=10)
    ax.grid(axis="y", color=grid, linewidth=0.8, alpha=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    fig.text(0.085, 0.718, "ASK PRICE / APPRAISED TV", color="#58D6A7", fontsize=9, fontweight="bold")
    fig.text(0.085, 0.202, "จุด = ข้อเสนอแต่ละรายการ   ·   กล่อง = ช่วง 25–75%   ·   ตัวเลขสี = มัธยฐาน",
             color=muted, fontsize=9)

    # Right performance panel
    rounded_box(fig.add_axes([0.695, 0.17, 0.25, 0.60], frameon=False), (0, 0, 1, 1), panel)
    top_pr_avg = float(np.mean([g["pr"] for g in games]))
    best_pr = float(games[0]["pr"])
    fig.text(0.725, 0.718, "TOP 5  /  REAL PR", color="#58D6A7", fontsize=10, fontweight="bold")
    fig.text(0.725, 0.637, f"{top_pr_avg:.3f}", color=ink, fontsize=34, fontweight="bold")
    fig.text(0.725, 0.603, f"PR เฉลี่ย   ·   best {best_pr:.3f}", color=muted, fontsize=11)
    fig.text(0.725, 0.548, f"{len(rows):,} ข้อเสนอที่ยืนยันแล้ว", color=ink, fontsize=13, fontweight="bold")
    fig.text(0.725, 0.501, "ผลงาน 5 เกมที่ PR สูงสุด", color=muted, fontsize=10)

    list_top, list_bottom = 0.455, 0.235
    step = (list_top - list_bottom) / TOP_GAMES
    for i, game in enumerate(games):
        y = list_top - i * step
        day = dt.datetime.fromisoformat(game["time"]).strftime("%d %b").upper()
        fig.text(0.725, y, f"{i+1:02d}   {day}", color=muted, fontsize=9, fontweight="bold")
        fig.text(0.91, y, f"{game['pr']:.3f}", color=ink, fontsize=12,
                 fontweight="bold", ha="right")
        bar_x, bar_y, bar_w, bar_h = 0.725, y - 0.025, 0.185, 0.008
        rounded_box(fig.add_axes([bar_x, bar_y, bar_w, bar_h], frameon=False), (0, 0, 1, 1), grid)
        fill = max(0.02, min(1.0, (game["pr"] - 0.80) / 0.20))
        rounded_box(fig.add_axes([bar_x, bar_y, bar_w * fill, bar_h], frameon=False),
                    (0, 0, 1, 1), "#58D6A7")

    fig.text(0.065, 0.085,
             "ชุดข้อมูล: Top 5 จาก 17 เกม Sandbox ที่มี PR จริงจาก reveal  ·  ไม่รวมเกมที่มีเฉพาะ PR ประมาณ",
             color=muted, fontsize=9)
    fig.text(0.065, 0.055,
             "PR สูงเป็นผลของทั้งราคา ปริมาณที่ขาย และของเหลือ  ·  เส้น 89% คือแนวป้องกันส่วนลด LLM ปัจจุบัน",
             color=muted, fontsize=9)

    for ext in ("png", "svg"):
        fig.savefig(OUT / f"offer_price_vs_appraisal.{ext}", dpi=220, facecolor=bg)
    ratios = np.array([r["price_pct_of_appraisal"] for r in rows])
    print(f"Selected games: {len(games)} of {len([r for r in load_jsonl(LOGS / 'game-summary.jsonl') if isinstance(r.get('pr'), (int, float))])}")
    print(f"Top-five average PR: {top_pr_avg:.4f}; best PR: {best_pr:.4f}")
    print(f"Accepted real-round offers: {len(rows)}; median price/appraisal: {np.median(ratios):.2f}%")
    for game in games:
        print(f"{game['time']}  PR {game['pr']:.4f}  matched lots {game['tv_match_count']}")
    print(f"Wrote {OUT / 'offer_price_vs_appraisal.png'}")
    print(f"Wrote {OUT / 'offer_price_vs_appraisal.svg'}")
    print(f"Wrote {csv_path}")
    plt.close(fig)


if __name__ == "__main__":
    main()
