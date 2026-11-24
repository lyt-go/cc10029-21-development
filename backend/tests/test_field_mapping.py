"""字段映射四行场景测试：缺列/显式空/合法零值/转换异常的拒收明细、领用核对与补交去重。"""
from __future__ import annotations

import pytest

from app.services.base import DATA_ROOT
from app.services.field_contract import FieldContractService
from app.services.reject_record import RejectRecordService
from app.services.target_register import TargetRegisterService
from app.store import store

BATCH = "BATCH-FIELD-FOUR"


@pytest.fixture(autouse=True)
def clean_artifacts():
    from app.seed import SEED_ROWS
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    _remove_batch_files()
    yield
    _remove_batch_files()


def _remove_batch_files() -> None:
    mapped_dir = DATA_ROOT / "mapped"
    for path in mapped_dir.glob("BATCH-*"):
        path.unlink()
    for path in mapped_dir.glob("SOUR-*_mapped.jsonl"):
        if path.name != "SOUR-0001_mapped.jsonl":
            path.unlink()


def _contracts() -> list[str]:
    svc = FieldContractService()
    bil = svc.create_contract("数量", "数量目标", "int")
    code = svc.create_contract("编码", "编码目标", "string")
    name = svc.create_contract("名称", "名称目标", "string")
    for entry in (bil, code, name):
        svc.run_action(entry["id"], "确认生效")
    return [bil["契约编号"], code["契约编号"], name["契约编号"]]


def _four_rows() -> list[dict]:
    return [
        {"record_key": "K1", "编码": "A1", "名称": "缺列行"},              # 缺「数量」列
        {"record_key": "K2", "数量": "", "编码": "A2", "名称": "显式空"},   # 数量显式空
        {"record_key": "K3", "数量": 0, "编码": "A3", "名称": "合法零"},    # 数量合法 0
        {"record_key": "K4", "数量": "待更正", "编码": "A4", "名称": "转换异常"},
    ]


def test_four_row_batch_pauses_on_conversion_and_claim_waits():
    svc = FieldContractService()
    codes = _contracts()
    result = svc.apply_contracts(
        "SOUR-FIELD", records=_four_rows(), batch_code=BATCH, contract_codes=codes)

    # 前三行已核清（零值合法入库），第四行转换异常暂停本批
    assert result["batch_status"] == "待契约更正"
    assert result["mapped_keys"] == ["K3"]
    assert {item["record_key"]: item["category"] for item in result["rejected"]} == {
        "K1": "缺列", "K2": "显式空", "K4": "转换异常",
    }

    # 拒收原因分别标明类别，并关联当时的契约版本
    receipts = RejectRecordService().get_field_rejections(batch_code=BATCH)
    by_key = {row["记录主键"]: row for row in receipts}
    assert by_key["K1"]["拒收原因"].startswith("缺列")
    assert by_key["K2"]["拒收原因"].startswith("显式空")
    assert by_key["K4"]["拒收原因"].startswith("转换异常")
    assert all(row["契约版本"] == "v1.0" for row in receipts)

    # 目标端在第四行未处理完时发起领用，只能等待本批完成
    claim = TargetRegisterService().request_ingest(BATCH)
    assert claim["status"] == "等待本批完成"
    assert claim["can_ingest"] is False
    assert claim["available"] == 0
    assert claim["validated_count"] == 1
    assert claim["rejected_count"] == 3
    assert claim["checked_count"] == 4
    assert set(claim["pending_keys"]) == {"K1", "K2", "K4"}

    # 补交请求有可核对来源：原键、类别、字段、契约版本、回执编号
    requests = {item["record_key"]: item
                for item in RejectRecordService().list_resubmit_requests(BATCH)}
    assert set(requests) == {"K1", "K2", "K4"}
    assert requests["K1"]["categories"] == ["缺列"]
    assert requests["K1"]["fields"] == ["数量"]
    assert requests["K1"]["contract_versions"] == ["v1.0"]
    assert requests["K1"]["receipt_codes"]
    assert requests["K2"]["categories"] == ["显式空"]
    assert requests["K4"]["categories"] == ["转换异常"]


