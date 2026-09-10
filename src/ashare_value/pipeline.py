"""有界数据请求、全市场扫描、错误降级及可复现快照。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .domain import Observation, Stock, needs_peer_group, preliminary, rank_market
from .storage import Store, digest

ROOT = Path(__file__).resolve().parents[2]


def load_rules(path=None):
    rules = json.loads(Path(path or ROOT / "configs/value_rules.json").read_text(encoding="utf-8-sig"))
    defaults = json.loads((ROOT / "configs/value_rules.json").read_text(encoding="utf-8-sig"))
    rules.setdefault("exchanges", ["sh", "sz"])
    rules.setdefault("require_peer_group", False)
    rules.setdefault("excluded_peer_groups", [])
    rules.setdefault("peer_group_industries", ["I65"])
    rules.setdefault("quality_mode", "legacy")
    rules.setdefault("quality_targets", defaults["quality_targets"])
    if rules["quality_mode"] not in ("legacy", "six_targets"):
        raise ValueError("quality_mode 必须为 legacy 或 six_targets")
    targets = rules["quality_targets"]
    if not isinstance(targets, dict) or set(targets) != set(defaults["quality_targets"]) or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not 0 < v < float("inf") for v in targets.values()):
        raise ValueError("六项质量目标须完整、有限且为正数")
    if set(rules) != set(defaults):
        raise ValueError("规则配置有缺失或未知字段")
    for key in ["price_position_max", "valuation_percentile_max", "peer_percentile_max", "minimum_median_roe", "minimum_latest_roe", "maximum_debt_ratio", "quality_weight", "cheapness_weight", "confidence_weight"]:
        if not isinstance(rules[key], (int, float)) or not 0 <= rules[key] <= 1:
            raise ValueError(f"{key} 必须介于 0 和 1")
    if abs(sum(rules[k] for k in ("quality_weight", "cheapness_weight", "confidence_weight"))-1) > 1e-9:
        raise ValueError("排序权重之和必须等于 1")
    if not 1 <= rules["maximum_candidates"] <= 20 or rules["minimum_peers"] < 3 or rules["minimum_annual_reports"] < 3:
        raise ValueError("名单上限 20；同行至少 3；完整年报至少 3")
    exchanges = rules["exchanges"]
    if not isinstance(exchanges, list) or not exchanges or any(x not in ("sh", "sz") for x in exchanges) or len(set(exchanges)) != len(exchanges):
        raise ValueError("exchanges 必须为不重复的 sh / sz 列表；北交所不在本项目范围")
    if not isinstance(rules["require_peer_group"], bool) or not isinstance(rules["excluded_peer_groups"], list) or not isinstance(rules["peer_group_industries"], list):
        raise ValueError("同行细分行业开关及排除列表格式错误")
    return rules


def market_scope(stocks, rules):
    exchanges = rules.get("exchanges", ["sh", "sz"])
    return [stock for stock in stocks if stock.code.split(".")[0] in exchanges]


def market_label(rules):
    return "、".join({"sh": "上交所", "sz": "深交所"}[x] for x in rules.get("exchanges", ["sh", "sz"])) + " A 股"


def research_day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


class DataClient:
    def __init__(self, store: Store, rules: dict, offline=False):
        self.store, self.rules, self.offline = store, rules, offline

    def request(self, operation, *, cache_seconds=0, **arguments):
        key = digest({"adapter": "1.0.0", "operation": operation, "arguments": arguments})
        cached = self.store.cached(key, cache_seconds)
        if cached is not None:
            return cached
        if self.offline:
            raise RuntimeError(f"离线缓存缺失：{operation}")
        last_error = None
        for attempt in range(self.rules["request_retries"]+1):
            with tempfile.TemporaryDirectory(prefix="value-fetch-") as folder:
                request, output = Path(folder) / "request.json", Path(folder) / "result.json"
                request.write_text(json.dumps({"operation": operation, "arguments": {**arguments, "timeout": self.rules["request_timeout_seconds"]}}, ensure_ascii=False), encoding="utf-8")
                env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
                try:
                    process = subprocess.run([sys.executable, "-m", "ashare_value.worker", str(request), str(output)], capture_output=True, env=env,
                                             timeout=self.rules["worker_timeout_seconds"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if process.returncode != 0 or not output.exists():
                        raise RuntimeError("数据子进程失败：" + process.stderr.decode("utf-8", errors="replace")[-400:])
                    result = json.loads(output.read_text(encoding="utf-8"))
                    if not result["ok"]:
                        raise RuntimeError(result["error"])
                    archive_hash = digest(result["value"])
                    archive_path = self.store.root / "raw" / (archive_hash + ".json")
                    archive_path.parent.mkdir(parents=True, exist_ok=True)
                    if not archive_path.exists():
                        with archive_path.open("x", encoding="utf-8") as archive:
                            json.dump(result["value"], archive, ensure_ascii=False, allow_nan=False)
                    if isinstance(result["value"], dict):
                        result["value"]["archive_sha256"] = archive_hash
                        result["value"]["archive_path"] = str(archive_path)
                    self.store.cache(key, result["value"])
                    return result["value"]
                except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                    last_error = exc
            if attempt < self.rules["request_retries"]:
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"{operation} 获取失败：{last_error}")


def fetch_observation(root, rules, obs, offline=False):
    # Each thread owns its SQLite connection; BaoStock sessions stay in separate
    # bounded worker processes. At most two companies are fetched at a time.
    store = Store(root)
    try:
        client = DataClient(store, rules, offline)
        prices = client.request("prices", code=obs.stock.code, end=obs.as_of, cache_seconds=7*24*3600)
        obs.prices = prices["prices"]
        obs.sources.append({k: v for k, v in prices.items() if k not in ("prices", "raw")})
        # An overnight run uses the real collection date for each company's
        # financials; it never pretends tomorrow's information was known today.
        obs.research_date = research_day()
        for attempt in range(2):
            try:
                financial = client.request("financials", code=obs.stock.code, available_at=obs.research_date, cache_seconds=24*3600)
                break
            except (RuntimeError, ValueError):
                new_day = research_day()
                if attempt or new_day == obs.research_date:
                    raise
                obs.research_date = new_day
        obs.financials = financial["financials"]
        if financial.get("raw"):
            from .providers import parse_financial_abstract
            columns = set().union(*(row.keys() for row in financial["raw"]))
            obs.financials = parse_financial_abstract(financial["raw"], columns, obs.research_date)
        obs.sources.append({k: v for k, v in financial.items() if k not in ("financials", "raw")})
        if rules.get("quality_mode") == "six_targets" and preliminary(obs, rules)["status"] == "待同行比较":
            try:
                metrics = client.request("target_market", code=obs.stock.code, end=obs.as_of, cache_seconds=24*3600)
                if metrics.get("raw_close") is not None and abs(metrics["raw_close"] - obs.prices[-1]["raw_close"]) > .005:
                    metrics = {**metrics, "market_cap": None, "dividend_yield": None, "errors": metrics.get("errors", []) + ["辅助估值收盘与主行情不一致，两项目标待核查"]}
                obs.target_market = metrics
                obs.sources.append({k: v for k, v in metrics.items() if k != "raw"})
            except (RuntimeError, ValueError) as exc:
                obs.target_market = {"as_of": obs.as_of, "errors": [str(exc)]}
    except (RuntimeError, ValueError, OSError) as exc:
        obs.errors.append(str(exc))
    finally:
        store.close()
    time.sleep(rules["request_pause_seconds"])
    return obs


def collect_observations(store, rules, observations, offline, progress):
    pending = [obs for obs in observations if obs.stock.industry_code in rules["supported_industry_codes"] and obs.stock.peer_group not in rules.get("excluded_peer_groups", []) and (not needs_peer_group(obs.stock.industry_code, rules) or obs.stock.peer_group) and "ST" not in obs.stock.name.upper() and obs.stock.active]
    total, finished, consecutive_failures = len(pending), 0, 0
    cursor = 0
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="value-data") as executor:
        active = {}
        while cursor < total or active:
            while cursor < total and len(active) < 2 and consecutive_failures < 8:
                obs = pending[cursor]
                active[executor.submit(fetch_observation, store.root, rules, obs, offline)] = obs
                cursor += 1
            if not active:
                for obs in pending[cursor:]:
                    obs.errors.append("连续数据错误触发熔断；本轮未完成，保留待下次重试")
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                obs = active.pop(future)
                try:
                    future.result()
                except Exception as exc:
                    obs.errors.append(f"采集工作线程失败：{exc}")
                finished += 1
                consecutive_failures = consecutive_failures+1 if obs.errors else 0
                progress(f"[已采集 {finished}/{total}] {obs.stock.code} {obs.stock.name}" + (f" 数据不足：{obs.errors[-1]}" if obs.errors else ""), flush=True)


def scan(store, rules, *, codes=None, limit=None, offline=False, progress=print, slot=None):
    research_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    client = DataClient(store, rules, offline)
    coverage = {"research_date": research_date, "as_of": "", "target": market_label(rules), "exchanges": rules.get("exchanges", ["sh", "sz"]), "errors": [], "mode": "抽样" if codes or limit else "所选交易所全量名录扫描",
                "limitations": [], "universe_count": 0, "requested_count": 0}
    coverage["scheduled_slot"] = slot
    engine_sources = {p.name: p.read_text(encoding="utf-8") for p in Path(__file__).parent.glob("*.py")}
    engine_hash = digest(engine_sources)
    engine_path = store.root / "engines" / (engine_hash + ".json")
    engine_path.parent.mkdir(parents=True, exist_ok=True)
    if not engine_path.exists():
        with engine_path.open("x", encoding="utf-8") as engine_file:
            json.dump(engine_sources, engine_file, ensure_ascii=False)
    coverage.update(engine_hash=engine_hash, engine_source_path=str(engine_path))
    observations = []
    scope = digest({"codes": sorted(codes or []), "limit": limit, "exchanges": sorted(coverage["exchanges"])})
    try:
        progress("获取证券名录、行业及最近可用交易日……", flush=True)
        universe = client.request("universe", cache_seconds=12*3600)
        stocks = [Stock(**r) for r in universe["stocks"]]
        coverage["source_universe_count"] = len(stocks)
        coverage["source_exchange_counts"] = dict(Counter(s.code[:2] for s in stocks))
        coverage["source_limitations"] = universe["limitations"]
        stocks = market_scope(stocks, rules)
        coverage["outside_scope_count"] = coverage["source_universe_count"] - len(stocks)
        coverage["primary_source_universe_count"] = coverage["source_universe_count"]
        coverage["primary_scope_count"] = len(stocks)
        if rules.get("require_peer_group"):
            try:
                sectors = client.request("peer_groups", cache_seconds=24*3600)
                groups = sectors["groups"]
                coverage["peer_group_source"] = {k: v for k, v in sectors.items() if k != "groups"}
                coverage["peer_group_status"] = "available"
                coverage["auxiliary_universe_count"] = sectors["total"]
                existing = {s.code for s in stocks}
                extras = {code: row for code, row in groups.items() if code not in existing and code.split(".")[0] in coverage["exchanges"]}
                for code, row in extras.items():
                    stocks.append(Stock(code, row["name"], "基础名录缺失，暂未覆盖", "UNKNOWN"))
                coverage["auxiliary_only_count"] = len(extras)
                coverage["auxiliary_only_codes"] = sorted(extras)
                coverage["source_universe_count"] += len(extras)
                for stock in stocks:
                    stock.peer_group = groups.get(stock.code, {}).get("peer_group", "")
                coverage["peer_group_missing_count"] = sum(not s.peer_group for s in stocks)
                coverage["limitations"].append(f"同行额外使用东方财富细分行业；半导体等配置类别暂未覆盖。辅助名录另有 {len(extras)} 家在主源无基础记录，已逐家列为暂未覆盖；并集名录不等于官方完整上市清单")
            except (RuntimeError, ValueError) as exc:
                coverage["peer_group_status"] = "unavailable"
                coverage["peer_group_error"] = str(exc)
                coverage["limitations"].append(f"细分行业接口不可用，{','.join(rules['peer_group_industries'])} 大类本轮暂未覆盖，不使用混合业务的估值确认低估。其他已覆盖行业维持披露的大类与财务质量比较，组内业务差异仍待核查。错误：{exc}")
        for stock in stocks:
            override = rules["industry_overrides"].get(stock.code)
            if override:
                if not override.get("source_url") or not override.get("reason"):
                    raise ValueError("行业细分覆盖需配置来源 source_url 和依据 reason")
                stock.industry_code, stock.industry = override["code"], override["name"]
        coverage["universe_count"] = len(stocks)
        coverage["industry_date"] = universe["industry_date"]
        coverage["limitations"].append(f"本轮范围为{coverage['target']}，北交所不在项目范围；候选和同行均限定于所选交易所。来源名录仍须核对完整性，不以来源总数冒充实际扫描数量")
        coverage["limitations"].append("C35专用设备、C38电气机械的免费大类混含强周期业务，未取得可靠细分时整类暂未覆盖；不能把家电与光伏直接混为同行")
        coverage["exchange_counts"] = dict(Counter(s.code[:2] for s in stocks))
        coverage["unknown_industry_count"] = sum(s.industry_code == "UNKNOWN" for s in stocks)
        selected_codes = {c if "." in c else ("sh." if c.startswith("6") else "sz.")+c for c in codes or []}
        if selected_codes:
            absent = selected_codes-{s.code for s in stocks}
            coverage["errors"].extend(f"名录中未找到 {c}" for c in sorted(absent))
            # Use the entire industry group for honest peer comparison in sample mode.
            groups = {s.industry_code for s in stocks if s.code in selected_codes}
            stocks = [s for s in stocks if s.code in selected_codes or (s.industry_code in groups and s.industry_code in rules["supported_industry_codes"])]
        if limit:
            # Explicit request codes precede comparison rows; sampling order is disclosed.
            stocks.sort(key=lambda s: (s.code not in selected_codes, s.code))
            stocks = stocks[:limit]
        coverage["requested_count"] = len(stocks)
        market_date = client.request("market_date", end=research_date, cache_seconds=1800)
        coverage["as_of"] = market_date
        if (date.fromisoformat(research_date)-date.fromisoformat(market_date)).days > rules["maximum_quote_age_days"]:
            raise ValueError("数据源最近行情过旧，不能产生正式候选")
        calendar_rows = client.request("calendar", end=market_date, cache_seconds=24*3600)
        calendar = [r["calendar_date"] for r in calendar_rows if r["is_trading_day"] == "1"]
        observations = [Observation(stock, market_date, research_date=research_date, calendar=calendar) for stock in stocks]
        collect_observations(store, rules, observations, offline, progress)
    except (RuntimeError, ValueError) as exc:
        coverage["errors"].append(str(exc))
    results = rank_market(observations, rules)
    coverage["research_date_end"] = max((o.research_date for o in observations), default=research_date)
    if coverage["research_date_end"] != research_date:
        coverage["limitations"].append(f"本轮跨日采集：{research_date} 至 {coverage['research_date_end']}，财务可见日期逐公司保存；行情统一为 {coverage['as_of']}，不是历史时点回测")
    coverage["status_counts"] = dict(Counter(r["status"] for r in results))
    coverage["observed_count"] = len(observations)
    coverage["financial_count"] = sum(bool(o.financials) for o in observations)
    coverage["price_count"] = sum(bool(o.prices) for o in observations)
    coverage["complete_3y_count"] = sum(r["windows"].get("3", {}).get("valid", False) for r in results)
    coverage["complete_5y_count"] = sum(r["windows"].get("5", {}).get("valid", False) for r in results)
    coverage["collection_failed_count"] = sum(bool(o.errors) for o in observations)
    if rules.get("quality_mode") == "six_targets":
        coverage["quality_target_policy"] = "六项等权软目标；市值和股息仅对历史低位与基本安全预筛合格公司补采，其他记录可能缺此两项；缺失不算达标"
        coverage["quality_target_complete_count"] = sum(r["quality"].get("target_known_count") == 6 for r in results)
        coverage["limitations"].append(coverage["quality_target_policy"])
    coverage["completed"] = bool(observations) and not coverage["errors"] and not any(o.errors for o in observations)
    # Failed scans have a durable report, but do not consume the schedule slot.
    identifier = store.save(observations, results, rules, coverage, kind="live", scope_key=scope)
    return identifier


def latest_due(now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    for offset in range(8):
        day = now.date()-timedelta(days=offset)
        if day.weekday() in (2, 5):
            due = datetime(day.year, day.month, day.day, 20, tzinfo=ZoneInfo("Asia/Shanghai"))
            if due <= now:
                return due.isoformat()
    raise AssertionError("一周内应有计划时段")
