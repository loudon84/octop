# 用户开通通用接缝与插件合同草案

状态：分批实现与接口冻结；I02a/I02b/I02c/I02e 已本地验收，I02d 已本地验收，其余接缝按节内状态推进，不表示整项 API 已实现。参考 openspec/changes/enterprise-user-private-knowledge。具体 source/测试白名单在各执行 brief 冻结；此文件不能作为任意核心改动授权。

## 一、已核实的现有能力与缺口

- UserRepo 的用户行有 email、disabled，无 updated_at/单调身份 revision。已有规范化邮箱唯一索引；普通创建、InviteRepo 原子兑换、SSO 与 LDAP 首次创建需要一致的通用策略。
- Host facade 的 get/list_records、stage/activate/resolve credential、多记录 CAS/RecordCheck 可复用。resolve_credential 仅允许 active；release_staged_credential 不能清理 active，因此需要窄的通用凭据删除前提来清理短期引导凭据。
- Host 已有 generation、实际事务 lease、跨 loop 取消/drain。后台 worker 必须进入该机制，不能自行创建不受管理的长期 task。
- WeKnora 私有检索 Key 固定 capabilities=[retrieve]、knowledge_base_ids=[private_kb_id] 且非空；Owner JWT 只用于开通阶段。企业范围继续使用另一真实 connection/key/tenant，不依赖个人目录发现企业共享库。

## 二、通用用户注册策略（I02a 本地实现与独立复审通过）

定义不可变 UserRegistrationPolicy(email_required: bool=False)，持久 key 为 `user_registration.email_required`。domain 负责严格解析，缺失默认 false，坏值不能静默当 false。管理员更新只接受真正 bool，普通员工不能修改。策略启用后不会随业务插件暂时卸载自动消失。

所有人类账号首次创建消费同一 domain 校验，沿用 parse_optional_email 的 trim/lower 语义；require_email 时 None/空/无效邮箱拒绝，重复返回既有 EMAIL_TAKEN。邀请失败不消费，SSO/LDAP 不得将重复邮箱降级为 None 后继续创建，不按邮箱自动绑定不同 subject。已存在身份正常登录与既有非企业部署保持兼容。

I02a 批次已冻结通用读取 `GET /api/auth/registration-policy` 仅返回 `{email_required: bool}`，在现有 JWT allowlist 只加此精确路径；管理员写 `PUT /api/settings/registration-policy` 使用 StrictBool/extra=forbid、require_admin()，domain 重新检查实际 DB 角色并记录安全审计。该公开读取不含业务插件/供应商字段、用户列表或凭据。CLI、邀请、管理创建及目录新建都必须经过 domain 策略，不把唯一性只放在页面。

除 UserManager/Invite/SSO 首登，还包括 LDAP 首次自动创建、离线 create_user_offline 与 octop init 新建管理员。init 增加可选 --admin-email / OCTOP_ADMIN_EMAIL，已有账号的幂等 bootstrap 不校验新邮箱或覆盖旧值；默认策略缺失为 false。SSO/LDAP 已绑定身份登录以及未启用策略时的既有 claim 行为保持兼容。启用策略不得把新用户重复邮箱降级为 None。精确20路径（部分现有测试可不改）与验收矩阵见 task-I02a-email-policy-brief.md；已捕获并核对基线，当前由唯一功能 writer 实施。

启用前检查历史邮箱是否已规范且唯一；有冲突/非规范行时报告待管理员修正，不自动重写历史账号或绕过唯一索引。此检查及新建必须通过合法仓储/SharedServices，无 router SQL。

## 三、服务端身份只读与提交前提（I02b 本地实现与独立复审通过）

```python
@dataclass(frozen=True)
class PluginUserIdentity:
    user_id: int
    email: str | None
    disabled: bool
    snapshot_token: str

@dataclass(frozen=True)
class PluginUserIdentityPage:
    items: tuple[PluginUserIdentity, ...]
    next_user_id: int | None

@dataclass(frozen=True)
class UserIdentityCheck:
    expected: PluginUserIdentity
```

trusted setup 通过 `register_user_identity_consumer()` 显式登记身份消费者能力，之后同一有效 facade 才能调用 `read_user_identity(user_id)` 与 `list_user_identities(after_user_id=0, limit=50)`。两者只在服务端提供，不注册成 HTTP/tool action。实际 user_id 从可信 DB 行取得，page 按稳定递增 id、limit 1..100；不返回 password_hash、SSO token/subject、权限全集或浏览器身份参数。

