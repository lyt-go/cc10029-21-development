"""字段映射校验与目标端领用接口：按批次执行字段校验、拒收回执核对、补交与入库领用。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.field_contract import FieldContractService
from app.services.reject_record import RejectRecordService
from app.services.target_register import TargetRegisterService

router = APIRouter(prefix="/api/field-mapping", tags=["字段映射批次"])


class MappingRecord(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class MappingApplyPayload(BaseModel):
    source_code: str
    batch_code: str
    records: list[dict[str, Any]]
    contract_codes: list[str] | None = None


class MappingContinuePayload(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    only_keys: list[str] | None = None
    contract_codes: list[str] | None = None


class ContractSpec(BaseModel):
    source_field: str
    target_field: str
    data_type: str = "string"


class ContractEnsurePayload(BaseModel):
    contracts: list[ContractSpec]


class ContractRevisionPayload(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)
    new_version: str | None = None


def _contract_service() -> FieldContractService:
    return FieldContractService()


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """列出所有映射批次的状态摘要，便于定位待更正、待补交的批次。"""
    batches = _contract_service().list_mapping_batches()
    return {"items": batches, "total": len(batches)}


@router.post("/apply")
def apply_batch(payload: MappingApplyPayload) -> dict[str, Any]:
    """用四行（或任意多行）抽取数据启动字段校验与映射批次。"""
    return _contract_service().apply_contracts(
        payload.source_code,
        records=payload.records,
        batch_code=payload.batch_code,
        contract_codes=payload.contract_codes,
    )


@router.get("/{batch_code}")
def batch_detail(batch_code: str) -> dict[str, Any]:
    """查询映射批次状态与目标端当前可入库的条数。"""
    return _contract_service().get_ingest_availability(batch_code)


@router.post("/{batch_code}/continue")
def continue_batch(batch_code: str, payload: MappingContinuePayload) -> dict[str, Any]:
    """继续未完成的映射批次（已确认的键不会重复写出）。"""
    return _contract_service().continue_batch(
        batch_code,
        payload.records,
        only_keys=payload.only_keys,
        contract_codes=payload.contract_codes,
    )


@router.post("/{batch_code}/resubmit")
def resubmit_batch(batch_code: str, payload: MappingContinuePayload) -> dict[str, Any]:
    """源端按拒收回执补交记录，只重算未确认的键。"""
    return _contract_service().resubmit_records(
        batch_code,
        payload.records,
        only_keys=payload.only_keys,
        contract_codes=payload.contract_codes,
    )


@router.post("/contracts/ensure")
def ensure_contracts(payload: ContractEnsurePayload) -> dict[str, Any]:
    """幂等准备字段契约（不存在则创建并生效），供批量校验前置使用。"""
    entries = _contract_service().ensure_contracts(
        [spec.model_dump() for spec in payload.contracts])
    return {"items": entries, "total": len(entries)}


@router.post("/contracts/{contract_ref}/revise")
def revise_contract(contract_ref: str, payload: ContractRevisionPayload) -> dict[str, Any]:
    """更正字段契约并推进版本，供转换中断后继续。"""
    contract = _contract_service().revise_contract(
        contract_ref, payload.updates, new_version=payload.new_version)
    if contract is None:
        raise HTTPException(status_code=404, detail=f"字段契约 {contract_ref} 不存在")
    return contract


@router.get("/{batch_code}/rejections")
def list_rejections(
    batch_code: str,
    category: str | None = Query(default=None),
    resubmit_state: str | None = Query(default=None),
) -> dict[str, Any]:
    """按批次查询字段级拒收回执，核对类别、原键和契约版本。"""
    rows = RejectRecordService().get_field_rejections(
        batch_code=batch_code, category=category or "", resubmit_state=resubmit_state or "")
    return {"items": rows, "total": len(rows)}


@router.get("/{batch_code}/resubmit-requests")
def list_resubmit_requests(batch_code: str) -> dict[str, Any]:
    """按拒收回执生成补交请求，供源端核对原键、类别和契约版本。"""
    requests = RejectRecordService().list_resubmit_requests(batch_code)
    return {"items": requests, "total": len(requests)}


@router.post("/{batch_code}/claim")
def claim_ingest(batch_code: str) -> dict[str, Any]:
    """目标端发起入库领用请求：整批完成后才交付可入库记录。"""
    return TargetRegisterService().request_ingest(batch_code)
