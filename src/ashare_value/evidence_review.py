"""证据复核后追加研究版本。人的会计确认与程序的算术/时点校验分别记录。"""
from __future__ import annotations

import json
from datetime import date
from uuid import uuid4

from .domain import normalized_ttm, number, percentile
from .research import validate_adjustment
from .storage import encode, utc_now


def comparable_normalized_window(series, expected_dates, as_of, scenario):
    """必须每个历史时点有独立、当时已公布的盈利、股本和证据；不能当前盈利平铺。"""
    rows = [r for r in series if r["date"] in set(expected_dates)]
    if len(rows) != len(expected_dates) or {r["date"] for r in rows} != set(expected_dates):
        raise ValueError("正常化历史序列未完整覆盖窗口")
    values = []
    for row in sorted(rows, key=lambda r: r["date"]):
        if row.get("method") != "dated_ttm_normalization" or not row.get("source_ids"):
            raise ValueError("历史正常化估值缺少独立时点方法或证据")
        if row.get("published_at", "9999") > row["date"] or row.get("period", "9999") > row["date"]:
            raise ValueError("历史正常化估值使用未来报表")
        earnings = number(row.get(scenario+"_earnings_ttm"))
        market_cap = number(row.get("market_cap"))
        if not earnings or earnings <= 0 or not market_cap or market_cap <= 0:
            raise ValueError("历史正常化估值有不适用值")
        values.append((row["date"], market_cap/earnings))
    current = next(v for d, v in values if d == as_of)
    return percentile(current, [v for _, v in values])