snapshot_token 固定为带用途前缀的 canonical JSON `[user_id, normalized_email, disabled]` 的 SHA-256 当前值指纹；比较完整 DTO，不只比 token。它是当前 user_id、规范邮箱与 disabled 的不透明值快照，不是授权 token 或单调历史版本，不声称发现 A→B→A 的中间变化。当前资格、稳定 user_id 和远端 mapping 才是权限依据。每次实际使用和激活都重读当前身份；不以相同邮箱把另一 user_id 当原用户。若后续需求要求历史变化 epoch，需要另行明确通用 schema/迁移，不能用时间戳或哈希虚构单调版本。

I02b 扩展 `facade.commit(..., identity_checks=())`：在与 records/credential activation 相同事务内锁定/读取 user 当前值，要求存在且与 expected 快照一致，不匹配整笔回滚。是否允许激活由插件检查 disabled=False，Host 不把该业务条件施加给所有写入，否则无法记录真实 disabled=True 的暂停状态。SQLite 沿用 BEGIN IMMEDIATE；PG 对被检查用户按 id 排序取共享行锁直到提交，避免校验后邮箱/禁用写入穿过激活。SQL 留在 UserRepo；Host 使用现有 executor/真实 generation lease，不在 event loop 做 DB I/O。普通无 identity_checks commit 完全保持现语义。

此能力是可信插件显式服务端权限，不是恶意 Python 插件沙箱；不通过构造 PluginActor 冒充员工，也不让任意客户端提交 expected 快照获得权限。

## 四、短期 active 凭据清理（I02b 本地实现与独立复审通过）

```python
@dataclass(frozen=True)
class CredentialDeletion:
    credential_id: str
    owner_user_id: int
    expected_revision: int
```

I02b 扩展 `facade.commit(..., credential_deletions=())`，限定同 plugin、owner、state=active、精确正 revision、当前有效 generation，在同一事务删除自有凭据并更新其引用记录。未知/失配不当成功；同一 ID 不能在一次提交中同时 activation/delete；不级联、不删除别的插件凭据，不增加 remote revoke/delete 行为。现有 stage/resolve/activate 与 staged release 语义不变。

开通意图在外呼前与引导凭据 activation 原子保存，避免崩溃后无法取得本地已生成密码。完成时 mapping READY、最终 scoped Key activation、引导凭据 deletion 同事务（最终 Key 的 KEY_STORED/READY 顺序以第十节为准）。按用户于 2026-10-25 的明确选择，引导凭据自首次持久登记起设置不可由重试/领取延长的 24h 使用截止：到期立即拒绝任何使用、登录或外呼并转人工恢复；服务运行时清理，停机则恢复后立即启动清理。不承诺停机期间离线物理到点删除，不增加独立常驻清理机制。此处是本地密文清理，不等于已证明远端 Owner 会话撤销；远端会话清理须按实际支持 API/权限另测，不把本地删除写成远端 revoke 成功。

I02b 精确四个 source/五个测试路径、输入边界与事务/生命周期证据矩阵在 task-I02b-identity-brief.md；九路径基线已捕获。身份 API 不注册成 HTTP/tool/action。此处冻结不代表功能已实现、PG 已验证或整项1.2已完成。

## 五、后台工作生命周期（I02c 本地实现与独立复审通过）

- `facade.register_background_worker(name, worker: Callable[[asyncio.Event], Awaitable[None]])` 仅在真实 setup 开放窗口登记，不立刻在任意线程 create_task。
- `PluginHost.start_background_workers()` 由 composition root 在可用应用 runtime 后异步启动，绑定唯一 owner loop；已运行时安装/重绑的 generation 通过该 loop 启动且不重复。worker 的 task 进入 Host 实际 tracking 与取消/drain。
- 插件自行决定扫描、退避和开通状态；Host 不知道 WeKnora 或用户开通业务。Host 只负责实例/世代/启动/异常安全退出和生命周期，不成为可配置任务平台。
- worker 使用局部状态，可信身份扫描修复遗漏；用户创建后通用通知只唤醒 worker，不能把通知当可靠队列。通知失败不影响已提交注册，重启对账仍可找到用户。
- provider/配置代际变化后旧 worker 禁止提交；取消必须保留实际 executor 事务 lease，不能因 awaiter 取消提前释放正在执行的 COMMIT。
- 缺配置时不反复发远端鉴权请求。身份分页/持久任务和每次网络尝试分别有界；跨进程唯一领取仍由 DB CAS/check 而非内存锁保证。