def test_revise_contract_resubmit_and_single_zero():
    svc = FieldContractService()
    codes = _contracts()
    svc.apply_contracts(
        "SOUR-FIELD", records=_four_rows(), batch_code=BATCH, contract_codes=codes)

    # 更正契约：int 改 string 以承接源端的占位文本
    revised = svc.revise_contract("数量", {"数据类型": "string"})
    assert revised is not None
    assert revised["契约版本"] == "v1.1"

    # 更正后继续第四行；已确认的 K3 不会再次写出
    continued = svc.continue_batch(BATCH, only_keys=["K4"], contract_codes=codes)
    assert continued["mapped_keys"] == ["K4"]
    assert continued["batch_status"] == "待补交"

    # 第二次领用：已通过校验的 K3、K4 仍不能交付，因为缺列/显式空还没补交
    claim_second = TargetRegisterService().request_ingest(BATCH)
    assert claim_second["status"] == "等待本批完成"
    assert claim_second["validated_count"] == 2
    assert claim_second["available"] == 0
    assert set(claim_second["pending_keys"]) == {"K1", "K2"}

    # 源端按拒收回执补交缺列、显式空两行；整批重跑也不能重写已确认键
    resubmit = svc.resubmit_records(
        BATCH,
        [
            {"record_key": "K1", "数量": 7, "编码": "A1", "名称": "缺列行"},
            {"record_key": "K2", "数量": 5, "编码": "A2", "名称": "显式空"},
        ],
        contract_codes=codes,
    )
    assert sorted(resubmit["mapped_keys"]) == ["K1", "K2"]
    assert sorted(resubmit["resupplied_keys"]) == ["K1", "K2"]
    assert resubmit["batch_status"] == "已完成"
    assert resubmit["mapped_count"] == 4

    # 合法零值最终只出现一条
    records = svc.get_mapped_records(BATCH)
    assert len(records) == 4
    zeros = [row for row in records if row["_record_key"] == "K3"]
    assert len(zeros) == 1
    assert zeros[0]["数量目标"] == 0

    # 整批再次应用不会写出任何重复键
    rerun = svc.apply_contracts(
        "SOUR-FIELD", records=_four_rows(), batch_code=BATCH, contract_codes=codes)
    assert rerun["mapped_keys"] == []
    assert len(svc.get_mapped_records(BATCH)) == 4

    # 回执收敛为已补交，并保留补交时使用的契约版本
    receipts = {row["记录主键"]: row for row in
                RejectRecordService().get_field_rejections(batch_code=BATCH)}
    assert receipts["K1"]["补交状态"] == "已补交"
    assert receipts["K2"]["补交状态"] == "已补交"
    assert receipts["K4"]["补交状态"] == "已补交"
    assert receipts["K1"]["补交契约版本"] == "v1.0,v1.1"
    assert "K3" not in receipts  # 合法零值从不产生拒收回执

    claim = TargetRegisterService().request_ingest(BATCH)
    assert claim["status"] == "可入库"
    assert claim["available"] == 4
    assert len(claim["records"]) == 4


def test_invalid_values_never_become_zero():
    svc = FieldContractService()
    bil = svc.create_contract("数量", "数量目标", "int")
    svc.run_action(bil["id"], "确认生效")
    result = svc.apply_contracts(
        "SOUR-INVALID",
        records=[{"record_key": "X1", "数量": "abc"}],
        batch_code="BATCH-INVALID",
    )
    assert result["rejected_count"] == 1
    assert result["mapped_keys"] == []
    assert svc.get_mapped_records("BATCH-INVALID") == []


