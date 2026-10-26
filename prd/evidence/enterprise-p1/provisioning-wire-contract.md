# I03b1 WeKnora provisioning HTTP adapter 实施合同

状态：**独立 Spec PASS / Builder PASS，root已完整阅读复审并核对修订**。审查记录task-I03b1-wire-adapter-plan-review.md及plan-fix1-review.md。此为本地两路径wire实施合同，尚无功能实现、真实worker、远端调用或READY验收声明；仅在编辑前基线fresh compare及唯一作者可用后派发。

**Goal:** 交付可被将来 worker 调用的严格七端点 HTTP adapter，以真实 MockTransport 验证身份阶段隔离、结果分类、取消结果归属及共享预算。

**Architecture:** 一个新内部模块，复用实际 `weknora.OperationBudget` 和现有输入验证器，拥有短生命周期、阶段独占的 HTTP client；不继承现有 read wrapper。HTTP 结果只交给调用方内存 completion，不做任何 Host / durable 提交。

**Tech Stack:** Python asyncio、httpx、Pydantic；测试 `uv run pytest`、httpx.MockTransport。

**Spec / evidence:** `prd/evidence/enterprise-p1/user-provisioning-contract.md` 第四、八、十节；`prd/evidence/enterprise-p1/provisioning-state-contract.md`；`openspec/changes/enterprise-user-private-knowledge/specs/enterprise-user-provisioning/spec.md`；同目录 `task-I03-wire-contract-inputs.md`、`task-I03b-wire-ready-preparation.md`、`task-I03a3-remote-id-inputs.md`。上述固定 v0.8.3 commit `3e22822c1d2397be730c458c7539d0eb8c113a24` 的研究已完成，本批不重查上游或部署。

## 1. 实码接缝与精确白名单

实际 `weknora.py` 的 `OperationBudget` 默认 90 秒总时限、30 秒单请求、最多 10 个 reserve；`remaining_seconds()` / `reserve()` 使用同一开始时间及计数，`cancel()` 终止后续使用。`_WeKnoraReadClient.aclose()` 和 `_abort_operation()` 都 cancel 预算；其 `_json_request()` 的读取错误没有写结果语义。因此本批不复用 read wrapper、不修改其既有 close 语义，也不抽取 readcore。

`provisioning_records.py` 已有 `_UInt64`（strict 1..18446744073709551615）、`ProvisioningTaskRecord.remote_id_shape()`（非空、有效 UTF-8、最多 36 codepoints、原样保留）、完整 ConfigPin / Attempt / intent / receipt 模型。task、retained intent 和 proof 是调用方持久前提；adapter 不能自行伪造这些记录。

**未来 source 白名单仅一项：** 新 `plugins/enterprise-knowledge/enterprise_knowledge/provisioning_wire.py`。阶段 client builder、内部 DTO、completion、严格 parser、七方法全部在此；不新增通用 transport 框架。

**未来 test 白名单仅一项：** 新 `tests/unit/plugins/test_enterprise_provisioning_wire.py`。如该文件确需公共 fixture，先请 root 扩白名单，不修改既有 fixture / conftest 掩盖问题。

明确不改：`weknora.py`、`provisioning*.py` 的其它既有文件、main/ActionSpec、Host/Core、worker/factory/readcore、private provider、UI/federation、持久状态 producers、OpenSpec 或 i18n。内部闭集 code 没有用户文案，不需要 i18n；将来映射公开 error_code 的责任在 worker/service。

## 2. 内部 DTO 与调用接口

以下名字为本草案待冻结的内部接口；不是公开 action DTO，不加 provisioning schema header。内部 DTO 继承既有 `models.StrictDto`，冻结、strict、extra forbid；raw upstream 对象先显式投影，不能用 extra forbid 拒绝上游合法但不消费的字段。

