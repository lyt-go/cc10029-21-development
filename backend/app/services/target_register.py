"""目标端登记领域服务：维护数据交换目标端点的接收格式、批量上限与落地确认。

本模块在确认登记后自动创建配套速率整形策略和到达对账规则，
确保目标端能按限速策略接收传输批次并完成到达核对。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TargetRegisterService(BaseModuleService):
    MODULE = "target_register"
    ENTITY = "目标端点"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["目标编号", "系统名称", "接收格式", "落地方式", "批量上限", "确认模式", "回执格式", "登记游标"]
    REQUIRED_FIELDS = ["目标编号", "系统名称", "接收格式"]
    STATUS_ORDER = ["草稿", "待校验", "已登记", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "确认登记": "已登记", "标记停用": "已停用"}
    NEGATIVE_ACTIONS = ["标记停用"]

    def register_target(self, values: dict[str, Any]) -> dict[str, Any]:
        """登记新目标端点并写入登记事件日志，初始化目标端配置快照。"""
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"缺少必填字段: {missing}")
        target_key = entry["目标编号"]
        # 初始化目标端水位
        self._write_checkpoint(f"target_{target_key}", {
            "target_key": target_key,
            "status": "registered",
            "registered_at": time.time(),
            "received_batches": 0,
            "total_records_received": 0,
        })
        # 写入登记日志
        self._write_jsonl(
            self._data_dir("events") / "target_register_log.jsonl",
            [{"action": "register", "target_key": target_key, "name": entry["系统名称"],
              "format": entry["接收格式"], "at": time.time()}]
        )
        return entry

    def confirm_register(self, target_id: int) -> dict[str, Any]:
        """确认目标端登记并自动创建配套速率整形策略和时效监控指标，保障接收链路限速与可观测。"""
        entry, msg = self.run_action(target_id, "确认登记")
        if entry is None:
            return {"error": msg, "target_id": target_id}
        target_key = entry["目标编号"]
        triggers = {}

        # 自动创建配套速率整形策略
        try:
            sc_svc = self._safe_import("app.services.speed_control", "SpeedControlService")()
            sc_entry = self._auto_create_seed("speed_control", {
                "策略编号": f"SPEE-{target_key}",
                "作用范围": f"目标端-{target_key}",
                "每秒记录数": "500",
                "每秒字节数": "1048576",
                "突发窗口": "60",
                "排队上限": "10000",
                "整形算法": "token_bucket",
                "策略版本": "v1.0",
            })
            if sc_entry:
                triggers["speed_control"] = {"created": sc_entry["策略编号"], "status": "已生效"}
        except Exception as e:
            triggers["speed_control"] = {"error": str(e)}

        # 自动创建配套时效监控指标
        try:
            sm_svc = self._safe_import("app.services.sla_monitor", "SlaMonitorService")()
            sm_entry = self._auto_create_seed("sla_monitor", {
                "指标编号": f"SLA_-{target_key}",
                "源端编号": target_key,
                "约定到达时限": "3600",
                "实际到达时长": "0",
                "超时批次数": "0",
                "迟到率": "0",
                "统计窗口": "daily",
                "指标版本": "v1.0",
            })
            if sm_entry:
                triggers["sla_monitor"] = {"created": sm_entry["指标编号"], "status": "已统计"}
        except Exception as e:
            triggers["sla_monitor"] = {"error": str(e)}

        return {
            "target_entry": entry,
            "target_key": target_key,
            "confirmed_at": time.time(),
            "auto_triggers": triggers,
        }

    def get_registered_targets(self) -> list[dict[str, Any]]:
        """获取所有已登记的目标端点列表，供下游传输批次路由和到达对账使用。"""
        return [r for r in store.rows(self.MODULE) if r.get("status") == "已登记"]

    # -- 目标端入库领用 --

    def request_ingest(self, batch_code: str) -> dict[str, Any]:
        """目标端发起入库领用请求：整批完成后才交付记录，否则返回等待结论。

        领用请求只读，不重复落地，也不改写映射批次状态：
        - 批次未完成时返回「等待本批完成」，并给出已校验但未承诺交付的条数；
        - 批次完成后返回可入库记录（按记录键去重，原合法零值只出现一条）。
        """
        fc_svc = self._safe_import("app.services.field_contract", "FieldContractService")()
        availability = fc_svc.get_ingest_availability(batch_code)
        if not availability.get("completed"):
            return {
                "batch_code": batch_code,
                "status": "等待本批完成",
                "can_ingest": False,
                "available": 0,
                "validated_count": availability.get("validated_count", 0),
                "rejected_count": availability.get("rejected_count", 0),
                "checked_count": availability.get("checked_count", 0),
                "rejected_by_category": availability.get("rejected_by_category", {}),
                "pending_keys": availability.get("pending_keys", []),
                "batch_status": availability.get("status"),
                "records": [],
            }
        records = availability.get("records", [])
        self._write_jsonl(
            self._data_dir("events") / "ingest_claim_log.jsonl",
            [{"action": "ingest_claim", "batch_code": batch_code,
              "record_count": len(records),
              "contract_version": availability.get("contract_version", ""),
              "at": time.time()}]
        )
        return {
            "batch_code": batch_code,
            "status": "可入库",
            "can_ingest": True,
            "available": len(records),
            "validated_count": availability.get("validated_count", 0),
            "rejected_count": availability.get("rejected_count", 0),
            "checked_count": availability.get("checked_count", 0),
            "rejected_by_category": availability.get("rejected_by_category", {}),
            "contract_version": availability.get("contract_version", ""),
            "records": records,
        }