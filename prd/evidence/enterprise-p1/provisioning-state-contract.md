# I03a3 本地状态与四动作实施合同

状态：四本地动作实现经 Fix2 独立 Spec/Quality PASS，F1–F8/G1全部关闭；最终两测试模块116项、前轮三文件106项及对应静态通过。仅为本地动作验收，不代表worker/远端/清理完成；原规划已独立 Spec/Builder PASS（task-I03a3-local-planning-review.md 最终复核）；I03a2 Fix2 已实际独立 Spec/Quality PASS，helper/header 接缝已核对；root 捕获八路径编辑前基线并标记 brief READY 后实施，不勾 3.1/1.2。后续功能/测试仍由指定 GPT-6 Luna 执行。本合同仅实现 `provisioning.status/retry/tasks.list/task.retry`；无 worker、远端 HTTP、provider、claim/scan 执行或空 handler。a1 十一公开 DTO 原样复用。

## 1. 已验证接缝与固定原则

- 工作树实码：`provisioning.py` 有 a2 `ProvisioningService`、`_now/_payload/_bad_record/_host_error/_fresh_admin/_settle`；`provisioning_records.py` 有 config/anchor/block/preparation 与 a2 retained header；`provisioning_actions.py` 现仅三 admin 动作。`main.py` 已调用 `register_provisioning_actions` 且登记 identity consumer，a3 不改 main/Core。
- Host facade 实码提供 `reauthorize_actor`、`read_user_identity`、`list_user_identities`、普通/retained 定点读、`commit`；混合提交要求每个普通写/activation/deletion owner 有 identity check、`requirements=()`；RecordCheck 仅比 revision。facade 无 wake 方法；本批不造唤醒。a2 `_settle` 用持有 operation task + shield 等待真实结束；写动作复用该 settlement 语义，未知提交读精确键核实，不按被取消的 await 推定未写。
- 状态动作不 resolve/admin/bootstrap/Key 密文，不生成远端 proof，不写 retained intent/receipt/claim；内部记录/DTO 绝不含密码、JWT、Key、原始远端响应。a2 `get_config` 为计算 `credential_configured` 会 resolve 管理员密文，四动作**不得调用它**。本批只按 config/anchor/精确 auth-block 本地不变量投影“允许进入后续检查”；不能由该投影宣称 active material 仍可 resolve、管理员 JWT 远端有效或个人 Key 可检索。后续真正使用者仍须独立验证 active 凭据/权限，失败转 CONFIG_REQUIRED 或相应阻塞。Host/error 转换复用 a2 固定安全码和 en/zh。

## 2. 七命名空间完整形状与本批落盘边界

通用：业务 payload 都**显式必需**严格 `schema_id="octop.enterprise.provisioning.v1"`、`version=1`；a3 内部基类用无默认值字段（可覆写 a2 `_ProvisioningOutput` 的默认值，或独立继承 `StrictDto`），从 raw payload 读取时先确认这两键确实存在再校验。不能依赖现有 a2 `_ProvisioningOutput` 的默认值把缺失 header 自动补成 v1；读取 a2 config/anchor/block 也先检查 raw header，a2 Fix2 的严格必需 header 已独立验收，直接复用，不重复增加 raw-header 验证框架。Pydantic `extra=forbid/frozen/strict`。`U`=canonical UUIDv4；`T`=a1 显式 UTC；`I+`=StrictInt>0；`N`=StrictInt>=0；`S`=非空 StrictStr。可空字段必须显式存在。整数拒 bool/float/string；Host envelope 是唯一自身 revision，payload 不复制；普通 owner 只在 envelope。未知版本/坏行/关系损坏→安全 UNAVAILABLE，不当缺行。以下完整字段供未来兼容，但本批只创建/更新第 1、2 行；其余仅严格模型/有界读和约束，不注册任何写路径。