I02c 选择 Host 在 owner loop 创建并初始置位的 Event 传给 worker，提供 app-local/thread-safe `wake_background_workers()`，不携带用户 ID、不创建全局通知总线。UserManager 注入可选回调并通过 `notify_user_created()` 安全触发；普通创建、邀请成功返回、SSO/LDAP 首建通知，既有登录不通知。通知失败只记录固定安全日志，不影响已提交注册；InviteService 不为此添加另一套回调，提交后到通知之间的遗漏由周期扫描恢复。精确十二路径（含两条既有 callback fixture 的真实 PluginContext 修复）与启动/动态绑定/真实协调器 drain/通知证据矩阵在 task-I02c-worker-brief.md；原 pre-edit 基线及两轮复审记录已保留。定向及静态通过，Fix2 取消/真实执行器入口/事务租约证据复审通过；全仓集成仍待最终门禁。

动态 bind 采用 Host 记录 owner loop 后自启动的方案，不逐个改写 Manager 加载分支。停用/卸载必须通过实际 Registry coordinator 验证“旧 worker finally/drain 在变更返回前完成”；现有 coordinator 已承担 action/COMMIT lease 的取消排空，不能只看到 Manager 内部同步 deactivate 就断言外层没有 drain。优先把 worker 纳入既有 tracking，只有真实聚焦生命周期测试证明缺口时才扩大 Manager 白名单。通知只作 best-effort wake，周期分页对账才是可靠性依据。

## 六、插件 actions 与后续来源合同准备

开通动作拟定分为管理员配置/任务列表/核对恢复和员工自己的 status/retry；输入严格 extra=forbid、revision 校验，身份来自实际 actor，不允许员工选 user_id。公开输出仅含状态/固定 error_code/安全时间/可用操作，不含密码、JWT、Key 或内部完整 mapping。管理员候选核对必须先服务端发现，不能任意粘贴 ID 接管。

空私库的 READY 含义冻结为本设计已有的“身份/私库/限定凭据与当前只读检索权限就绪”，允许真实空结果；不表示将来文档摄取、向量引擎或所有模型已验证。固定源码补充核对见 task-private-kb-model-contract-inputs.md：document KB 创建只必填 name，模型不是建空库的前置；新个人 tenant 只看自身/builtin 模型，不继承另一 tenant 的自定义模型。因此不虚构 embedding/rerank ID，也不复制模型凭据或扩大为远端模型配置功能。

后续 producer 必须先可靠保存最终非空 KB 白名单的 retrieve Key，再用该 Key 及真实 tenant 执行与实际 private producer 相同 provider/payload 的 LIST+SEARCH 路径，确认权限/归属与有效返回后才能激活 READY。不要用 Owner JWT 的成功替代 scoped Key 验证，也不要以特殊禁用向量参数的探测冒称常规检索全部可用。探测 query 用固定无身份信息文本，不创建文档、不进入最终回答模型；它仍计入同一次 90/30/10 网络预算。缺配置/模型/存储/检索驱动导致失败时按实际固定错误保持阻塞/恢复检查点，不创建第二份 KB/Key 规避失败。确切 payload/错误/检查点在 I03 brief 冻结；此段不代表实现或远端验收。

当前来源 producer/session/evidence 是单企业 connection；私有+企业来源 DTO、兼容版本和反向映射事务需要在来源任务前单独冻结，页面不得调用本段尚未定名的动作。已审 design 确定的是同问可选择两类来源、各自凭据/tenant、共同预算、任何失败无最终模型及来源限定续读；不能通过给 raw KB ID 加字符串前缀而跳过服务端映射。

## 七、下一批交付顺序

1. 先完成独立的 P08a 通用 requirements read（已在实施，功能不加入本草案字段）。
2. 冻结本草案中邮箱策略精确 API/路径与其测试 brief，然后实现；随后身份提交前提/凭据清理与后台生命周期各自独立验收。
3. 先完成第八节通用保留记录的增量设计审查与独立实施验收，再冻结插件开通动作/状态 DTO 并实现 Mock HTTP 合同、可靠重试/不确定状态恢复。
4. 冻结多来源 DTO/兼容与业务 UI action 表后实现来源和页面；原 P08 通用容器可先进行，业务页面等待新契约。