复用边界：`UInt64` 直接用已有 `_UInt64` 类型配合 TypeAdapter；Remote36 **先用非 nullable StrictStr / TypeAdapter拒绝None及非string，再**调用已有 `ProvisioningTaskRecord.remote_id_shape()`，不能把接受None的helper单独当完整wire验证器。所有响应user/KB/creator/knowledge_base_ids元素与输入kb_id同样处理，不复制数值/UTF-8/长度规则。canonical URL 复用 `normalize_base_url()` 并要求等于输入；不重新定义 URL schema。UUID 使用 `_canonical_uuid4()`。名称用 `_trimmed_bounded()` 并要求返回值等于原值，不静默 trim。

最小值对象：

| DTO | 字段 / 不变量 |
|---|---|
| `UserIdentity` | `remote_user_id: Remote36, tenant_id: UInt64, username: StrictStr, email: StrictStr, is_active: StrictBool` |
| `CreatedUser` | `user: UserIdentity`；只由严格通过的 create-user **201** 生成 |
| `ExistingUser` | `user: UserIdentity`；只由严格通过的 create-user **200** 生成；与 CreatedUser 不共用可被 login 接受的基类 |
| `OwnerSession` | `user: UserIdentity, token: SecretStr`；仅 login 的完全匹配 user / active_tenant / owner membership 后产生；不保留 refresh_token |
| `OwnedKB` | `kb_id: Remote36, tenant_id: UInt64, creator_id: Remote36, name: StrictStr, description: StrictStr, type: Literal['document']` |
| `OwnedKBMetadata` | `kb_id: Remote36, tenant_id: UInt64, creator_id: Remote36, name: StrictStr, description: Optional[StrictStr], type: StrictStr`；用于完整列表，description缺失或null投影为None（明确无marker），合法非document行不筛掉；其它字段必需 |
| `KeyMetadata` | `key_id: UInt64, name: StrictStr, scope_type: Literal['tenant'], full_access: StrictBool, knowledge_base_ids: tuple[Remote36,...], capabilities: tuple[StrictStr,...]`；范围核对见第4节 |
| `CreatedKey` | `metadata: KeyMetadata, api_key: SecretStr`；一次性明文只交 caller；不返回 raw token / raw response |
| `KeyDeleted` | 无业务字段；表示 DELETE 精确 200 且 success is True |
| `WireFailure` | `operation: WireOperation, effect: Literal['NO_EFFECT','UNKNOWN'], code: WireCode, status: Optional[int]`；全部安全字段，无 body/request/url/exception/secret/callback原因 |
| `WireOutcome[T]` | 严格判别 union：`WireSuccess[T](value:T)` 或 `WireFailure`；不存在 success 与 failure 同时成立的对象 |

`WireOperation` 闭集 `CREATE_USER/LOGIN/CREATE_KB/LIST_KBS/CREATE_KEY/LIST_KEYS/DELETE_KEY`。`WireCode` 闭集 `INVALID_INPUT/PRE_SEND_REJECTED/BUDGET_EXHAUSTED/CLIENT_UNAVAILABLE/AUTH_REJECTED/IDENTITY_CONFLICT/HTTP_REJECTED/SCHEMA_INVALID/TRANSPORT_FAILED/CANCELLED`。仅 status 保存数值 HTTP 状态；没有 arbitrary message。

`WireCompletion[T]` 是 caller **在 await 之前**创建并持有的单次 completion slot；不是 exception，不是 Host record：