| namespace / key / 类别 | payload 业务字段（统一头除外） | 本批 |
|---|---|---|
| `provision_tasks` / 十进制 local user_id / ordinary、员工 owner | `task_id:U, identity:Identity, state:a1 state, error_code:a1 code|null, checkpoint:NONE|USER_CONFIRMED|OWNER_CONFIRMED|KB_CONFIRMED|KEY_STORED|READY, domain_id:U?, base_url:S?, user_intent_id:U?, kb_intent_id:U?, key_intent_id:U?, bootstrap_ref:U?, bootstrap_revision:Literal[2]?, bootstrap_created_at:T?, bootstrap_expires_at:T?, final_key_ref:U?, final_key_revision:Literal[2]?, remote_user_id:Remote36?, tenant_id:UInt64?, kb_id:Remote36?, key_id:UInt64?, attempt:Attempt?, consecutive_failures:N, updated_at:T, last_attempt_at:T?, next_retry_at:T?, last_retry:RetryAudit?` | 只 seed 与显式 retry；seed 所有 future refs/IDs/times/attempt=null、checkpoint=NONE、failures=0。 |
| `provision_task_index` / task_id U / ordinary、同员工 owner | `local_user_id:I+` | 与 seed 同事务 CAS0；之后不可变。 |
| `provision_credential_preparations` / 新 credential U / ordinary、员工 owner | `task_id:U, intent_id:U, purpose:BOOTSTRAP|RETRIEVE, attempt_id:U, created_at:T, expires_at:T?, state:PREPARING|ACTIVE|ABORTED|DELETED, staged_cleanup:PENDING|NOT_REQUIRED|RELEASED|UNRESOLVED, updated_at:T` | 模型形状；不 stage/activate/delete。 |
| `provision_intents` / intent U / retained | a2 header `domain_id:U, local_user_id:I+, resource_kind:USER|KB|KEY, phase:PREPARED|WRITE_PENDING|UNKNOWN|VERIFIED|REJECTED, receipt_id:U?`，追加 `task_id:U, created_by_attempt_id:U, last_send_attempt_id:U?, created_at:T, updated_at:T, parent_intent_id:U?, remote_user_id:Remote36?, tenant_id:UInt64?, kb_id:Remote36?, absence_proof:NOT_SENT|DEFINITE_NO_EFFECT|null` | 保持 a2 header 兼容；无写。 |
| `provision_receipts` / receipt U / retained | a2 header `intent_id:U, domain_id:U, local_user_id:I+, resource_kind:USER|KB|KEY, proof_kind:CREATED_201|ADMIN_ATTESTED|OWNER_MATCH`，追加 `task_id:U, source_attempt_id:U, recorded_at:T, remote_user_id:Remote36, tenant_id:UInt64, kb_id:Remote36?, key_id:UInt64?` | 无写。 |
| `provision_resource_claims` / direct canonical JSON tuple / retained | `domain_id:U, resource_kind:USER|TENANT|KB|KEY, tenant_id:UInt64?, remote_id:S, local_user_id:I+, task_id:U, intent_id:U, receipt_id:U, claimed_at:T` | 无写。 |
| `provision_scan` / `primary` / retained | `after_user_id:N, updated_at:T` | 无写、无后台扫描。 |

`Identity={user_id:I+, email:StrictStr|null, disabled:StrictBool, snapshot_token:StrictStr}`；只取 Host 可信身份，seed 时保存。尚无任何 intent 的任务可在**显式 retry**、同一 task CAS 中更新邮箱快照；有 intent 后邮箱变更只投影 IDENTITY_CONFLICT，不重绑。token 是当前值指纹，非历史 epoch，A→B→A 不声称可检出。`RetryAudit={actor_user_id:I+, kind:SELF|ADMIN, requested_at:T, reason:StrictStr|null}`；SELF reason=null；ADMIN reason 是已校验 1..500 文本，只在普通 task，覆盖最近一次、不输出。

`Remote36` 是原样非空有效 UTF-8 StrictStr、最多 36 codepoints，不 trim/截断/大小写变换、不强加 UUIDv4；`UInt64` 是 strict 1..2^64−1。固定官方源码的 TenantAPIKey.ID/响应 ID 与 tenant 都为 uint64，故 `key_id` 不能沿旧提案用 S；本批无 wire 解析。claim 的 remote_id：USER/KB 原文，TENANT/KEY 从已验证 UInt64 生成无前导零十进制。logical key **直接** `json.dumps([domain_id,kind,tenant_scope,remote_id],ensure_ascii=True,separators=(",",":"),allow_nan=False)`；USER/TENANT scope=null，KB/KEY scope=typed UInt64。读取重建 key 并核 payload/receipt；不 double hex（Host repo 已编码各段）。纯长度估算 logical 487 ASCII bytes/Host settings key 1091，PG 实际索引边界留真实受控回归，不宣称已验证。

