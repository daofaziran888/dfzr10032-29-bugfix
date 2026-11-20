"""采集登记迁移接口：构建对账数据集、处理明确对照、地址更正、计划受理与采样领取。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.migration.service import MigrationError, MigrationService
from app.schemas import ActionResult, EntryPayload, PageResult

router = APIRouter(prefix="/api/migration", tags=["采集登记迁移"])

service = MigrationService()


def _ok(message: str, **extra: Any) -> ActionResult:
    return ActionResult(ok=True, message=message, entry=extra or None)


def _fail(message: str) -> ActionResult:
    return ActionResult(ok=False, message=message)


def _page(items: list[dict[str, Any]]) -> PageResult[dict]:
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.post("/reconciliation/build", response_model=ActionResult)
def build_reconciliation(payload: EntryPayload | None = None) -> ActionResult:
    """从内存样本表构建采集登记迁移对账数据集；重复构建只补缺、不覆盖已确认记录。"""
    mappings = []
    if payload and payload.values:
        mappings = payload.values.get("mappings") or payload.values.get("明确对照") or []
    try:
        summary = service.build_reconciliation(explicit_mappings=list(mappings))
    except MigrationError as exc:
        return _fail(str(exc))
    return _ok("对账数据集构建完成", **summary)


@router.get("/reconciliation", response_model=PageResult[dict])
def list_reconciliation(
    binding_status: str | None = Query(default=None, description="LANDED 已落库 / REVIEW 待核对"),
) -> PageResult[dict]:
    """查看采集登记迁移对账数据集，可按已落库/待核对过滤。"""
    return _page(service.list_reconciliation(binding_status))


@router.post("/mappings", response_model=ActionResult)
def provide_mapping(payload: EntryPayload) -> ActionResult:
    """登记一份明确的任务与来源对照，供待核对记录与后续新计划复用。"""
    values = payload.values
    try:
        result = service.provide_mapping(
            related_task=str(values.get("关联任务") or values.get("related_task") or ""),
            source_code=str(values.get("源端编号") or values.get("source_code") or ""),
            note=values.get("备注") or values.get("note"),
        )
    except MigrationError as exc:
        return _fail(str(exc))
    return _ok("任务与来源对照已登记", **result)


@router.get("/mappings", response_model=PageResult[dict])
def list_mappings() -> PageResult[dict]:
    return _page(service.list_mappings())


@router.post("/reconciliation/{entry_id}/resolve", response_model=ActionResult)
def resolve_review(entry_id: int, payload: EntryPayload) -> ActionResult:
    """把一条待核对记录按明确来源转为已落库；不允许分配到同编号的其他来源。"""
    values = payload.values
    try:
        result = service.resolve_review(
            entry_id=entry_id,
            source_code=str(values.get("源端编号") or values.get("source_code") or ""),
            note=values.get("备注") or values.get("note"),
        )
    except MigrationError as exc:
        return _fail(str(exc))
    return _ok("待核对记录已按明确对照落库", 对账记录=result)


@router.get("/sources", response_model=PageResult[dict])
def list_sources() -> PageResult[dict]:
    return _page(service.list_sources())


@router.get("/sources/{source_code}/addresses", response_model=PageResult[dict])
def list_addresses(source_code: str) -> PageResult[dict]:
    """查看某来源各次确认的接入地址版本。"""
    try:
        items = service.list_addresses(source_code)
    except MigrationError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _page(items)


@router.post("/sources/{source_code}/address-correction", response_model=ActionResult)
def correct_address(source_code: str, payload: EntryPayload) -> ActionResult:
    """同一来源收到地址更正：生成新确认版本，已生成计划的地址快照保持不变。"""
    values = payload.values
    try:
        result = service.correct_address(
            source_code=source_code,
            new_address=str(values.get("接入地址") or values.get("address") or ""),
            note=values.get("备注") or values.get("note"),
        )
    except MigrationError as exc:
        return _fail(str(exc))
    return _ok("地址更正已确认并生成新版本", **result)


@router.post("/legacy/intake", response_model=ActionResult)
def intake_legacy_record(payload: EntryPayload) -> ActionResult:
    """接收旧阶段在切换窗口内继续提交的状态/排期记录。"""
    values = payload.values
    kind = str(values.get("kind") or values.get("记录类型") or "").strip()
    body = values.get("payload") or values.get("记录") or values
    try:
        result = service.intake_legacy_record(kind, dict(body))
    except MigrationError as exc:
        return _fail(str(exc))
    return _ok("旧阶段记录已接收", **result)


@router.get("/legacy/reporting")
def get_legacy_reporting() -> dict[str, Any]:
    """查询当前是否继续向旧登记处报送排期。"""
    return service.get_legacy_reporting()


@router.post("/legacy/reporting", response_model=ActionResult)
def set_legacy_reporting(payload: EntryPayload) -> ActionResult:
    """决定切换窗口内是否继续向旧登记处报送排期。"""
    enabled = bool(payload.values.get("enabled", payload.values.get("继续向旧登记处报送", False)))
    result = service.set_legacy_reporting(enabled)
    return _ok("旧登记处报送开关已更新", **result)


@router.get("/legacy/reports", response_model=PageResult[dict])
def list_legacy_reports() -> PageResult[dict]:
    return _page(service.list_legacy_reports())


@router.post("/plans/arrival", response_model=ActionResult)
def receive_plan(payload: EntryPayload) -> ActionResult:
    """切换窗口内新送达一条绑定来源的计划，返回此刻可否受理。"""
    try:
        result = service.receive_plan(dict(payload.values))
    except MigrationError as exc:
        return _fail(str(exc))
    return _ok("计划送达处理完成", **result)


@router.post("/dispatch/generate", response_model=ActionResult)
def generate_dispatch_plans() -> ActionResult:
    """从对账数据集的有效绑定生成可核对的待执行计划。"""
    result = service.generate_dispatch_plans()
    return _ok("待执行计划生成完成", **result)


@router.get("/dispatch", response_model=PageResult[dict])
def list_dispatch_plans(
    status: str | None = Query(default=None, description="PENDING 待执行 / CLAIMED 已领取 / CLOSED"),
) -> PageResult[dict]:
    return _page(service.list_dispatch_plans(status))


@router.post("/dispatch/claim-next", response_model=ActionResult)
def claim_next_sampling(payload: EntryPayload | None = None) -> ActionResult:
    """值班员领取下一次采样，返回实际使用的地址确认版本。"""
    operator = "值班员"
    if payload and payload.values:
        operator = str(payload.values.get("operator") or payload.values.get("值班员") or "值班员")
    result = service.claim_next_sampling(operator)
    if not result.get("可领取"):
        return _fail(result.get("说明", "没有可领取的采样"))
    return _ok("下一次采样已领取", **result)


@router.get("/claims", response_model=PageResult[dict])
def list_claims() -> PageResult[dict]:
    return _page(service.list_claims())


@router.get("/plans/{plan_code}/admission")
def check_plan_admission(plan_code: str) -> dict[str, Any]:
    """查询计划此刻可否受理、领取采样会使用哪一次确认的地址。"""
    return service.check_plan_admission(plan_code)


@router.get("/plans/{plan_code}/undispatched")
def explain_undispatched(plan_code: str) -> dict[str, Any]:
    """解释旧清单中找不到来源的计划为何没有派发（重启后仍可查）。"""
    return service.explain_undispatched(plan_code)


@router.get("/compatibility/{code}")
def compatibility_lookup(code: str) -> dict[str, Any]:
    """兼容窗口：按原编号查询原状态与迁移后的绑定、地址版本。"""
    return service.compatibility_lookup(code)


@router.get("/events", response_model=PageResult[dict])
def list_events(limit: int = 100) -> PageResult[dict]:
    return _page(service.list_events(limit=limit))
