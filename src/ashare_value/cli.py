"""中文命令行入口，所有日期与计划按北京时间解释。"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .pipeline import latest_due, load_rules, scan
from .reports import export_reports
from .storage import Store, digest, process_lock, utc_now


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地A股价值选股：规则筛选、证据研究、Excel和HTML报告")
    parser.add_argument("--data-dir", default="exports/value-investing", help="数据、缓存、报告和证据目录")
    parser.add_argument("--rules", help="规则JSON路径")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "due"):
        command = sub.add_parser(name, help="手动扫描" if name == "scan" else "定时检查及错过计划补跑")
        command.add_argument("--no-ai", action="store_true", help="本次只输出基础报告")
        if name == "scan":
            command.add_argument("--codes", help="逗号分隔代码，自动带入相同行业以做同行比较")
            command.add_argument("--limit", type=int, help="限制样本规模；不宣称全市场扫描")
            command.add_argument("--offline", action="store_true", help="只使用仍有效的缓存")
    sub.add_parser("demo", help="完全离线的合成流程验证")
    doctor = sub.add_parser("doctor", help="环境和订阅登录检查")
    doctor.add_argument("--probe-ai", action="store_true", help="实际调用一次CLI，会消耗现有订阅额度")
    report = sub.add_parser("report", help="从不可变快照另生成报告，不获取数据")
    report.add_argument("--snapshot")
    research = sub.add_parser("research", help="补做公告与财报AI研究")
    research.add_argument("--limit", type=int, default=20)
    research.add_argument("--force", action="store_true", help="忽略退避时刻，再试一次待处理任务")
    research.add_argument("--code", help="额外核查本快照指定公司；不改变资格")
    research.add_argument("--snapshot")
    review = sub.add_parser("review", help="从复核JSON追加正常化盈利及事件研究版本")
    review.add_argument("file", help="复核JSON文件；保留所有先前记录")
    sub.add_parser("status", help="最近快照及待处理任务")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    store = None
    try:
        rules = load_rules(args.rules)
        if args.command == "doctor":
            import importlib.metadata

            from .research import STRING, invoke_codex, login_status, object_schema
            value = {"python": sys.version.split()[0], "now_beijing": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                     "codex": login_status(), "versions": {p: importlib.metadata.version(p) for p in ("akshare", "baostock", "pandas", "openpyxl", "jsonschema", "pypdf")}}
            if args.probe_ai:
                value["actual_call"] = invoke_codex('仅返回JSON {"status":"VALUE_RESEARCH_OK"}，不用工具。', schema=object_schema({"status": STRING}), search=False, timeout=120)
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        with process_lock(args.data_dir):
            store = Store(args.data_dir)
            output = None
            if args.command == "status":
                rows = [dict(r) for r in store.db.execute("SELECT id,created_at,as_of,kind FROM snapshots ORDER BY created_at DESC,rowid DESC LIMIT 5")]
                queue = [dict(r) for r in store.db.execute("SELECT code,status,attempts,next_attempt,last_error FROM ai_tasks WHERE status IN ('pending','retry','needs_review')")]
                print(json.dumps({"snapshots": rows, "research_tasks": queue}, ensure_ascii=False, indent=2))
                return 0
            if args.command == "demo":
                from .demo import save_demo
                identifier = save_demo(store, rules)
            elif args.command == "report":
                identifier = args.snapshot or store.get()["id"]
            elif args.command == "review":
                from .evidence_review import apply_review
                identifier = apply_review(store, json.loads(Path(args.file).read_text(encoding="utf-8-sig")))
            elif args.command == "research":
                from .research import process_queue
                snap = store.get(args.snapshot)
                if snap["kind"] != "live":
                    raise ValueError("禁止将合成数据发送给 AI 作为真实公司研究")
                if args.code:
                    match = next((r["code"] for r in snap["results"] if r["code"] == args.code or r["code"].endswith("."+args.code)), None)
                    if not match:
                        raise ValueError("该快照不存在指定公司")
                    with store.db:
                        store.db.execute("INSERT OR IGNORE INTO ai_tasks(id,snapshot_id,code,next_attempt) VALUES(?,?,?,?)", (uuid4().hex, snap["id"], match, utc_now()))
                research_result = process_queue(store, rules, limit=args.limit, force=args.force, snapshot_id=snap["id"] if args.code or args.snapshot else None,
                                                code=match if args.code else None)
                print(json.dumps(research_result, ensure_ascii=False))
                for updated in research_result["updated_snapshots"]:
                    if updated != snap["id"]:
                        print(json.dumps(export_reports(store, updated), ensure_ascii=False))
                identifier = snap["id"]
            else:
                slot = latest_due() if args.command == "due" else None
                activation_path = store.root / "schedule_activation.json"
                if slot and activation_path.exists():
                    activated = json.loads(activation_path.read_text(encoding="utf-8-sig"))["activated_at"]
                    if datetime.fromisoformat(slot) < datetime.fromisoformat(activated):
                        print("尚未到安装后的第一个计划时段。")
                        return 0
                rule_hash = digest(rules)
                slot_key = slot + "|" + rule_hash if slot else None
                done = store.db.execute("SELECT snapshot_id FROM schedule_slots WHERE slot=?", (slot_key,)).fetchone() if slot else None
                if slot and not done:
                    legacy = store.db.execute("SELECT snapshot_id FROM schedule_slots WHERE slot=?", (slot,)).fetchone()
                    if legacy and store.get(legacy["snapshot_id"])["rule_hash"] == rule_hash:
                        done = legacy
                if done:
                    identifier = done["snapshot_id"]
                    if args.no_ai:
                        print("最近计划已完成，无需重复扫描。")
                        return 0
                else:
                    # A failed network scan retries after two hours instead of every scheduler tick.
                    from datetime import timezone
                    retry = store.cached("schedule_retry", 7200) if slot else None
                    if retry and retry.get("slot") == slot and retry.get("rule_hash") == rule_hash:
                        print("最近计划获取数据失败，两小时退避后重试；可用手动 scan 立即重跑。")
                        return 0
                    identifier = scan(store, rules, codes=args.codes.split(",") if getattr(args, "codes", None) else None,
                                      limit=getattr(args, "limit", None), offline=getattr(args, "offline", False), slot=slot)
                    if slot and not store.get(identifier)["coverage"]["completed"]:
                        store.cache("schedule_retry", {"slot": slot, "rule_hash": rule_hash, "attempted_at": datetime.now(timezone.utc).isoformat()})
                    output = export_reports(store, identifier)
                    if slot and store.get(identifier)["coverage"]["completed"]:
                        with store.db:
                            store.db.execute("INSERT INTO schedule_slots VALUES(?,?)", (slot_key, identifier))
                    print(json.dumps({"基础报告已保存": output}, ensure_ascii=False, indent=2), flush=True)
                if not args.no_ai:
                    from .research import process_queue
                    research_result = process_queue(store, rules)
                    print(json.dumps(research_result, ensure_ascii=False), flush=True)
                    for updated in research_result["updated_snapshots"]:
                        if updated != identifier:
                            print(json.dumps(export_reports(store, updated), ensure_ascii=False))
                    if done and research_result["completed"] == 0:
                        print("最近计划已完成，本次没有新增研究报告。")
                        return 0
                    if identifier in research_result["updated_snapshots"]:
                        output = None
            output = output or export_reports(store, identifier)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            snapshot = store.get(identifier)
            return 2 if args.command in ("scan", "due") and not snapshot["coverage"].get("completed") else 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