每批使用 pre-edit baseline、GPT-6 Luna 功能实现、定向真实验证、独立 Spec/Quality 审查；新接口未冻结不冒充已完成任务1.2。全仓 make all/实际 Windows/远端两用户与库隔离是后续集成门禁。

## 八、删除账号后的非敏感保留记录（I02d 本地实现与独立复审通过）

现有 022 的普通 plugin_records 和 plugin_credentials 都有 owner_user_id 外键并 ON DELETE CASCADE。不能用这些行、换成管理员 owner、可清理的 audit 或内存来承诺删除后的意图/回执保留。增加独立 PluginRetainedRecordRepo，复用 settings 的内部保留前缀，不改 schema、SettingsRepo、AuditRepo、用户删除或普通凭据生命周期。该能力只向可信服务端 facade 提供，不注册 HTTP/tool/raw settings API。

### 精确存储与 DTO

键固定为 `octop.plugin-retained.v1:<plugin_hex>:<namespace_hex>:<logical_key_hex>`，各组件为原字符串 UTF-8 字节的小写十六进制，冒号分隔，无歧义、无客户端原始 key 参数。沿用已有 plugin/namespace/logical_key 校验与保留命名空间拒绝；字面 prefix 转为相同 hex 前缀，不能把 `%`/`_` 解释成 SQL 通配符。分页按编码 key 的二进制升序（SQLite BINARY、PG C），after_key 为上一页逻辑键；limit 1..100，读取 limit+1 判断终页。分页不承诺跨提交一致快照。

值为严格 JSON envelope `{"format_version":1,"revision":N,"payload":{...}}`，N 是正 int 且不是 bool；payload 复用现有有限 JSON 校验/深复制规则。未知版本、错误结构、非法 JSON 或 revision 不能当缺行/重置；返回安全 unavailable，不带原始内容。revision=0 仅表示确实缺行，保留记录无删除操作。

```python
RetainedRecord(namespace: str, logical_key: str, revision: int, payload: dict)
RetainedRecordWrite(namespace: str, logical_key: str, payload: dict, expected_revision: int)
RetainedRecordCheck(namespace: str, logical_key: str, expected_revision: int)
RetainedRecordPage(items: tuple[RetainedRecord, ...], next_logical_key: str | None)
```

DTO frozen，页 items 复制为 tuple；payload 与现有 HostRecord 一样隔离调用者后续修改，不另造 JSON 类型体系。facade 绑定真实 plugin_id，不接受指定其他插件；get 返回缺行 None。新增 `get_retained_record(namespace, logical_key)`、`list_retained_records(namespace, prefix="", *, after_key=None, limit=50)` 与 `commit(..., retained_writes=(), retained_checks=())`。commit 返回原顺序 `list[HostRecord]`，不混入 retained DTO；成功保留写 revision=expected+1，需要内容可再 get。输入拒绝 bool/负 revision、重复写键、不一致重复检查；同键 check/write 必须有相同 expected_revision。全部检查基于事务初态。

RepoBundle/SharedServices 增加 `plugin_retained_record_repo`；只有需要保留能力的调用才访问新 repo，旧 no-retained 提交及旧 fake services 保持兼容。repo 的 get/list_prefix_page/cas/lock_for_checks 支持 conn，独立 CAS 自开事务，Host 必须传现有同一 conn。create 使用 INSERT ON CONFLICT(key) DO NOTHING，update 的 WHERE 比较完整旧 envelope 原文；rowcount 不符就是 conflict，不使用无条件 upsert 或依赖 DB JSON 运算符。

### 原子性与生命周期

仅在含 retained writes/checks 的新路径上，普通 writes、activations、credential_deletions 的每一个 owner_user_id 都必须在 identity_checks 中；未覆盖返回 invalid，且在任何写入前拒绝。此路径不接受非空 requirements（即使 actor 已检查也拒绝），避免既有 requirement 实际 owner 与 actor 不同而隐含申请遗漏用户锁。纯 retained 无普通/凭据操作时不要求 identity_checks；旧 no-retained 提交及 requirements 语义完全不变。identity_checks 先按 user_id 排序锁住全部已覆盖用户行，再对 plugin_records、settings 依次取 PG SHARE ROW EXCLUSIVE 表锁；凡含 retained writes/checks 都按此顺序，缺行也受保护。SQLite 沿用 BEGIN IMMEDIATE。允许组合的普通/保留 checks、两类 CAS、凭据 activation/deletion 在同一实际事务/既有 generation lease 内完成，任一条件失败全部回滚。不得网络等待、另开隐藏提交或把取消 awaiter 当 COMMIT 已停止。粗表锁会短暂串行 settings 写入，阶段一低频开通接受此代价；不加新锁平台。

