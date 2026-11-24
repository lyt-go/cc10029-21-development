"""拒收回执领域服务：维护校验与对账环节被拒收记录的拒收原因归档与退回回执。

本模块在校验核对发现差异或在冲突裁决后对不满足质量要求的记录进行拒收处置，
创建拒收记录、归档拒收原因、生成退回回执并记录处置时间。

字段级拒收（`create_field_rejection`）是字段映射校验的配套能力：字段契约在
转换抽取数据时，把不合格记录的原键、字段类别（缺列/显式空/转换异常）、
源字段和当时的契约版本同步到这里，形成可核对的补交来源。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

FIELD_STAGE = "字段校验"
CATEGORY_MISSING = "缺列"
CATEGORY_EMPTY = "显式空"
CATEGORY_CONVERSION = "转换异常"
FIELD_CATEGORIES = (CATEGORY_MISSING, CATEGORY_EMPTY, CATEGORY_CONVERSION)

WAITING_RESUBMIT = "待补交"
RESUPPLIED = "已补交"
RECHECKED = "已复检"


class RejectRecordService(BaseModuleService):
    MODULE = "reject_record"
    ENTITY = "拒收记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "拒收编号", "批次编号", "记录主键", "拒收环节", "拒收原因", "回执编号",
        "退回目标", "处置时间", "字段类别", "源字段", "契约版本", "补交状态", "补交契约版本",
    ]
    REQUIRED_FIELDS = ["拒收编号", "批次编号", "记录主键"]
    STATUS_ORDER = ["草稿", "待处置", "已退回", "已丢弃"]
    ACTION_RULES = {"提交处置": "待处置", "确认退回": "已退回", "标记丢弃": "已丢弃"}
    NEGATIVE_ACTIONS = ["标记丢弃"]

    def create_rejection(self, batch_code: str, stage: str, reason: str) -> dict[str, Any]:
        """创建拒收记录：在校验或对账环节对不满足质量要求的批次创建拒收，归档原因与回执。"""
        reject_code = f"REJE-{batch_code}-{int(time.time())}"
        values = {
            "拒收编号": reject_code,
            "批次编号": batch_code,
            "记录主键": f"PK_REJ_{batch_code}",
            "拒收环节": stage,
            "拒收原因": reason,
            "回执编号": f"RC-{reject_code}",
            "退回目标": f"SOURCE_{batch_code}",
            "处置时间": time.strftime("%Y-%m-%d", time.localtime(time.time())),
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建拒收记录失败: {missing}")
        # 写入拒收日志
        self._write_jsonl(
            self._data_dir("events") / "reject_log.jsonl",
            [{"action": "reject", "reject_code": reject_code, "batch_code": batch_code,
              "stage": stage, "reason": reason, "at": time.time()}]
        )
        return {"reject_code": reject_code, "reject_id": entry["id"], "batch_code": batch_code,
                "stage": stage, "reason": reason}

    # -- 字段级拒收：字段契约 -> 拒收回执 --

    def create_field_rejection(
        self,
        *,
        batch_code: str,
        record_key: str,
        category: str,
        source_field: str = "",
        contract_version: str = "",
        source_code: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        """按记录键写入一条字段级拒收回执，明确标注缺列/显式空/转换异常。

        同批次、同记录键、同类别、同源字段、同契约版本的拒收回执是幂等的：
        重复校验不会重复写回执。回执中保留不合格数据的原键、类别和当时使用的
        契约版本，供源端按回执补交、目标端按回执核对。
        """
        key = str(record_key)
        existing = self._find_field_rejection(
            batch_code=batch_code, record_key=key, category=category,
            source_field=source_field, contract_version=contract_version,
        )
        if existing is not None:
            return {
                "reject_code": existing["拒收编号"],
                "reject_id": existing["id"],
                "batch_code": batch_code,
                "record_key": key,
                "category": category,
                "contract_version": contract_version,
                "created": False,
            }

        seq = sum(1 for row in store.rows(self.MODULE)
                  if row.get("批次编号") == batch_code) + 1
        reject_code = f"REJE-{batch_code}-{seq:03d}"
        full_reason = reason or f"{category}：{source_field or '记录'}（契约 {contract_version or '未知'}）"
        values = {
            "拒收编号": reject_code,
            "批次编号": batch_code,
            "记录主键": key,
            "拒收环节": FIELD_STAGE,
            "拒收原因": full_reason,
            "回执编号": f"RC-{reject_code}",
            "退回目标": f"SOURCE_{source_code or batch_code}",
            "处置时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
            "字段类别": category,
            "源字段": source_field,
            "契约版本": contract_version,
            "补交状态": WAITING_RESUBMIT,
            "补交契约版本": "",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建字段拒收回执失败: {missing}")
        # 回执生成后即进入待处置，便于补交清单直接读取。
        entry, _ = self.run_action(entry["id"], "提交处置")

        self._write_jsonl(
            self._data_dir("events") / "reject_log.jsonl",
            [{"action": "reject_field", "reject_code": reject_code,
              "batch_code": batch_code, "record_key": key, "category": category,
              "source_field": source_field, "contract_version": contract_version,
              "at": time.time()}]
        )
        return {
            "reject_code": reject_code,
            "reject_id": entry["id"] if entry else None,
            "batch_code": batch_code,
            "record_key": key,
            "category": category,
            "contract_version": contract_version,
            "created": True,
        }

    def _find_field_rejection(
        self, *, batch_code: str, record_key: str, category: str,
        source_field: str, contract_version: str,
    ) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if (
                row.get("拒收环节") == FIELD_STAGE
                and row.get("批次编号") == batch_code
                and str(row.get("记录主键", "")) == record_key
                and row.get("字段类别") == category
                and row.get("源字段", "") == source_field
                and row.get("契约版本", "") == contract_version
            ):
                return row
        return None

    def get_field_rejections(
        self,
        batch_code: str = "",
        record_key: str = "",
        category: str = "",
        resubmit_state: str = "",
    ) -> list[dict[str, Any]]:
        """查询字段级拒收回执，可按批次、记录键、类别和补交状态过滤。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("拒收环节") == FIELD_STAGE]
        if batch_code:
            rows = [r for r in rows if r.get("批次编号") == batch_code]
        if record_key:
            rows = [r for r in rows if str(r.get("记录主键", "")) == str(record_key)]
        if category:
            rows = [r for r in rows if r.get("字段类别") == category]
        if resubmit_state:
            rows = [r for r in rows if r.get("补交状态") == resubmit_state]
        return rows

    def mark_field_rejection_resupplied(
        self, batch_code: str, record_key: str, contract_version: str,
    ) -> int:
        """把某记录键下待补交的回执标记为已补交，并记录补交时使用的契约版本。"""
        marked = 0
        for row in self.get_field_rejections(batch_code=batch_code, record_key=record_key):
            if row.get("补交状态") == WAITING_RESUBMIT:
                row["补交状态"] = RESUPPLIED
                row["补交契约版本"] = contract_version
                row["补交时间"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                marked += 1
        return marked

    def mark_field_rejection_rechecked(
        self, batch_code: str, record_key: str, contract_version: str,
    ) -> int:
        """补交复检仍不合格时，把旧版本回执标记为已复检，保留完整的历史版本链路。"""
        marked = 0
        for row in self.get_field_rejections(batch_code=batch_code, record_key=record_key):
            if row.get("补交状态") == WAITING_RESUBMIT and row.get("契约版本") != contract_version:
                row["补交状态"] = RECHECKED
                row["补交契约版本"] = contract_version
                row["补交时间"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                marked += 1
        return marked

    def list_resubmit_requests(self, batch_code: str) -> list[dict[str, Any]]:
        """按记录键汇总待补交的空字段拒收回执，生成可核对的补交请求。

        每条请求携带原记录键、不合格类别、源字段、处理时的契约版本和回执编号，
        源端据此补交；已经补交成功或复检过的回执不再出现在请求里。
        """
        grouped: dict[str, dict[str, Any]] = {}
        for row in self.get_field_rejections(
                batch_code=batch_code, resubmit_state=WAITING_RESUBMIT):
            key = str(row.get("记录主键", ""))
            request = grouped.setdefault(key, {
                "request_code": f"RESUBMIT-{batch_code}-{key}",
                "batch_code": batch_code,
                "record_key": key,
                "categories": [],
                "fields": [],
                "contract_versions": [],
                "receipt_codes": [],
            })
            category = row.get("字段类别", "")
            if category and category not in request["categories"]:
                request["categories"].append(category)
            source_field = row.get("源字段", "")
            if source_field and source_field not in request["fields"]:
                request["fields"].append(source_field)
            version = row.get("契约版本", "")
            if version and version not in request["contract_versions"]:
                request["contract_versions"].append(version)
            if row.get("回执编号"):
                request["receipt_codes"].append(row["回执编号"])
        return list(grouped.values())

    def process_rejection(self, reject_id: int, decision: str = "return") -> dict[str, Any]:
        """处置拒收记录：按退回或丢弃决定处理不满足质量要求的条目并写入处置产物。

        本方法实现数据交换平台的质量防线：对不合格数据执行退回（返回源端修复）
        或丢弃（标记废弃），并生成处置产物文件供下游追溯。
        """
        reject_entry = self.get_entry(reject_id)
        if reject_entry is None:
            return {"error": f"拒收记录 {reject_id} 不存在"}

        entry, msg = self.run_action(reject_id, "提交处置")
        if entry is None:
            return {"error": msg, "reject_id": reject_id}

        reject_code = reject_entry["拒收编号"]
        if decision == "return":
            entry["处置时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time()))
            final_entry, _ = self.run_action(reject_id, "确认退回")
            result_action = "已退回"
        else:
            final_entry, _ = self.run_action(reject_id, "标记丢弃")
            result_action = "已丢弃"

        # 写入处置产物文件
        disposition_file = self._data_dir("dispositions") / f"{reject_code}_disposition.jsonl"
        self._write_jsonl(disposition_file, [{
            "reject_code": reject_code,
            "batch_code": reject_entry["批次编号"],
            "record_key": reject_entry.get("记录主键", ""),
            "category": reject_entry.get("字段类别", ""),
            "contract_version": reject_entry.get("契约版本", ""),
            "decision": decision,
            "stage": reject_entry["拒收环节"],
            "result": result_action,
            "processed_at": time.time(),
        }])

        return {
            "reject_code": reject_code,
            "decision": decision,
            "result": result_action,
        }

    def get_pending_rejections(self, batch_code: str = "") -> list[dict[str, Any]]:
        """获取待处置的拒收记录列表，支持按批次过滤。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("status") == "待处置"]
        if batch_code:
            rows = [r for r in rows if batch_code in r.get("批次编号", "")]
        return rows
