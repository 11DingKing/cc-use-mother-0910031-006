# 积分交易撮合清算

双积分（`CREDIT_A` 标的 / `CREDIT_B` 计价）限价交易 Python 后端：管理账户冻结额、
限价订单、价格时间优先级、成交回报与清算分录，撮合与扣减在同一事务完成，
全程支持幂等请求与未完成清算的幂等重放。纯标准库实现（SQLite 单库）。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/credit_exchange/`：交易后端。
  - `db.py`：连接与 `BEGIN IMMEDIATE` 立即写锁事务（WAL + busy_timeout）。
  - `schema.py`：账户、单一真值余额（available/frozen）、订单、成交、
    清算分录、总账流水、监管动作、幂等请求表。
  - `service.py`：开户/入账、冻结原语、下单/撤单/失效、价格时间优先撮合、
    成交回报（剩余量 + 成交依据）、监管暂停/恢复、请求幂等。
  - `clearing.py`：PENDING→POSTED 过账、幂等重放、买单价差释放、账实核对。
  - `api.py`：标准库 HTTP/JSON 接口（`ThreadingHTTPServer`）。
  - `admin.py`：管理命令行（含 `replay-clearing`）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约、核心交易、延迟清算重放、并发、HTTP 端到端回归测试。

## 关键设计

### 1. 订单冻结额度

- 下单即冻结：卖单冻结 A（数量），买单冻结 B（`数量×限价`），
  可用不足直接拒绝且无任何副作用。
- 成交以更优价格执行时，价差对应的冻结立即释放回可用（部分成交同理）。
- 撤单/失效只释放**剩余量**对应的冻结；已成交部分在自动过账前
  仍由 PENDING 清算分录所对应的冻结覆盖，账永远平。
- `audit_freezes` 对账公式：`frozen == 活动单义务 + PENDING 扣冻结义务`。

### 2. 价格时间优先

- 买单吃卖单：按卖价升序、同价按订单序号（时间）升序；反之降序。
- 每个成交回报带 `basis`（与谁成交、为什么是它）与 `counterparty_order_id`。

### 3. 成交清算原子性

- 下单事务内完成：冻结校验 → 写入 trade → 写 4 条 PENDING 清算分录
  → 更新双方订单 filled/status → 价差释放 → 过账（默认同事务 POSTED）。
- 每笔成交固定 4 条分录（买方 Debit B / Credit A；卖方 Debit A / Credit B），
  过账前校验完整性，不完整则回滚。
- 支持延迟过账模式（`auto_post=False` / `--deferred-clearing`），
  模拟过账前宕机：成交与 PENDING 分录已提交但未动可用余额。

### 4. 幂等重放恢复

- 写接口取请求体 `idempotency_key`（或 `Idempotency-Key` 头），
  同键同内容重放返回首次响应快照（`idempotency_replay=true`），不重复成交；
  同键不同内容返回 409 `IDEMPOTENCY_CONFLICT`。
- 撤单幂等：已撤销的订单不会被二次释放。
- 管理命令 `replay-clearing`：只处理 PENDING 分录，POSTED 永不重复应用；
  每笔成交四条分录同事务过账；可安全反复执行。
- 并发由 `BEGIN IMMEDIATE` 串行化：两个同幂等键并发请求恰好执行一次；
  撤单与撮合同事发生时，成交与撤单互斥，账实一致。

### 5. 部分成交 / 订单失效 / 监管暂停 / 并发撤单

| 场景 | 行为 |
| --- | --- |
| 部分成交 | `remaining_qty` 实时返回；冻结 = 剩余量义务（卖 A / 买 B）+ 待清算 |
| 订单失效 | `expires_at` 到期由 `expire` 命令批量失效，只释放剩余冻结，重复执行无副作用 |
| 监管暂停 | `suspend` 后拒绝新单与撮合（423），但允许撤单与清算过账；`resume` 恢复 |
| 并发撤单 | 写锁串行：先成交则撤单报错且不释放；先撤单则对手单挂起，无成交 |

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/accounts` | 开户（可带初始双积分余额） |
| POST | `/accounts/{id}/deposits` | 积分入账 |
| GET  | `/accounts/{id}/balances[?asset=]` | 查看可用/冻结 |
| POST | `/orders` | 限价下单并立即撮合，返回剩余量与成交依据 |
| POST | `/orders/{coid}/cancel` | 撤单并释放剩余冻结 |
| GET  | `/orders/{coid}` | 订单剩余量 + 全部成交依据 |
| GET  | `/order-book` / `/trades` | 盘口 / 成交回报 |
| POST | `/admin/suspend` / `/admin/resume` | 监管暂停 / 恢复 |
| POST | `/admin/expire` | 失效到期订单 |
| GET  | `/admin/clearing` | 待过账清算 |
| POST | `/admin/clearing/replay` | 重放未完成清算（幂等） |
| GET  | `/admin/audit` | 账实核对 |

## 管理命令与验证

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译
python3 -m compileall -q src tools tests

# 契约检查
python3 tools/check_contract.py domain/contract.json

# 管理命令（默认库 exchange.sqlite3，--db 可换）
python3 -m credit_exchange.admin init-db
python3 -m credit_exchange.admin open-account acc-maker 做市账户 --credit-a 1000 --credit-b 1000
python3 -m credit_exchange.admin suspend "例行监管核查"
python3 -m credit_exchange.admin expire
python3 -m credit_exchange.admin clearing-status
python3 -m credit_exchange.admin replay-clearing   # 重放未完成清算，不产生第二笔交易
python3 -m credit_exchange.admin audit             # 退出码非 0 表示账实不一致
python3 -m credit_exchange.admin serve --port 8080 # HTTP 服务
```