`ConfigPin={owner_user_id:I+, config_revision:I+, admin_credential_ref:U, admin_credential_revision:Literal[2], domain_id:U, base_url:S, anchor_revision:I+}`；`Attempt={attempt_id:U, mode:ADVANCE|RECONCILE|VERIFY_KEY, config:ConfigPin, employee:Identity, started_at:T, deadline_at:T, lease_expires_at:T, finished_at:T?}`。这些是未来 worker 模型形状；本批永不创建 Attempt。config pin 完整比对当前 config owner/payload 与 retained anchor URL/domain/revision；RecordCheck(config revision) + owner 当前身份 check 共同挡删除重建同 revision 的 ABA：若旧 owner 删除，旧 identity check 必冲突。anchor 无 owner、不可变；不因仅 revision 相等就信任 payload。

模型约束：task/index 双向 task_id/local_user_id/owner 一致；bootstrap 与 final ref/revision 成对，bootstrap created/expires 成对且 expires=created+24h；checkpoint 单调、READY 必须可靠 proof 与 active final Key（本批不产生）；preparation 生命周期与 a2 类似（PREPARING/PENDING，ACTIVE/NOT_REQUIRED，DELETED/NOT_REQUIRED，ABORTED/RELEASED|UNRESOLVED）。USER receipt 只含 user/tenant，KB 另含 KB，KEY 四 ID 齐全且 `key_id:UInt64`；USER 原子占 USER+TENANT claim，KB/KEY 占本身并核父 claim；冲突不可覆盖，retained proof 无授权性。这些属于将来远端 producer 的模型/事务前提，本批无对应转换或假 proof。

## 3. 实际四动作签名、投影与读边界

`ProvisioningStateService(facade: Any)` 仅实现：

```python
async def get_status(self, actor: PluginActor) -> ProvisioningStatus: ...
async def retry_self(self, actor: PluginActor, request: ProvisioningRetryInput) -> ProvisioningStatus: ...
async def list_tasks(self, actor: PluginActor, request: ProvisioningTasksInput) -> ProvisioningTasksOutput: ...
async def retry_task(self, actor: PluginActor, request: ProvisioningTaskRetryInput) -> ProvisioningStatus: ...
```

上列 `...` 仅合同签名，实际交付不得留 stub。`register_provisioning_actions` 追加四个真实 ActionSpec：`status(EmptyInput)→ProvisioningStatus`、`retry(ProvisioningRetryInput)→ProvisioningStatus` 为 authenticated；`tasks.list(ProvisioningTasksInput)→ProvisioningTasksOutput`、`task.retry(ProvisioningTaskRetryInput)→ProvisioningStatus` 为 admin。闭包传真实 Host actor，不从 payload/owner 构造 actor；入口、读返回前重 `reauthorize_actor`，写提交前紧邻重验（admin 对管理动作）。失败依 a2 `_host_error` 安全映射。

`status`：仅真实 actor.user_id，定点读 identity、task、若 task 存在则 index、config/anchor/block；不 seed、不写 cursor、不 resolve、不发 HTTP。无 task 时 revision=0、三个时间=null、按可信当前资格投影；普通 task/index 不吻合→UNAVAILABLE。输出只显式构造 a1 字段。`tasks.list`：admin 一次 `list_user_identities(after_user_id,limit)`；逐项定点读 task/index 和资格，缺 task 跳过、不补页、不 seed，`next_user_id` 原样沿该身份页，即使 items 空；email 仅当前 identity，非 task 保存的旧 email。读后复核 admin 与每个输出身份，变化安全失败；页不是快照。`task.retry`：UUID→index→user_id→task/identity 双向核对；缺 index 或员工已删 PROVISIONING_NOT_FOUND（插件本地安全码，404），损坏/错 owner UNAVAILABLE，不从 retained 恢复。员工 retry 无 user_id/task_id 输入。