def apply_review(store, packet):
    if not packet.get("reviewer") or packet.get("reviewed_at") != date.today().isoformat():
        raise ValueError("复核文件必须记录复核人和今天的复核日期")
    task = store.db.execute("SELECT * FROM ai_tasks WHERE id=?", (packet["task_id"],)).fetchone()
    if not task:
        raise ValueError("研究任务不存在")
    previous = store.db.execute("SELECT payload FROM ai_analyses WHERE task_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (task["id"],)).fetchone()
    if not previous:
        raise ValueError("须先完成 AI 材料研究和下载核验")
    payload = json.loads(previous[0])
    sources = {s["id"]: s for s in payload["sources"] if s["status"] == "原文匹配"}
    raw = store.observation(task["snapshot_id"], task["code"])
    financials = raw["financials"]
    periods = {r["period"] for r in financials}
    approved = []
    keys = set()
    for adjustment in packet.get("approved_adjustments", []):
        checked = validate_adjustment(adjustment, sources, periods)
        if checked["numeric_validation"] != "通过":
            raise ValueError("调整未通过程序校验：" + encode(checked["checks"]))
        key = (adjustment["title"], adjustment["source_id"], adjustment["period"])
        if key in keys:
            raise ValueError("调整项目重复")
        keys.add(key)
        approved.append({**adjustment, "status": "validated", "reviewer": packet["reviewer"], "reviewed_at": packet["reviewed_at"]})
    valuation = packet.get("valuation", {})
    if valuation.get("date") != raw["as_of"] or valuation.get("source_id") not in sources:
        raise ValueError("正常化估值必须有同交易日实际总市值来源")
    market_cap = number(valuation.get("market_cap"))
    if not market_cap or market_cap <= 0:
        raise ValueError("总市值必须为正，单位人民币元")
    period = max(periods)
    scenarios = normalized_ttm(financials, period, market_cap, approved)
    current_pe = raw["prices"][-1]["pe"]
    if current_pe <= 0 or abs(scenarios[0]["reported_pe"] / current_pe - 1) > .05:
        raise ValueError("给定总市值及报表TTM与行情PE偏差超过5%，先核查股本/归母口径")
    snapshot = store.get(task["snapshot_id"])
    screened = next(r for r in snapshot["results"] if r["code"] == task["code"])
    normalized_pass = False
    comparison_errors = []
    for scenario in scenarios:
        scenario["status"] = "经复核的补充研究，保留基础资格和排序"
        scenario["historical_percentiles"] = {}
        for years in (3, 5):
            window = screened["windows"].get(str(years), {})
            if not window.get("valid"):
                scenario["historical_percentiles"][str(years)] = None
                continue
            expected = [r["date"] for r in raw["prices"] if window["start"] <= r["date"] <= raw["as_of"] and r["trading"]]
            try:
                history = packet.get("normalized_history", [])
                if any(any(s not in sources for s in r.get("source_ids", [])) for r in history):
                    raise ValueError("正常化历史来源未核验")
                percent = comparable_normalized_window(history, expected, raw["as_of"], scenario["scenario"])
                # Exact current earnings must reconcile with the adjusted TTM build.
                last = next(r for r in history if r["date"] == raw["as_of"])
                if abs(last[scenario["scenario"]+"_earnings_ttm"]-scenario["normalized_ttm"]) > max(1, abs(scenario["normalized_ttm"])*1e-6):
                    raise ValueError("历史序列末值与本次TTM调整不一致")
                scenario["historical_percentiles"][str(years)] = percent
                peers = packet.get("normalized_peers", [])
                eligible = set(screened["peer"].get("codes", []))
                if len({p["code"] for p in peers}) != len(peers) or any(p["code"] not in eligible for p in peers):
                    raise ValueError("正常化同行必须来自基础质量可比同行，且无重复")
                valid_peers = [p for p in peers if p["date"] == raw["as_of"] and p.get("method") == "dated_ttm_normalization" and p.get("source_ids") and all(s in sources for s in p["source_ids"])
                               and number(p.get(scenario["scenario"]+"_earnings_ttm")) and p[scenario["scenario"]+"_earnings_ttm"] > 0 and number(p.get("market_cap")) and p["market_cap"] > 0]
                if len(valid_peers) < snapshot["rules"]["minimum_peers"]:
                    raise ValueError("正常化口径可比同行不足")
                peer_position = percentile(scenario["normalized_pe"], [p["market_cap"]/p[scenario["scenario"]+"_earnings_ttm"] for p in valid_peers])
                scenario["peer_percentile"] = peer_position
                if scenario["scenario"] == "conservative" and window["passed"] and percent <= snapshot["rules"]["valuation_percentile_max"] and peer_position <= snapshot["rules"]["peer_percentile_max"]:
                    normalized_pass = True
            except ValueError as exc:
                scenario["historical_percentiles"][str(years)] = None
                comparison_errors.append(str(exc))
    event_valid = False
    event = packet.get("approved_event")
    if event and normalized_pass and screened["status"] == "候选":
        if event.get("classification") not in ("一次性", "持续性", "混合") or not event.get("relationship_evidence") or not event.get("ongoing_impact"):
            raise ValueError("事件性质、持续影响或关联证据缺失")
        if not event.get("source_ids") or any(s not in sources for s in event["source_ids"]):
            raise ValueError("事件证据未核验")
        before, after = event.get("before_date", ""), event.get("after_date", "")
        if not before < event.get("date", "") <= after <= raw["as_of"]:
            raise ValueError("事件与价格观察区间顺序无效")
        prices = {p["date"]: p["adjusted_close"] for p in raw["prices"]}
        if before not in prices or after not in prices or prices[after] >= prices[before]:
            raise ValueError("同口径真实价格未证实所述下跌")
        event["observed_drawdown"] = prices[after]/prices[before]-1
        event_valid = True
    payload.update(adjustments=approved, normalized=scenarios, event_label_validated=event_valid,
                   review={"reviewer": packet["reviewer"], "reviewed_at": packet["reviewed_at"]},
                   pending=sorted(set(comparison_errors)), events=[event] if event else payload.get("events", []))
    with store.db:
        store.db.execute("INSERT INTO ai_analyses VALUES(?,?,?,?,?)", (uuid4().hex, task["id"], utc_now(), "reviewed", encode(payload)))
        store.db.execute("UPDATE ai_tasks SET status='reviewed' WHERE id=?", (task["id"],))
    return task["snapshot_id"]
