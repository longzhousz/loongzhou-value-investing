"""明确标记的离线合成样本，只验证流程，不混入正式市场报告。"""
from datetime import date, timedelta

from .domain import Observation, Stock, anniversary, rank_market


def fixture_market():
    end = date(2026, 9, 8)
    day = anniversary(end, 5)
    calendar = []
    while day <= end:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    observations = []
    for index in range(12):
        stock = Stock(f"demo.{index:06}", f"合成验证公司{index+1}", "C15离线测试行业", "C15", "2000-01-01")
        stock.peer_group = "合成验证业务分组"
        prices = []
        for i, day in enumerate(calendar):
            factor = i/max(1, len(calendar)-1)
            prices.append({"date": day, "adjusted_close": 30-20*factor+index*0.1, "raw_close": 15-10*factor+index*0.05,
                           "pe": 30-20*factor+index, "pb": 5-3*factor+index*0.1, "trading": True, "is_st": False,
                           "adjustment": "hfq", "valuation_basis": "provider_reported_ttm_mrq"})
        reports = [{"period": f"{year}-12-31", "available_at": f"{year+1}-04-25", "published_at": f"{year+1}-04-25", "profit": 1e8*(1+0.05*(year-2021)),
                    "net_profit": 1.02e8*(1+0.05*(year-2021)), "revenue": 1e9, "ocf": 1.5e8, "roe": 0.15, "equity": 1e9, "debt_ratio": 0.3,
                    "deducted_profit": 1e8} for year in range(2021, 2026)]
        reports.extend([{**reports[-1], "period": f"{year}-06-30", "available_at": f"{year}-08-20", "published_at": f"{year}-08-20", "profit": 6e7, "net_profit": 6.2e7} for year in (2025, 2026)])
        observations.append(Observation(stock, end.isoformat(), research_date=end.isoformat(), calendar=calendar, prices=prices, financials=reports,
                                        sources=[{"provider": "synthetic", "note": "完全合成，仅验证逻辑，周内日历并非真实交易日历"}]))
    return observations


def save_demo(store, rules):
    observations = fixture_market()
    results = rank_market(observations, rules)
    coverage = {"as_of": observations[0].as_of, "research_date": observations[0].as_of, "mode": "离线合成验证", "target": "测试场景",
                "universe_count": len(observations), "requested_count": len(observations), "observed_count": len(observations),
                "price_count": len(observations), "financial_count": len(observations), "complete_3y_count": len(observations),
                "complete_5y_count": len(observations), "errors": [], "limitations": ["全部公司、价格、财务和日历均为合成数据，不作投资研究"],
                "status_counts": {"候选": sum(r["status"] == "候选" for r in results)}, "completed": True}
    return store.save(observations, results, rules, coverage, kind="synthetic", scope_key="demo")