投影优先顺序：先严格校验所有读取记录及关系，坏结构/关系直接 UNAVAILABLE；当前 disabled→`SUSPENDED/USER_DISABLED`；已绑定远端意图后的邮箱变化→`IDENTITY_CONFLICT/IDENTITY_CONFLICT`；未 READY 且首次 bootstrap deadline 已过→`MANUAL_REVIEW/BOOTSTRAP_EXPIRED`；关联意图存在未核对写入→`MANUAL_REVIEW/REMOTE_OUTCOME_UNKNOWN`；无邮箱→`EMAIL_REQUIRED/EMAIL_REQUIRED`；确实缺 config（允许仅保留合法 anchor）、enabled=false 或合法匹配的精确 ref block→`CONFIG_REQUIRED/CONFIG_REQUIRED`；再用持久 task state/error/checkpoint。存在 config 但 anchor 缺失、domain/URL 不匹配，或 block owner/domain/revision 错绑，一律 UNAVAILABLE，不允许当缺配置重建。READY 也须当下 identity/config 可用才显示 READY；这仍只是本地记录投影，绝非可用 Key 的能力令牌；KEY_STORED 永不显示 READY。缺 task 合格→PENDING。可重试判定只对当前无未完成 attempt、身份/配置合格、未过期、没有未核对意图，且 task 持久态为 PENDING/RETRY_WAIT/RETRY_REQUIRED 或已解除 CONFIG_REQUIRED/EMAIL_REQUIRED；无 task 合格可 retry0。持久 EMAIL_REQUIRED 且当前邮箱已补齐、无 intent 的只读投影为 PENDING/can_retry=true，实际同一 task 身份快照仅在成功 retry CAS 更新；配置仍缺则显示 CONFIG_REQUIRED，不排队。MANUAL_REVIEW、IDENTITY_CONFLICT、BOOTSTRAP_EXPIRED、READY 均 false。`can_retry` 是瞬时 UI 投影，真正写必须重读/CAS，不承诺远端效果。

每次 status/list/retry 对已有 task 的非空 user_intent_id/kb_intent_id/key_intent_id 最多三个键定点读 retained intent，不扫描；每项最多读取一个绑定 receipt。完整严格模型校验 key/domain/local_user/task/resource_kind、parent 链与已确认父 ID，和 task 的相应引用必须吻合；非空引用却找不到 intent 或关系错绑为 UNAVAILABLE。WRITE_PENDING/UNKNOWN 无论普通 task state 是否仍 RETRY_WAIT/PENDING 均覆盖为不可重试人工核对；VERIFIED 缺 receipt_id 或指定 receipt 确实不存在亦不可重试人工核对。PREPARED 只有 absence_proof=NOT_SENT 且无 receipt、REJECTED 只有 absence_proof=DEFINITE_NO_EFFECT 且无 receipt 才能作为可继续的确定无效果形状；其它不确定形状同样人工核对。存在的 receipt 必须严格核对 intent/domain/local_user/task/resource_kind 及资源 typed IDs；错绑/坏格式 UNAVAILABLE。VERIFIED+匹配 receipt 只表已保存证明，不授予 READY/检索权；后续 worker 仍独立核全部 claims。无 intent seed 不增加 retained 读取。retry commit 附所读最多三 intent 与三 receipt 的 RetainedRecordCheck（指定缺失 receipt 为0），防检查后证明状态变化；继续使用原 held revisions，不刷新重放。

读取已过期 bootstrap 必须立即拒其使用且上述投影为 MANUAL_REVIEW/can_retry=false；status/list **严格只读**。24h 自首次持久登记起，重试不延期；到期运行/恢复启动清理是后续真实 worker/cleanup 批的交付要求，不从 a3 无 worker 冒称物理清理已实现。已 READY 且已清 bootstrap 不因旧 deadline 改状态；停机无离线物理到点保证，SQL 删除不等于介质擦除或远端会话撤销。

## 4. retry 精确提交与并发矩阵

所有 retry 本地提交前：复核 actor；重读 current identity/config/anchor/精确 auth block，比较完整 config pin 的实际字段；重新检查 task/index/资格/expiry；构造 `UserIdentityCheck` 覆盖 employee、当前 config owner、实际 admin actor（若不同，按 user_id 去重）。提交同时使用 task/index 的 RecordWrite/RecordCheck、config RecordCheck（缺配置为 0）、auth-block RecordCheck（当前 ref 的存在或 0）、anchor RetainedRecordCheck（含缺行为 0）；`requirements=()`。若无有效 config，仅真实 authenticated self 的 retry0 可 seed blocked task，不把 CONFIG_REQUIRED 改 PENDING；此时 config0/anchor revision 检查与 employee/actor identity 仍必需。已配置时 current config owner 删除→失败，不切换 owner。所有相关 payload 在提交前严格验证；CAS 冲突不能拿新 revision 自动重放旧请求。

