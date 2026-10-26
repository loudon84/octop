# N04 联网执行合同

状态：**READY**。本合同冻结 N04b/N04c 的持久领取、批准消费、主备执行和截止时间语义；N05 以后只能接线，不得放宽这些边界。

## 1. 身份、版本与输入

- `search_sources/default` 的 Host revision 是唯一搜索配置 revision。session 快照新增 `search_revision`；operation 创建时复制为 `search_revision`，approval 再从 operation 复制。旧 KB_ONLY 记录缺失该字段时按 `0` 读取，且不能据此联网。
- 联网领取同时绑定 `user_id`、`agent_id`、`thread_id`、`operation_id`、`approval_id`、session/search/binding/grant/config revisions。当前员工身份由 Host `reauthorize_actor(..., "authenticated")` 取得；专家权限来自 operation 已固定的 binding revision 与 `web_allowed`，不得信任调用参数。
- WEB_ONLY 的 readiness 只依赖当前搜索配置、恢复状态、员工权限与专家 `web_allowed`，不得检查知识连接、知识 probe 或知识库选择。
- 公开词必须是用户显式确认的原字符串。原词只存于批准该次 operation 的进程内存中；Host 仅保存 SHA-256 摘要、批准/领取/消费状态和计数。日志、错误、审计与验收材料不得包含完整词。重启后原词丢失，旧批准永久不可恢复。

## 2. 持久状态与原子领取

每个 operation 最多一个 `search_executions/<operation_id>`：

`PRIMARY_RESERVED -> SECONDARY_RESERVED -> COMPLETED|EMPTY|FAILED`

任一活动态还可进入 `CANCELLED|INTERRUPTED|UNKNOWN`；所有终态不可重开。记录保存来源角色/provider 快照、search revision、approval id、query digest、`attempt_count`（0..2）、领取 token 和安全结果/错误摘要，不保存公开词、凭据、响应正文或配置 URL。

首次领取必须在一个 Host CAS commit 内同时：

1. 检查 operation、approval、session、binding、search config 与恢复 intent revision；
2. 把 approval 从 `PENDING` 改成 `CONSUMED`；
3. 创建 execution 为 `PRIMARY_RESERVED` 且 `attempt_count=1`。

因此“批准已消费但未取得外发资格”与“已取得外发资格但批准仍可消费”都不可见。只有持有该持久领取 token 的调用可发请求。备用预留以 execution CAS 把计数从 1 增至 2，不再次消费批准。相同 operation 的并发/重复调用观察同一 execution 结果或失败，不得另开一轮。

预留发生后进程崩溃、Host commit 回执未知或发送结果未知，一律保守为 `UNKNOWN`：不得清零计数、恢复批准或自动重放。重启后的 service 不继承领取 token；只能观察旧记录。重试必须创建新 operation 并重新批准。

取消/中断/配置 revision 变化后拒绝新的首次或备用预留。已发请求可由资源所有者排空，但响应提交必须再次 CAS 复核；终态、撤权、配置变化或取消后不得发布迟到结果。

## 3. 请求预算与资源所有权

- `attempt_count` 是持久实际发送预留计数；每次 HTTP send 前必须先成功持久预留。单 operation 最大 2。
- 整次联网 operation 的 22 秒 deadline 从首次领取成功后的单调时钟时刻起算；每个来源从该来源 attempt 开始最多 10 秒，并受整次剩余预算进一步收紧。授权、TLS/client 构造、发送、读 body 和 parser 都消耗这些预算，不得重置。
- 两次实际 send 前分别调用 `pre_send`，复核当前身份、operation/approval 绑定、当前 search revision、恢复状态、生命周期、attempt 角色与计数。
- `SearchHttpAttempt` 拥有 TLS future、parser future 与 client；编排 service 拥有其 drain task。业务结果在 deadline 到达时立即确定。`aclose()` 可在受追踪的生命周期中继续排空，但不得把可能无界的 drain 串行等待在备用请求之前，也不得因此越过或重置 22 秒。service 的显式 `aclose()` 负责最终回收，禁止无归属后台任务。

