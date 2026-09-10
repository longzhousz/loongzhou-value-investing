"""用 pandas 对已保存真实快照独立复算；不请求网络。"""
import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ashare_value.storage import Store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="离线独立复核已保存真实样本的窗口分位、价格和质量")
    parser.add_argument("--data-dir", default="exports/value-investing")
    parser.add_argument("--snapshot")
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        snapshot = store.get(args.snapshot)
        assert snapshot["kind"] == "live", "本验证需要真实快照"
        checks = []
        rules = snapshot["rules"]
        candidates = [r for r in snapshot["results"] if r["status"] == "候选"]
        assert len(candidates) <= rules["maximum_candidates"] <= 20
        exchanges = rules.get("exchanges")
        if exchanges:
            assert all(r["code"].split(".")[0] in exchanges for r in snapshot["results"])
        by_code = {r["code"]: r for r in snapshot["results"]}
        peer_checks = []
        target_checks = []
        for result in snapshot["results"]:
            observation = store.observation(snapshot["id"], result["code"])
            if not observation["prices"]:
                continue
            frame = pd.DataFrame(observation["prices"]).set_index("date").sort_index()
            if result.get("price") is not None:
                assert math.isclose(result["price"], frame.loc[result["as_of"], "raw_close"])
            for years, window in result["windows"].items():
                if not window["valid"]:
                    continue
                data = frame.loc[window["start"]:window["end"]]
                assert len(data) == window["expected_days"]
                data = data[data["trading"]]
                prices = data["adjusted_close"]
                spread = prices.max()-prices.min()
                position = (prices.iloc[-1]-prices.min())/spread if spread > 0 else .5
                assert math.isclose(position, window["price_position"], abs_tol=1e-12)
                for key in ("pe", "pb"):
                    # pandas average rank is 1-based; subtract .5 to get mid-CDF.
                    independent = (data[key].rank(method="average").iloc[-1]-.5)/len(data)
                    assert math.isclose(independent, window[key+"_percentile"], abs_tol=1e-12)
                checks.append({"code":result["code"],"window_years":int(years),"traded_days":len(data),"matched":True})
            if result["quality"].get("passed"):
                financial = pd.DataFrame(observation["financials"]).sort_values("period")
                annual = financial[financial["period"].str.endswith("12-31")].tail(5)
                assert math.isclose(annual["roe"].median(), result["quality"]["median_roe"], abs_tol=1e-12)
                if annual["net_profit"].sum() > 0:
                    assert math.isclose(annual["ocf"].sum()/annual["net_profit"].sum(), result["quality"]["cash_conversion"], abs_tol=1e-12)
                else:
                    assert result["quality"]["cash_conversion"] is None
                if rules.get("quality_mode") == "six_targets":
                    q = result["quality"]
                    last = annual.iloc[-1]
                    market = observation.get("target_market", {})
                    values = [result["pe"], market.get("market_cap"), last.get("gross_margin"), last.get("net_margin"), market.get("dividend_yield"), last["roe"]]
                    t = rules["quality_targets"]
                    limits = [t[k] for k in ("pe_max", "market_cap_min", "gross_margin_min", "net_margin_min", "dividend_yield_min", "annual_roe_min")]
                    known = [v is not None and math.isfinite(v) for v in values]
                    met = [ok and (0 < v <= limit if i == 0 else v > limit) for i, (v, limit, ok) in enumerate(zip(values, limits, known))]
                    assert q["target_count"] == sum(met) and q["target_known_count"] == sum(known)
                    assert math.isclose(q["score"], 100*sum(met)/6, abs_tol=1e-12)
                    for target, ok, hit in zip(q["targets"], known, met):
                        assert target["status"] == ("待核查" if not ok else "达标" if hit else "未达标")
                    if market.get("market_cap") is not None:
                        raw_quote = market["raw"]["quote"]["result"]["data"][0]
                        assert raw_quote["TRADE_DATE"][:10] == result["as_of"]
                        assert math.isclose(raw_quote["TOTAL_MARKET_CAP"], market["market_cap"])
                    if market.get("dividend_yield") is not None:
                        start = (pd.Timestamp(result["as_of"])-pd.DateOffset(years=1)).strftime("%Y-%m-%d")
                        dividends = market["raw"]["dividends"]["result"]["data"]
                        cash = sum(row["PRETAX_BONUS_RMB"]/10 for row in dividends if row.get("ASSIGN_PROGRESS") == "实施分配" and start < str(row.get("EX_DIVIDEND_DATE") or "")[:10] <= result["as_of"])
                        assert math.isclose(cash/result["price"], market["dividend_yield"], abs_tol=1e-12)
                    target_checks.append({"code": result["code"], "matched": True, "targets_met": int(sum(met)), "targets_known": int(sum(known))})
        for result in candidates:
            assert result["windows"]["3"]["valid"] and result["quality"]["passed"]
            assert any(w["passed"] for w in result["windows"].values())
            peers = [by_code[code] for code in result["peer"]["codes"]]
            assert len(peers) >= rules["minimum_peers"] and result["code"] not in result["peer"]["codes"]
            for peer in peers:
                assert peer["industry_code"] == result["industry_code"] and peer["as_of"] == result["as_of"]
                if rules.get("require_peer_group") and result["industry_code"] in rules.get("peer_group_industries", [result["industry_code"]]):
                    assert result["peer_group"] and peer["peer_group"] == result["peer_group"]
                    assert result["peer_group"] not in rules.get("excluded_peer_groups", [])
                assert peer["quality"]["passed"] and peer["quality"]["peer_period"] == result["quality"]["peer_period"]
                assert abs(peer["quality"]["median_roe"]-result["quality"]["median_roe"]) <= rules["peer_roe_distance"]
                assert abs(peer["quality"]["cash_conversion"]-result["quality"]["cash_conversion"]) <= rules["peer_cash_conversion_distance"]
            for key in ("pe", "pb"):
                values = pd.Series([peer[key] for peer in peers])
                assert (values > 0).all() and result[key] > 0
                independent = ((values < result[key]).sum()+.5*(values == result[key]).sum())/len(values)
                assert math.isclose(independent, result["peer"][key+"_percentile"], abs_tol=1e-12)
                assert independent <= rules["peer_percentile_max"]
            peer_checks.append({"code": result["code"], "peer_count": len(peers), "matched": True})
            if rules.get("quality_mode") == "six_targets":
                assert result["score"] == round(result["quality"]["target_count"]*15 + result["secondary_score"]*.099, 2)
        if rules.get("quality_mode") == "six_targets":
            counts = [r["quality"]["target_count"] for r in sorted(candidates, key=lambda r:r["rank"])]
            assert counts == sorted(counts, reverse=True)
        value = {"snapshot":snapshot["id"],"verified_at":datetime.now(timezone.utc).isoformat(),"checked_windows":len(checks),
                 "checks":checks,"peer_checks":peer_checks,"target_checks":target_checks,"coverage":snapshot["coverage"],"candidate_codes":[r["code"] for r in candidates]}
        folder = Path(args.data_dir)/"validation"
        folder.mkdir(exist_ok=True)
        target = folder / (snapshot["id"]+"-"+datetime.now(timezone.utc).strftime("%H%M%S%f")+".json")
        target.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"全部通过":True,"独立复算窗口数":len(checks),"候选同行比较复核数":len(peer_checks),"目标计分复核数":len(target_checks),"验证记录":str(target)},ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()