允许 `commit([], retained_writes=...)`；这种提交不要求用户仍存在，也不自动提供有效身份。保留至应用数据生命周期结束，用户/配置管理员删除、插件停用/卸载/重装同 plugin_id 均不清除；本次无 delete/purge/TTL。普通 records、staged/active credentials 原有级联不变。

### 插件生产者必须遵守

- 外呼前，保留意图与普通状态、引导凭据 activation、当前 identity_check 原子提交，然后才 POST。
- 可靠结果的回执、反向占用与正常 READY mapping、最终 Key activation、引导凭据 deletion 可同事务提交；身份必须仍有效且 disabled=False。
- 若上述激活因删除/身份变化失败，有效 generation 可按原意图与可靠返回另做 retained-only CAS，保存非授权回执，不写 READY、不激活/恢复凭据。崩溃或世代失效未及保存时，已存意图仍是不确定结果，必须先核对；不承诺网络 exactly-once。
- 删除后不再自动创建远端资源，缺本人凭据时保留人工核对线索，不为全自动恢复保留密钥。另一 user_id 复用邮箱不能认领原资源。
- 反向唯一键使用不可替换的实际远端 origin、资源种类、实际 ID（必要时 tenant），不使用邮箱、库名或裸 KB ID；配置改名/密钥轮换不能变更真实身份域。实际 provider 编码在生产者 brief 冻结。
- 仅保留 opaque local user_id、意图/资源/origin ID、可靠证明类型/状态及必要版本；不存密码、JWT、Key、原始响应或无必要邮箱。owner 元数据不授予权限，所有实际读取/激活仍重读可信身份；管理员核对仍要求 fresh admin、服务端候选与 CAS。

### I02d 执行边界与验收

候选 source 仅新增 `infra/db/repos/plugin_retained_records.py`，改 `infra/db/services.py` 与 `infra/agents/plugins/{host_contracts,host}.py`。测试限定新 `tests/unit/db/test_plugin_retained_records.py`、`tests/unit/plugins/test_host_retained_records.py`、`tests/integration/plugins/test_retained_records_postgres.py` 和现有 `tests/unit/plugins/test_host_records.py` 的直接兼容场景；实际目录/基线须在 I02b/I02c 冻结后核对再 READY。无迁移/API/UI/provider 进入核心。

独立增量设计审查、strict OpenSpec 后，功能/测试由 GPT-6 Luna 实施；定向证据必须包括隔离/坏 envelope/字面前缀与分页、缺行/正 revision CAS、删除员工及配置管理员后保留、普通记录与密钥仍级联、混合事务失败全回滚、retained-only 非授权回执、实际 generation/drain/取消 COMMIT，及真实 PG 独立连接的缺行 check/write 竞争、同键 CAS 唯一赢家与锁顺序；明确验证未覆盖 owner 的 mixed 输入与 retained+requirements 在无写入时拒绝，并交错用户 DELETE 与所有 owner 已检查的允许 mixed 提交，证明不在表锁后申请遗漏用户行锁。不能用 SQL 字符串断言代替 PG 并发；最终 make all 仍是集成门禁。本节是待审合同，不表示已有实现/远端账号。

## 九、可信 action actor 的当前权限复核（I02e 本地实现与独立复审通过）

已核实：HTTP current_user 可读 request.state/UserManager 内存；现有 facade.invoke_action 使用传入 actor 快照检查。Host 已有 authorize_persistent_actor 和同步 helper，读取实际 DB role/disabled 并校验范围，但 facade 没有公开的窄复核方法。此缺口不能靠 I02b 的无角色身份快照、虚构 trusted turn 或客户端 user_id 补上。

新增 `await facade.reauthorize_actor(actor: PluginActor, permission: Literal["authenticated", "admin", "agent_owner"]="authenticated") -> PluginActor`。仅接收实际 PluginActor 和三个精确 permission 值，否则 invalid；沿用真实可信 Host action/turn 的 actor，不从 payload/邮箱/配置 owner 拼造身份。返回实际数据库角色、permissions 和现有持久 helper 的 locale/相同 scope 语义，不回传 UserRow、密码/SSO 数据或角色全集目录。它是可信服务端复核能力，不是浏览器登录、act-as 或恶意 Python 插件沙箱。