| 输入与观察 | 本地结果 |
|---|---|
| `retry.self(expected=0)`、task/index 都确实缺失、资格完整 | 同事务 task CAS0 + index CAS0，独立 task UUID；task PENDING、checkpoint NONE、last_retry SELF、revision=1。 |
| `expected=0`、task 已由竞争者建立 | 重读双方完整绑定；若当前已 PENDING 或有效 PROVISIONING，返回其投影；否则 CONFLICT。不生成第二 UUID。 |
| task 存在、expected 与 envelope revision 相等、允许重试（含已补齐邮箱的 EMAIL_REQUIRED） | 仅 task CAS 更新 PENDING、error_code=null、consecutive_failures=0、next_retry_at=null、updated_at/last_retry；无 intent 时可更新可信当前 identity；保留同 task_id/index、checkpoint、IDs、intent/proof/ref、最初 bootstrap deadline；revision+1。 |
| 有效未完成 attempt、或**持久 task state 已 PENDING**（包括并发已排队） | 仍先读取并检查关联 intent/receipt、不确定即不可重试；不写、不重置 deadline/计数；返回当前投影。持久 EMAIL_REQUIRED 补齐邮箱后的**只读投影 PENDING 不触发本幂等分支**，必须走上一行同 task CAS。过期 attempt **不**由本批抢租或结束；所有未完成 attempt 均 can_retry=false，等后续 worker reconcile；不把已过期当可重领。 |
| stale expected，且现态非已排队/有效领取 | PROVISIONING_CONFLICT；不得以 stale click 解除 MANUAL_REVIEW/耗尽状态。 |
| disabled/身份冲突/未解决 config 或 block/bootstrap expired/MANUAL_REVIEW/READY | 不写；返回当前不可重试状态；若 expected stale 且非重复排队，先 CONFLICT。 |
| 两 admin 对同 task、或 admin 与 SELF 同时合法 retry | task CAS 单赢家；败者仅在赢家已 PENDING/有效领取且双向绑定仍好时返回当前，否则 CONFLICT；admin reason 只在赢家 task 写入。 |
| key 已按未来路径到 KEY_STORED、且持久态可 retry | 仅重排本地状态，保留 Key/ref/checkpoint；绝不重新 POST KB/Key。本批不执行 VERIFY_KEY。 |

retry0 在缺 email/缺配置且真实 actor 仍可 authenticated 时可建唯一普通 task 记录相应阻塞状态（不排为 PENDING），以便后续 UI 观察；不可把 bad record、损坏 index、已删身份当缺行。若投影冲突/过期优先于显式请求，绝不靠点击重开 bootstrap 24h 或 UNKNOWN 意图。写提交 await 取消后先 settlement；结果仍不确定则读定点 task/index 校验 task_id、revision、last_retry，不能二次 seed/重放。普通读取没有隐藏写。

## 5. 明确分批边界与白名单

本批没有扫描/claim/HTTP，故 30 秒 tick、身份页50、每轮一个90秒 attempt、租约与 7 次退避/第8次耗尽、外呼前 WRITE_PENDING、远端 UNKNOWN/receipt/claim、KEY_STORED→LIST+SEARCH→READY/删除 bootstrap、retained-only 收据、运行/恢复过期清理，全部是既定后续 worker/provider 批的**必守合同**，不是当前四动作的假实现或可由本批测试声称通过。实际 cleanup 后续从 user_id=0 有界恢复扫描，先清理再外呼、所有外呼独立查截止；无配置时也执行本地到期清理。未知 staged 只按精确 ref settle/release，未证实删除不标 DELETED。该调度不需要在 a3 创建空 worker 或 scan cursor。

待 a2 独立审查接受后，未来实现**精确源白名单**：`plugins/enterprise-knowledge/enterprise_knowledge/provisioning_records.py`（追加严格 a3 models，不改 a2 现有字段）；新 `plugins/enterprise-knowledge/enterprise_knowledge/provisioning_state.py`（四动作逻辑；复用 a2 安全 error/settle helper）；`plugins/enterprise-knowledge/enterprise_knowledge/provisioning_actions.py`（追加四 ActionSpec）；`plugins/enterprise-knowledge/enterprise_knowledge/locales/{en,zh}.json`（四 action 文案，新增 PROVISIONING_NOT_FOUND 的安全任务未找到文案）。不改 `provisioning.py`、`main.py`、a1 DTO、Core/DB/API/UI/openspec/现有 a2 配置测试。若审查证明 helper import 不宜复用，应先回 root 调整白名单，不能自行复制权限或取消算法。