def test_rows_after_conversion_error_stay_pending_until_revision():
    """转换异常之后的记录不能按错误契约落地，等更正后从这些键继续。"""
    svc = FieldContractService()
    codes = _contracts()
    rows = [
        {"record_key": "P1", "数量": 1, "编码": "A1", "名称": "正常"},
        {"record_key": "P2", "数量": "坏值", "编码": "A2", "名称": "转换异常"},
        {"record_key": "P3", "数量": 3, "编码": "A3", "名称": "异常之后"},
    ]
    result = svc.apply_contracts(
        "SOUR-PENDING", records=rows, batch_code="BATCH-PENDING", contract_codes=codes)
    assert result["batch_status"] == "待契约更正"
    assert result["mapped_keys"] == ["P1"]
    assert result["pending_keys"] == ["P2", "P3"]

    # 更正后继续未完成范围：P2、P3 一起处理，P1 已确认不重写
    svc.revise_contract("数量", {"数据类型": "string"})
    continued = svc.continue_batch("BATCH-PENDING", contract_codes=codes)
    assert sorted(continued["mapped_keys"]) == ["P2", "P3"]
    assert continued["batch_status"] == "已完成"
    assert continued["mapped_count"] == 3


def test_failed_recheck_marks_old_receipt_rechecked_and_records_new_version():
    """补交在新契约版本下仍不合格：旧回执标为已复检，新回执记录新版本。"""
    svc = FieldContractService()
    bil = svc.create_contract("数量", "数量目标", "int")
    svc.run_action(bil["id"], "确认生效")
    svc.apply_contracts(
        "SOUR-RECHECK",
        records=[{"record_key": "Q1", "数量": "待补交"}],
        batch_code="BATCH-RECHECK",
    )
    svc.revise_contract("数量", {"数据类型": "int"}, new_version="v1.2")
    svc.resubmit_records(
        "BATCH-RECHECK", [{"record_key": "Q1", "数量": "还是坏值"}])

    receipts = sorted(
        RejectRecordService().get_field_rejections(batch_code="BATCH-RECHECK"),
        key=lambda row: row["契约版本"],
    )
    assert [row["契约版本"] for row in receipts] == ["v1.0", "v1.2"]
    assert receipts[0]["补交状态"] == "已复检"
    assert receipts[1]["补交状态"] == "待补交"
    assert svc.get_mapped_records("BATCH-RECHECK") == []


def test_claim_does_not_duplicate_zero_after_repeated_claims():
    """重复领用只读：合法零值始终只有一条，领用不会再次落地。"""
    svc = FieldContractService()
    bil = svc.create_contract("数量", "数量目标", "int")
    svc.run_action(bil["id"], "确认生效")
    svc.apply_contracts(
        "SOUR-ZERO",
        records=[{"record_key": "Z1", "数量": 0}, {"record_key": "Z2", "数量": 0}],
        batch_code="BATCH-ZERO",
    )
    target = TargetRegisterService()
    first = target.request_ingest("BATCH-ZERO")
    second = target.request_ingest("BATCH-ZERO")
    assert first["available"] == second["available"] == 2
    assert sorted(row["数量目标"] for row in second["records"]) == [0, 0]
    assert len(svc.get_mapped_records("BATCH-ZERO")) == 2


def test_blank_string_is_not_business_zero():
    """显式空必须是拒收，不能作为 0 入库；合法数字 0 才按 0 映射。"""
    svc = FieldContractService()
    bil = svc.create_contract("数量", "数量目标", "int")
    svc.run_action(bil["id"], "确认生效")
    result = svc.apply_contracts(
        "SOUR-BLANK",
        records=[{"record_key": "B1", "数量": "   "}, {"record_key": "B2", "数量": 0}],
        batch_code="BATCH-BLANK",
    )
    assert [item["record_key"] for item in result["rejected"]] == ["B1"]
    assert result["rejected"][0]["category"] == "显式空"
    records = svc.get_mapped_records("BATCH-BLANK")
    assert len(records) == 1
    assert records[0]["_record_key"] == "B2"
    assert records[0]["数量目标"] == 0
