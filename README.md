# 油气供应韧性与现场准入服务

本项目是一套可直接运行的 Python 服务端系统，用于记录原油基准报价、油田与终端设施、输送线路、库存批次、日提名和供应情景，并保留油田巡检机器人统计准入流程。系统面向价格连续波动、关键输油线路恢复、库存调拨和现场设备验证同时发生的运营环境，让调度、风险和审计人员在同一个 SQLite 数据库中获得可追溯结论。

供应调度子域提供以下能力：

- 原油基准报价按交易日和来源修订登记，历史版本不会被覆盖；
- 同一交易日的候选报价按授权来源分别保留，价差超过可配置容忍度（默认 0.50 美元）时自动形成争议，进入争议队列；
- 独立复核人（risk 角色）可选择任一候选、录入经核定值或退回补证，提交报价的人不能复核自己参与的记录；复核结论带决定依据并写入审计链；
- 只有已确认值进入价格摘要、情景运行和估值快照；结论一经下游使用即锁定不可覆盖，迟到候选只能开启新一轮争议，旧值与旧快照原样保留；
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

健康检查为 `GET /health`。除健康检查外，请求通过 `X-Actor-Id` 携带操作者编号。可用接口覆盖报价、报价争议、设施、线路、停运事件、库存批次、提名、能力分配、发运、供应情景、估值快照和审计链。服务重启后，SQLite 中的业务状态和历史版本会继续保留。

报价争议相关接口：

- `POST /quotes`：登记候选报价，返回 `confirmed`、`within_tolerance` 或 `disputed`；
- `POST /prices/tolerance`：risk 角色配置某基准价容忍度（美元）；
- `GET /prices/disputes`：争议队列，可按 `price_index` 过滤；
- `GET /prices/disputes/{id}`：争议详情，含全部候选与上一轮已确认值；
- `POST /prices/disputes/{id}/decisions`：复核决定，`action` 为 `select_candidate`（带 `selected_quote_id`）、`adjudicate`（带 `close_usd`）或 `return`，均须填写 `rationale`；
- `GET /prices/history/{index}?trade_date=...`：候选、确认值、争议与决定的完整历史；
- `POST /valuation/snapshots`：只用已确认值生成估值快照并锁定所用价格结论。