```python
class WireCompletion(Generic[T]):
    def take_outcome(self) -> WireOutcome[T] | None: ...  # move，一次取走；未完成返回 None
    # adapter 唯一私有 _publish(outcome)，同步、仅一次，内部字段 repr=False

PreSendCheck = Callable[[WireOperation], Awaitable[bool]]

class ProvisioningWireStage:
    def __init__(self, *, base_url: str, budget: OperationBudget,
                 stage: Literal['ADMIN','LOGIN','OWNER'],
                 pre_send: PreSendCheck,
                 credential: SecretStr | None = None,
                 owner: OwnerSession | None = None,
                 ca_bundle: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None: ...
    async def create_user(self, *, username: str, email: str, password: SecretStr,
                          completion: WireCompletion[CreatedUser | ExistingUser]) -> None: ...
    async def login_created_user(self, *, created: CreatedUser, password: SecretStr,
                                completion: WireCompletion[OwnerSession]) -> None: ...
    async def create_kb(self, *, name: str, description: str,
                        completion: WireCompletion[OwnedKB]) -> None: ...
    async def list_owned_kbs(self, *, completion: WireCompletion[tuple[OwnedKBMetadata,...]]) -> None: ...
    async def create_key(self, *, kb_id: str, name: str,
                         completion: WireCompletion[CreatedKey]) -> None: ...
    async def list_keys(self, *, completion: WireCompletion[tuple[KeyMetadata,...]]) -> None: ...
    async def delete_key(self, *, key_id: int, completion: WireCompletion[KeyDeleted]) -> None: ...
    async def aclose(self) -> None: ...
```

这些 `...` 仅合同签名，实施不留 stub。三个阶段同一 owner loop 顺序使用。ADMIN 必须只有 credential、无 owner；LOGIN 必须 credential/owner 都 None；OWNER 必须只有 owner、无 credential。create_user 仅 ADMIN、login 仅 LOGIN，其余仅 OWNER。transport 是可信测试 seam，生产默认 None，不来自 action/config payload。

增加唯一安全usage异常 `WireUsageError(code: Literal['INVALID_CONSTRUCTION','COMPLETION_ALREADY_USED'])`：固定code/args，无原输入、secret、raw exception/cause/context。constructor非法stage/URL/credential-owner组合同步抛INVALID_CONSTRUCTION，无completion、请求或预算消费。

completion生命周期固定 **EMPTY→CLAIMED→PUBLISHED→TAKEN**，take不能把TAKEN重置EMPTY；未publish时take返回None而不改状态，publish后第一次take移走结果、再次take返回None。方法在第一次await前同步claim slot并检查/设置stage busy；已CLAIMED/PUBLISHED/TAKEN的slot抛COMPLETION_ALREADY_USED，不覆盖原结果、不callback/reserve/send。stage busy时第二调用若带新EMPTY slot，先claim后立即publish NO_EFFECT/INVALID_INPUT并返回，不排队；正常业务输入/错误阶段/已关闭stage也向新slot publish NO_EFFECT/INVALID_INPUT并正常返回。只有获得busy权的调用在finally释放它。

除此明确usage拒绝，每方法正常返回None前completion必已同步publish唯一终态；caller调用take_outcome取得所有权。slot repr不显示内容。普通异常不得含结果；任何边界转换不能带raw body/secret作为args、cause或context。捕获后安全异常需在原异常处理作用域外抛出，避免仅from None隐藏而仍保留敏感__context__。

## 3. 可信 pre-send 门与取消所有权

每一次物理 HTTP（包括 GET/login/DELETE）都必须调用 `pre_send(operation)`；它是后续真实 worker 提供的 **trusted callback**，不接受浏览器证明、不从只读状态投影推导许可。callback 在调用前已掌握本次完整 task/attempt/config/intent context。返回 True 的合同为：实际 worker 已重新检查当前 generation/claim lease、employee、完整 config pin/anchor、精确 auth block、首次 bootstrap 的绝对 24h 截止，并且本次资源写入的 durable WRITE_PENDING 已在正确事务完成且仍对应此 attempt。对 read/login 同样检查当下资格/截止，但不能假称 login 有 USER 写 intent。

adapter **不读写 Host，不验证数据库事务，不声称 callback 返回 True 就构成 durable proof**。未接入真实 worker 的单元测试仅验证调用顺序与失败边界，不声称 WRITE_PENDING 已实现。callback 不负责在此点生成新密码/marker，也不能在失败后向 adapter 返回放宽预算的替代上下文。

