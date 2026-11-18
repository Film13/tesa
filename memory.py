"""What the agent remembers between rounds, saved as JSON so a restart does not forget."""
import json
import statistics
from collections import defaultdict
from pathlib import Path


class Memory:
    PREMIUM_PROBE_STEP_THB = 1.0
    PREMIUM_SAFETY_THB = 1.0

    def __init__(self, path: str, buyer_history_path: str | None = None, game_id: str | None = None):
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.sent: dict = data.get("sent", {})         # round_id -> offers we sent (with markup and buyer type)
        self.rounds: list = data.get("rounds", [])     # results of cleared rounds, oldest first
        self.premium_specs: list = data.get("premium_specs", [])   # SPECIALTY/COMMERCIAL specs seen this game
        self.specialty_specs: list = data.get("specialty_specs", [])   # SPECIALTY specs seen this game
        self.buyer_specs: dict = data.get("buyer_specs", {})   # buyer -> {spec, buyer_type} seen this game
        self.buyer_history_path = Path(buyer_history_path) if buyer_history_path else None
        self.game_id = game_id or self.path.stem
        history = {}
        if self.buyer_history_path and self.buyer_history_path.exists():
            history = json.loads(self.buyer_history_path.read_text(encoding="utf-8"))
        self.buyer_history: dict = history.get("buyers", {})
        for result in self.rounds:
            self._remember_buyer_result(result, save=False)
        self._save_buyer_history()

    @staticmethod
    def _premium_evidence(observations: list[dict]) -> tuple[float | None, float | None]:
        def premium(observation: dict) -> float | None:
            value = observation.get("premium_thb")
            if isinstance(value, (int, float)):
                return float(value)
            price, markup = observation.get("price_per_kg"), observation.get("markup")
            if isinstance(price, (int, float)) and isinstance(markup, (int, float)) and markup > 0:
                return price * (1 - 1 / markup)
            return None

        within_limit = [value for o in observations
                    if (o.get("status") in ("ACCEPTED", "PARTIAL") and o.get("filled_kg", 0) > 0)
                    or o.get("status") == "OUTBID"
                    if (value := premium(o)) is not None]
        above_limit = [value for o in observations if o.get("status") == "ABOVE_LIMIT"
                       if (value := premium(o)) is not None]
        return (max(within_limit) if within_limit else None, min(above_limit) if above_limit else None)

    def buyer_behavior(self, buyer: str) -> dict:
        """How this buyer behaved in past games, as markups (price / our TV): a hint, not a hard limit.
        Medians, not extremes, so one noisy lot does not pin the price for ever."""
        obs = self.buyer_history.get(buyer, {}).get("observations", [])

        def med(values):
            return round(statistics.median(values), 4) if values else None

        def markups(*statuses, filled=False):
            return [o["markup"] for o in obs if isinstance(o.get("markup"), (int, float))
                    and o.get("status") in statuses and (not filled or (o.get("filled_kg") or 0) > 0)]

        return {"accepted_median": med(markups("ACCEPTED", "PARTIAL", filled=True)),
                "above_limit_median": med(markups("ABOVE_LIMIT")),
                "outbid_median": med(markups("OUTBID")),
                "n": len(obs)}

    def remember_specs(self, rfqs: list[dict]) -> None:
        changed = False
        for r in rfqs:
            if r.get("buyer_type") in ("SPECIALTY", "COMMERCIAL") and r.get("spec") not in self.premium_specs:
                self.premium_specs.append(r["spec"])
                changed = True
            if r.get("buyer_type") == "SPECIALTY" and r.get("spec") not in self.specialty_specs:
                self.specialty_specs.append(r["spec"])
                changed = True
            b = r.get("buyer")
            if b and self.buyer_specs.get(b, {}).get("spec") != r.get("spec"):
                self.buyer_specs[b] = {"spec": r.get("spec"), "buyer_type": r.get("buyer_type")}
                changed = True
        if changed:
            self.save()

    def recommended_premium(self, buyer: str, fallback: float) -> float:
        history = self.buyer_history.get(buyer)
        if not history:
            return fallback
        accepted, above_limit = self._premium_evidence(history.get("observations", []))
        if above_limit is not None:
            return above_limit - self.PREMIUM_SAFETY_THB
        if accepted is not None:
            return accepted + self.PREMIUM_PROBE_STEP_THB
        return fallback

    def _save_buyer_history(self) -> None:
        if not self.buyer_history_path:
            return
        for buyer, profile in self.buyer_history.items():
            within_limit, above_limit = self._premium_evidence(profile.get("observations", []))
            profile["ceiling_at_least_premium_thb"] = within_limit
            profile["ceiling_below_premium_thb"] = above_limit
            profile["next_game_premium_thb"] = self.recommended_premium(buyer, 0.0)
            profile["behavior_markup"] = self.buyer_behavior(buyer)
        self.buyer_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.buyer_history_path.write_text(
            json.dumps({"buyers": self.buyer_history}, ensure_ascii=False, indent=1), encoding="utf-8")

    def _remember_buyer_result(self, result: dict, save: bool = True) -> None:
        round_id = result.get("round_id")
        if not round_id:
            return
        market_rows = {row.get("rfq_id"): row for row in result.get("market_summary", [])
                       if row.get("rfq_id")}
        sent = {(offer["rfq_id"], offer["lot_id"]): offer
                for offer in self.sent.get(round_id, [])}
        changed = False

        for offer in result.get("my_offers", []):
            rfq_id, lot_id = offer.get("rfq_id"), offer.get("lot_id")
            sent_offer = sent.get((rfq_id, lot_id), {})
            market_row = market_rows.get(rfq_id, {})
            buyer = market_row.get("buyer") or sent_offer.get("buyer")
            if not buyer or sent_offer.get("spec_longshot"):
                continue
            record_id = f"{self.game_id}:{round_id}:{rfq_id}:{lot_id}"
            profile = self.buyer_history.setdefault(buyer, {
                "buyer_type": market_row.get("buyer_type") or sent_offer.get("buyer_type"),
                "observations": [],
                "market_rounds": [],
            })
            if any(row.get("id") == record_id for row in profile["observations"]):
                continue
            price = offer.get("price_per_kg")
            markup = sent_offer.get("markup")
            if not isinstance(markup, (int, float)) and isinstance(price, (int, float)):
                tv_estimate = sent_offer.get("tv_estimate")
                if isinstance(tv_estimate, (int, float)) and tv_estimate > 0:
                    markup = price / tv_estimate
            if isinstance(markup, (int, float)):
                tv_estimate = sent_offer.get("tv_estimate")
                if not isinstance(tv_estimate, (int, float)) and isinstance(price, (int, float)) and markup > 0:
                    tv_estimate = price / markup
                profile["observations"].append({
                    "id": record_id,
                    "round_id": round_id,
                    "status": offer.get("status"),
                    "markup": round(markup, 4),
                    "price_per_kg": price,
                    "tv_estimate": round(tv_estimate, 2) if isinstance(tv_estimate, (int, float)) else None,
                    "premium_thb": round(price - tv_estimate, 2)
                    if isinstance(price, (int, float)) and isinstance(tv_estimate, (int, float)) else None,
                    "filled_kg": offer.get("filled_kg", 0) or 0,
                })
                changed = True

        for rfq_id, row in market_rows.items():
            buyer = row.get("buyer")
            if not buyer:
                continue
            profile = self.buyer_history.setdefault(buyer, {
                "buyer_type": row.get("buyer_type"),
                "observations": [],
                "market_rounds": [],
            })
            record_id = f"{self.game_id}:{round_id}:{rfq_id}"
            if any(item.get("id") == record_id for item in profile["market_rounds"]):
                continue
            profile["market_rounds"].append({
                "id": record_id,
                "round_id": round_id,
                "wanted_kg": row.get("wanted_kg", 0),
                "bought_kg": row.get("bought_kg", 0),
                "offers": row.get("offers", 0),
                "price_min": row.get("price_min"),
                "price_avg": row.get("price_avg"),
                "price_max": row.get("price_max"),
            })
            changed = True

        if changed and save:
            self._save_buyer_history()

    def import_legacy_files(self, paths: list[Path], team_id: str) -> None:
        current_sent, current_game_id = self.sent, self.game_id
        try:
            for path in paths:
                if path.resolve() == self.path.resolve():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                sent = data.get("sent", {})
                lot_ids = {offer.get("lot_id") for offers in sent.values() for offer in offers}
                if not lot_ids or not all(f"-{team_id}-" in lot_id for lot_id in lot_ids):
                    continue
                self.sent = sent
                self.game_id = path.stem
                for result in data.get("rounds", []):
                    self._remember_buyer_result(result, save=False)
        finally:
            self.sent, self.game_id = current_sent, current_game_id
        self._save_buyer_history()

    def buyer_profiles(self) -> dict[str, dict]:
        profiles = {}

        def profile_for(buyer: str, buyer_type: str | None) -> dict:
            return profiles.setdefault(buyer, {
                "buyer_type": buyer_type,
                "round_ids": set(),
                "wanted_kg": 0,
                "bought_kg": 0,
                "offer_count": 0,
                "market_sold_prices": [],
                "market_reported_averages": [],
                "our_offer_prices": [],
                "our_filled_kg": 0,
                "our_filled_prices": [],
                "within_limit_markups": [],
                "above_limit_markups": [],
                "statuses": defaultdict(int),
            })

        for rnd in self.rounds:
            market_summary = rnd.get("market_summary", [])
            by_rfq = {row["rfq_id"]: row for row in market_summary if row.get("rfq_id")}
            sent = {(offer["rfq_id"], offer["lot_id"]): offer
                    for offer in self.sent.get(rnd.get("round_id"), [])}

            for row in market_summary:
                buyer = row.get("buyer")
                if not buyer:
                    continue
                profile = profile_for(buyer, row.get("buyer_type"))
                profile["round_ids"].add(rnd.get("round_id"))
                profile["wanted_kg"] += row.get("wanted_kg", 0) or 0
                profile["bought_kg"] += row.get("bought_kg", 0) or 0
                offer_count = row.get("offers", 0) or 0
                profile["offer_count"] += offer_count
                for key in ("price_min", "price_max"):
                    price = row.get(key)
                    if isinstance(price, (int, float)):
                        profile["market_sold_prices"].append(price)
                average = row.get("price_avg")
                if isinstance(average, (int, float)):
                    profile["market_reported_averages"].append(average)

            for offer in rnd.get("my_offers", []):
                row = by_rfq.get(offer.get("rfq_id"), {})
                sent_offer = sent.get((offer.get("rfq_id"), offer.get("lot_id")), {})
                buyer = row.get("buyer") or sent_offer.get("buyer")
                price = offer.get("price_per_kg")
                if not buyer:
                    continue
                profile = profile_for(buyer, row.get("buyer_type") or sent_offer.get("buyer_type"))
                profile["round_ids"].add(rnd.get("round_id"))
                status = offer.get("status", "")
                profile["statuses"][status] += 1
                markup = sent_offer.get("markup")
                if isinstance(price, (int, float)):
                    profile["our_offer_prices"].append(price)
                if isinstance(markup, (int, float)):
                    if ((status in ("ACCEPTED", "PARTIAL") and (offer.get("filled_kg", 0) or 0) > 0)
                            or status == "OUTBID"):
                        profile["within_limit_markups"].append(markup)
                    elif status == "ABOVE_LIMIT":
                        profile["above_limit_markups"].append(markup)

                filled_kg = offer.get("filled_kg", 0) or 0
                if filled_kg > 0 and isinstance(price, (int, float)):
                    profile["our_filled_kg"] += filled_kg
                    profile["our_filled_prices"].append(price)

        return {
            buyer: {
                "buyer_type": profile["buyer_type"],
                "rounds_observed": len(profile["round_ids"]),
                "wanted_kg": profile["wanted_kg"],
                "bought_kg": profile["bought_kg"],
                "offer_count": profile["offer_count"],
                "market_sold_min": min(profile["market_sold_prices"]) if profile["market_sold_prices"] else None,
                "market_sold_avg": round(sum(profile["market_reported_averages"]) /
                                         len(profile["market_reported_averages"]), 2)
                if profile["market_reported_averages"] else None,
                "market_sold_max": max(profile["market_sold_prices"]) if profile["market_sold_prices"] else None,
                "our_offer_min": min(profile["our_offer_prices"]) if profile["our_offer_prices"] else None,
                "our_offer_max": max(profile["our_offer_prices"]) if profile["our_offer_prices"] else None,
                "our_filled_kg": profile["our_filled_kg"],
                "our_filled_price_min": min(profile["our_filled_prices"]) if profile["our_filled_prices"] else None,
                "our_filled_price_max": max(profile["our_filled_prices"]) if profile["our_filled_prices"] else None,
                "ceiling_at_least_markup": max(profile["within_limit_markups"])
                if profile["within_limit_markups"] else None,
                "ceiling_below_markup": min(profile["above_limit_markups"])
                if profile["above_limit_markups"] else None,
                "statuses": dict(profile["statuses"]),
            }
            for buyer, profile in profiles.items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"sent": self.sent, "rounds": self.rounds, "premium_specs": self.premium_specs,
                                         "specialty_specs": self.specialty_specs,
                                         "buyer_specs": self.buyer_specs,
                                         "buyer_profiles": self.buyer_profiles()}, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    def record_sent(self, round_id: str, offers: list[dict], state: dict, tv: dict) -> None:
        rfqs = {r["rfq_id"]: r for r in state["rfqs"]}
        samples = {l["lot_id"]: l.get("sample", {}) for l in state["lots"]}

        def sample_fails(lot_id: str, spec: dict | None) -> bool:
            """A long-shot offer (sample outside the spec): its price says nothing about the market."""
            s = samples.get(lot_id) or {}
            if not spec or not s:
                return False
            return (s.get("defect_pct", 0) > spec["max_defect_pct"] or s.get("moisture_pct", 0) > spec["max_moisture_pct"]
                    or (spec.get("min_screen") is not None and s.get("screen", 99) < spec["min_screen"]))

        self.sent[round_id] = [{**o, "buyer": rfqs[o["rfq_id"]]["buyer"],
                                "buyer_type": rfqs[o["rfq_id"]]["buyer_type"],
                                "spec": rfqs[o["rfq_id"]].get("spec"),
                                "spec_longshot": sample_fails(o["lot_id"], rfqs[o["rfq_id"]].get("spec")),
                                "tv_estimate": tv[o["lot_id"]],
                                "markup": round(o["price_per_kg"] / tv[o["lot_id"]], 3)} for o in offers]
        self.save()

    def record_results(self, results: dict | None) -> None:
        if not results or not results.get("round_id") or any(r["round_id"] == results["round_id"] for r in self.rounds):
            return
        self.rounds.append(results)
        self._remember_buyer_result(results)
        self.save()

    def summary(self, last: int = 3) -> str:
        """A few short lines for the LLM prompt."""
        lines = []
        for rnd in self.rounds[-last:]:
            sent = {(o["rfq_id"], o["lot_id"]): o for o in self.sent.get(rnd["round_id"], [])}
            mine = []
            for o in rnd["my_offers"]:
                s = sent.get((o["rfq_id"], o["lot_id"]), {})
                mine.append(f"{s.get('buyer_type', '?')} x{s.get('markup', '?')} {o['status']}")
            by_type = defaultdict(lambda: [0, 0])
            market_summary = rnd.get("market_summary", [])
            for row in market_summary:
                by_type[row["buyer_type"]][0] += row["wanted_kg"]
                by_type[row["buyer_type"]][1] += row["bought_kg"]
            market = ", ".join(f"{t} bought {b * 100 // max(w, 1)}% of wanted" for t, (w, b) in by_type.items())
            prices = "; ".join(
                f"{row['buyer_type']} {row['rfq_id']} raw ฿/kg min/avg/max "
                f"{row.get('price_min', '?')}/{row.get('price_avg', '?')}/{row.get('price_max', '?')}"
                for row in market_summary
            )
            lines.append(f"{rnd['round_id']}: my offers [{'; '.join(mine) or 'none'}]; "
                         f"market: {market or 'no demand data'}; price ranges: {prices or 'none'}")
        buyer_lines = []
        for buyer, profile in self.buyer_history.items():
            b = self.buyer_behavior(buyer)
            buyer_lines.append(f"{buyer} ({profile.get('buyer_type', '?')}): markup medians sold {b['accepted_median']}, "
                               f"OUTBID {b['outbid_median']}, ABOVE_LIMIT {b['above_limit_median']}")
        if buyer_lines:
            lines.append("Buyer behaviour in past games (markup = price / our TV; a hint, not a limit): "
                         + "; ".join(buyer_lines))
        return "\n".join(lines) or "none yet"
