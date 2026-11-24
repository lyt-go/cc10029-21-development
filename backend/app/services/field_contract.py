"""字段契约领域服务：维护源端到目标端的字段映射契约、类型转换规则与契约版本管理。

本模块负责定义并执行字段映射转换，从源端抽取产物文件读取数据，
按契约规则转换后写入目标端产物文件，输出转换统计和校验报告。

字段映射批次的处理原则（与拒收回执模块协同）：
- 逐记录、逐字段校验抽取数据：缺列、显式空、转换异常分别拒收，
  拒收原因和原记录键同步写进拒收回执，并关联处理时的契约版本。
- 合法数值 0 是有效值，必须按 0 映射入库；无效值绝不降级成 0。
- 转换异常会暂停本批（待契约更正），更正契约版本后可从未完成范围继续。
- 已确认交付的记录键不会在继续/补交/整批重跑时再次写出。
- 只有整批完成后，目标端才能领用本批已映射数据。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

BATCH_CONVERTING = "转换中"
BATCH_NEEDS_REVISION = "待契约更正"
BATCH_WAITING_RESUBMIT = "待补交"
BATCH_COMPLETED = "已完成"

CATEGORY_MISSING = "缺列"
CATEGORY_EMPTY = "显式空"
CATEGORY_CONVERSION = "转换异常"

_INT_TYPES = {"int", "integer", "bigint", "long", "short"}
_FLOAT_TYPES = {"float", "double", "number", "decimal", "numeric"}
_BOOL_TYPES = {"bool", "boolean"}


def record_key_of(record: dict[str, Any], index: int = 0) -> str:
    """提取记录键：优先业务主键字段，缺失时退回行号，最后使用序号占位。"""
    for field in ("record_key", "记录主键", "line_no", "id"):
        value = record.get(field)
        if value is not None and str(value).strip() != "":
            return str(value)
    return f"row-{index + 1}"


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _convert_value(value: Any, data_type: str) -> Any:
    """按契约数据类型转换单个值；失败时抛出 ValueError，绝不回退成 0。"""
    dtype = (data_type or "string").strip().lower()
    if dtype in _INT_TYPES:
        if isinstance(value, bool):
            raise ValueError("布尔值不能作为整型数量")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"小数 {value} 不能作为整型数量")
            return int(value)
        text = str(value).strip()
        return int(text)
    if dtype in _FLOAT_TYPES:
        if isinstance(value, bool):
            raise ValueError("布尔值不能作为数值数量")
        value_float = float(str(value).strip())
        if value_float != value_float or value_float in (float("inf"), float("-inf")):
            raise ValueError(f"非法数值 {value}")
        return value_float
    if dtype in _BOOL_TYPES:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "是", "y", "yes"}:
            return True
        if text in {"false", "0", "否", "n", "no"}:
            return False
        raise ValueError(f"无法识别布尔值 {value}")
    # string 及其它类型按直接映射保留原值（避免把 0 变成 "0"）。
    return value


class FieldContractService(BaseModuleService):
    MODULE = "field_contract"
    ENTITY = "字段映射"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["契约编号", "源字段", "目标字段", "数据类型", "转换规则", "必填标记", "默认值", "契约版本"]
    REQUIRED_FIELDS = ["契约编号", "源字段", "目标字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = ["标记废弃"]

    # -- 契约维护 --

    def create_contract(self, source_field: str, target_field: str, data_type: str = "string") -> dict[str, Any]:
        """创建源端到目标端字段映射契约，登记转换规则与数据类型。"""
        contract_code = f"FIEL-{source_field}-{target_field}"
        values = {
            "契约编号": contract_code,
            "源字段": source_field,
            "目标字段": target_field,
            "数据类型": data_type,
            "转换规则": "直接映射" if data_type == "string" else "类型转换",
            "必填标记": "是",
            "默认值": "",
            "契约版本": "v1.0",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建字段映射契约失败: {missing}")
        entry["_managed"] = True
        self._write_jsonl(
            self._data_dir("events") / "contract_log.jsonl",
            [{"action": "create", "contract_code": contract_code, "source_field": source_field,
              "target_field": target_field, "at": time.time()}]
        )
        return entry

    def ensure_contracts(self, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按（源字段, 目标字段, 数据类型）幂等创建并生效字段契约，便于批量校验前置准备。"""
        ensured = []
        for spec in specs:
            source_field = str(spec.get("source_field", "")).strip()
            target_field = str(spec.get("target_field", "")).strip()
            if not source_field or not target_field:
                continue
            data_type = str(spec.get("data_type", "string"))
            existing = next(
                (row for row in store.rows(self.MODULE)
                 if row.get("源字段") == source_field and row.get("目标字段") == target_field),
                None,
            )
            if existing is None:
                entry = self.create_contract(source_field, target_field, data_type)
                self.run_action(entry["id"], "确认生效")
                entry = self.get_entry(entry["id"]) or entry
            else:
                existing["_managed"] = True
                if existing.get("status") != "已生效":
                    self.run_action(existing["id"], "确认生效")
                entry = existing
            ensured.append(entry)
        return ensured

    def revise_contract(
        self,
        contract_id_or_code: int | str,
        updates: dict[str, Any] | None = None,
        *,
        new_version: str | None = None,
    ) -> dict[str, Any] | None:
        """更正字段契约并推进契约版本，供转换中断后续做。

        只允许修改契约的可变属性（数据类型、转换规则、必填标记、默认值等），
        每次更正都会生成新的契约版本，新版本用于后续补交与复检。
        """
        contract = self._find_contract(contract_id_or_code)
        if contract is None:
            return None
        for field in ("数据类型", "转换规则", "必填标记", "默认值"):
            if updates and field in updates:
                contract[field] = updates[field]
        if updates:
            for field, value in updates.items():
                if field not in self.LIST_FIELDS and not field.startswith("_"):
                    contract[field] = value
        old_version = str(contract.get("契约版本", "v1.0"))
        contract["契约版本"] = new_version or self._bump_version(old_version)
        contract["_managed"] = True
        self._write_jsonl(
            self._data_dir("events") / "contract_log.jsonl",
            [{"action": "revise", "contract_code": contract.get("契约编号"),
              "from_version": old_version, "to_version": contract["契约版本"],
              "updates": updates or {}, "at": time.time()}]
        )
        return contract

    def _find_contract(self, contract_id_or_code: int | str) -> dict[str, Any] | None:
        if isinstance(contract_id_or_code, int) or str(contract_id_or_code).isdigit():
            found = store.find(self.MODULE, int(contract_id_or_code))
            if found is not None:
                return found
        for row in store.rows(self.MODULE):
            if row.get("契约编号") == contract_id_or_code:
                return row
        # 允许直接按源字段定位契约，便于按不合格字段更正。
        matches = [row for row in store.rows(self.MODULE)
                   if row.get("源字段") == contract_id_or_code]
        if len(matches) == 1:
            return matches[0]
        return None

    @staticmethod
    def _bump_version(version: str) -> str:
        text = str(version or "v1.0")
        prefix = "v" if text.lower().startswith("v") else ""
        parts = text[len(prefix):].split(".")
        try:
            numbers = [int(part) for part in parts]
        except ValueError:
            return f"{text}.1"
        if len(numbers) == 1:
            numbers.append(1)
        else:
            numbers[-1] += 1
        return prefix + ".".join(str(number) for number in numbers)

    def get_active_contracts(self) -> list[dict[str, Any]]:
        """获取所有已生效的字段映射契约列表，供下游模块按规则执行类型转换。"""
        return [r for r in store.rows(self.MODULE) if r.get("status") == "已生效"]

    # -- 抽取数据字段值校验 --

    def validate_record(
        self, record: dict[str, Any], contracts: list[dict[str, Any]], key: str = "",
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """校验一条抽取数据的字段值，返回（可入库映射，不合格明细）。

        不合格明细区分三类：缺列（记录里没有该字段）、显式空（字段存在但为空）、
        转换异常（有值但按契约类型转换失败）。合法 0 不属于任何一类。
        """
        issues: list[dict[str, Any]] = []
        mapped: dict[str, Any] = {}
        for contract in contracts:
            source_field = contract.get("源字段", "")
            target_field = contract.get("目标字段", "")
            data_type = contract.get("数据类型", "string")
            required = str(contract.get("必填标记", "是")).strip() in {"是", "true", "True", "1"}
            contract_version = str(contract.get("契约版本", "v1.0"))

            if source_field not in record:
                if required:
                    issues.append({
                        "record_key": key, "category": CATEGORY_MISSING,
                        "source_field": source_field, "target_field": target_field,
                        "contract_code": str(contract.get("契约编号", "")),
                        "contract_version": contract_version, "value": None,
                        "reason": f"缺列：记录缺少必填字段 {source_field}（契约 {contract_version}）",
                    })
                else:
                    mapped[target_field] = None
                continue

            value = record.get(source_field)
            if _is_empty(value):
                if required:
                    issues.append({
                        "record_key": key, "category": CATEGORY_EMPTY,
                        "source_field": source_field, "target_field": target_field,
                        "contract_code": str(contract.get("契约编号", "")),
                        "contract_version": contract_version, "value": value,
                        "reason": f"显式空：字段 {source_field} 有键但值为空（契约 {contract_version}）",
                    })
                else:
                    mapped[target_field] = None
                continue

            try:
                mapped[target_field] = _convert_value(value, data_type)
            except (ValueError, TypeError) as error:
                issues.append({
                    "record_key": key, "category": CATEGORY_CONVERSION,
                    "source_field": source_field, "target_field": target_field,
                    "contract_code": str(contract.get("契约编号", "")),
                    "contract_version": contract_version, "value": value,
                    "reason": f"转换异常：字段 {source_field} 的值 {value!r} 无法按 {data_type} 转换（契约 {contract_version}）：{error}",
                })
        if issues:
            return None, issues
        return mapped, []

    # -- 映射批次执行 --

    def apply_contracts(
        self,
        source_code: str,
        record_count: int = 10,
        *,
        records: list[dict[str, Any]] | None = None,
        batch_code: str | None = None,
        contract_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """对指定源端抽取产物应用字段映射契约，执行字段校验与类型转换。

        - 不传 records / batch_code 时保持原有行为：读取抽取产物或生成模拟记录，
          转换后追加写入 ``<source_code>_mapped.jsonl``。
        - 传 records 或 batch_code 时进入可续做的映射批次模式，批次状态落盘到
          ``data/mapped/<batch_code>_state.json``，用于断点继续、补交去重与领用核对。
        """
        batch_mode = records is not None or batch_code is not None
        if records is None:
            records = self._load_source_records(source_code, record_count, [])
        contracts = self._select_contracts(records, contract_codes, batch_mode=batch_mode)
        if not contracts and not batch_mode:
            # 无已生效契约时保留旧行为的默认映射，避免历史调用直接失败。
            contracts = [{
                "契约编号": f"FIEL-{source_code}",
                "源字段": "ext_a",
                "目标字段": "dst_a",
                "数据类型": "string",
                "转换规则": "直接映射",
                "契约版本": "v1.0",
            }]
        if not contracts:
            return {
                "source_code": source_code,
                "batch_code": batch_code or source_code,
                "contract_count": 0,
                "records_converted": 0,
                "conversions": 0,
                "rejected_count": 0,
                "rejected": [],
                "status": BATCH_NEEDS_REVISION,
                "batch_status": BATCH_NEEDS_REVISION,
                "message": "没有可执行的字段契约",
            }
        if not batch_mode:
            return self._apply_legacy(source_code, records, contracts, record_count)
        return self._process_batch(
            source_code=source_code,
            batch_code=batch_code or source_code,
            records=records,
            contracts=contracts,
            process_keys=None,
        )

    def continue_batch(
        self,
        batch_code: str,
        records: list[dict[str, Any]] | None = None,
        *,
        only_keys: list[str] | None = None,
        contract_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """继续未完成的映射批次（例如契约更正后继续原来转换异常的那一行）。

        只处理尚未映射、或显式指定且尚未确认的记录键；已确认的键不会重写。
        """
        state = self.get_batch_state(batch_code)
        if state is None:
            return {"error": f"映射批次 {batch_code} 不存在", "batch_code": batch_code}
        merged_records = self._merge_records(state, records or [])
        if merged_records:
            self._save_state(state)
        contracts = self._select_contracts(
            state.get("input_records"), contract_codes, batch_mode=True, state=state)
        if not contracts:
            return {"error": "没有可执行的字段契约", "batch_code": batch_code}
        return self._process_batch(
            source_code=state.get("source_code", batch_code),
            batch_code=batch_code,
            records=merged_records,
            contracts=contracts,
            process_keys=[str(key) for key in only_keys] if only_keys else None,
            state=state,
        )

    def resubmit_records(
        self,
        batch_code: str,
        records: list[dict[str, Any]],
        *,
        only_keys: list[str] | None = None,
        contract_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """源端按拒收回执补交记录：只重算未确认的键，并把对应回执标记为已补交。"""
        result = self.continue_batch(
            batch_code, records, only_keys=only_keys, contract_codes=contract_codes)
        if "error" in result:
            return result
        # 补交成功的记录键：回执已在该键落地时收敛为已补交，这里给出补交清单。
        result["resupplied_keys"] = list(result.get("resupplied_keys", []))
        return result

    def _process_batch(
        self,
        *,
        source_code: str,
        batch_code: str,
        records: list[dict[str, Any]],
        contracts: list[dict[str, Any]],
        process_keys: list[str] | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """执行一次映射批次，逐条记录校验、拒收、落地，并维护批次状态。"""
        state = state or self.get_batch_state(batch_code) or self._new_state(
            batch_code, source_code, contracts)
        # 输入快照按记录键合并，补交只覆盖对应键，不会丢掉其余行。
        records = self._merge_records(state, records)
        state["contract_versions"] = self._contract_versions(contracts)
        state["contract_version"] = self._version_label(contracts)
        state["status"] = BATCH_CONVERTING
        state["updated_at"] = time.time()

        mapped_path = self._mapped_path(batch_code)
        existing_rows = self._read_jsonl(mapped_path)
        mapped_keys = {str(row.get("_record_key")) for row in existing_rows}
        state["mapped_keys"] = sorted(mapped_keys)
        state["mapped_count"] = len(mapped_keys)

        wanted = {str(key) for key in process_keys} if process_keys else None
        added_rows: list[dict[str, Any]] = []
        mapped_now: list[str] = []
        resupplied_now: list[str] = []
        rejected_now: list[dict[str, Any]] = []
        conversion_pause = False
        pending_keys: list[str] = []

        for index, record in enumerate(records):
            key = record_key_of(record, index)
            record["record_key"] = record.get("record_key") or key
            if key in mapped_keys:
                # 已确认写入的键：无论整批重跑还是只处理未完成范围，都不再写出。
                state["records"][key] = {
                    "status": "mapped",
                    "contract_version": state["records"].get(key, {}).get(
                        "contract_version", state.get("contract_version", "")),
                    "updated_at": state["records"].get(key, {}).get(
                        "updated_at", time.time()),
                }
                continue
            if wanted is not None and key not in wanted:
                continue
            if conversion_pause:
                # 契约待更正期间，后续记录不再处理，等更正后从这些键继续。
                pending_keys.append(key)
                state["records"][key] = {"status": "pending", "updated_at": time.time()}
                continue

            mapped, issues = self.validate_record(record, contracts, key)
            if issues:
                categories = [issue["category"] for issue in issues]
                version_label = issues[0]["contract_version"]
                # 新版本复检失败：旧版本待补交回执先收敛为已复检，避免重复待补交。
                self._sync_rejection_history(key, batch_code, version_label, categories)
                receipts = self._write_field_rejections(batch_code, source_code, issues)
                state["records"][key] = {
                    "status": "rejected",
                    "categories": categories,
                    "contract_version": version_label,
                    "updated_at": time.time(),
                }
                rejected_now.append({
                    "record_key": key,
                    "category": categories[0],
                    "categories": categories,
                    "issues": issues,
                    "receipts": receipts,
                })
                if CATEGORY_CONVERSION in categories:
                    # 转换异常说明契约本身需要更正：暂停本批，等待契约修订后继续。
                    conversion_pause = True
                    state["status"] = BATCH_NEEDS_REVISION
                    state["pause_reason"] = "转换异常"
                    state["pause_key"] = key
                    state["pause_contract_version"] = version_label
                continue

            row = dict(mapped)
            row["_record_key"] = key
            row["_source_code"] = source_code
            row["_contract_version"] = self._record_contract_versions(record, contracts)
            row["_converted_at"] = time.time()
            added_rows.append(row)
            mapped_keys.add(key)
            mapped_now.append(key)
            previous = state["records"].get(key, {})
            if previous.get("status") == "rejected":
                # 补交/复检成功：把该键下待补交的回执收敛为已补交。
                self._mark_resupplied(key, batch_code, row["_contract_version"])
                resupplied_now.append(key)
            state["records"][key] = {
                "status": "mapped",
                "contract_version": row["_contract_version"],
                "updated_at": time.time(),
            }

        if added_rows:
            self._write_jsonl(mapped_path, added_rows)
        state["mapped_keys"] = sorted(mapped_keys)
        state["mapped_count"] = len(mapped_keys)

        all_keys = [record_key_of(record, index) for index, record in enumerate(records)]
        unmapped = [key for key in all_keys if key not in mapped_keys]
        state["pending_keys"] = unmapped
        rejected = [
            key for key in unmapped
            if state["records"].get(key, {}).get("status") == "rejected"
        ]
        if conversion_pause:
            state["status"] = BATCH_NEEDS_REVISION
            state["confirmed"] = False
        elif not unmapped:
            state["status"] = BATCH_COMPLETED
            state["confirmed"] = True
            state["confirmed_at"] = state.get("confirmed_at") or time.time()
        elif rejected:
            # 还有被拒收记录等待源端按回执补交，本批不能提前承诺交付。
            state["status"] = BATCH_WAITING_RESUBMIT
            state["confirmed"] = False
        else:
            state["status"] = BATCH_CONVERTING
            state["confirmed"] = False

        self._save_state(state)
        rejections = self._current_rejections(state)
        return {
            "source_code": source_code,
            "batch_code": batch_code,
            "contract_count": len(contracts),
            "contract_version": state.get("contract_version", ""),
            "records_processed": len(mapped_now) + len(rejected_now),
            "records_converted": len(added_rows),
            "conversions": sum(
                1 for row in added_rows
                for field, value in row.items()
                if not field.startswith("_") and value is not None
            ),
            "mapped_keys": mapped_now,
            "resupplied_keys": resupplied_now,
            "mapped_count": state["mapped_count"],
            "rejected": rejected_now,
            "rejected_count": len(rejected_now),
            "rejection_summary": rejections,
            "pending_keys": state.get("pending_keys", []),
            "status": state["status"],
            "batch_status": state["status"],
            "artifact_path": str(mapped_path),
        }

    def _apply_legacy(
        self,
        source_code: str,
        records: list[dict[str, Any]],
        contracts: list[dict[str, Any]],
        record_count: int,
    ) -> dict[str, Any]:
        """无批次上下文的直接转换（兼容原有调用）：校验不合格即拒收，合法值正常写出。"""
        output_file = self._data_dir("mapped") / f"{source_code}_mapped.jsonl"
        transformed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            key = record_key_of(record, index)
            mapped, issues = self.validate_record(record, contracts, key)
            if issues:
                receipts = self._write_field_rejections(source_code, source_code, issues)
                rejected.append({
                    "record_key": key,
                    "category": issues[0]["category"],
                    "categories": [issue["category"] for issue in issues],
                    "receipts": receipts,
                })
                continue
            row = dict(mapped)
            row["_record_key"] = key
            row["_source_code"] = source_code
            row["_contract_version"] = self._record_contract_versions(record, contracts)
            row["_converted_at"] = time.time()
            transformed.append(row)
        written = self._write_jsonl(output_file, transformed)
        self._write_jsonl(
            self._data_dir("events") / "contract_apply_log.jsonl",
            [{"action": "apply", "source_code": source_code, "contract_count": len(contracts),
              "records_converted": written, "rejected_count": len(rejected),
              "conversions": sum(
                  1 for row in transformed
                  for field, value in row.items()
                  if not field.startswith("_") and value is not None),
              "at": time.time()}]
        )
        return {
            "source_code": source_code,
            "contract_count": len(contracts),
            "records_converted": written,
            "conversions": sum(
                1 for row in transformed
                for field, value in row.items()
                if not field.startswith("_") and value is not None),
            "rejected_count": len(rejected),
            "rejected": rejected,
            "artifact_path": str(output_file),
        }

    # -- 批次状态与入库可用量 --

    def get_batch_state(self, batch_code: str) -> dict[str, Any] | None:
        """读取映射批次状态快照，不存在返回 None。"""
        path = self._state_path(batch_code)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            return None

    def get_mapped_records(self, batch_code: str) -> list[dict[str, Any]]:
        """读取某映射批次已落地的目标端记录，按记录键去重。"""
        unique: dict[str, dict[str, Any]] = {}
        for row in self._read_jsonl(self._mapped_path(batch_code)):
            key = str(row.get("_record_key", ""))
            if key:
                unique[key] = row
        return list(unique.values())

    def get_ingest_availability(self, batch_code: str) -> dict[str, Any]:
        """查询目标端当前可入库/已校验但未承诺交付的记录数，供领用前核对。"""
        state = self.get_batch_state(batch_code)
        if state is None:
            return {
                "batch_code": batch_code, "status": "无此批次", "completed": False,
                "available": 0, "mapped_count": 0, "rejected_count": 0,
                "pending_count": 0, "can_ingest": False,
            }
        records = self.get_mapped_records(batch_code)
        completed = state.get("status") == BATCH_COMPLETED and state.get("confirmed", False)
        rejections = self._current_rejections(state)
        return {
            "batch_code": batch_code,
            "status": state.get("status"),
            "contract_version": state.get("contract_version", ""),
            "completed": completed,
            "can_ingest": completed,
            "available": len(records) if completed else 0,
            "validated_count": len(records),
            "mapped_count": len(records),
            "rejected_count": len(rejections.get("by_key", {})),
            "rejected_by_category": rejections.get("by_category", {}),
            "checked_count": len(records) + len(rejections.get("by_key", {})),
            "pending_count": len(state.get("pending_keys", [])),
            "pending_keys": state.get("pending_keys", []),
            "records": records if completed else [],
        }

    def list_mapping_batches(self) -> list[dict[str, Any]]:
        """列出磁盘上已有的映射批次状态摘要。"""
        summaries = []
        for path in sorted(self._data_dir("mapped").glob("*_state.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
            except (json.JSONDecodeError, OSError):
                continue
            summaries.append({
                "batch_code": state.get("batch_code", path.name[:-len("_state.json")]),
                "source_code": state.get("source_code", ""),
                "status": state.get("status", ""),
                "contract_version": state.get("contract_version", ""),
                "mapped_count": state.get("mapped_count", 0),
                "pending_keys": state.get("pending_keys", []),
            })
        return summaries

    # -- 内部工具 --

    def _load_source_records(
        self, source_code: str, record_count: int, contracts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        extract_dir = self._data_dir("extracted")
        source_records: list[dict[str, Any]] = []
        for extract_file in extract_dir.glob(f"*{source_code[:8]}*.jsonl"):
            source_records = self._read_jsonl(extract_file)
            if source_records:
                break
        if not source_records:
            source_records = self._simulate_records(
                source_code, count=record_count, field_prefix="ext")
        return source_records

    def _select_contracts(
        self,
        records: list[dict[str, Any]] | None,
        contract_codes: list[str] | None,
        *,
        batch_mode: bool = False,
        state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        active = self.get_active_contracts()
        if contract_codes:
            return [c for c in active if c.get("契约编号") in contract_codes]
        if state and state.get("contract_versions"):
            # 续做沿用批次建立时的契约集合，避免被后来创建的无关契约污染。
            codes = set(state["contract_versions"])
            recorded = [c for c in active if c.get("契约编号") in codes]
            if recorded:
                return recorded
        managed = [c for c in active if c.get("_managed")]
        if managed:
            # 显式创建的契约定义了本批次字段集（含整列缺失的缺列校验），不做字段过滤。
            return managed
        if not active:
            return []
        if not batch_mode:
            return active
        if records:
            fields = {key for record in records for key in record}
            relevant = [c for c in active if c.get("源字段") in fields]
            if relevant:
                return relevant
        # 批次模式下宁可不处理，也不能拿无关的示例契约兜底。
        return []

    def _new_state(
        self, batch_code: str, source_code: str, contracts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "batch_code": batch_code,
            "source_code": source_code,
            "status": BATCH_CONVERTING,
            "contract_versions": self._contract_versions(contracts),
            "contract_version": self._version_label(contracts),
            "records": {},
            "input_records": [],
            "mapped_keys": [],
            "mapped_count": 0,
            "pending_keys": [],
            "confirmed": False,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    def _merge_records(
        self, state: dict[str, Any], new_records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按记录键合并输入快照：补交记录覆盖同键旧值，其余记录保持原顺序。"""
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for index, record in enumerate(state.get("input_records", [])):
            key = record_key_of(record, index)
            if key not in merged:
                order.append(key)
            merged[key] = record
        for index, record in enumerate(new_records):
            key = record_key_of(record, index)
            if key not in merged:
                order.append(key)
            merged[key] = record
        result = [merged[key] for key in order]
        state["input_records"] = result
        return result

    def _write_field_rejections(
        self, batch_code: str, source_code: str, issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        receipts = []
        try:
            rr_svc = self._safe_import("app.services.reject_record", "RejectRecordService")()
            for issue in issues:
                receipts.append(rr_svc.create_field_rejection(
                    batch_code=batch_code,
                    record_key=issue["record_key"],
                    category=issue["category"],
                    source_field=issue.get("source_field", ""),
                    contract_version=issue.get("contract_version", ""),
                    source_code=source_code,
                    reason=issue.get("reason", ""),
                ))
        except Exception as error:  # pragma: no cover - 拒收模块不可用时不阻断映射
            return [{"error": str(error)}]
        return receipts

    def _mark_resupplied(self, record_key: str, batch_code: str, contract_version: str) -> None:
        try:
            rr_svc = self._safe_import(
                "app.services.reject_record", "RejectRecordService")()
            rr_svc.mark_field_rejection_resupplied(batch_code, record_key, contract_version)
        except Exception:
            return

    def _sync_rejection_history(
        self, record_key: str, batch_code: str, contract_version: str,
        categories: list[str],
    ) -> None:
        """新版本复检失败时，把旧版本下仍待补交的回执收敛为已复检。

        历史版本的回执保留原契约版本，复检版本记录在补交契约版本字段，
        这样同一记录键能看清「哪一次处理用了哪版契约」。
        """
        try:
            rr_svc = self._safe_import(
                "app.services.reject_record", "RejectRecordService")()
            rr_svc.mark_field_rejection_rechecked(batch_code, record_key, contract_version)
        except Exception:
            return

    def _current_rejections(self, state: dict[str, Any]) -> dict[str, Any]:
        by_key: dict[str, list[str]] = {}
        by_category: dict[str, int] = {}
        for key, info in state.get("records", {}).items():
            if info.get("status") != "rejected":
                continue
            categories = info.get("categories") or []
            by_key[key] = categories
            for category in categories:
                by_category[category] = by_category.get(category, 0) + 1
        return {"by_key": by_key, "by_category": by_category, "count": len(by_key)}

    @staticmethod
    def _contract_versions(contracts: list[dict[str, Any]]) -> dict[str, str]:
        return {
            str(contract.get("契约编号", "")): str(contract.get("契约版本", "v1.0"))
            for contract in contracts
        }

    @staticmethod
    def _version_label(contracts: list[dict[str, Any]]) -> str:
        versions = sorted({str(contract.get("契约版本", "v1.0")) for contract in contracts})
        return ",".join(versions)

    @staticmethod
    def _record_contract_versions(
        record: dict[str, Any], contracts: list[dict[str, Any]],
    ) -> str:
        versions = sorted({str(contract.get("契约版本", "v1.0")) for contract in contracts})
        return ",".join(versions)

    def _state_path(self, batch_code: str):
        return self._data_dir("mapped") / f"{batch_code}_state.json"

    def _mapped_path(self, batch_code: str):
        return self._data_dir("mapped") / f"{batch_code}_mapped.jsonl"

    def _save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = time.time()
        path = self._state_path(state["batch_code"])
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, default=str)
