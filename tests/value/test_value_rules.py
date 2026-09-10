"""价值选股关键边界；默认纯离线。"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_value.demo import fixture_market
from ashare_value.domain import (
    normalize_profit,
    percentile,
    preliminary,
    quality_metrics,
    rank_market,
    window_metrics,
)
from ashare_value.pipeline import latest_due, load_rules


@pytest.fixture
def rules():
    return load_rules()


@pytest.fixture
def market():
    return fixture_market()


def test_ties_and_real_position(rules, market):
    assert percentile(5, [5, 5, 5]) == 0.5
    obs = market[0]
    w = window_metrics(obs, 3, rules)
    assert w["passed"] and w["price_position"] == 0
    assert w["pe_percentile"] > 0  # separate definitions


def test_one_missing_day_cannot_be_formal(rules, market):
    obs = market[0]
    del obs.prices[-60]
    assert not window_metrics(obs, 3, rules)["valid"]
    result = preliminary(obs, rules)
    assert result["status"] == "数据不足"
    assert "仅三年有效数据" not in result["tags"]
    assert result["confidence"] == "不足" and result["confidence_score"] == 0


def test_incomplete_calendar_cannot_certify_years(rules, market):
    market[0].calendar = market[0].calendar[-300:]
    assert not window_metrics(market[0], 3, rules)["valid"]


def test_three_year_only_never_fabricates_five(rules, market):
    obs = market[0]
    obs.stock.ipo_date = "2022-07-01"
    obs.prices = [r for r in obs.prices if r["date"] >= obs.stock.ipo_date]
    result = preliminary(obs, rules)
    assert result["windows"]["3"]["valid"]
    assert not result["windows"]["5"]["valid"]
    assert result["windows"]["5"]["pe_percentile"] is None
    assert result["confidence"] == "较低"


def test_business_groups_cannot_use_chip_peers_to_make_software_cheap(rules, market):
    for obs in market:
        obs.stock.industry_code = "I65"
        obs.stock.peer_group = "软件开发" if obs is market[0] else "半导体"
    results = rank_market(market, rules)
    software = next(r for r in results if r["code"] == market[0].stock.code)
    assert software["status"] == "数据不足" and software["peer"]["count"] == 0
    assert all(r["status"] == "暂未覆盖" for r in results if r["code"] != market[0].stock.code)


def test_missing_business_group_does_not_fall_back_to_broad_industry(rules, market):
    rules["peer_group_industries"] = ["C15"]
    for obs in market:
        obs.stock.peer_group = ""
    assert all(r["status"] == "暂未覆盖" for r in rank_market(market, rules))


@pytest.mark.parametrize("key,value", [("pe", -10), ("pb", 0), ("pe", float("nan")), ("adjusted_close", None), ("adjustment", "qfq")])
def test_invalid_metrics_are_not_cheap(rules, market, key, value):
    market[0].prices[-15][key] = value
    assert not window_metrics(market[0], 3, rules)["valid"]


def test_windows_cannot_be_mixed(rules, market):
    obs = market[0]
    # Five years: huge old price peak yields low price position, but old valuation is low.
    # Three years: current valuation is low, but current price is the recent high.
    for r in obs.prices:
        if r["date"] < "2023-09-08":
            r.update(adjusted_close=1000, pe=2, pb=0.2)
        else:
            r.update(adjusted_close=10, pe=100, pb=10)
    obs.prices[-1].update(adjusted_close=20, pe=20, pb=2)
    w3, w5 = (window_metrics(obs, y, rules) for y in (3, 5))
    assert w3["pe_percentile"] < .3 and w3["price_position"] == 1
    assert w5["price_position"] < .25 and w5["pe_percentile"] > .3
    assert preliminary(obs, rules)["status"] != "待同行比较"


def test_five_year_pass_even_when_three_price_fails(rules, market):
    obs = market[0]
    for r in obs.prices:
        r.update(adjusted_close=100 if r["date"] < "2023-09-08" else 10, pe=40, pb=6)
    obs.prices[-1].update(adjusted_close=12, pe=8, pb=1)
    p = preliminary(obs, rules)
    assert not p["windows"]["3"]["passed"] and p["windows"]["5"]["passed"]
    assert p["status"] == "待同行比较"


def test_single_period_decline_penalized_not_rejected(rules, market):
    obs = market[0]
    obs.financials[-1]["profit"] = 3e7
    q = quality_metrics(obs, rules)
    assert q["passed"]
    assert q["profit_yoy"] == -.5 and len(q["penalties"]) > 0


def test_sustained_deterioration_rejected(rules, market):
    obs = market[0]
    for row in obs.financials:
        if row["period"] == "2024-12-31":
            row.update(profit=8e7, revenue=8e8)
        if row["period"] == "2025-12-31":
            row.update(profit=6e7, revenue=6e8)
    assert not quality_metrics(obs, rules)["passed"]


def test_financial_futures_not_visible(rules, market):
    obs = market[0]
    for row in obs.financials:
        row["available_at"] = "2027-01-01"
    assert not quality_metrics(obs, rules)["passed"]


def test_poor_quality_peers_cannot_create_opportunity(rules, market):
    for obs in market[1:]:
        for f in obs.financials:
            f["roe"] = .02
    result = next(r for r in rank_market(market, rules) if r["code"] == market[0].stock.code)
    assert result["status"] == "数据不足" and result["peer"]["count"] == 0


def test_peers_without_price_low_still_compared(rules, market):
    for obs in market[1:]:
        obs.prices[-1]["adjusted_close"] = 100
    results = rank_market(market, rules)
    first = next(r for r in results if r["code"] == market[0].stock.code)
    assert first["status"] == "候选" and first["peer"]["count"] == 11


def test_candidates_no_padding_and_max_twenty(rules, market):
    results = rank_market(market, rules)
    assert 1 <= sum(r["status"] == "候选" for r in results) <= 20
    assert all(r["status"] != "候选" for r in rank_market(market[:2], rules))


@pytest.mark.parametrize("industry", ["J66", "B06", "UNKNOWN", "C39"])
def test_uncovered_never_enters(rules, market, industry):
    market[0].stock.industry_code = industry
    assert preliminary(market[0], rules)["status"] == "暂未覆盖"


def test_st_excluded(rules, market):
    market[0].stock.name = "*ST测试"
    assert preliminary(market[0], rules)["status"] == "不入选"


def test_normalization_sign_scenarios_and_real_balance_effect():
    items = [{"status": "validated", "period": "2025-12-31", "after_tax_parent_impact": -20, "conservative_fraction": .5,
              "cash_impact": "现金实际减少20"}, {"status": "pending", "period": "2025-12-31", "after_tax_parent_impact": -999}]
    value = normalize_profit(100, 1000, items, "2025-12-31")
    assert value[0]["normalized_profit"] == 110 and value[1]["normalized_profit"] == 120
    assert value[1]["normalized_pe"] == pytest.approx(1000/120)
    assert value[1]["adjustment_items"][0]["cash_impact"] == "现金实际减少20"
    assert "不参与资格" in value[1]["comparison_status"]


def test_scheduling_beijing_and_catchup():
    assert latest_due(datetime(2026,9,9,19,59,tzinfo=ZoneInfo("Asia/Shanghai"))).startswith("2026-09-05T20:00")
    assert latest_due(datetime(2026,9,9,20,0,tzinfo=ZoneInfo("Asia/Shanghai"))).startswith("2026-09-09T20:00")
    assert latest_due(datetime(2026,9,12,12,0,tzinfo=ZoneInfo("UTC"))).startswith("2026-09-12T20:00")


def test_ttm_cumulative_and_prior_loss_conservatism():
    from ashare_value.domain import normalized_ttm
    rows = [{"period":"2025-12-31","profit":100}, {"period":"2025-06-30","profit":40}, {"period":"2026-06-30","profit":60}]
    adjustment = [{"period":"2025-06-30","status":"validated","after_tax_parent_impact":-20,"conservative_fraction":.5}]
    result = normalized_ttm(rows, "2026-06-30", 1000, adjustment)
    assert result[0]["reported_ttm"] == 120
    assert result[0]["normalized_ttm"] <= result[1]["normalized_ttm"]
    assert result[1]["normalized_ttm"] == 100


def test_nonrecurring_gain_removed_in_both_scenarios():
    result = normalize_profit(100, 1000, [{"status":"validated","period":"2025-12-31","after_tax_parent_impact":30,"conservative_fraction":.5}], "2025-12-31")
    assert [r["normalized_profit"] for r in result] == [70,70]


def test_price_still_reported_when_historical_pe_invalid(rules, market):
    market[0].prices[-5]["pe"] = -1
    result = window_metrics(market[0], 3, rules)
    assert result["price_position"] is not None and result["pb_percentile"] is not None
    assert result["pe_percentile"] is None and not result["valid"]
