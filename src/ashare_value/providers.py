"""免费真实数据适配器。在独立进程内运行，超时后丢弃会话及部分结果。"""
from __future__ import annotations

import re
import socket
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .domain import anniversary, number


class BaoSession:
    def __init__(self, timeout=60):
        import baostock as bs
        self.bs = bs
        socket.setdefaulttimeout(timeout)
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"BaoStock 登录失败：{login.error_msg}")

    def close(self):
        import baostock.common.context as context
        sock = getattr(context, "default_socket", None)
        if sock:
            sock.close()
            context.default_socket = None

    def query(self, method, required, **kwargs):
        response = getattr(self.bs, method)(**kwargs)
        if response.error_code != "0":
            raise RuntimeError(f"{method}: {response.error_code} {response.error_msg}")
        if not set(required).issubset(response.fields):
            raise ValueError(f"{method}: 返回字段不符，可能会话错位：{response.fields}")
        result = []
        while response.next():
            row = response.get_row_data()
            if len(row) != len(response.fields):
                raise ValueError(f"{method}: 行字段数量不符")
            result.append(dict(zip(response.fields, row)))
        # A failed next-page query may leave 2000 valid-looking but incomplete rows.
        if response.error_code != "0":
            raise RuntimeError(f"{method}: 分页未完整取得，丢弃部分数据：{response.error_msg}")
        return result


def get_universe(timeout=60):
    session = BaoSession(timeout)
    try:
        basic = session.query("query_stock_basic", ["code", "type", "status", "ipoDate"])
        industries = session.query("query_stock_industry", ["code", "industry", "updateDate"])
        if len(basic) < 3000 or len(industries) < 3000:
            raise ValueError("股票或行业全表数量异常，拒绝标为全市场")
        mapping = {r["code"]: r for r in industries}
        stocks = []
        for r in basic:
            if r["type"] != "1" or r["status"] != "1" or not re.fullmatch(r"(sh\.6\d{5}|sz\.[03]\d{5}|bj\.\d{6})", r["code"]):
                continue
            industry = mapping.get(r["code"], {}).get("industry", "")
            match = re.match(r"[A-Z]\d{2}", industry)
            stocks.append({"code": r["code"], "name": r["code_name"], "ipo_date": r["ipoDate"], "active": True,
                           "industry": industry or "行业缺失", "industry_code": match.group(0) if match else "UNKNOWN"})
        return {"stocks": stocks, "raw_basic": basic, "raw_industry": industries,
                "industry_date": max((r["updateDate"] for r in industries), default=""),
                "limitations": ["BaoStock 名录不等于沪深京全部 A 股；北交所名录及其行情/行业尚未完整验证，暂未覆盖"]}
    finally:
        session.close()


def get_calendar(end, timeout=60):
    session = BaoSession(timeout)
    try:
        start = anniversary(date.fromisoformat(end), 5).isoformat()
        rows = session.query("query_trade_dates", ["calendar_date", "is_trading_day"], start_date=start, end_date=end)
        if len(rows) != (date.fromisoformat(end)-date.fromisoformat(start)).days+1:
            raise ValueError("交易日历不完整")
        return rows
    finally:
        session.close()