请求顺序固定：同步claim slot/busy并校验本地参数/阶段 → 在共享剩余总时限内创建client/TLS → trusted callback → callback True后 `budget.reserve()` → 同步组装局部request → **同步 `budget.remaining_lifetime_seconds()` 最后截止门** → 将extensions timeout设为min(reserve返回值,此刻lifetime) → 立刻设置request_started=True并调用client.send。最终门耗尽即NO_EFFECT/BUDGET_EXHAUSTED、零send；不要用remaining_seconds()替代，因为第10次reserve后计数上限会错误拒绝本来合法的第10次发送。

reserve后、send前不再await Host/client init/callback；外层使用原始同一操作截止，不重开90秒。callback等待、TLS/client初始化、同步组装及前序阶段/Host等待都消耗共享wallclock。这个最后本地发送门不提供callback与物理网络之间的数据库原子性；期间身份/lease变化仍由未来worker回执提交的fresh checks防止激活。

callback返回False或异常、输入/client初始化失败、预算拒绝、send调用前取消均为NO_EFFECT；不保留callback异常文本或对象。reserve已计数但未send也不回退。**写/LOGIN的send一旦调用，timeout、断连、取消、未知异常均UNKNOWN**，包括ConnectError；不能按异常名称猜“未写”。GET以第5节读规则为准，失败/取消为NO_EFFECT读取失败，不改变父写UNKNOWN。

取消路径必须先同步publish，再传播普通asyncio.CancelledError：尚未send → NO_EFFECT/CANCELLED；写/LOGIN已send尚无可靠解析结果 → UNKNOWN/CANCELLED；GET已send取消 → NO_EFFECT/CANCELLED读取失败；已在同步parser片段publish的可靠成功保持原值。parse成功到publish之间不await。异常对象不能附secret DTO，GET规则不被通用write handler覆盖。

caller 持有 completion；被取消的 await **不保证返回值**。caller 在其受管理的 finally / settlement 中 take_outcome，按结果保存可靠非授权回执或 UNKNOWN；这属于未来 worker。adapter 不 shield 远端 HTTP 来承诺取消后还能恢复回执，不留后台写请求继续运行。取消同时 `budget.cancel()`，清理当前阶段，所有后续阶段即便换 client 都被同一 budget 拒绝。如果进程崩溃、generation 失效或 caller 未能持久保存 outcome，既有 durable WRITE_PENDING 仍未知；completion 不是恢复日志，也不能恢复丢失的 Key 明文。

## 4. 请求与严格成功 parser

路径在已 canonical 的 `base_url` 后拼固定 suffix；沿用现有配置的 `/api/v1` 基准，不追加第二个 `/api/v1`。tenant/key path 只从已验证 UInt64 转 canonical 十进制；query 固定 `creator=mine`，无分页/任意 URL 输入。所有调用一次 send，无自动 retry、redirect、鉴权刷新或后续自动 endpoint。HTTP 3xx 不跟随。

JSON 必须有效 UTF-8、对象/数组类型正确；拒重复对象键、NaN/Infinity 和 required 字段缺失/null/类型转换。`success` 必须 identity `is True`，不是 1/'true'；整数严格拒 bool、float、字符串、0、负数、2^64 溢出。Remote36 保留收到原值，不 trim、改大小写或强制 UUID。上游额外字段不进入返回 DTO；parser/ValidationError 一律在边界转安全 failure，禁止暴露 Pydantic 的 inputs 或 raw JSON。

