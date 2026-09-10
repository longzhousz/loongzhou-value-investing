from datetime import date, timedelta

import pytest

from ashare_value.evidence_review import comparable_normalized_window
from ashare_value.providers import BaoSession
from ashare_value.storage import digest, pack, unpack


def test_compressed_observations_are_lossless_and_content_stable():
    value = {"company":"中文公司", "price":12.34, "missing":None, "dates":["2026-09-09"]}
    assert unpack(pack(value)) == value
    assert digest(unpack(pack(value))) == digest(value)


def test_baostock_partial_page_is_not_accepted():
    class Result:
        error_code = "0"
        error_msg = "分页超时"
        fields = ["code"]
        count = 0
        def next(self):
            self.count += 1
            if self.count == 1:
                return True
            self.error_code = "1001"
            return False
        def get_row_data(self):
            return ["sh.600519"]
    class Fake:
        def query(self):
            return Result()
    session = BaoSession.__new__(BaoSession)
    session.bs = Fake()
    with pytest.raises(RuntimeError, match="分页未完整"):
        session.query("query", ["code"])


def test_baostock_wrong_response_schema_is_not_accepted():
    class Result:
        error_code = "0"
        fields = ["industry"]
    class Fake:
        def query(self):
            return Result()
    session = BaoSession.__new__(BaoSession)
    session.bs = Fake()
    with pytest.raises(ValueError, match="返回字段不符"):
        session.query("query", ["date"])


def test_current_earnings_backfilled_to_history_rejected():
    days = [(date(2023,1,1)+timedelta(days=i)).isoformat() for i in range(4)]
    rows = [{"date":d,"method":"dated_ttm_normalization", "source_ids":["s"], "published_at":"2026-04-01", "period":"2025-12-31",
             "market_cap":1000,"base_earnings_ttm":100} for d in days]
    with pytest.raises(ValueError, match="未来报表"):
        comparable_normalized_window(rows, days, days[-1], "base")
    for row in rows:
        row.update(published_at="2022-10-28",period="2022-09-30")
    assert comparable_normalized_window(rows, days, days[-1], "base") == .5
    with pytest.raises(ValueError, match="完整覆盖"):
        comparable_normalized_window(rows[:-1], days, days[-1], "base")


def test_review_appends_ttm_analysis_without_mutating_snapshot(tmp_path, monkeypatch):
    import json
    from uuid import uuid4

    from ashare_value.demo import fixture_market
    from ashare_value.domain import rank_market, ttm_components
    from ashare_value.evidence_review import apply_review
    from ashare_value.pipeline import load_rules
    from ashare_value.storage import Store, encode, utc_now
    store = Store(tmp_path)
    try:
        rules = load_rules()
        market = fixture_market()
        results = rank_market(market, rules)
        identifier = store.save(market, results, rules, {"as_of": market[0].as_of})
        task = store.db.execute("SELECT * FROM ai_tasks ORDER BY code LIMIT 1").fetchone()
        payload = {"sources":[{"id":"S1","status":"原文匹配","quote":"本期税后归母一次性损失为1万元；不影响既有负债确认。"}], "events":[], "pending":[]}
        review_time = utc_now()
        monkeypatch.setattr("ashare_value.evidence_review.utc_now", lambda: review_time)
        with store.db:
            store.db.execute("INSERT INTO ai_analyses VALUES(?,?,?,?,?)",(uuid4().hex,task['id'],review_time,'needs_review',encode(payload)))
        components = ttm_components(market[0].financials,'2026-06-30')
        reported_ttm = sum(r['profit']*sign for r,sign in components)
        adjustment = {'title':'一次性事项','period':'2026-06-30','source_id':'S1','amount_text':'1万元','amount_in_source_units':1,
                      'unit_multiplier':10000,'after_tax_parent_impact':-10000,'conservative_fraction':.5,'reason':'复核为一次性损失',
                      'cash_impact':'现金实际减少','equity_impact':'净资产实际减少','liability_impact':'负债按实际列报'}
        packet = {'task_id':task['id'],'reviewer':'离线测试复核人','reviewed_at':date.today().isoformat(),'approved_adjustments':[adjustment],
                  'valuation':{'date':market[0].as_of,'market_cap':reported_ttm*10,'source_id':'S1'}}
        assert apply_review(store,packet) == identifier
        assert store.get(identifier)['results'] == results
        latest = json.loads(store.db.execute('SELECT payload FROM ai_analyses ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()[0])
        assert latest['normalized'][1]['normalized_ttm'] == reported_ttm + 10000
        assert not latest['event_label_validated']
        assert latest['pending']  # no historical comparison was invented
    finally:
        store.close()
