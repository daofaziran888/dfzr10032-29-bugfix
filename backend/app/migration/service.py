"""采集登记迁移业务规则。

关键口径：
- 重建对账数据集只补缺、不覆盖：源端已存在则跳过，已确认的对账记录整体跳过；
- 自动绑定只承认“关联任务 == 源端编号”的精确匹配，同编号的其他来源不得自动分配；
- 地址更正生成新版本，已生成的待执行计划保留生成时确认的地址快照；
- 待执行计划只从对账数据集中 LANDED 且已确认的有效绑定生成；
- 没有对应源的计划只留待核对记录，不派发、不猜配。
"""
from __future__ import annotations

import re
from typing import Any

from app.migration.db import MigrationDB, utc_now
from app.store import store

# 绑定状态 / 方式 / 计划状态的中英文口径
BINDING_STATUS_LABEL = {"LANDED": "已落库", "REVIEW": "待核对"}
BINDING_METHOD_LABEL = {
    "AUTO_EXACT": "精确匹配",
    "EXPLICIT": "明确对照",
    "NONE": "未绑定",
}
DISPATCH_STATUS_LABEL = {"PENDING": "待执行", "CLAIMED": "已领取", "CLOSED": "已关闭"}
ORIGIN_LABEL = {"MIGRATION": "迁移生成", "INTAKE": "新送达"}

TRAILING_DIGITS = re.compile(r"(\d+)$")


class MigrationError(ValueError):
    """迁移流程内的可读业务错误，路由层直接把消息返回给值班员。"""