| endpoint | 请求 | 严格确认 |
|---|---|---|
| POST `/system/admin/users/create` | 仅 username/email/显式 password | **201 原始 user**，无 success/data 包装；user.id/tenant_id/username/email/is_active 正确且 name/email 精确等于请求、active is True → CreatedUser。**200** 同形且匹配 → ExistingUser，永不自动 login。显式传 password，不消费 generated_password，不依赖远端自动生成密码。 |
| POST `/auth/login` | 仅 CreatedUser.user.email + 同次已持久 password | 精确200、success True、user identity 等于 CreatedUser；active True；active_tenant.id == user.tenant_id；memberships 是数组且恰好一项对应当前 tenant、role 严格 'owner'；tenant_name 是 string；无 status 假字段。token 非空 SecretStr；refresh_token 按固定响应存在且非空验证后丢弃，不存入 DTO。 |
| POST `/knowledge-bases` | `{name,description,type:'document'}` | 精确201、success True、data对象；完整 OwnedKB；tenant/creator 精确等于 OwnerSession，name/description/type 精确回显。 |
| GET `/knowledge-bases?creator=mine` | 无 body | 精确200、success True、data完整数组；每项OwnedKBMetadata、tenant/creator等于当前Owner、id不重复；合法其它type/description缺失或null行完整返回，不过滤。不截断数组、不跳过坏行。只有worker候选目标要求document+完整精确name/description/归属；恰好一条决策归worker。 |
| POST `/tenants/{tenant}/api-keys` | `{name,full_access:false,knowledge_base_ids:[kb_id],capabilities:['retrieve']}` | 精确201、success True、data对象；strict key_id；scope_type='tenant'、name等于请求、full_access is False、KB数组精确单元素本次 KB、capability精确单元素 retrieve。api_key 与 token 均非空 string，且本地接收策略要求两明文一致；不一致 UNKNOWN（这是安全接收策略，不声称上游额外唯一性保证）。只返回 SecretStr(api_key)。 |
| GET `/tenants/{tenant}/api-keys` | 无 body | 精确200、success True、data完整数组；所有 key_id strict 且不重复；投影 KeyMetadata，不保留 api_key/token 掩码。列表可以含其它范围/名字的 Key，严格解析 actual bool / 数组但不要求每把都 retrieve/单库；worker 对目标按精确name+scope+KB+capability核对，不能恢复明文。 |
| DELETE `/tenants/{tenant}/api-keys/{key_id}` | 无 body | 仅精确200、success is True → KeyDeleted。仅显式调用；adapter 不自动清理、撤销、重建。 |

list KB的合法非document/无marker行不是坏行，不能因此让整体对账失败或把它们筛除；真正坏ID/字段类型/归属仍使整次读取失败。login不要求上游没有返回的membership status/is_system_admin。Key list不能把masked string填CreatedKey。

输入名称最大 username50/KB255/Key128，username最小2，其余非空且不含首尾空白；description 仅接受 worker 提供的稳定 opaque marker，不添加邮箱。复用现有 helper 验证，不推测数据库额外限制。email 使用 callback 绑定的可信当前 normalized identity 值，要求非空且精确不改写；adapter 不另造邮箱 schema/邮箱绑定规则。password 接受非空 SecretStr；32字符随机复杂密码的生成、持久登记、重用与24h截止属于 worker，不由 adapter生成/延长。候选名称和 description 模板直接沿用准备文件已补证方案，worker 负责生成持久化；adapter 不为冲突追加后缀或更换名字。

## 5. 端点结果分类（禁止通用4xx无效果）

`NO_EFFECT` 意为对**本次目标调用**有固定证据表明未执行目标变更；不意味着未有旧资源，也不授权重试/接管。读失败采用 NO_EFFECT 仅说明本 GET 没有写，不能清除父 POST 的 UNKNOWN。