实现仅在现有 facade._run/真实 generation lease 内复用 Host._authorize_persistent_actor_sync：services 由现有 DI 获得，DB I/O 在既有 run_in_executor，不套第二 executor、不在核心新增 SQL/缓存。调用前和返回新 actor 前检查当前 generation；缺失/禁用用户或权限/范围不符沿用 denied/not_found/invalid，DB 故障安全 unavailable，不回显异常/数据。旧 facade 或等待读取期间被失效不得交付新权限结果；真实 operation lease 排空后才释放执行中的读取。

不要求身份 consumer 注册；不改 invoke_action、API router、现有 action 权限行为、身份 DTO/schema 或业务插件数据。该方法对输入快照只信可信 user_id/scope，is_admin/permissions 必须以 DB 当前值替换；不承诺把角色锁一直持有到调用者之后的另一事务，I02b 当前值检查也不包含角色。

新开通 action service 在入口复核（员工 authenticated，管理员 admin）；管理 config/retry/confirm 本地准备后紧邻提交前再复核，网络 discovery 返回/保存候选前复核，confirm 另一次真实 action。调用者仅使用返回 actor 决策；中途降权不得沿用旧 is_admin。worker 无管理员 actor，已成功批准的全局配置持续有效至停用、记录/凭据变化或 owner 删除，不因 owner 后来降权追溯撤销审批；降权账号不能再管理。worker 每次读当前 config/anchor/凭据及真实 employee identity 并固定 revision，不从 owner_user_id 构造 PluginActor。

I02e 精确候选两路径：`src/octop/infra/agents/plugins/host.py` 与新 `tests/unit/plugins/test_host_reauthorization.py`。已先完成独立增量审查，并在 I02c 接受后捕获两路径基线、由 GPT-6 Luna 实现；25 项定向测试、静态检查及独立 Spec/Quality 复审通过，在 I02d 前验收。实际 Host/SQLite 必测缓存 admin=true 而 DB 已降权、缓存 false 而 DB admin 的刷新、禁用/删除拒绝、无 Agent 的 admin/authenticated 合法入口、owner/thread 范围拒绝、严格参数、DB 安全错误、失效前/读取期间失效与事件循环继续前进。无网络/PG 专用要求；最终集成 make all 不被定向测试替代。

## 十、开通生产者的分批冻结（I03a1/a2 本地验收通过，后续状态/生产者分批实施）

本节将已接受产品选择落实为可审查的小批执行合同，不新增产品范围。3.1 拆为 I03a1 严格公开 DTO、I03a2 本地配置与保留摘要动作、I03a3 状态/意图/反向占用与领取；3.2/3.3 的 HTTP/provider/worker 另有精确 brief。完成 a1 不勾选 3.1/1.2，也不注册不存在的动作。I02d/I02e 必须先验收，功能/测试仍只由 GPT-6 Luna 实施。

### 跨后续批次的绑定规则