class MigrationService:
    def __init__(self, db: MigrationDB | None = None) -> None:
        self.db = db or MigrationDB()

    # ============================ 序列化 ============================
    def _serialize_source(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["源端编号"] = data.pop("source_code")
        data["当前地址版本"] = data.pop("current_address_version")
        return data

    def _serialize_recon(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["计划编号"] = data.pop("plan_code")
        data["关联任务"] = data.pop("related_task")
        data["调度周期"] = data.pop("schedule_cycle")
        data["开始时间"] = data.pop("start_at")
        data["结束时间"] = data.pop("end_at")
        data["原状态"] = data.pop("legacy_status")
        data["绑定状态"] = BINDING_STATUS_LABEL.get(data["binding_status"], data["binding_status"])
        data["绑定方式"] = BINDING_METHOD_LABEL.get(data.get("binding_method") or "NONE", "未绑定")
        data["源端编号"] = data.pop("bound_source_code")
        data["地址版本"] = data.pop("bound_address_version")
        data["核对原因"] = data.pop("review_reason")
        data["已确认"] = bool(data.pop("confirmed"))
        return data

    def _serialize_dispatch(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["计划编号"] = data.pop("plan_code")
        data["源端编号"] = data.pop("source_code")
        data["地址版本"] = data.pop("address_version")
        data["确认地址"] = data.pop("address_snapshot")
        data["调度周期"] = data.pop("schedule_cycle")
        data["关联任务"] = data.pop("related_task")
        data["来源"] = ORIGIN_LABEL.get(data["origin"], data["origin"])
        data["计划状态"] = DISPATCH_STATUS_LABEL.get(data["status"], data["status"])
        data["可受理"] = bool(data.pop("accepted"))
        data["受理说明"] = data.pop("acceptance_note")
        return data

    def _serialize_claim(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["采样领取"] = data.pop("id")
        data["待执行计划"] = data.pop("dispatch_id")
        data["值班员"] = data.pop("operator")
        data["使用地址版本"] = data.pop("address_version")
        data["使用地址"] = data.pop("address_used")
        data["领取时间"] = data.pop("claimed_at")
        return data

    # ============================ 源端迁移 ============================
    def _sync_sources(self, conn) -> dict[str, dict[str, Any]]:
        """把内存样本表 datasource 登记进 SQLite；已存在的源端一律不动。"""
        synced: dict[str, dict[str, Any]] = {}
        for row in store.rows("datasource"):
            code = str(row.get("源端编号") or "").strip()
            if not code:
                continue
            if self.db.get_source(conn, code) is not None:
                # 数据库构建期间不能把已确认的修改覆盖成初始种子
                continue
            payload = {
                "source_code": code,
                "legacy_id": row.get("id"),
                "connection_type": row.get("连接类型"),
                "address": row.get("接入地址"),
                "auth_mode": row.get("认证方式"),
                "sample_cycle": row.get("采样周期"),
                "timeout_seconds": row.get("超时秒数"),
                "retry_limit": row.get("重试上限"),
                "owner": row.get("负责人"),
            }
            self.db.insert_source(conn, payload)
            synced[code] = payload
        return synced

    def list_sources(self) -> list[dict[str, Any]]:
        return [self._serialize_source(row) for row in self.db.list_sources()]

    def list_addresses(self, source_code: str) -> list[dict[str, Any]]:
        with self.db._conn() as conn:
            if self.db.get_source(conn, source_code) is None:
                raise MigrationError(self._missing_source_message(conn, source_code))
        return self.db.list_addresses(source_code)

    def _missing_source_message(self, conn, source_code: str) -> str:
        available = [row["source_code"] for row in conn.execute(
            "SELECT source_code FROM sources ORDER BY source_code"
        ).fetchall()]
        return (
            f"源端 {source_code} 不在迁移库中；当前可绑定的源端为："
            f"{'、'.join(available) if available else '（空，请先构建迁移数据集）'}"
        )

    # ============================ 任务与来源对照 ============================
    def provide_mapping(
        self, related_task: str, source_code: str, note: str | None = None
    ) -> dict[str, Any]:
        """登记一份明确的任务与来源对照；来源不存在时拒绝并给出可选项。"""
        related_task = (related_task or "").strip()
        source_code = (source_code or "").strip()
        if not related_task or not source_code:
            raise MigrationError("关联任务与源端编号都必须填写")
        with self.db._conn() as conn:
            if self.db.get_source(conn, source_code) is None:
                raise MigrationError(self._missing_source_message(conn, source_code))
            self.db.upsert_mapping(conn, related_task, source_code, note)
            self.db.add_event(
                conn,
                "MAPPING_PROVIDED",
                {"related_task": related_task, "source_code": source_code, "note": note},
                ref_code=related_task,
            )
        return {"关联任务": related_task, "源端编号": source_code, "备注": note}

    def list_mappings(self) -> list[dict[str, Any]]:
        return self.db.list_mappings()

    # ============================ 绑定解析 ============================
    def _same_number_candidates(self, conn, related_task: str) -> list[str]:
        """找出尾号相同但编号不同的来源，用于在核对原因里说明“不得跨来源分配”。"""
        match = TRAILING_DIGITS.search(related_task or "")
        if not match:
            return []
        suffix = match.group(1)
        candidates = []
        for row in conn.execute("SELECT source_code FROM sources").fetchall():
            code = row["source_code"]
            other = TRAILING_DIGITS.search(code)
            if other and other.group(1) == suffix and code != related_task:
                candidates.append(code)
        return sorted(candidates)

    def _resolve_binding(
        self, conn, related_task: str
    ) -> tuple[str | None, int | None, str, str | None]:
        """返回 (源端编号, 地址版本, 绑定方式, 核对原因)。

        顺序：明确对照 > 关联任务与源端编号完全相等；其他情况一律待核对。
        """
        mapping = self.db.get_mapping(conn, related_task)
        if mapping is not None:
            source = self.db.get_source(conn, mapping["source_code"])
            if source is not None:
                return (
                    source["source_code"],
                    source["current_address_version"],
                    "EXPLICIT",
                    None,
                )
        source = self.db.get_source(conn, related_task)
        if source is not None:
            return (
                source["source_code"],
                source["current_address_version"],
                "AUTO_EXACT",
                None,
            )
        candidates = self._same_number_candidates(conn, related_task)
        if candidates:
            reason = (
                f"关联任务「{related_task}」与任何源端编号都不完全一致；"
                f"同编号来源 {'、'.join(candidates)} 不得按编号自动分配，"
                "请提供明确的任务与来源对照后再核对"
            )
        else:
            reason = (
                f"关联任务「{related_task}」在迁移库中没有对应源端，"
                "保留核对记录、不派发；请补充明确的任务与来源对照"
            )
        return None, None, "NONE", reason

    # ============================ 构建对账数据集 ============================
    def build_reconciliation(
        self, explicit_mappings: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        """从内存样本表生成/刷新采集登记迁移对账数据集（幂等）。"""
        explicit_mappings = explicit_mappings or []
        with self.db._conn() as conn:
            # 1) 先落来源（只补缺）
            synced = self._sync_sources(conn)

            # 2) 接收随构建提供的明确对照，来源不合法直接拒绝整批
            for item in explicit_mappings:
                task = str(item.get("related_task") or item.get("关联任务") or "").strip()
                code = str(item.get("source_code") or item.get("源端编号") or "").strip()
                if not task or not code:
                    raise MigrationError("明确对照必须同时包含关联任务与源端编号")
                if self.db.get_source(conn, code) is None:
                    raise MigrationError(self._missing_source_message(conn, code))
                self.db.upsert_mapping(conn, task, code, item.get("note"))

            # 3) 已确认计划受保护，不重复进数据集
            confirmed_codes = {
                row["plan_code"]
                for row in conn.execute(
                    "SELECT DISTINCT plan_code FROM reconciliation_entries WHERE confirmed = 1"
                ).fetchall()
            }

            version = self.db.max_dataset_version(conn) + 1
            landed = 0
            review = 0
            protected = 0
            for plan in store.rows("schedule"):
                plan_code = str(plan.get("计划编号") or "").strip()
                related_task = str(plan.get("关联任务") or "").strip()
                if not plan_code:
                    continue
                if plan_code in confirmed_codes:
                    protected += 1
                    continue
                source_code, addr_version, method, reason = self._resolve_binding(
                    conn, related_task
                )
                entry = {
                    "plan_code": plan_code,
                    "legacy_id": plan.get("id"),
                    "related_task": related_task,
                    "schedule_cycle": plan.get("调度周期"),
                    "start_at": plan.get("开始时间"),
                    "end_at": plan.get("结束时间"),
                    "legacy_status": plan.get("status"),
                    "binding_status": "LANDED" if source_code else "REVIEW",
                    "binding_method": method,
                    "bound_source_code": source_code,
                    "bound_address_version": addr_version,
                    "review_reason": reason,
                }
                self.db.insert_recon_entry(conn, entry, version)
                if source_code:
                    landed += 1
                else:
                    review += 1

            self.db.add_event(
                conn,
                "RECON_BUILT",
                {
                    "dataset_version": version,
                    "sources_synced": sorted(synced),
                    "landed": landed,
                    "review": review,
                    "protected_confirmed": protected,
                    "explicit_mappings": len(explicit_mappings),
                },
            )
            return {
                "dataset_version": version,
                "新增源端": sorted(synced),
                "已落库": landed,
                "待核对": review,
                "受保护跳过": protected,
            }

    def list_reconciliation(
        self, binding_status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.db.list_recon_entries(binding_status)
        return [self._serialize_recon(row) for row in rows]

    def resolve_review(
        self, entry_id: int, source_code: str, note: str | None = None
    ) -> dict[str, Any]:
        """值班员用明确对照把一条待核对记录转为已落库（不允许猜配）。"""
        source_code = (source_code or "").strip()
        with self.db._conn() as conn:
            row = conn.execute(
                "SELECT * FROM reconciliation_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise MigrationError(f"对账记录 {entry_id} 不存在")
            if row["binding_status"] == "LANDED":
                raise MigrationError("该记录已落库，无需重复核对")
            source = self.db.get_source(conn, source_code)
            if source is None:
                raise MigrationError(self._missing_source_message(conn, source_code))
            # 同步登记任务与来源对照，后续重建/新计划直接复用
            self.db.upsert_mapping(conn, row["related_task"], source_code, note)
            updated = self.db.bind_recon_entry(
                conn,
                entry_id,
                source_code,
                source["current_address_version"],
                "EXPLICIT",
            )
            self.db.add_event(
                conn,
                "REVIEW_RESOLVED",
                {
                    "entry_id": entry_id,
                    "plan_code": row["plan_code"],
                    "source_code": source_code,
                },
                ref_code=row["plan_code"],
            )
            return self._serialize_recon(updated)

    # ============================ 地址更正（版本化） ============================
    def correct_address(
        self, source_code: str, new_address: str, note: str | None = None
    ) -> dict[str, Any]:
        new_address = (new_address or "").strip()
        if not new_address:
            raise MigrationError("新接入地址不能为空")
        with self.db._conn() as conn:
            source = self.db.get_source(conn, source_code)
            if source is None:
                raise MigrationError(self._missing_source_message(conn, source_code))
            last = conn.execute(
                "SELECT MAX(version) AS v FROM source_addresses WHERE source_code = ?",
                (source_code,),
            ).fetchone()
            new_version = int(last["v"] or 0) + 1
            confirmed_at = utc_now()
            conn.execute(
                "INSERT INTO source_addresses(source_code, version, address, "
                "change_note, confirmed_at) VALUES (?, ?, ?, ?, ?)",
                (source_code, new_version, new_address, note, confirmed_at),
            )
            conn.execute(
                "UPDATE sources SET current_address_version = ?, confirmed_at = ?, "
                "updated_at = ? WHERE source_code = ?",
                (new_version, confirmed_at, confirmed_at, source_code),
            )
            self.db.add_event(
                conn,
                "ADDRESS_CORRECTED",
                {
                    "source_code": source_code,
                    "version": new_version,
                    "address": new_address,
                    "note": note,
                },
                ref_code=source_code,
            )
            return {
                "源端编号": source_code,
                "确认地址版本": new_version,
                "确认地址": new_address,
                "确认时间": confirmed_at,
            }

    # ============================ 旧阶段报送 ============================
    def get_legacy_reporting(self) -> dict[str, Any]:
        enabled = self.db.get_setting("legacy_report_enabled", "1") == "1"
        return {"继续向旧登记处报送": enabled}

    def set_legacy_reporting(self, enabled: bool) -> dict[str, Any]:
        self.db.set_setting("legacy_report_enabled", "1" if enabled else "0")
        with self.db._conn() as conn:
            self.db.add_event(
                conn,
                "LEGACY_REPORT_SWITCH",
                {"legacy_report_enabled": enabled},
            )
        return {"继续向旧登记处报送": enabled}

    def intake_legacy_record(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """接收旧阶段在切换窗口内继续提交的状态/排期记录。"""
        kind = (kind or "").upper()
        if kind not in {"STATUS", "SCHEDULE"}:
            raise MigrationError("旧登记处记录类型只支持 STATUS（状态）或 SCHEDULE（排期）")
        plan_code = str(payload.get("计划编号") or payload.get("plan_code") or "").strip()
        related_task = str(payload.get("关联任务") or payload.get("related_task") or "").strip()
        enabled = self.db.get_setting("legacy_report_enabled", "1") == "1"
        with self.db._conn() as conn:
            reported = False
            if enabled:
                self.db.insert_legacy_report(
                    conn, kind, payload, plan_code or None, related_task or None
                )
                reported = True
            # 状态记录只刷新兼容窗口里的原状态，已确认绑定与地址快照不动
            status_note = None
            if kind == "STATUS" and plan_code:
                entry = self.db.get_recon_entry(conn, plan_code)
                if entry is not None and payload.get("status"):
                    conn.execute(
                        "UPDATE reconciliation_entries SET legacy_status = ?, "
                        "updated_at = ? WHERE id = ?",
                        (str(payload["status"]), utc_now(), entry["id"]),
                    )
                    status_note = f"原状态已更新为「{payload['status']}」，绑定与确认版本不变"
            self.db.add_event(
                conn,
                "LEGACY_INTAKE",
                {
                    "kind": kind,
                    "plan_code": plan_code,
                    "reported_to_legacy_registry": reported,
                    "note": status_note,
                },
                ref_code=plan_code or None,
            )
        return {
            "已接收": True,
            "记录类型": "状态" if kind == "STATUS" else "排期",
            "已报送旧登记处": reported,
            "说明": status_note or ("旧登记处报送已关闭，仅留存事件流水" if not reported else "已写入旧登记处报送记录"),
        }

    def list_legacy_reports(self) -> list[dict[str, Any]]:
        return self.db.list_legacy_reports()

    # ============================ 新计划送达与受理 ============================
    def receive_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """切换窗口内新送达一条绑定来源的计划，判定此刻可否受理。"""
        plan_code = str(payload.get("计划编号") or payload.get("plan_code") or "").strip()
        related_task = str(payload.get("关联任务") or payload.get("related_task") or "").strip()
        if not plan_code or not related_task:
            raise MigrationError("计划编号与关联任务为必填项")
        with self.db._conn() as conn:
            existed = self.db.get_dispatch(conn, plan_code)
            if existed is not None:
                return {
                    "可受理": False,
                    "计划编号": plan_code,
                    "受理说明": f"计划 {plan_code} 已存在（{DISPATCH_STATUS_LABEL[existed['status']]}），重复送达不再受理",
                }
            source_code, addr_version, method, reason = self._resolve_binding(
                conn, related_task
            )
            if source_code is None:
                # 无对应源：留核对记录，绝不分配到同编号的其他来源
                version = self.db.max_dataset_version(conn)
                entry = {
                    "plan_code": plan_code,
                    "legacy_id": None,
                    "related_task": related_task,
                    "schedule_cycle": payload.get("调度周期") or payload.get("schedule_cycle"),
                    "start_at": payload.get("开始时间"),
                    "end_at": payload.get("结束时间"),
                    "legacy_status": payload.get("status", "旧阶段送达"),
                    "binding_status": "REVIEW",
                    "binding_method": "NONE",
                    "bound_source_code": None,
                    "bound_address_version": None,
                    "review_reason": reason,
                }
                self.db.insert_recon_entry(conn, entry, version)
                review_row = conn.execute(
                    "SELECT * FROM reconciliation_entries WHERE plan_code = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (plan_code,),
                ).fetchone()
                self.db.add_event(
                    conn,
                    "PLAN_RECEIVED_REVIEW",
                    {"plan_code": plan_code, "related_task": related_task, "reason": reason},
                    ref_code=plan_code,
                )
                return {
                    "可受理": False,
                    "计划编号": plan_code,
                    "受理说明": reason,
                    "对账记录": self._serialize_recon(review_row),
                }

            source = self.db.get_source(conn, source_code)
            address_row = conn.execute(
                "SELECT * FROM source_addresses WHERE source_code = ? AND version = ?",
                (source_code, source["current_address_version"]),
            ).fetchone()
            # 同时进对账数据集，保持“计划与来源对照”可查
            recon_version = self.db.max_dataset_version(conn)
            self.db.insert_recon_entry(
                conn,
                {
                    "plan_code": plan_code,
                    "legacy_id": None,
                    "related_task": related_task,
                    "schedule_cycle": payload.get("调度周期") or payload.get("schedule_cycle"),
                    "start_at": payload.get("开始时间"),
                    "end_at": payload.get("结束时间"),
                    "legacy_status": payload.get("status", "旧阶段送达"),
                    "binding_status": "LANDED",
                    "binding_method": method,
                    "bound_source_code": source_code,
                    "bound_address_version": source["current_address_version"],
                    "review_reason": None,
                },
                recon_version,
            )
            dispatch_id = self.db.insert_dispatch_plan(
                conn,
                {
                    "plan_code": plan_code,
                    "source_code": source_code,
                    "address_version": source["current_address_version"],
                    "address_snapshot": address_row["address"],
                    "schedule_cycle": payload.get("调度周期") or payload.get("schedule_cycle"),
                    "related_task": related_task,
                    "origin": "INTAKE",
                    "status": "PENDING",
                    "accepted": True,
                    "acceptance_note": f"按{BINDING_METHOD_LABEL[method]}绑定 {source_code}，"
                    f"受理时确认地址为第 {source['current_address_version']} 版",
                },
            )
            self.db.add_event(
                conn,
                "PLAN_RECEIVED_ACCEPTED",
                {
                    "plan_code": plan_code,
                    "source_code": source_code,
                    "address_version": source["current_address_version"],
                    "binding_method": method,
                },
                ref_code=plan_code,
            )
            dispatch = self.db.get_dispatch_by_id(conn, dispatch_id)
            return {
                "可受理": True,
                "计划编号": plan_code,
                "受理说明": dispatch["acceptance_note"],
                "待执行计划": self._serialize_dispatch(dispatch),
            }

    # ============================ 待执行计划生成与领取 ============================
    def generate_dispatch_plans(self) -> dict[str, Any]:
        """从对账数据集的有效绑定生成可核对的待执行计划（幂等）。"""
        created: list[dict[str, Any]] = []
        with self.db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM reconciliation_entries WHERE binding_status = 'LANDED' "
                "AND confirmed = 1 ORDER BY dataset_version, id"
            ).fetchall()
            for row in rows:
                if self.db.get_dispatch(conn, row["plan_code"]) is not None:
                    continue
                source = self.db.get_source(conn, row["bound_source_code"])
                if source is None:
                    continue
                address_row = conn.execute(
                    "SELECT * FROM source_addresses WHERE source_code = ? AND version = ?",
                    (source["source_code"], source["current_address_version"]),
                ).fetchone()
                dispatch_id = self.db.insert_dispatch_plan(
                    conn,
                    {
                        "plan_code": row["plan_code"],
                        "source_code": source["source_code"],
                        "address_version": source["current_address_version"],
                        "address_snapshot": address_row["address"],
                        "schedule_cycle": row["schedule_cycle"],
                        "related_task": row["related_task"],
                        "origin": "MIGRATION",
                        "status": "PENDING",
                        "accepted": True,
                        "acceptance_note": "由对账数据集有效绑定生成，保留原计划与来源对照",
                    },
                )
                created.append(
                    self._serialize_dispatch(self.db.get_dispatch_by_id(conn, dispatch_id))
                )
            self.db.add_event(
                conn,
                "DISPATCH_GENERATED",
                {"created": [item["计划编号"] for item in created]},
            )
        return {"生成数量": len(created), "待执行计划": created}

    def list_dispatch_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        return [
            self._serialize_dispatch(row)
            for row in self.db.list_dispatch_plans(status)
        ]

    def claim_next_sampling(self, operator: str) -> dict[str, Any]:
        """值班员领取下一次采样：地址以计划生成/受理时确认的快照版本为准。"""
        operator = (operator or "值班员").strip() or "值班员"
        with self.db._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dispatch_plans WHERE status = 'PENDING' "
                "AND accepted = 1 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return {
                    "可领取": False,
                    "说明": "当前没有可领取的待执行采样；待核对计划不会派发",
                }
            self.db.update_dispatch_status(conn, row["id"], "CLAIMED")
            self.db.insert_claim(
                conn,
                row["id"],
                operator,
                row["address_version"],
                row["address_snapshot"],
            )
            source = self.db.get_source(conn, row["source_code"])
            current_version = source["current_address_version"] if source else None
            self.db.add_event(
                conn,
                "SAMPLING_CLAIMED",
                {
                    "plan_code": row["plan_code"],
                    "operator": operator,
                    "address_version_used": row["address_version"],
                    "source_current_version": current_version,
                },
                ref_code=row["plan_code"],
            )
            legacy_enabled = self.db.get_setting("legacy_report_enabled", "1") == "1"
            return {
                "可领取": True,
                "计划编号": row["plan_code"],
                "源端编号": row["source_code"],
                "使用地址版本": row["address_version"],
                "使用地址": row["address_snapshot"],
                "源端当前地址版本": current_version,
                "地址是否为最新确认": current_version == row["address_version"],
                "旧登记处报送": "继续报送" if legacy_enabled else "已停止报送",
                "领取时间": utc_now(),
                "值班员": operator,
            }

    def list_claims(self) -> list[dict[str, Any]]:
        return [self._serialize_claim(row) for row in self.db.list_claims()]

    # ============================ 核对查询 ============================
    def check_plan_admission(self, plan_code: str) -> dict[str, Any]:
        """回答：这项计划此刻可否受理、领取采样会用哪一次确认的地址。"""
        plan_code = (plan_code or "").strip()
        with self.db._conn() as conn:
            dispatch = self.db.get_dispatch(conn, plan_code)
            if dispatch is not None:
                source = self.db.get_source(conn, dispatch["source_code"])
                recon = self.db.get_recon_entry(conn, plan_code)
                method = recon["binding_method"] if recon is not None else "AUTO_EXACT"
                return {
                    "计划编号": plan_code,
                    "可否受理": bool(dispatch["accepted"]),
                    "计划状态": DISPATCH_STATUS_LABEL[dispatch["status"]],
                    "绑定源端": dispatch["source_code"],
                    "绑定方式": BINDING_METHOD_LABEL.get(method, method),
                    "采样使用地址版本": dispatch["address_version"],
                    "采样使用地址": dispatch["address_snapshot"],
                    "源端当前地址版本": source["current_address_version"] if source else None,
                    "说明": dispatch["acceptance_note"],
                }
            recon = self.db.get_recon_entry(conn, plan_code)
            if recon is not None:
                return {
                    "计划编号": plan_code,
                    "可否受理": False,
                    "绑定状态": BINDING_STATUS_LABEL[recon["binding_status"]],
                    "核对原因": recon["review_reason"],
                    "原状态": recon["legacy_status"],
                    "说明": "计划仍在待核对名单，未生成待执行采样，不能领取",
                }
            return {
                "计划编号": plan_code,
                "可否受理": False,
                "说明": "迁移库与旧清单中都查不到该计划",
            }

    def explain_undispatched(self, plan_code: str) -> dict[str, Any]:
        """解释旧清单里找不到来源的计划重启后为何没有派发。"""
        plan_code = (plan_code or "").strip()
        with self.db._conn() as conn:
            recon = self.db.get_recon_entry(conn, plan_code)
            if recon is None:
                return {"计划编号": plan_code, "说明": "对账数据集中没有该计划的记录"}
            events = [
                event
                for event in self.db.list_events(limit=500)
                if event.get("ref_code") == plan_code
            ]
            return {
                "计划编号": plan_code,
                "是否派发": False,
                "绑定状态": BINDING_STATUS_LABEL[recon["binding_status"]],
                "关联任务": recon["related_task"],
                "原状态": recon["legacy_status"],
                "未派发原因": recon["review_reason"],
                "同编号来源保护": "未自动分配到任何同编号其他来源",
                "事件流水": events,
            }

    def compatibility_lookup(self, code: str) -> dict[str, Any]:
        """兼容窗口：按原编号查原状态与迁移后的绑定/地址版本。"""
        code = (code or "").strip()
        with self.db._conn() as conn:
            source = self.db.get_source(conn, code)
            recon_rows = conn.execute(
                "SELECT * FROM reconciliation_entries WHERE plan_code = ? "
                "ORDER BY dataset_version DESC, id",
                (code,),
            ).fetchall()
            result: dict[str, Any] = {"查询编号": code}
            if source is not None:
                result["源端"] = self._serialize_source(source)
            if recon_rows:
                result["计划对账记录"] = [self._serialize_recon(row) for row in recon_rows]
            if source is None and not recon_rows:
                result["说明"] = "兼容窗口内未查到该原编号"
            return result

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.list_events(limit=limit)