| operation / 观察 | outcome |
|---|---|
| 任一 send 未调用 | NO_EFFECT，按固定输入/门禁/预算/client/cancel code；未执行证明只针对本次调用 |
| create-user 严格201 | Success(CreatedUser)，仅内存可靠结果；不等于 receipt/claim 已落盘 |
| create-user 严格200既有 | Success(ExistingUser)，**无新建证明、禁止以本次密码登录**；caller处理身份冲突，不接管 |
| create-user 固定409 | NO_EFFECT/IDENTITY_CONFLICT；固定该 endpoint 的 username/email 身份冲突，不能扩展其它409 |
| ADMIN create-user 或 OWNER KB/Key/list/delete 精确401/403 | NO_EFFECT/AUTH_REJECTED；依据固定路由守卫写前拒绝。仅 ADMIN 拒绝供未来 worker 隔离精确 admin lineage；adapter不写 block，不将 Owner失败误标全局admin block |
| POST create-user/KB/Key、DELETE：除以上固定拒绝之外的4xx/409、3xx、5xx、非预期2xx | UNKNOWN/HTTP_REJECTED；不凭通常HTTP含义推断未写；DELETE404包括不存在/撤销失败语义，不能当已撤销成功 |
| 任何写/LOGIN 的预期2xx但 JSON/类型/归属/回显/secret不完整 | UNKNOWN/SCHEMA_INVALID；200 user body损坏也不创建 ExistingUser，不自动login |
| 写/LOGIN send后 timeout/断连/取消/未知异常 | UNKNOWN；不自动重发 |
| LOGIN 非200（含401/403） | UNKNOWN/HTTP_REJECTED（401/403 code AUTH_REJECTED）；不把login当SystemAdmin守卫写前证明，不宣称未签发会话；不影响已确认 USER证明 |
| GET list 非200、坏JSON/行、timeout/断连/取消 | NO_EFFECT读取失败；保留该请求安全code；**父创建状态不变**，无“列表无项→首次POST没发生”推断 |

GET取消虽不产生写未知，同样 abort整个共享预算；request_started记录仍可供测试，但不声称资源变更。接收201之后若取消发生在同步publish之前，按无可靠成功处理；不会把 status201本身当 proof。

## 6. 阶段 client、header 与共享预算

一个 attempt 在最外层创建真实同一个 OperationBudget；ADMIN client关闭 → LOGIN client关闭 → OWNER client关闭 → 将来 private scoped client 使用同预算。阶段 `aclose()` 只关本 stage transport、清局部credential/owner引用，不 cancel 借来的budget。close不返还计数、不重置开始时间；只有取消/最外 attempt owner最终 abort 才cancel。后续 scoped provider尚未交付，本批用真实下一stage/预算断言接缝，不做伪 readiness probe。

每 stage 独占新 `httpx.AsyncClient`：TLS client构造在 executor；follow_redirects=False、trust_env=False、无default auth、无credential header、无cookie、无event hook重写。`ca_bundle`仅可信server参数；绝不 verify=False。测试 transport仅用于MockTransport。

用**新建 `httpx.Request` + `client.send(request, auth=None, follow_redirects=False)`**，不调用会合并client defaults/cookies的build_request；request extensions显式设置 reserve所得HTTPX timeout，并外层 asyncio timeout限制。如此就算client曾存Set-Cookie或默认 Authorization/X-API-Key/X-Tenant-ID，实际 request仍只有本阶段显式头。测试必须给实际client注入poison auth/default headers/cookies，并在MockTransport看到最终请求断言隔离，不能只检查构造headers字典。

实际credential头：ADMIN只有`Authorization: Bearer <SystemAdminJWT>`，没有X-Tenant-ID/X-API-Key/Cookie；LOGIN无Authorization/X-Tenant-ID/X-API-Key/Cookie；OWNER只有`Authorization: Bearer <OwnerJWT>`与已验证UInt64转十进制的`X-Tenant-ID`，无管理员JWT/X-API-Key/Cookie。JSON写自动Content-Type application/json；不打印局部headers/body。响应Set-Cookie永远不进入下一请求；同Owner连续请求也不能带Cookie。

client创建future用shield避免取消丢失未来client引用；取消/总timeout时登记唯一late-close task，在future完成后立即aclose。stage持有cleanup引用；aclose幂等，重复取消期间也保留同一cleanup task并在受管理finally真正等它结束，不把取消awaiter当资源已释放；不得因此新增HTTP、重开budget或延迟发请求。若executor仍在建TLS client，不冒称已关闭；late cleanup仅关client，没有后台继续write。失败只安全code，不记录future异常repr。整个网络操作仍90秒，实际资源排空不冒充可在90秒内强制终止阻塞executor。

