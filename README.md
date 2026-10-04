# 积分交易撮合清算

本项目维护积分交易撮合清算的领域约定、角色边界与样例数据，并提供一套零第三方依赖的
Python 后端（标准库 + SQLite 真实事务），供后端服务、接口和自动化验证统一使用。契约覆盖
企业申报员、核算专员、交易运营员、监管审计员，并落实四条关键约束：

| 契约不变量 | 实现方式 |
| --- | --- |
| 订单冻结额度 | 下单即冻结（买单冻结 `量×价` 的对价积分，卖单冻结数量）；`可用 = 总额 - 冻结`；成交/撤单/失效/暂停逐笔释放，冻结余额恒等于活动订单（NEW/PARTIAL）持有量之和，重放命令持续对账 |
| 价格时间优先 | 限价簿按最优价、同价按订单序号（`orders.seq`）撮合；成交价取挂单方（maker）报价 |
| 成交清算原子性 | 订单撮合与双方四科目余额扣减在同一个 `BEGIN IMMEDIATE` 事务内提交；清算分录在 `(成交,账户,资产,方向)` 上唯一，`posted` 行级闸门保证每条分录至多入账一次 |
| 幂等重放恢复 | 全部写接口凭 `X-Request-Id` 幂等（同参返回首次结果，异参拒绝）；`replay` 管理命令按成交顺序补齐/过账未完成清算，重复执行不产生第二笔成交 |

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/credit_exchange/`：撮合清算后端
  - `db.py`：表结构（账户/余额/订单/成交/回报/清算分录/幂等记录）。
  - `service.py`：冻结、限价撮合、成交回报、清算扣减、撤单、失效、监管暂停、重放对账。
  - `httpapi.py` / `server.py`：HTTP 接口（`http.server`）。
  - `cli.py`：管理命令（清算重放、演示数据）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、核心场景（部分成交、撤单、暂停、幂等、崩溃重放）、多连接并发与 HTTP 端到端测试。

## HTTP 接口

所有写接口需带 `X-Request-Id` 头（或报文 `request_id` 字段）实现网络重试幂等：

```
POST /admin/accounts                 开户
POST /admin/issue                    授信/充值（幂等）
POST /admin/markets                  建立双积分交易对
POST /admin/markets/{code}/halt      监管暂停（释放全部活动订单冻结，幂等）
POST /admin/markets/{code}/resume    恢复交易（幂等）
POST /admin/replay?dry_run=1         重放未完成清算（幂等）
POST /orders                         限价委托并即时撮合（幂等）
POST /orders/{id}/cancel             撤单（与撮合并发安全，幂等）
POST /orders/{id}/expire             订单失效（幂等）
GET  /orders/{id}                    返回剩余量、状态与逐笔成交依据（trade_id/对手方/价格/数量/时间）
GET  /accounts/{id}/balance?asset=   总额 / 冻结 / 可用
GET  /markets/{code}/book            限价簿
```

下单响应示例：

```json
{
  "order_id": "ORD-…", "side": "BUY", "price": 10, "qty": 30,
  "filled_qty": 30, "remaining_qty": 0, "avg_fill_price": 10,
  "status": "FILLED",
  "fills": [{"trade_id": "T:…", "price": 10, "qty": 30,
             "counter_order_id": "ORD-…", "counterparty_account": "B"}]
}
```

## 管理命令

```bash
PYTHONPATH=src python3 -m credit_exchange.cli seed   /path/ex.db   # 初始化双积分演示环境
PYTHONPATH=src python3 -m credit_exchange.cli replay /path/ex.db --dry-run   # 只扫描不落库
PYTHONPATH=src python3 -m credit_exchange.cli replay /path/ex.db             # 修复未过账/缺失分录并对账冻结
PYTHONPATH=src python3 -m credit_exchange.server /path/ex.db --port 8080     # 启动 HTTP 服务
```

重放输出 `entries_repaired`（MISSING_ENTRY / UNPOSTED_ENTRY）、`freeze_drift`
（账面冻结与活动订单持有不一致清单）和 `new_trades_created`（恒为 0）。
发现冻结漂移时命令以退出码 2 告警，但不自动调账，交核算专员人工处理。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
