#!/usr/bin/env python3
"""采集登记迁移端到端场景核验（只依赖标准库 + 本项目代码）。

覆盖：
1. 源端/计划存量迁移到 SQLite，按源端编号与关联任务整理绑定；
2. 明确任务与来源对照；对账数据集区分已落库 LANDED 与待核对 REVIEW；
3. 重建不覆盖已确认修改/种子保护；旧阶段状态与排期继续接收、报送开关；
4. 原编号/原状态兼容窗口可查；无来源计划只留核对、不分配到同编号其他来源；
5. 地址更正版本化；待执行计划只从有效绑定生成并保留地址快照；
6. 新计划送达受理判定；采样领取使用确认版本；
7. 重启进程后读出同一份已确认版本，并能解释未派发计划。

用法：MIGRATION_DB=... .venv/bin/python -I scripts/migration_scenario.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.migration.db import MigrationDB  # noqa: E402
from app.migration.service import MigrationService  # noqa: E402
from app.store import store  # noqa: E402

DB_PATH = os.environ.get("MIGRATION_DB", str(BACKEND_DIR / "data" / "scenario_migration.db"))

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, bool, str]] = []


def check(title: str, condition: bool, detail: str = "") -> None:
    results.append((title, bool(condition), detail))
    mark = "✅" if condition else "❌"
    print(f"{mark} {title}" + (f" —— {detail}" if detail else ""))
    if not condition:
        check.failed += 1


check.failed = 0


def section(name: str) -> None:
    print(f"\n===== {name} =====")


def main() -> int:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(DB_PATH + suffix).unlink(missing_ok=True)

    service = MigrationService(MigrationDB(DB_PATH))

    # ---------- 阶段一：构建对账数据集 ----------
    section("阶段一：存量迁移与对账数据集构建")
    summary = service.build_reconciliation(
        explicit_mappings=[
            {"related_task": "采集调度样例1", "source_code": "DATA-0001", "note": "值班员提供的明确对照"}
        ]
    )
    check("三个源端从内存样本表落入 SQLite", set(summary["新增源端"]) == {"DATA-0001", "DATA-0002", "DATA-0003"}, str(summary))
    check("一条计划按明确对照已落库", summary["已落库"] == 1 and summary["待核对"] == 2, str(summary))

    recon = service.list_reconciliation()
    by_plan = {row["计划编号"]: row for row in recon}
    check("SCHE-0001 已落库并绑定 DATA-0001（明确对照）",
          by_plan["SCHE-0001"]["绑定状态"] == "已落库"
          and by_plan["SCHE-0001"]["源端编号"] == "DATA-0001"
          and by_plan["SCHE-0001"]["绑定方式"] == "明确对照")
    check("SCHE-0002/0003 待核对且未绑定任何源",
          all(by_plan[p]["绑定状态"] == "待核对" and not by_plan[p]["源端编号"] for p in ("SCHE-0002", "SCHE-0003")))
    check("原状态在兼容窗口内保留（SCHE-0002=已排期、SCHE-0003=运行中）",
          by_plan["SCHE-0002"]["原状态"] == "已排期" and by_plan["SCHE-0003"]["原状态"] == "运行中")
    check("待核对记录带可读核对原因", bool(by_plan["SCHE-0003"]["核对原因"]))

    # 同编号保护：尾号相同但编号不同，不得自动分配
    no_match = service.receive_plan({
        "计划编号": "SCHE-0011",
        "关联任务": "TASK-0003",
        "调度周期": "每日",
        "开始时间": "2026-11-20",
    })
    check("同编号不同来源不得自动分配（TASK-0003 不落到 DATA-0003）",
          no_match["可受理"] is False and "DATA-0003" in no_match["受理说明"] and "不得" in no_match["受理说明"],
          no_match["受理说明"])
    orphan_recon = service.list_reconciliation("REVIEW")
    check("无来源计划只留核对记录、没有待执行计划",
          any(r["计划编号"] == "SCHE-0011" for r in orphan_recon)
          and all(p["计划编号"] != "SCHE-0011" for p in service.list_dispatch_plans()))

    # ---------- 种子保护：重建不覆盖已确认修改 ----------
    section("阶段二：数据库构建期间的已确认修改保护")
    # 模拟内存样本表仍被旧阶段改写（初始种子值）
    for row in store.rows("datasource"):
        if row["源端编号"] == "DATA-0001":
            row["接入地址"] = "SEED-TAMPER-ADDRESS"
    for row in store.rows("schedule"):
        if row["计划编号"] == "SCHE-0001":
            row["status"] = "种子篡改状态"
    rebuild = service.build_reconciliation()
    check("重建不新增源端（已确认源端不被种子覆盖）", rebuild["新增源端"] == [])
    check("已确认的 SCHE-0001 受保护跳过", rebuild["受保护跳过"] == 1, str(rebuild))
    addresses = service.list_addresses("DATA-0001")
    check("DATA-0001 初始确认地址未被种子篡改覆盖",
          addresses[0]["address"] == "数据源配置样例1" and "SEED-TAMPER" not in addresses[0]["address"])
    latest_0001 = service.compatibility_lookup("SCHE-0001")["计划对账记录"][0]
    check("已确认绑定的原状态不被旧种子值回写", latest_0001["原状态"] != "种子篡改状态")

    # ---------- 旧阶段报送 ----------
    section("阶段三：旧阶段状态/排期接收与旧登记处报送开关")
    status_intake = service.intake_legacy_record("STATUS", {
        "计划编号": "SCHE-0001", "关联任务": "采集调度样例1", "status": "已暂停",
    })
    check("旧状态记录被接收并更新兼容窗口原状态", status_intake["已接收"] and status_intake["已报送旧登记处"])
    after_status = service.compatibility_lookup("SCHE-0001")["计划对账记录"][0]
    check("原状态更新为已暂停且绑定确认版本不变",
          after_status["原状态"] == "已暂停" and after_status["源端编号"] == "DATA-0001" and after_status["已确认"])
    service.intake_legacy_record("SCHEDULE", {"计划编号": "SCHE-0002", "排期": "2026-11-21 09:00"})
    reports_before = len(service.list_legacy_reports())
    switch_off = service.set_legacy_reporting(False)
    check("可关闭向旧登记处报送", switch_off["继续向旧登记处报送"] is False)
    service.intake_legacy_record("SCHEDULE", {"计划编号": "SCHE-0002", "排期": "2026-11-22 09:00"})
    check("关闭后旧排期仍接收但不再写入旧登记处报送记录", len(service.list_legacy_reports()) == reports_before)
    check("关闭状态可查询", service.get_legacy_reporting()["继续向旧登记处报送"] is False)

    # ---------- 切换窗口：地址更正 + 新计划 + 领取 ----------
    section("阶段四：切换窗口的地址更正、计划受理与采样领取")
    generated = service.generate_dispatch_plans()
    check("待执行计划只从有效绑定生成（仅 SCHE-0001）",
          [p["计划编号"] for p in generated["待执行计划"]] == ["SCHE-0001"])
    plan_0001 = service.list_dispatch_plans("PENDING")[0]
    check("生成时固化第 1 版确认地址快照",
          plan_0001["地址版本"] == 1 and plan_0001["确认地址"] == "数据源配置样例1")

    correction = service.correct_address("DATA-0001", "https://corrected-v2.example.com/path", note="值班员收到的地址更正")
    check("地址更正生成第 2 版确认", correction["确认地址版本"] == 2)
    check("更正后源端当前版本为 2", service.list_sources()[0]["当前地址版本"] == 2)
    check("已生成的 SCHE-0001 地址快照仍停在第 1 版",
          service.list_dispatch_plans()[0]["地址版本"] == 1
          and service.list_dispatch_plans()[0]["确认地址"] == "数据源配置样例1")

    # 最后一份旧排期送达（在地址更正之后、新绑定计划之前）
    last_schedule = service.intake_legacy_record("SCHEDULE", {"计划编号": "SCHE-0010", "关联任务": "采集调度样例1"})
    check("最后一份旧排期被接收（报送已关，仅留痕）", last_schedule["已接收"] and not last_schedule["已报送旧登记处"])

    arrival = service.receive_plan({
        "计划编号": "SCHE-0010",
        "关联任务": "采集调度样例1",
        "调度周期": "每小时",
        "开始时间": "2026-11-20T10:00:00",
    })
    check("更正后送达且绑定该来源的新计划可受理", arrival["可受理"] is True, arrival["受理说明"])
    check("新计划受理时确认并固化第 2 版地址",
          arrival["待执行计划"]["地址版本"] == 2
          and arrival["待执行计划"]["确认地址"] == "https://corrected-v2.example.com/path")

    admission = service.check_plan_admission("SCHE-0010")
    check("受理查询：SCHE-0010 可受理且采样使用第 2 版地址",
          admission["可否受理"] is True and admission["采样使用地址版本"] == 2
          and admission["源端当前地址版本"] == 2)

    claim_1 = service.claim_next_sampling("值班员甲")
    check("第一次领取的是先生成的 SCHE-0001", claim_1["计划编号"] == "SCHE-0001")
    check("SCHE-0001 采样使用第 1 版确认地址（不被后续更正影响）",
          claim_1["使用地址版本"] == 1 and claim_1["使用地址"] == "数据源配置样例1"
          and claim_1["地址是否为最新确认"] is False)
    check("领取结果同时告知旧登记处报送口径", claim_1["旧登记处报送"] == "已停止报送")

    claim_2 = service.claim_next_sampling("值班员乙")
    check("第二次领取的是新送达的 SCHE-0010", claim_2["计划编号"] == "SCHE-0010")
    check("SCHE-0010 采样使用第 2 版确认地址",
          claim_2["使用地址版本"] == 2 and claim_2["地址是否为最新确认"] is True)

    claim_3 = service.claim_next_sampling("值班员丙")
    check("待核对计划不派发，队列耗尽时无可领取", claim_3["可领取"] is False)

    duplicate = service.receive_plan({"计划编号": "SCHE-0010", "关联任务": "采集调度样例1"})
    check("重复送达的计划不再受理", duplicate["可受理"] is False and "已存在" in duplicate["受理说明"])

    # ---------- 待核对记录的人工核对 ----------
    section("阶段五：人工核对与兼容查询")
    review_0002 = next(r for r in service.list_reconciliation("REVIEW") if r["计划编号"] == "SCHE-0002")
    resolved = service.resolve_review(review_0002["id"], "DATA-0002", note="人工核对确认")
    check("待核对记录可按明确来源落库并置为已确认",
          resolved["绑定状态"] == "已落库" and resolved["源端编号"] == "DATA-0002" and resolved["已确认"])
    bad = None
    try:
        service.resolve_review(99999, "DATA-0001")
    except Exception as exc:  # noqa: BLE001
        bad = str(exc)
    check("不存在的对账记录给出可读错误", bad is not None and "不存在" in bad)
    regenerated = service.generate_dispatch_plans()
    check("补核对后再生成可派发出 SCHE-0002（SCHE-0001/0010 不重复生成）",
          [p["计划编号"] for p in regenerated["待执行计划"]] == ["SCHE-0002"])

    compat_plan = service.compatibility_lookup("SCHE-0001")
    check("兼容窗口按原计划编号可查原状态与绑定",
          compat_plan["计划对账记录"][0]["原状态"] == "已暂停"
          and compat_plan["计划对账记录"][0]["源端编号"] == "DATA-0001")
    compat_source = service.compatibility_lookup("DATA-0001")
    check("兼容窗口按原源端编号可查当前地址版本", compat_source["源端"]["当前地址版本"] == 2)

    undispatched_0003 = service.explain_undispatched("SCHE-0003")
    check("可解释 SCHE-0003 为何未派发",
          undispatched_0003["是否派发"] is False and bool(undispatched_0003["未派发原因"]))
    undispatched_0011 = service.explain_undispatched("SCHE-0011")
    check("可解释同编号孤儿 SCHE-0011 为何未派发",
          undispatched_0011["是否派发"] is False and "DATA-0003" in undispatched_0011["未派发原因"])

    # ---------- 重启：新进程读同一份已确认版本 ----------
    section("阶段六：进程重启后的持久化核验")
    restart_code = (
        "import json, os, sys; "
        f"sys.path.insert(0, {str(BACKEND_DIR)!r}); "
        "from app.migration.service import MigrationService; "
        "from app.migration.db import MigrationDB; "
        "s = MigrationService(MigrationDB(os.environ['MIGRATION_DB'])); "
        "out = {"
        "'dispatch': [(p['计划编号'], p['计划状态'], p['地址版本']) for p in s.list_dispatch_plans()], "
        "'claims': [(c['待执行计划'], c['使用地址版本']) for c in s.list_claims()], "
        "'landed': len(s.list_reconciliation('LANDED')), "
        "'review': len(s.list_reconciliation('REVIEW')), "
        "'undispatched_0003': s.explain_undispatched('SCHE-0003')['是否派发'], "
        "'undispatched_0011': s.explain_undispatched('SCHE-0011')['是否派发'], "
        "'addresses_v2': s.list_addresses('DATA-0001')[-1]['version'], "
        "'legacy_switch': s.get_legacy_reporting()['继续向旧登记处报送']}; "
        "print(json.dumps(out, ensure_ascii=False))"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", restart_code],
        cwd=BACKEND_DIR,
        env={**os.environ, "MIGRATION_DB": DB_PATH},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        check("重启子进程正常退出", False, proc.stderr.strip())
    else:
        check("重启子进程正常退出", True)
        state = json.loads(proc.stdout.strip())
        check("重启后待执行计划与地址版本一致",
              state["dispatch"] == [["SCHE-0001", "已领取", 1], ["SCHE-0010", "已领取", 2], ["SCHE-0002", "待执行", 1]],
              str(state["dispatch"]))
        check("重启后两次采样领取记录仍在", state["claims"] == [[1, 1], [2, 2]], str(state["claims"]))
        check("重启后对账数据集已落库/待核对数量一致", state["landed"] >= 3 and state["review"] >= 2, str((state["landed"], state["review"])))
        check("重启后仍能查到孤儿计划未派发原因",
              state["undispatched_0003"] is False and state["undispatched_0011"] is False)
        check("重启后地址更正版本与报送开关保留",
              state["addresses_v2"] == 2 and state["legacy_switch"] is False)

    print(f"\n数据库文件：{DB_PATH}")
    print(f"核验结果：{len(results) - check.failed}/{len(results)} 通过，{check.failed} 失败")
    return 1 if check.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