所有普通结果在close前已publish，close失败不覆写可靠成功/ExistingUser结果。异步context管理或caller finally必须调用aclose；最外worker以后负责Host生命周期/drain，不由本模块注册后台worker。

## 7. secrecy

password、AdminJWT、OwnerJWT、api_key只为局部SecretStr、请求与caller-owned secret DTO所需。SecretStr返回值仍是敏感值，caller可以显式get_secret_value用于后续阶段/加密存储；不能把“repr已遮盖”误说成非敏感receipt。OwnerSession / CreatedKey / completion 的repr不显示秘密；错误只含WireFailure闭集字段。

HTTPX response/request/headers/body、ValidationError、callback异常及任何原始exception都不附在结果/异常中；wire自身不用str(exc)/repr(exc)、不链cause/context，也不产生body、response.text、完整URL、密码/JWT/Key日志。这里限定wire自身；HTTPX依赖可能INFO记录request URL，本批不改全局依赖日志策略，不声称所有依赖天然不记URL。固定path/query不含secret；sentinel测试断言密码/JWT/Key不进入依赖日志，不能断言完全没有URL日志。忽略的generated_password/refresh_token/raw token也不留在DTO；清引用不声称物理擦除Python字符串。

## 8. 单一实施切片与可核验测试

> For agentic workers: 在root批准执行后按仓库保留的实施方式使用 executing-plans / subagent-driven-development；本草案本身不授权启动agent或实现。无commit/push授权。

- [ ] 先在唯一新测试文件写最小真实MockTransport合同，执行 `uv run pytest tests/unit/plugins/test_enterprise_provisioning_wire.py -q` 验证目标缺失失败；不通过假HTTP wrapper或仅model构造证明外呼行为。
- [ ] 实现唯一新wire模块，完成下面矩阵；每个请求直接由AsyncClient送至MockTransport，检查实际路径、query、body、最终headers、请求数量及completion。
- [ ] 定向测试通过后运行本批Ruff/mypy要求；root最终集成门禁为 `make all`，本纯规划不运行它也不声称绿色。

聚焦测试矩阵（至少覆盖下列行为，参数化允许）：

1. 201 raw create-user通过，200 ExistingUser明确分支；将ExistingUser传login在send前拒绝、请求数不增；create409 NO_EFFECT而KB/Key409 UNKNOWN。成功形状多一层data包装失败。验证请求始终显式password。
2. ADMIN→LOGIN→OWNER实际client头隔离；poison client默认auth、Authorization/X-API-Key/X-Tenant-ID/Cookie和cookie jar，response Set-Cookie后再发Owner请求；MockTransport实际收到仅合法stage凭据。redirect不访问Location，retry计数始终1。
3. login active/tenant/owner绑定；string tenant、bool/float/0/负值/溢出均拒绝；多owner匹配/缺token/坏refresh_token/错误email/user/tenant/role不返回OwnerSession；不要求不存在的status字段。
4. tenant/key uint64端点解析与path边界，上限18446744073709551615可接受；拒string '42'、42.0、True、0、负数、溢出。Remote36先StrictStr再现有helper：接受有效原样非UUID/36-codepoint补充平面字符；各user/KB/creator/KB数组元素及输入kb_id拒null、非string、37、空、非法UTF-8 surrogate；不trim/大小写改写。
5. KB201归属/marker回显；list正例同时含完整匹配document目标+合法非document/无marker其它行，完整返回两者。错tenant/creator、重复ID、坏类型仍整次失败。Key201严格单KB/retrieve/full_access False/name/scope及secret alias；Key list掩码不能成为CreatedKey；GET不自动关联marker/重发POST；DELETE仅200successTrue通过，404 UNKNOWN。
6. 各写端点malformed预期2xx（非JSON/重复key/nonfinite/wrongroot/缺字段/假success/错回显）→UNKNOWN；只读失败不抹父写UNKNOWN；固定401/403与create409分类没有扩成generic4xx。
7. callback每次实际执行一次、False/抛敏感异常/取消时零send；顺序client ready→callback→reserve→send；预算超时发生callback或TLS等待期间也是send前NO_EFFECT，使用独立event控制不用真实长sleep。
8. MockTransport handler记录请求开始并用Event阻塞后cancel：write/login completion UNKNOWN、普通CancelledError向上传播、预算cancel后不能下一stage；GET为NO_EFFECT读取失败但预算同样停止。解析并同步publish成功后再取消caller，slot仍可一次取走成功；异常repr不含秘密。取消绝不保证恢复远端回执。
9. 单个真实OperationBudget跨Admin/Login/Owner关闭累计计数；close不cancel/reset。受控clock验证90秒共享wallclock，timeout<=30且<=最终lifetime；第10次reserve可send、第11次不send。同步request组装推进clock过截止时最终remaining_lifetime_seconds门拒绝、零send；callback/client构造同样耗预算，不每阶段新建budget。
10. TLS builder future迟到：取消/timeout先publish NO_EFFECT，future后创建的client被准确关闭一次，cleanup受阶段finally排空，不留下HTTP task；close失败不覆写已publish的CreatedKey，敏感sentinel不出现在repr、exception args/cause、caplog或failure DTO。
11. constructor非法stage/URL/credential-owner只抛固定WireUsageError、零请求/预算；方法错误stage/closed/坏input向新slot写NO_EFFECT/INVALID_INPUT。用Event阻塞第一次调用，第二调用新slot立即失败、不callback/reserve/send、不覆盖首slot；重用CLAIMED/PUBLISHED/TAKEN slot抛固定usage code；take两次仅第一次取走结果。repr/args/cause/context都不带sentinel。

