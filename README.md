# 跨系统数据交换与批量传输平台

面向异构系统间批量数据交换的字段契约、增量窗口抽取、暂存落地、校验和对账、断点续传与到达核对平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 源端登记 | `source_register` | 源端系统 | 源端编号、系统名称、数据域 |
| 目标端登记 | `target_register` | 目标端点 | 目标编号、系统名称、接收格式 |
| 字段契约 | `field_contract` | 字段映射 | 契约编号、源字段、目标字段 |
| 增量窗口 | `extract_window` | 抽取窗口 | 窗口编号、源端编号、增量字段 |
| 传输批次 | `transfer_batch` | 传输批次 | 批次编号、窗口编号、目标编号 |
| 暂存落地 | `staging_area` | 暂存分区 | 暂存编号、批次编号、落地路径 |
| 校验核对 | `checksum_verify` | 校验任务 | 校验编号、批次编号、源端校验值 |
| 到达对账 | `arrival_reconcile` | 对账单 | 对账编号、批次编号、应到条数 |
| 字段冲突 | `conflict_resolve` | 冲突记录 | 冲突编号、记录主键、冲突字段 |
| 断点续传 | `resume_point` | 续传断点 | 断点编号、批次编号、已完成分片 |
| 速率整形 | `speed_control` | 限速策略 | 策略编号、作用范围、每秒记录数 |
| 拒收回执 | `reject_record` | 拒收记录 | 拒收编号、批次编号、记录主键 |
| 格式适配 | `format_adapt` | 适配版本 | 适配编号、记录格式、旧版结构 |
| 传输血缘 | `transfer_lineage` | 血缘链路 | 链路编号、源端编号、抽取窗口 |
| 批次归档 | `history_archive` | 归档卷 | 归档编号、批次范围、归档路径 |
| 历史重放 | `exchange_replay` | 重放任务 | 重放编号、归档编号、重放范围 |
| 时效监控 | `sla_monitor` | 时效指标 | 指标编号、源端编号、约定到达时限 |
| 通道配置 | `channel_config` | 传输通道 | 通道编号、传输协议、并发连接数 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 字段映射校验与目标端领用

字段契约在转换抽取数据时会逐条校验字段值，不合格记录按记录键写入拒收回执：

| 类别 | 含义 | 处理 |
| --- | --- | --- |
| 缺列 | 记录里根本没有该必填字段 | 写拒收回执并暂停等待补交 |
| 显式空 | 字段存在但值为 `null` 或空串 | 写拒收回执并暂停等待补交 |
| 转换异常 | 有值但无法按契约类型转换 | 写拒收回执并暂停本批等待契约更正 |

- 合法数值 `0` 是有效值，按 `0` 映射入库；无效值绝不降级成 `0`。
- 回执保留原记录键、字段类别、源字段和**处理时的契约版本**；补交成功后再记录补交契约版本。
- 已确认交付（已写入映射产物）的记录键，无论整批重跑还是只补交未完成范围，都不会再次写出。
- 转换异常会暂停本批；用新版本契约继续未完成范围，或按回执补交缺列/显式空记录。
- 目标端领用只在整批完成后交付可入库数据，未完成时返回「等待本批完成」及当前已通过校验的条数。

接口（后端）：

```text
POST /api/field-mapping/contracts/ensure       幂等准备字段契约
POST /api/field-mapping/contracts/{ref}/revise 更正契约并推进版本
POST /api/field-mapping/apply                  启动字段校验/映射批次
POST /api/field-mapping/{batch}/continue       继续未完成范围
POST /api/field-mapping/{batch}/resubmit       按回执补交
GET  /api/field-mapping/{batch}                批次状态与可入库条数
GET  /api/field-mapping/{batch}/rejections     字段级拒收回执
GET  /api/field-mapping/{batch}/resubmit-requests 补交请求（原键/类别/版本/回执）
POST /api/field-mapping/{batch}/claim          目标端发起入库领用
```

前端在「字段契约 / 拒收回执 / 目标端登记」页面内嵌字段映射批次面板，
并在字段契约页提供「启动四行转换批次」快捷入口。

领域：数据工程
子方向：数仓与数据湖
