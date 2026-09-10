"""数据子进程入口；通信文件只用于结构化结果。"""
import json
import sys
from pathlib import Path

from . import providers


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    target = Path(sys.argv[2])
    try:
        operations = {"universe": providers.get_universe, "calendar": providers.get_calendar, "market_date": providers.get_market_date,
                      "prices": providers.get_prices, "financials": providers.get_financials, "peer_groups": providers.get_peer_groups, "target_market": providers.get_target_market}
        value = operations[request["operation"]](**request["arguments"])
        output = {"ok": True, "value": value}
    except Exception as exc:
        output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    target.write_text(json.dumps(output, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