def get_peer_groups(timeout=30):
    """独立免费细分行业名录；不使用其行情替换既定复权口径。"""
    import requests
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    rows, expected = {}, None
    page = 1
    while page <= 100:
        response = requests.get(url, params={"pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                                            "fid": "f12", "fs": "m:1+t:2,m:1+t:23", "fields": "f12,f13,f14,f100"}, timeout=timeout)
        response.raise_for_status()
        data = response.json().get("data") or {}
        total, batch = data.get("total"), data.get("diff")
        if not isinstance(total, int) or total < 2000 or not isinstance(batch, list) or not batch:
            raise ValueError("细分行业名录字段或分页数量异常")
        if expected is not None and total != expected:
            raise ValueError("细分行业名录分页期间数量变化，拒绝不完整数据")
        expected = total
        for row in batch:
            if row.get("f13") != 1 or not re.fullmatch(r"6\d{5}", str(row.get("f12", ""))):
                raise ValueError("细分行业名录出现非上交所证券或错误代码")
            code = "sh." + row["f12"]
            if code in rows:
                raise ValueError("细分行业分页重复证券，拒绝不完整名录")
            group = row.get("f100")
            rows[code] = {"name": row.get("f14", ""), "peer_group": group if isinstance(group, str) and group not in ("", "-") else ""}
        if len(rows) == expected:
            return {"groups": rows, "total": expected, "provider": "eastmoney", "url": url,
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "basis": "东方财富细分行业f100，仅作同行分组及名录交叉检查，不替换行情"}
        if len(rows) > expected:
            raise ValueError("细分行业名录超过声明总数")
        page += 1
    raise ValueError("细分行业分页超过上限")


def get_market_date(end, timeout=60):
    from datetime import timedelta
    session = BaoSession(timeout)
    try:
        rows = session.query("query_history_k_data_plus", ["date", "code", "close"], code="sh.000001", fields="date,code,close",
                             start_date=(date.fromisoformat(end)-timedelta(days=20)).isoformat(), end_date=end, frequency="d", adjustflag="3")
        if not rows or any(r["code"] != "sh.000001" or r["date"] > end for r in rows):
            raise ValueError("无法确认市场最近数据交易日")
        return max(r["date"] for r in rows)
    finally:
        session.close()


def get_prices(code, end, timeout=60):
    session = BaoSession(timeout)
    try:
        fields = "date,code,close,tradestatus,peTTM,pbMRQ,isST"
        start = anniversary(date.fromisoformat(end), 5).isoformat()
        data = {}
        for flag in ("1", "3"):
            data[flag] = session.query("query_history_k_data_plus", fields.split(","), code=code, fields=fields, start_date=start, end_date=end, frequency="d", adjustflag=flag)
            if any(r["code"] != code or not start <= r["date"] <= end for r in data[flag]):
                raise ValueError("价格返回错误证券或日期")
        raw = {r["date"]: r for r in data["3"]}
        if len(raw) != len(data["3"]) or {r["date"] for r in data["1"]} != set(raw):
            raise ValueError("复权与实际收盘的日期不一致或有重复")
        prices = []
        for row in data["1"]:
            actual = raw[row["date"]]
            if row["peTTM"] != actual["peTTM"] or row["pbMRQ"] != actual["pbMRQ"]:
                raise ValueError("复权前后 PE/PB 不一致，口径不可确认")
            prices.append({"date": row["date"], "adjusted_close": number(row["close"]), "raw_close": number(actual["close"]),
                           "adjustment": "hfq", "trading": actual["tradestatus"] == "1", "is_st": False if actual["isST"] == "0" else True if actual["isST"] == "1" else None,
                           "pe": number(actual["peTTM"]), "pb": number(actual["pbMRQ"]), "valuation_basis": "provider_reported_ttm_mrq"})
        return {"prices": prices, "raw": data, "provider": "baostock", "url": "https://www.baostock.com/", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    finally:
        session.close()


def parse_financial_abstract(records, columns, available_at):
    # Sina repeats some fields in different sections. Equal duplicates are fine; conflicting ones are not.
    fields = {"profit": "归母净利润", "net_profit": "净利润", "revenue": "营业总收入", "ocf": "经营现金流量净额",
              "roe": "净资产收益率(ROE)", "debt_ratio": "资产负债率", "equity": "股东权益合计(净资产)", "deducted_profit": "扣非净利润", "gross_margin": "毛利率", "net_margin": "销售净利率"}
    financials = []
    for period in sorted(c for c in columns if re.fullmatch(r"\d{8}", str(c))):
        day = f"{period[:4]}-{period[4:6]}-{period[6:]}"
        if day > available_at or int(period[:4]) < int(available_at[:4])-6:
            continue
        out = {"period": day, "available_at": available_at, "published_at": None, "basis": "累计报告期；归母盈利与合并净利润分列；现金转化率分母使用合并净利润", "currency": "CNY", "unit": "元"}
        for key, label in fields.items():
            values = [number(r.get(period)) for r in records if r.get("指标") == label]
            valid = [v for v in values if v is not None]
            if valid and max(valid)-min(valid) > max(1e-7, abs(valid[0])*1e-8):
                raise ValueError(f"{day} {label} 重复字段数值冲突")
            out[key] = valid[0] if valid else None
            if key in ("roe", "debt_ratio", "gross_margin", "net_margin") and out[key] is not None:
                out[key] /= 100
        financials.append(out)
    return financials


def get_financials(code, available_at, timeout=30):
    import akshare as ak
    import requests
    if available_at != datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ValueError("财务摘要没有公告日，不能用于回填历史研究日期；跨日扫描请重跑")
    original = requests.sessions.Session.request

    def bounded(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = bounded
    frame = ak.stock_financial_abstract(symbol=code.split(".")[-1])
    if available_at != datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ValueError("财务请求跨午夜完成，须按实际新日期重试，不回填前一日可见信息")
    if "指标" not in frame.columns:
        raise ValueError("新浪财务摘要字段不符")
    records = frame.astype(object).where(frame.notna(), None).to_dict("records")
    return {"financials": parse_financial_abstract(records, frame.columns, available_at), "raw": records,
            "provider": "akshare_sina", "url": f"https://money.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/{code[-6:]}.phtml",
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "publication_policy": "公告日期缺失；只确认采集时已可见，不冒充历史时点可见"}


def parse_target_market(quote_rows, dividends, code, end):
    """总市值元；近12个月已除息税前每股现金/对应日实际收盘。"""
    result = {"as_of": end, "market_cap": None, "dividend_yield": None, "errors": [],
              "basis": "总市值为元；股息率=近12个月已实施除息税前每股现金分红/实际收盘，不计未实施预案；送转股口径待核查"}
    rows = [r for r in quote_rows if r.get("SECURITY_CODE") == code[-6:] and str(r.get("TRADE_DATE", ""))[:10] == end]
    if len(rows) != 1:
        result["errors"].append("目标市值缺少唯一同交易日估值记录")
        return result
    q = rows[0]
    close, cap, shares = (number(q.get(k)) for k in ("CLOSE_PRICE", "TOTAL_MARKET_CAP", "TOTAL_SHARES"))
    if close is None or close <= 0 or cap is None or cap <= 0 or shares is None or shares <= 0 or abs(cap-close*shares) > max(1, cap*.001):
        result["errors"].append("总市值、总股本与收盘价口径不一致")
        return result
    result.update(market_cap=cap, raw_close=close)
    if dividends is None:
        result["errors"].append("完整分红记录不可用，股息率待核查")
        return result
    start = anniversary(date.fromisoformat(end), 1).isoformat()
    selected, seen = [], set()
    for r in dividends:
        if r.get("SECURITY_CODE") != code[-6:]:
            result["errors"].append("分红记录证券不符")
            return result
        ex = str(r.get("EX_DIVIDEND_DATE") or "")[:10]
        if not ex or not start < ex <= end or r.get("ASSIGN_PROGRESS") != "实施分配":
            continue
        try:
            date.fromisoformat(ex)
        except ValueError:
            result["errors"].append("分红除息日格式无效")
            return result
        identity = (r.get("REPORT_DATE"), ex)
        if identity in seen:
            result["errors"].append("分红记录重复，不能重复计息")
            return result
        seen.add(identity)
        if any((number(r.get(k)) or 0) != 0 for k in ("BONUS_RATIO", "IT_RATIO", "BONUS_IT_RATIO")):
            result["errors"].append("近12个月涉及送转股，缺少可比每股调整，股息率待核查")
            return result
        cash = number(r.get("PRETAX_BONUS_RMB"))
        text = r.get("IMPL_PLAN_PROFILE") or ""
        matched = re.search(r"10派([0-9]+(?:\.[0-9]+)?)元", text)
        if cash is None or cash < 0 or not matched or abs(float(matched[1])-cash) > 1e-6:
            result["errors"].append("分红每10股金额与实施方案不一致，股息率待核查")
            return result
        if not r.get("NOTICE_DATE") or str(r["NOTICE_DATE"])[:10] > end:
            result["errors"].append("分红实施公告日期不可确认")
            return result
        selected.append({"ex_date": ex, "cash_per_share": cash/10, "notice_date": str(r["NOTICE_DATE"])[:10], "plan": text})
    cash_sum = sum(r["cash_per_share"] for r in selected)
    result.update(dividend_yield=cash_sum/close, dividend_cash_per_share=cash_sum, dividend_items=selected)
    return result


def get_target_market(code, end, timeout=30):
    import requests
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    raw, errors = {}, []
    for label, report, sort, filter_value in [
        ("quote", "RPT_VALUEANALYSIS_DET", "TRADE_DATE", f'(SECURITY_CODE="{code[-6:]}")(TRADE_DATE=\'{end}\')'),
        ("dividends", "RPT_SHAREBONUS_DET", "EX_DIVIDEND_DATE", f'(SECURITY_CODE="{code[-6:]}")'),
    ]:
        try:
            response = requests.get(url, params={"reportName": report, "columns": "ALL", "filter": filter_value,
                                    "sortColumns": sort, "sortTypes": "-1", "pageSize": "5000", "pageNumber": "1"}, timeout=timeout)
            response.raise_for_status()
            value = response.json()
            raw[label] = value
            data = value.get("result") or {}
            if not value.get("success") or not isinstance(data.get("data"), list) or data.get("count") != len(data["data"]) or data.get("pages", 1) > 1:
                raise ValueError("返回字段或完整分页不符")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{label}辅助指标获取失败：{exc}")
            raw[label] = {"error": str(exc)}
    quotes = (raw.get("quote", {}).get("result") or {}).get("data", [])
    dividends = (raw.get("dividends", {}).get("result") or {}).get("data")
    result = parse_target_market(quotes, dividends, code, end)
    result["errors"].extend(errors)
    result.update(raw=raw, provider="eastmoney_datacenter", url=url, fetched_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
    return result