**精确测试白名单**：新 `tests/unit/plugins/test_enterprise_provisioning_state.py`（真实 Host+SQLite 服务动作），新 `tests/unit/plugins/test_enterprise_provisioning_records.py`（严格模型、remote ID uint64/canonical key），现有 `tests/unit/plugins/test_enterprise_provisioning_actions.py`（仅登记与 ingress 增量）。未来受控 PG claim-key 最大边界回归属于后续 producer/retained 写批，当前无 claims 写入。含 retained check 的 retry 混合提交须以真实 Host 测试原子回滚，而非只断言 mock 调用。

必测：无 task status 零 revision 且无写；retry0 双并发仅一 task/index，删员工两行级联且 retained 不复活；tasks.list 一页、空 items 仍 next、无补页/seed；权限缓存 admin 降权/禁用与读中变化、admin 与员工交错重试；task/index 错绑 fail closed；disabled/email 变更与 A→B→A 限制；先无邮箱 seed，再补邮箱同 task/index retry 成功，以及补邮箱后仍缺配置保持阻塞；config 禁用/blocked lineage、配置删重建 revision=1 的旧 owner pin 拒旧提交；仅 anchor 合法缺配置与 config 存在但 anchor 缺失/错绑的对照；重复/过期 expected 矩阵、admin reason 仅 ordinary；普通 task 可重试但关联 UNKNOWN/WRITE_PENDING/VERIFIED缺receipt 覆盖阻塞，坏绑定 unavailable，PREPARED/REJECTED 确定无效果与其反例，读取至多3intent+3receipt且 proof读后变化使 retry CAS失败；过期 bootstrap 投影只读/拒 retry、KEY_STORED 保留；混合 CAS 故障回滚与取消 settlement 后不二次写；模型拒 float/string/bool/0/>uint64 key_id、36 字符上界。无当前 caller 的 canonical claim-key 函数及 PG边界测试顺延 producer 批，不为凑测试写假函数。实现阶段定向 `uv run pytest ... -q`、最终集成 `make all`；本次草稿没有运行测试。

## 6. 实施前条件与验收边界

1. a2 Fix2 独立验收已通过；root 核对实际 _now/_payload/_bad_record/_error/_host_error/_unavailable 与 ProvisioningService._settle 接缝。_settle 是 bound async method，可用真实 ProvisioningService(facade) 实例复用，仅作 operation settlement，不调用 get_config 读取管理秘密；取消与错误同时发生时，settle 后优先传播已记录取消。新八路径基线由 root 派发前捕获。
2. root 已核现有 errors.NOT_FOUND 文案仅指配置，采用新插件本地 PROVISIONING_NOT_FOUND/404，两 locale 加任务专用文案；不得把损坏 index 也变成 404。
3. 在线/恢复清理必须在后续真实 cleanup/worker 批独立验收；本批不能用只读 status 代替，也不能据此勾选完整3.1/1.2。

## 已纳入独立规划审查的 root 澄清（实施仍待前置条件）

- 禁用用户的 self actor 在真实 reauthorize_actor(authenticated) 即拒绝，不 seed；管理员只可观察已有 disabled task，task.retry 不新增 admin-seed 能力。上一表中 disabled 不写分支适用于管理员已有 task。无任务+无邮箱/配置仅对真实 authenticated self retry0 可登记 blocked task。
- a2 persistent header 修复通过后直接复用严格必需 header；无需 a3 重复 raw-presence validator。当前 Fix2 已实际接受；以已核对真实 helper 为准。
- current动作仅需要严格解析未来 task/attempt/refs 的既定形状；claims 编码如完全无 current caller，可推迟函数实现到生产者批，但固定表示/字段合同不变，不能留假函数。records 必须验证会被四动作读取的实际状态，不能先写接受任意 dict 的 future stub。
- 本批没有 grant/provider 权限，状态投影不赋予检索授权。BOOTSTRAP_EXPIRED/UNKNOWN 等不可由 retry 按钮解除。
- 除 a2 source 固定接受接缝外，以上合同可先独立审查；通过后仍需 fresh检查 a2 模型/settle/error 导入、捕获8路径 baseline 才能 READY 派 Luna。
