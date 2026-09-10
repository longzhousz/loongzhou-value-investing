"""纯领域规则：仅依赖标准库；不访问网络、数据库或 UI。"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from statistics import median
from typing import Any


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def anniversary(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def percentile(value: float, values: list[float]) -> float:
    """中秩经验分位：平值全部相同为 50%，不能伪装成历史最低。"""
    if not values:
        raise ValueError("分位样本为空")
    return (sum(v < value for v in values) + 0.5 * sum(v == value for v in values)) / len(values)


def needs_peer_group(industry_code, rules):
    return rules.get("require_peer_group", False) and industry_code in rules.get("peer_group_industries", [industry_code])


@dataclass
class Stock:
    code: str
    name: str
    industry: str
    industry_code: str
    ipo_date: str = ""
    active: bool = True
    peer_group: str = ""


@dataclass
class Observation:
    stock: Stock
    as_of: str
    research_date: str = ""
    calendar: list[str] = field(default_factory=list)
    prices: list[dict] = field(default_factory=list)
    financials: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    target_market: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**{**value, "stock": Stock(**value["stock"])})


def window_metrics(obs: Observation, years: int, rules: dict) -> dict:
    end = date.fromisoformat(obs.as_of)
    start = anniversary(end, years)
    expected = sorted(d for d in obs.calendar if start.isoformat() <= d <= obs.as_of)
    result = {"years": years, "start": start.isoformat(), "end": obs.as_of,
              "valid": False, "passed": False, "price_position": None,
              "pe_percentile": None, "pb_percentile": None, "reason": "", "expected_days": len(expected)}
    # Calendar must reach both ends. A recent-only calendar cannot certify a full window.
    if not expected or date.fromisoformat(expected[0]) > start + timedelta(days=15) or expected[-1] != obs.as_of:
        result["reason"] = "交易日历未覆盖完整窗口"
        return result
    if obs.stock.ipo_date and obs.stock.ipo_date > expected[0]:
        result["reason"] = f"上市不足 {years} 年"
        return result
    expected_set = set(expected)
    rows = [r for r in obs.prices if r["date"] in expected_set]
    by_day = {r["date"]: r for r in rows}
    result["observed_days"] = len(by_day)
    if len(by_day) != len(rows) or set(by_day) != set(expected):
        result["reason"] = "历史交易日缺失或重复"
        return result
    traded = [by_day[d] for d in expected if by_day[d].get("trading") is True]
    if len(traded) < years * 200 or by_day[obs.as_of].get("trading") is not True:
        result["reason"] = "有效交易样本不足或当前停牌"
        return result
    result["traded_days"] = len(traded)
    for metric in ["adjusted_close", "raw_close"]:
        if any(number(r.get(metric)) is None or r[metric] <= 0 for r in traded):
            result["reason"] = f"{metric} 含缺失或不适用值，无法确认完整有效窗口"
            return result
    if any(r.get("adjustment") != "hfq" for r in traded):
        result["reason"] = "复权口径不统一"
        return result
    prices = [r["adjusted_close"] for r in traded]
    spread = max(prices) - min(prices)
    position = (prices[-1] - min(prices)) / spread if spread > 0 else 0.5
    result["price_position"] = position
    invalid = []
    for metric in ("pe", "pb"):
        if any(number(r.get(metric)) is None or r[metric] <= 0 for r in traded):
            invalid.append(metric)
        else:
            result[metric+"_percentile"] = percentile(traded[-1][metric], [r[metric] for r in traded])
    if invalid:
        result["reason"] = "/".join(invalid) + " 含缺失或不适用值，无法确认完整有效窗口；其他可算指标单列"
        return result
    pe, pb = result["pe_percentile"], result["pb_percentile"]
    result["valid"] = True
    result["passed"] = position <= rules["price_position_max"] and pe <= rules["valuation_percentile_max"] and pb <= rules["valuation_percentile_max"]
    result["reason"] = "同一窗口价格、PE、PB 同时低位" if result["passed"] else "同一窗口低位条件未同时满足"
    return result


def quality_metrics(obs: Observation, rules: dict) -> dict:
    reasons, penalties, unknown = [], [], []
    research_date = obs.research_date or obs.as_of
    rows = sorted([r for r in obs.financials if r["period"] <= research_date and r.get("available_at", research_date) <= research_date], key=lambda r: r["period"])
    annual = [r for r in rows if r["period"].endswith("12-31")][-5:]
    result = {"passed": False, "reasons": reasons, "penalties": penalties, "unknown": unknown, "annual_count": len(annual), "score": 0.0}
    if len({r["period"] for r in rows}) != len(rows):
        reasons.append("财务期间重复")
    if len(annual) < rules["minimum_annual_reports"]:
        reasons.append("完整年度财务报表不足三年")
        return result
    years = [int(r["period"][:4]) for r in annual]
    if years != list(range(years[0], years[-1] + 1)):
        reasons.append("年度财务报表不连续")
    expected_annual_year = int(research_date[:4]) - (1 if research_date[5:] >= "05-01" else 2)
    if years[-1] < expected_annual_year:
        reasons.append("最近应披露年度的完整年报缺失")
    latest = rows[-1]
    if (date.fromisoformat(obs.as_of) - date.fromisoformat(latest["period"])).days > rules["maximum_financial_age_days"]:
        reasons.append("最新财务报表过旧")
    required = ["profit", "revenue", "roe", "ocf", "debt_ratio", "equity", "net_profit"]
    if any(number(r.get(k)) is None for r in annual + [latest] for k in required):
        reasons.append("盈利、ROE、现金流、负债或净资产数据缺失")
        return result
    if any(r["revenue"] <= 0 or r["equity"] <= 0 or not 0 <= r["debt_ratio"] < 1 for r in annual + [latest]):
        reasons.append("非正收入/净资产或资产负债率异常")
        return result
    profit = [r["profit"] for r in annual]
    if sum(p <= 0 for p in profit) >= 2 or profit[-1] <= 0 or latest["profit"] <= 0:
        reasons.append("长期亏损或最近期间盈利为非正")
    roe = median(r["roe"] for r in annual)
    conversion = sum(r["ocf"] for r in annual) / sum(r["net_profit"] for r in annual) if sum(r["net_profit"] for r in annual) > 0 else None
    if conversion is None:
        unknown.append("多年累计合并净利润非正，现金转化率不适用，不能用于同行比较")
    result.update(median_roe=roe, latest_roe=annual[-1]["roe"], cash_conversion=conversion,
                  debt_ratio=latest["debt_ratio"], latest_period=latest["period"], peer_period=annual[-1]["period"],
                  equity=latest["equity"], annual_profit=profit[-1])
    target_mode = rules.get("quality_mode") == "six_targets"
    if not target_mode and (roe < rules["minimum_median_roe"] or annual[-1]["roe"] < rules["minimum_latest_roe"]):
        reasons.append("长期或最近年度 ROE 未达门槛")
    if conversion is None or conversion < rules["minimum_cash_conversion"] or sum(r["ocf"] > 0 for r in annual) < len(annual) - 1 or all(r["ocf"] <= 0 for r in annual[-2:]):
        if target_mode:
            penalties.append({"reason": "多年现金流支撑偏弱", "points": 10})
        else:
            reasons.append("多年现金流不足以支撑报表盈利")
    if latest["debt_ratio"] > rules["maximum_debt_ratio"]:
        if target_mode:
            penalties.append({"reason": "资产负债率超过65%风险参考线", "points": 10})
        else:
            reasons.append("资产负债率超过门槛")
    annual_declines = [profit[i] < profit[i-1] * 0.90 for i in range(1, len(profit))]
    revenue_declines = [annual[i]["revenue"] < annual[i-1]["revenue"] * 0.95 for i in range(1, len(annual))]
    if len(annual_declines) >= 2 and all(annual_declines[-2:]) and all(revenue_declines[-2:]):
        reasons.append("连续两年收入和盈利显著恶化")
    previous_period = str(int(latest["period"][:4])-1) + latest["period"][4:]
    previous = next((r for r in rows if r["period"] == previous_period), None)
    if previous and number(previous.get("profit")) and previous["profit"] > 0:
        change = latest["profit"] / previous["profit"] - 1
        result["profit_yoy"] = change
        if change < 0:
            penalties.append({"reason": "最近同期间盈利下滑，须核查一次性与持续性因素", "points": rules["decline_penalty"]})
        older_period = str(int(latest["period"][:4])-2) + latest["period"][4:]
        older = next((r for r in rows if r["period"] == older_period), None)
        if older and all(number(r.get("revenue")) is not None and number(r.get("profit")) is not None for r in (older, previous)):
            if older["profit"] > 0 and previous["profit"] < older["profit"]*.90 and change < -.10 and previous["revenue"] < older["revenue"]*.95 and latest["revenue"] < previous["revenue"]*.95:
                reasons.append("连续两次同期间收入和盈利显著恶化")
    else:
        unknown.append("最近同期间盈利同比缺少比较基数")
    if latest["ocf"] < 0:
        penalties.append({"reason": "最近累计经营现金流为负，须核查营运资金与季节性", "points": 5})
    # No single-quarter deterioration rule. Large reported/non-recurring differences await evidence.
    if number(latest.get("deducted_profit")) is not None and abs(latest["profit"] - latest["deducted_profit"]) > abs(latest["profit"]) * 0.30:
        unknown.append("扣非与归母净利润差异超过 30%，待核查异常项目")
        penalties.append({"reason": "非经常损益占比较大", "points": 10})
    if any(not r.get("published_at") for r in rows):
        unknown.append("免费财务摘要缺少准确公告日；仅作采集日当前研究，不用于历史时点回测")
    result["score"] = max(0, min(100, 40 * min(roe / 0.20, 1) + 40 * min(max(conversion or 0, 0) / 1.2, 1) + 20 * (1 - latest["debt_ratio"]) - sum(p["points"] for p in penalties)))
    if target_mode:
        result["risk_adjusted_score"] = result["score"]
        result.update(target_metrics(obs, annual[-1], rules))
        result["score"] = result["target_count"] / 6 * 100
    result["passed"] = not reasons
    return result


def target_metrics(obs, annual, rules):
    """六项等权软目标；未知不算达标，不设最低达标数。"""
    targets = rules["quality_targets"]
    price = max(obs.prices, key=lambda r: r["date"], default={})
    market = obs.target_market if obs.target_market.get("as_of") == obs.as_of else {}
    specs = [
        ("pe", "PE(TTM)", number(price.get("pe")), targets["pe_max"], "0<x<=", obs.as_of),
        ("market_cap", "总市值（元）", number(market.get("market_cap")), targets["market_cap_min"], ">", obs.as_of),
        ("gross_margin", "毛利率", number(annual.get("gross_margin")), targets["gross_margin_min"], ">", annual["period"]),
        ("net_margin", "净利率", number(annual.get("net_margin")), targets["net_margin_min"], ">", annual["period"]),
        ("dividend_yield", "税前现金股息率（近12个月已除息）", number(market.get("dividend_yield")), targets["dividend_yield_min"], ">", obs.as_of),
        ("annual_roe", "最近完整年度ROE", number(annual.get("roe")), targets["annual_roe_min"], ">", annual["period"]),
    ]
    items = []
    for key, label, value, threshold, operator, period in specs:
        met = value is not None and (0 < value <= threshold if operator == "0<x<=" else value > threshold)
        items.append({"metric": key, "label": label, "value": value, "threshold": threshold, "operator": operator,
                      "period": period, "status": "待核查" if value is None else "达标" if met else "未达标"})
    return {"targets": items, "target_count": sum(i["status"] == "达标" for i in items),
            "target_known_count": sum(i["status"] != "待核查" for i in items),
            "target_period": annual["period"], **{key: value for key, _, value, _, _, _ in specs}}


def preliminary(obs: Observation, rules: dict) -> dict:
    s = obs.stock
    result = {"code": s.code, "name": s.name, "industry": s.industry, "industry_code": s.industry_code, "peer_group": s.peer_group,
              "as_of": obs.as_of, "status": "不入选", "reasons": [], "tags": [], "risks": [], "unknown": [],
              "penalties": [], "windows": {}, "quality": {}, "peer": {}, "score": None,
              "confidence": "不足", "confidence_score": 0, "sources": obs.sources, "normalized": [], "events": []}
    if s.industry_code not in rules["supported_industry_codes"] or s.peer_group in rules.get("excluded_peer_groups", []):
        result.update(status="暂未覆盖", reasons=["基础名录或行业资料缺失，暂未覆盖" if s.industry_code == "UNKNOWN" else "金融、强周期或尚未验证专门行业规则"])
        return result
    if needs_peer_group(s.industry_code, rules) and not s.peer_group:
        result.update(status="暂未覆盖", reasons=["行业大类混含芯片等不同业务，可靠细分资料缺失，暂未覆盖"])
        return result
    if not needs_peer_group(s.industry_code, rules):
        result["peer_group"] = s.industry + "（大类）"
    if not s.active or "ST" in s.name.upper() or "退" in s.name:
        result["reasons"].append("ST、退市风险或非正常上市状态")
        return result
    if obs.errors:
        result["unknown"].extend(obs.errors)
    latest = sorted(obs.prices, key=lambda r: r["date"])[-1] if obs.prices else None
    if not latest or latest["date"] != obs.as_of or latest.get("is_st") is not False:
        result.update(status="数据不足", reasons=["最新行情缺失、日期不一致或无法确认非 ST"])
        return result
    result.update(price=latest.get("raw_close"), pe=latest.get("pe"), pb=latest.get("pb"))
    if latest.get("valuation_basis") != "provider_reported_ttm_mrq":
        result.update(status="数据不足", reasons=["历史估值口径未经确认"])
        return result
    result["windows"] = {str(y): window_metrics(obs, y, rules) for y in (3, 5)}
    result["quality"] = quality_metrics(obs, rules)
    result["unknown"].extend(result["quality"]["unknown"])
    if rules.get("quality_mode") == "six_targets" and "targets" in result["quality"]:
        q = result["quality"]
        result["tags"].append(f"质量目标 {q['target_count']}/6 达标")
        result["unknown"].extend(f"质量目标待核查：{t['label']}" for t in q["targets"] if t["status"] == "待核查")
        result["unknown"].extend(obs.target_market.get("errors", []))
    result["penalties"].extend(result["quality"]["penalties"])
    result["reasons"].extend(result["quality"]["reasons"])
    valid = [w for w in result["windows"].values() if w["valid"]]
    passed = [w for w in valid if w["passed"]]
    result["coverage_years"] = round((date.fromisoformat(obs.as_of)-date.fromisoformat(min(r["date"] for r in obs.prices))).days/365.2425, 2)
    if not result["windows"]["3"]["valid"]:
        result["status"] = "数据不足"
        result["reasons"].append("未取得完整有效三年历史")
    elif not passed:
        result["reasons"].append("三年和五年窗口均未同时满足价格及估值低位")
    elif result["quality"]["passed"]:
        result["status"] = "待同行比较"
        result["tags"].extend(f"{w['years']} 年窗口通过" for w in passed)
    result["confidence_score"] = 80 if result["windows"]["5"]["valid"] else 65
    if result["windows"]["3"]["valid"] and not result["windows"]["5"]["valid"]:
        result["penalties"].append({"reason": "五年窗口不可评估，仅评估三年", "points": rules["short_history_penalty"]})
        result["tags"].append("仅三年有效数据")
    if result["unknown"]:
        result["confidence_score"] -= 10
    result["confidence"] = "中等" if result["confidence_score"] >= 70 else "较低"
    if not result["windows"]["3"]["valid"]:
        result["confidence"], result["confidence_score"] = "不足", 0
    result["risks"] = [p["reason"] for p in result["penalties"]]
    result["unknown"].append("尚未全面核实审计意见、关联交易、担保诉讼和资产可回收性；不代表已排除所有价值陷阱")
    return result


def rank_market(observations: list[Observation], rules: dict) -> list[dict]:
    results = [preliminary(obs, rules) for obs in observations]
    for item in results:
        if item["status"] != "待同行比较":
            continue
        if needs_peer_group(item["industry_code"], rules) and not item["peer_group"]:
            item.update(status="数据不足", reasons=["可靠同行细分行业缺失，不能确认相对便宜"])
            continue
        q = item["quality"]
        if number(q.get("cash_conversion")) is None:
            item.update(status="数据不足", reasons=["累计合并净利润非正，现金转化率不适用，不能确认同行可比性"])
            continue
        peers = [p for p in results if p["code"] != item["code"] and p["industry_code"] == item["industry_code"]
                 and (not needs_peer_group(item["industry_code"], rules) or p["peer_group"] == item["peer_group"])
                 and p["as_of"] == item["as_of"] and p["quality"].get("passed") and p["quality"].get("peer_period") == q["peer_period"]
                 and number(p.get("pe")) is not None and p["pe"] > 0 and number(p.get("pb")) is not None and p["pb"] > 0
                 and number(p["quality"].get("cash_conversion")) is not None
                 and abs(p["quality"]["median_roe"] - q["median_roe"]) <= rules["peer_roe_distance"]
                 and abs(p["quality"]["cash_conversion"] - q["cash_conversion"]) <= rules["peer_cash_conversion_distance"]]
        item["peer"] = {"count": len(peers), "codes": [p["code"] for p in peers], "basis": ("同东方财富细分行业、" if needs_peer_group(item["industry_code"], rules) else "同大类行业、") + "同交易日、相同最新年报期间、质量合格、ROE及现金转化率接近；均为报表口径"}
        item["peer"]["members"] = [{"code": p["code"], "name": p["name"], "pe": p["pe"], "pb": p["pb"], "median_roe": p["quality"]["median_roe"], "cash_conversion": p["quality"]["cash_conversion"]} for p in peers]
        item["unknown"].append("同行已按披露行业及财务质量筛选，同组内产品结构与商业模式差异仍需核查")
        if len(peers) < rules["minimum_peers"]:
            item.update(status="数据不足", reasons=["质量可比同行不足，不能确认相对便宜"])
            continue
        pe = percentile(item["pe"], [p["pe"] for p in peers])
        pb = percentile(item["pb"], [p["pb"] for p in peers])
        item["peer"].update(pe_percentile=pe, pb_percentile=pb)
        if max(pe, pb) > rules["peer_percentile_max"]:
            item.update(status="不入选", reasons=["相对质量可比同行的 PE 或 PB 偏贵"])
            continue
        window = min((w for w in item["windows"].values() if w["passed"]), key=lambda w: w["pe_percentile"] + w["pb_percentile"] + w["price_position"])
        cheapness = 100 * (1-(window["price_position"] + window["pe_percentile"] + window["pb_percentile"] + pe + pb)/5)
        score = rules["quality_weight"] * q.get("risk_adjusted_score", q["score"]) + rules["cheapness_weight"] * cheapness + rules["confidence_weight"] * item["confidence_score"]
        score -= sum(p["points"] for p in item["penalties"] if p["reason"].startswith("五年"))
        if rules.get("quality_mode") == "six_targets":
            item["secondary_score"] = round(max(0, min(100, score)), 4)
            score = 15 * q["target_count"] + .099 * item["secondary_score"]
        item.update(status="候选", score=round(max(0, score), 2), reasons=["基本财务安全检查通过（六项质量目标按达标数计分）" if rules.get("quality_mode") == "six_targets" else "盈利、现金流和负债通过质量门槛", "同一历史窗口的价格、PE、PB 同时低位", "质量可比同行估值不贵"])
    candidates = sorted([p for p in results if p["status"] == "候选"], key=lambda p: (-p["score"], p["code"]))
    for rank, item in enumerate(candidates, 1):
        item["rank"] = rank
        if rank > min(20, rules["maximum_candidates"]):
            item["status"] = "合格未列前20"
    return sorted(results, key=lambda p: (p.get("rank", 999999), p["code"]))


def normalize_profit(reported: float, market_cap: float, items: list[dict], period: str) -> list[dict]:
    """只接受已核验的税后归母损益影响；现金、净资产和负债不做反向恢复。"""
    accepted = [i for i in items if i.get("status") == "validated" and i.get("period") == period]
    result = []
    for scenario in ("conservative", "base"):
        adjustment = sum(i["after_tax_parent_impact"] * (i.get("conservative_fraction", 0) if scenario == "conservative" and i["after_tax_parent_impact"] < 0 else 1) for i in accepted)
        profit = reported - adjustment
        result.append({"scenario": scenario, "period": period, "reported_profit": reported, "normalized_profit": profit,
                       "normalized_pe": market_cap/profit if profit > 0 and market_cap > 0 else None,
                       "adjustment_items": accepted, "balance_sheet_policy": "保留现金、净资产及负债实际影响，不恢复资产负债表",
                       "comparison_status": "待核查：未建立逐历史时点和同行的正常化可比序列，不参与资格或加分"})
    return result


def ttm_components(financials, period):
    """累计报表转换 TTM：最近年报 + 本年累计 - 上年同期；不相加四份累计季报。"""
    rows = {r["period"]: r for r in financials}
    year = int(period[:4])
    components = [(period, 1)] if period.endswith("12-31") else [(f"{year-1}-12-31", 1), (period, 1), (str(year-1)+period[4:], -1)]
    if any(p not in rows or number(rows[p].get("profit")) is None for p, _ in components):
        raise ValueError("计算 TTM 的最近年报或累计同期缺失")
    return [(rows[p], sign) for p, sign in components]


def normalized_ttm(financials, period, market_cap, adjustments):
    components = ttm_components(financials, period)
    reported = sum(r["profit"]*sign for r, sign in components)
    scenarios = []
    for scenario in ("conservative", "base"):
        profit = reported
        selected = []
        for row, sign in components:
            items = [a for a in adjustments if a.get("status") == "validated" and a["period"] == row["period"]]
            for item in items:
                effect = -item["after_tax_parent_impact"] * sign
                # A prior-YTD loss is subtracted in TTM. Its removal reduces TTM,
                # so a conservative scenario cannot retain part of that benefit.
                fraction = item.get("conservative_fraction", 0) if scenario == "conservative" and effect > 0 else 1
                profit += effect * fraction
            selected.extend(items)
        scenarios.append({"scenario": scenario, "period": period, "reported_ttm": reported, "normalized_ttm": profit,
                          "normalized_pe": market_cap/profit if market_cap > 0 and profit > 0 else None,
                          "reported_pe": market_cap/reported if market_cap > 0 and reported > 0 else None,
                          "adjustment_items": selected, "cash_and_balance_sheet": "现金、净资产、负债使用实际报表余额，不恢复一次性损失"})
    return scenarios
