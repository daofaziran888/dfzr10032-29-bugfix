#!/usr/bin/env python3
"""通过真实 HTTP 接口（stdlib urllib）核验迁移路由与重启可读性。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8011"
failed = 0


def call(method: str, path: str, body: dict | None = None) -> dict:
    global failed
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        failed += 1
        print(f"HTTP {exc.code} {method} {path}: {exc.read().decode()[:200]}")
        return {}


def expect(title: str, condition: bool, detail: str = "") -> None:
    global failed
    print(("✅" if condition else "❌"), title, ("—— " + detail if detail else ""))
    if not condition:
        failed += 1


# 1. 健康检查与既有模块不受影响
health = call("GET", "/api/health")
expect("健康检查正常且内存样本模块数为 17", health.get("ok") and health.get("modules") == 17, str(health))
legacy = call("GET", "/api/schedule?page=1&size=5")
expect("原采集调度列表接口仍可用（内存样本表不受影响）", legacy.get("total") == 3)

# 2. 构建对账数据集（带一份明确对照）
build = call("POST", "/api/migration/reconciliation/build", {
    "values": {"mappings": [{"related_task": "采集调度样例1", "source_code": "DATA-0001"}]}
})
expect("构建接口返回已落库 1 / 待核对 2",
       build.get("ok") and build["entry"]["已落库"] == 1 and build["entry"]["待核对"] == 2, str(build))

recon = call("GET", "/api/migration/reconciliation")
expect("对账数据集列表共 3 条", recon.get("total") == 3)
review = call("GET", "/api/migration/reconciliation?binding_status=REVIEW")
expect("待核对过滤返回 2 条", review.get("total") == 2)

# 3. 非法明确对照被拒
bad_mapping = call("POST", "/api/migration/mappings", {
    "values": {"关联任务": "采集调度样例2", "源端编号": "DATA-9999"}
})
expect("指向不存在源端的对照被拒绝", bad_mapping.get("ok") is False and "DATA-9999" in bad_mapping.get("message", ""))

# 4. 旧阶段报送与开关
intake = call("POST", "/api/migration/legacy/intake", {
    "values": {"kind": "STATUS", "payload": {"计划编号": "SCHE-0001", "status": "已暂停"}}
})
expect("旧状态记录接收并报送", intake.get("ok") and intake["entry"]["已报送旧登记处"] is True)
switch = call("POST", "/api/migration/legacy/reporting", {"values": {"enabled": False}})
expect("关闭旧登记处报送", switch["entry"]["继续向旧登记处报送"] is False)

# 5. 地址更正 -> 生成派发 -> 新计划送达 -> 领取
gen = call("POST", "/api/migration/dispatch/generate")
expect("生成一条待执行计划 SCHE-0001",
       [p["计划编号"] for p in gen["entry"]["待执行计划"]] == ["SCHE-0001"])
correction = call("POST", "/api/migration/sources/DATA-0001/address-correction", {
    "values": {"接入地址": "https://corrected-v2.example.com/path"}
})
expect("地址更正确认第 2 版", correction["entry"]["确认地址版本"] == 2)
arrival = call("POST", "/api/migration/plans/arrival", {
    "values": {"计划编号": "SCHE-0010", "关联任务": "采集调度样例1", "调度周期": "每小时"}
})
expect("新送达计划可受理并使用第 2 版地址",
       arrival["entry"]["可受理"] is True and arrival["entry"]["待执行计划"]["地址版本"] == 2)
claim1 = call("POST", "/api/migration/dispatch/claim-next", {"values": {"值班员": "甲"}})
expect("首次领取 SCHE-0001 使用第 1 版地址",
       claim1["entry"]["计划编号"] == "SCHE-0001" and claim1["entry"]["使用地址版本"] == 1)
claim2 = call("POST", "/api/migration/dispatch/claim-next", {"values": {"值班员": "乙"}})
expect("二次领取 SCHE-0010 使用第 2 版地址",
       claim2["entry"]["计划编号"] == "SCHE-0010" and claim2["entry"]["使用地址版本"] == 2)
claim3 = call("POST", "/api/migration/dispatch/claim-next")
expect("队列耗尽返回不可领取", claim3.get("ok") is False)

# 6. 受理查询 / 未派发解释 / 兼容窗口
admission = call("GET", "/api/migration/plans/SCHE-0010/admission")
expect("受理查询可查地址版本", admission.get("采样使用地址版本") == 2)
orphan = call("POST", "/api/migration/plans/arrival", {
    "values": {"计划编号": "SCHE-0021", "关联任务": "TASK-0002"}
})
expect("同编号孤儿计划受理被拒", orphan["entry"]["可受理"] is False and "DATA-0002" in orphan["entry"]["受理说明"])
why = call("GET", "/api/migration/plans/SCHE-0021/undispatched")
expect("未派发原因可查", why.get("是否派发") is False and "不得" in why.get("未派发原因", ""))
compat = call("GET", "/api/migration/compatibility/SCHE-0001")
expect("兼容窗口原编号可查原状态", compat["计划对账记录"][0]["原状态"] == "已暂停")

print(f"\nHTTP 核验：{'全部通过' if failed == 0 else f'{failed} 项失败'}")
raise SystemExit(1 if failed else 0)