- 首次 provisioning 配置原子建立 retained `provision_domains/primary`，只含 schema/version、domain_id(UUIDv4)、canonical_base_url。此单一锚点永久保留；后续保存或配置管理员删除后的重建只能使用相同 canonical URL。沿用 normalize_base_url 的文本等价及精确测试 HTTP 例外，不能另起域 UUID/配置 ID 绕过历史占用。URL 规则不证明物理服务器指纹，同 URL 换服务器仍保守保留冲突；迁移另行设计。既有共享 connection 不变。
- 最终 retrieve Key 必须先在同一事务保存可靠回执/claim、激活加密凭据及 KEY_STORED 检查点，固定当前 employee/config/intent revisions；KEY_STORED 不授权常规私库检索。之后从实际持久 ref resolve 同一把 Key，通过后续冻结的真实私有 provider/context 执行 LIST+SEARCH，共享同一次 90/30/10。全部成功后，fresh identity 与所有 CAS/proof 检查通过，READY 映射和 bootstrap active deletion 同事务提交；已激活的 Key 不再次 staged→active。这澄清旧文“Key 激活与 READY 同事务”的授权含义，不允许 staged Key resolve。验证失败保留 KEY_STORED，不再建 KB/Key；缺少模型也不伪造探测结果。
- 对实际管理员凭据确认 401/403 后，持久普通 `provision_auth_blocks` 标记阻止同一 credential ref/revision 的所有用户自动/自助重试继续鉴权。标记绑定当前 domain 与凭据 lineage，不只绑定任务；显示名修改、任务重试或仅增长 config revision 不解除。全新凭据 ref/revision 可形成新 lineage；不持久保存 token 指纹/原文/响应。该普通标记 owner 与 config 同 owner，owner 删除后 config/密钥也缺失，仍无外呼。worker 只能记录固定拒绝状态，不能伪造管理 actor 或改变已审批 URL/enabled。精确 payload/CAS 与 HTTP 拒绝分类在 a2/b brief 冻结后实施。
- 扫描每 30 秒/wake 至多读一页 50 身份、启动一个网络 attempt；cursor 推到实际已检查/领取的最后 user_id，领取即停止本轮，不跳过其后未检查者、不因首项失败回退；确认实际页尾才归零。cursor/task CAS 同事务并覆盖实际 owner identities。配置缺失时有界检查该页，不发 HTTP。
- 已删员工的普通任务不复活。管理员后续 `provisioning.retained.list` 只分页固定 provision_intents，可按每项至多一个绑定 receipt 投影非敏感线索；无邮箱/remote IDs/任意 payload/理由/Key，无候选确认或授权效果。不会用 owner 替代实际请求 actor。
- 一次 attempt 的 OperationBudget 归整个操作所有，中间 Owner 客户端释放不得取消后续 scoped provider 阶段预算；不同身份凭据只在各次请求局部设置，不共享可变全局 auth。具体 client 生命周期和 private provider factory 在 I03b 实际源码核对后另冻结；当前不授权虚构 ConnectionConfig/企业 grant/actor。

### I03a1 精确公开 DTO（仅这些模型，不含存储/网络/动作注册）

统一继承既有 models.StrictDto，schema_id 固定 `octop.enterprise.provisioning.v1`、version 固定严格整数 1（bool/float 拒绝），extra forbid/frozen/strict。所有 int 字段拒绝 bool/float/string；所有 str 拒绝类型转换。UUID 为 canonical 小写带连字符 UUIDv4，非规范拼写拒绝而不静默改写。UTC 时间为真实 ISO8601 字符串，要求显式 Z 或 +00:00，非零 offset、naive/无效日期拒绝；缺省 null 字段仍必须有声明。limit 默认 50、范围 1..100。可空字段显式 null；不接受远端 ID、owner、凭据引用或任意 dict。

state 闭集：EMAIL_REQUIRED/PENDING/PROVISIONING/RETRY_WAIT/RETRY_REQUIRED/CONFIG_REQUIRED/IDENTITY_CONFLICT/MANUAL_REVIEW/READY/SUSPENDED。
error_code 闭集：EMAIL_REQUIRED/CONFIG_REQUIRED/IDENTITY_CONFLICT/REMOTE_OUTCOME_UNKNOWN/BOOTSTRAP_EXPIRED/REMOTE_REJECTED/REMOTE_UNAVAILABLE/RETRIEVAL_FAILED/RETRY_EXHAUSTED/USER_DISABLED，或 null。状态与 can_retry 的动态资格由后续 service 计算，DTO 不伪称授权检查。