Review Focus固定为：本次200误login；client默认auth/cookie渗漏；strictuint64转换；取消丢失/泄露secret结果；共享budget被中间close终止或重置。上述1/2/4/8/9/10直接验证它们，不以架构叙述替代测试。

## 9. 待root和独立审查冻结的具体决定

1. **Completion接口**：本草案选择caller-owned一次性slot，禁止把secret result塞进CancelledError；审查需确认move语义、同步publish及正常/取消路径均可实现，不需要额外持久写授权。
2. **HTTP构造**：选择Request+send隔绝实际client defaults/cookies，显式timeout/auth；独立审查已核对锁定httpx0.28.1支持此接缝，后续用实际MockTransport验证，不重查上游。最终门使用remaining_lifetime_seconds，不误挡第10次reserve。
3. **Key aliases / 列表metadata**：create api_key/token一致是本地接收政策；KB列表改独立metadata DTO，完整保留合法其它type/无marker行，仅候选目标受document+完整marker限制；不加上游保证、不放宽创建Key scope。
4. **Login失败语义**：所有已send、未完全确认login均UNKNOWN会话结果，绝不撤销USER201证明；未来worker再冻结何种资格下可重新login，不能由本adapter推导可重试或持久状态转换。
5. **实际worker接入欠缺**：pre_send是契约，不是durableWRITE_PENDING实现；真实claim/pin/24h/intent原子写、secret加密保存、retained-only回执、auth-block、KEY_STORED及实际private LIST+SEARCH均留后续明确白名单。I03b1通过也不勾3.1/3.2/3.3/1.2，不声称部署版本、网络、账号或资源实测完成。

## 10. 独立审查修订记录

按同目录task-I03b1-wire-adapter-plan-review.md完整采纳：B1分离创建/列表KB DTO并加合法混合列表正例；B2冻结WireUsageError、slot四态、stage busy及可观察拒绝；B3增加reserve后同步最终lifetime门和第10/11次测试。同步补Remote36先非nullable StrictStr、GET取消例外、重复取消drain及wire/依赖日志边界；保留两文件范围与NOT READY。修订未替代下一轮root/独立复核。

本次只读本地已冻结证据、实码与独立审查并修订本草案；未读环境/凭据/secret，未访问网络或用户部署，未生成密码，未修改source/test/spec，未运行测试、Git或DDL。
