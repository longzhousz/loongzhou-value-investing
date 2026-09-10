"""免费数据实测；与默认离线测试分离，输出原始样本到指定目录。"""
import argparse
import importlib.metadata
import json
import socket
import time
from datetime import date, timedelta
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=["baostock", "akshare"])
    parser.add_argument("--output", default="exports/value-probe")
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    socket.setdefaulttimeout(20)
    results = {"source": args.source, "version": importlib.metadata.version(args.source), "observed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": []}

    def check(name, call):
        started = time.monotonic()
        try:
            frame = call()
            if hasattr(frame, "error_code"):
                if frame.error_code != "0":
                    raise RuntimeError(frame.error_msg)
                import pandas as pd
                records = []
                while frame.next():
                    records.append(frame.get_row_data())
                if frame.error_code != "0":
                    raise RuntimeError("分页未完整取得：" + frame.error_msg)
                frame = pd.DataFrame(records, columns=frame.fields)
            frame.to_csv(output / f"{args.source}-{name}.csv", index=False, encoding="utf-8-sig")
            item = {"name": name, "rows": len(frame), "columns": list(frame.columns), "sample": frame.head(2).to_dict("records")}
        except Exception as exc:
            item = {"name": name, "error": str(exc)}
            if args.source == "baostock":
                import baostock.common.context as context
                old_socket = getattr(context, "default_socket", None)
                if old_socket:
                    old_socket.close()
                    context.default_socket = None
                bs.login()
        item["seconds"] = round(time.monotonic() - started, 2)
        results["checks"].append(item)
        (output / f"{args.source}.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(item, ensure_ascii=False, default=str), flush=True)

    end = date.today().isoformat()
    start = (date.today() - timedelta(days=5 * 366 + 15)).isoformat()
    if args.source == "baostock":
        import baostock as bs
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(login.error_msg)
        try:
            check("universe", bs.query_stock_basic)
            check("industry", bs.query_stock_industry)
            check("calendar", lambda: bs.query_trade_dates(start_date=start, end_date=end))
            for code in ["sh.600519", "sz.000333", "sh.600036", "sh.601088", "sz.002594"]:
                for flag in ["1", "3"]:
                    check(f"{code}-{flag}", lambda c=code, f=flag: bs.query_history_k_data_plus(c, "date,code,close,tradestatus,peTTM,pbMRQ,isST", start_date=start, end_date=end, adjustflag=f))
                for method in ["profit", "cash_flow", "balance", "growth"]:
                    check(f"{code}-{method}", lambda c=code, m=method: getattr(bs, f"query_{m}_data")(code=c, year=date.today().year-1, quarter=4))
        finally:
            bs.logout()
    else:
        import akshare as ak
        check("names", ak.stock_info_a_code_name)
        check("history", lambda: ak.stock_zh_a_hist(symbol="600519", start_date=start.replace("-", ""), end_date=end.replace("-", ""), adjust="hfq", timeout=15))
        check("financial", lambda: ak.stock_financial_abstract(symbol="600519"))
        check("valuation", lambda: ak.stock_zh_valuation_baidu(symbol="600519", indicator="市盈率(TTM)", period="近五年"))


if __name__ == "__main__":
    main()