| 模型 | 精确字段（除统一 schema_id/version；输入无此二字段） |
|---|---|
| ProvisioningStatus | revision>=0,state,error_code,updated_at,last_attempt_at,next_retry_at（后三项 UTC 时间或 null）,can_retry:StrictBool |
| ProvisioningConfig | revision>=0,anchor_revision>=0,name/base_url/updated_at 可 null,enabled:StrictBool,credential_configured:StrictBool；name 非 null 时去首尾后 1..80，base_url 非 null 时复用 normalize_base_url |
| ProvisioningConfigSaveInput | expected_revision>=0,expected_anchor_revision>=0,name（去首尾后1..80）,base_url（复用 normalize_base_url）,enabled:StrictBool,system_admin_jwt:SecretStr或null（默认null，null表示保留；非null须严格字符串且非空/无首尾空白，不校验真实JWT签名或暴露内容） |
| ProvisioningRetryInput | expected_revision>=0；自身 actor 由后续服务取得，不接受 user_id |
| ProvisioningTaskSummary | task_id:UUIDv4,user_id>0,email:StrictStr或null,status:ProvisioningStatus；email 后续从当前可信 identity 读取，不从 retained 恢复；本批不复制邮箱业务校验 |
| ProvisioningTasksInput | after_user_id>=0默认0,limit |
| ProvisioningTasksOutput | items:list[ProvisioningTaskSummary],next_user_id>0或null |
| ProvisioningTaskRetryInput | task_id:UUIDv4,expected_revision>0,reason（去首尾后1..500）；不接受任意 remote ID/owner |
| ProvisioningRetainedSummary | intent_id:UUIDv4,local_user_id>0,resource_kind(USER/KB/KEY),phase(PREPARED/WRITE_PENDING/UNKNOWN/VERIFIED/REJECTED),proof_kind(CREATED_201/ADMIN_ATTESTED/OWNER_MATCH或null),has_receipt:StrictBool,review_required:StrictBool,revision>0；保留意图生命周期闭集由后续实际 state service 使用，不推导 READY |
| ProvisioningRetainedListInput | after_key:StrictStr或null默认null,limit；after_key 非null须 canonical UUIDv4，因为该固定namespace逻辑key就是intent UUID，不接受namespace/SQL/remote ID |
| ProvisioningRetainedListOutput | items:list[ProvisioningRetainedSummary],next_key:UUIDv4或null |

所有输出有统一 schema/version；所有输入不接受 schema/version。不得从模型本身记录/打印原始输入或 validation exception，未来真实 Host ingress 的 invalid 安全封装需要动作阶段另证。SecretStr 的 JSON 序列化仅掩码；公开输出模型无秘密字段。列表严格元素类型，列表复制/深不可变并非本批新增要求（沿用既有 StrictDto 语义）。candidate/HTTP/internal task record DTO 在后续精确批次冻结，不提前添加。

后续动作名称已经确定为 status/retry/config.get/config.save/tasks.list/task.retry/retained.list，均带 `provisioning.` 前缀；本批仅冻结模型，后续注册服务实现才逐项注册。员工仅 status/retry，其他 fresh admin；动态 can_retry/脱敏和真实 ingress 权限尚待服务实际测试，不以模型测试替代。

## 十一、本地配置子批精确接线（I03a2 Fix2 本地独立验收通过）

具体存储、owner/CAS、同材料比较与权限时点、独立准备记录/原子轮换、失败取消收尾、retained header/摘要及最小八路径见同目录 provisioning-config-contract.md。该合同只接线三个真实 admin 动作：provisioning.config.get/config.save/retained.list；自助 status/retry、管理员 tasks.list/task.retry 等候实际 a3 task/state/UUID索引，不注册占位。a1 十一公开 DTO 保持原字段不变，也不因 a2 只有三个动作删除后续已冻结模型。

config/密钥原 owner 在其他管理员更新时不转移；保存时复核真实 actor 与所有实际 owner 身份。当前 JWT 相同（含显式再次提交）保留同 ref/revision/quarantine，null/name-only/启停也不清除；不同当前材料才能新建 ref，历史回放检测不在无指纹合同能力中。stage rev1，唯一 activation 后active rev2；没有 resolve expected_revision/返回metadata 的虚构 API。成功 activation/config replacement/旧active deletion/preparation COMMITTED 同事务；未知 staged 仅保留不可授权的精确引用，不因超时覆写/清除，也不阻止下一管理员自己的 CAS 保存。

retained reader 只选取固定 header 字段并显式投影；一页最多100/默认50，每项至多一个 receipt，不扫全表/联合身份分页、不输出remote IDs/邮箱/秘密/原因，也不恢复已删任务。未来 a3 完整 intent/receipt 必须兼容该最小 header，严格未知版本/坏值 fail closed。旧核心权限语义及企业共享连接保持兼容；实际 service/ingress tests 必须证明当前权限复核和脱敏，不以 DTO 测试替代。

此批由 GPT-6 Luna 实现并经 Fix2 独立 Spec/Quality 验收，当前配置文件68项、前轮两文件64项及限定静态通过。真实远端、worker、bootstrap清理不在此本地切片内，整体3.1/1.2仍未完成。后续四个本地状态/重试动作的独立规划PASS合同见同目录 provisioning-state-contract.md；实施须复用已接受的严格内部header和settlement，另捕获8路径基线，不注册未实现的worker/HTTP占位。