## 4. 模式与结果

- KB_ONLY：零 Web 请求。
- WEB_ONLY：在显式有效批准后执行主备搜索，零知识请求（包括 probe）。
- HYBRID：内部检索必须先终结。内部 `EMPTY` 或失败不得由模型自动生成、补写或改写公开词；只有 operation 已显式处于 HYBRID、存在仍有效的用户原词批准且所有版本复核通过，才可外发。
- 内部成功、Web 失败时保留内部来源并标记部分失败；内部失败、Web 成功时保留 Web 来源并标记部分失败；两边失败则停止有据回答。当前用户或 operation 授权被拒绝时整体 `DENIED`，不得用任何部分成功掩盖。
- Web `EMPTY` 是正常终态且不切备用。成功只投影受验证的 WEB evidence（provider、title、snippet、locator 等）；原文打开仅使用已验证的 http/https locator，并在 N05+ 的读取入口再次授权。内部与外部证据分别授权，合并时保持各来源顺序并应用统一产品上限。

## 5. 错误切换闭集

| 主源结果 | 是否切备用 | 最终语义（无备用或备用结束） |
|---|---:|---|
| `TIMEOUT` | 是 | `FAILED/TIMEOUT` |
| HTTP 429 / `RATE_LIMITED` | 是 | `FAILED/RATE_LIMITED` |
| HTTP 5xx / `UPSTREAM_5XX` | 是 | `FAILED/UPSTREAM_5XX` |
| HTTP 401/403 / `AUTH_REJECTED` | 否 | `FAILED/AUTH_REJECTED` |
| 合法空数组 / `EMPTY` | 否 | `EMPTY` |
| 协议、schema、其它 HTTP、TLS/连接错误 | 否 | `FAILED`（对应安全错误码） |
| 当前权限、版本或恢复状态失效 | 否 | `DENIED` 或冲突 |
| caller 取消 / operation 取消 | 否 | `CANCELLED` |
| commit 回执、发送结果或崩溃点未知 | 否 | `UNKNOWN` |

adapter 不做自动 retry、redirect 或第三次请求。备用只使用首次消费时内存中的同一原词；若原词已因重启丢失，则进入 `UNKNOWN`，不能从摘要还原。

## 6. HYBRID 分支表

| 内部终态 | 显式 HYBRID + 有效批准 | Web 终态 | operation 输出 |
|---|---:|---|---|
| SUCCESS | 否 | 不执行 | 内部成功 |
| SUCCESS | 是 | SUCCESS/EMPTY | 合并成功 / 内部成功且 Web 空 |
| SUCCESS | 是 | FAILED | 内部成功 + Web 部分失败 |
| EMPTY/FAILED | 否 | 不执行 | EMPTY/FAILED，不生成 query |
| EMPTY/FAILED | 是 | SUCCESS | Web 成功 + 内部部分失败 |
| EMPTY/FAILED | 是 | EMPTY/FAILED | EMPTY/FAILED，停止有据回答 |
| 任意 | 任意 | DENIED/CANCELLED/UNKNOWN | 对应终态，不发布迟到结果 |

## 7. 测试矩阵

必须以真实 Host CAS/SQLite 证明：并发首次领取只有一个成功、approval 最多消费一次、attempt count 最大 2、重建 service 不重放活动记录、取消/配置变化拒绝预留及提交。必须以 MockTransport 和可控时钟/Event 证明：主 10 秒、整次 22 秒、仅三类错误切备、两次 `pre_send`、取消不切备、卡住 TLS/parser/drain 不阻塞业务 deadline 且没有未归属清理任务。所有矩阵均精确断言 HTTP 请求数，不使用真实 22 秒 sleep。
