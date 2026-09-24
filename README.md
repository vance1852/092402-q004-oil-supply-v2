# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 同一交易日的多个授权来源候选分别保留，差值超过可配置容忍度时自动开立争议轮次；
- 争议由独立复核人选择候选、录入核定值或退回补证，报价提交人不得复核自己的记录；
- 只有已确认值进入价格摘要、情景运行和估值快照，已使用的复核结论在数据库层不可覆盖或删除；
- 油田、储罐、终端与炼厂设施建档，线路保存日能力、在途时间和损耗规则；
- 线路停运或降容事件按 UTC 时间区间生效，日分配会计算实际可用能力；
- 库存批次保留油品、牌号、数量、单位成本和接收时间，可计算加权库存成本；
- 托运提名支持载荷级幂等、优先级分配、库存扣减和在途交接；
- 供应情景保存价格变化、线路能力变化和需求变化，审批后产生可重放的确定性结果；
- 关键写操作进入哈希串联审计日志，可离线验证事件顺序和内容完整性。

现场准入子域位于 `robot_trials` 包，负责油田巡检机器人的设备构建登记、不可变试验协议、观测分片导入、异常观测复核、统计任务租约、准入决定和审计报告。该子域不连接机器人硬件，只处理已经结构化的试验记录。

## 目录

- `src/oil_supply/`：报价、设施、线路、库存、提名、供应情景、HTTP API 与离线验收；
- `src/robot_trials/`：油田巡检机器人试验与统计准入；
- `fixtures/`：现场准入演示协议和结构化观测；
- `tests/`：核心规则、错误边界、API 和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 无第三方运行依赖

在依赖已经准备好的容器中安装：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用内存数据库和临时目录，不访问公网，也不会启动常驻服务。

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m oil_supply.acceptance --workspace .
```

该命令会在内存数据库中登记六个交易日的布伦特报价，创建油田、终端和输送线路，完成库存入账、提名分配、发运及供应情景分析，最后输出一行 JSON。成功时退出码为 `0` 且 `status` 为 `ok`。

现场准入子域也保留独立验收入口：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m oil_supply.api --database oil_supply.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、设施、线路、停运事件、库存批次、提名、能力分配、发运、供应情景和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

### 报价争议接口

- `POST /quotes`：登记一条来源候选。响应 `effect` 说明该候选的后果：`recorded`（首个来源，等待对照）、`auto_confirmed`（两个来源差值在容忍度内，自动确认）、`dispute_opened`（超过容忍度或迟到候选与已确认值冲突，返回 `dispute_id`）、`dispute_candidate_added`（争议进行中追加候选）、`within_tolerance`（与已确认值一致，不影响结论）。
- `POST /quotes/tolerance` / `GET /quotes/tolerance/{price_index}`：复核人设置按基准品种生效的美元容忍度（默认 0.50）。
- `GET /disputes`：待处理争议队列，含每条候选的来源、价格、登记人和登记时间。
- `GET /disputes/{id}`：争议详情与复核结论依据（选择候选 / 核定值 / 退回理由）。
- `POST /disputes/{id}/decide`：复核人提交结论，载荷为 `{"decision": "select", "quote_id": 12, "reason": "..."}`、`{"decision": "adjudicate", "close_usd": "98.55", "reason": "..."}` 或 `{"decision": "return", "reason": "..."}`。
- `GET /disputes/history?price_index=BRENT&trade_date=2026-09-23`：历史争议与决定依据读取。
- `POST /valuations/snapshots` / `GET /valuations/snapshots/{id}`：基于已确认价格生成持仓估值快照；快照生成时对应确认值被标记为已使用。

复核权限属于 `risk` 角色；报价提交人对自己参与的争议自动失去复核资格。退回补证后到达的新证据会开启新一轮争议；迟到候选若与已使用的确认值差值超容忍度，同样只开启新一轮，旧结论及引用它的快照保持不变。所有报价、争议和复核动作进入哈希串联审计日志。
