# Spec Delta

## Purpose

让启用企业知识能力的 Octop 新账号通过唯一邮箱拥有明确的 WeKnora 身份和独立私有知识库；远端开通故障不影响本地注册成功，并以可恢复、可追踪的流程完成开通，避免混用身份或重复创建资源。

## ADDED Requirements

### Requirement: Registration uses a unique normalized email

启用企业用户开通策略后，系统 MUST 对邀请注册、管理员创建及 SSO 首次创建统一要求有效邮箱，使用 trim/lowercase 后的邮箱进行数据库唯一校验；既有邀请准入与已绑定 SSO 身份规则 MUST 保留。

#### Scenario: Concurrent registration uses equivalent email spellings

- **WHEN** 两个新账号并发提交大小写或首尾空白不同的同一邮箱
- **THEN** 最多一个本地账号创建成功，另一请求返回本地化邮箱占用错误且不消费其邀请，不触发第二份远端开通。

#### Scenario: First SSO creation has no usable email

- **WHEN** SSO 首次创建没有可信配置来源的有效邮箱，或该邮箱已被不同本地身份使用
- **THEN** 系统不创建无邮箱或重复邮箱账号、不静默按邮箱合并身份，并显示完成邮箱登记或联系管理员的明确状态。

#### Scenario: Ordinary Octop deployment does not enable the policy

- **WHEN** 部署未启用企业用户开通策略
- **THEN** 原有非企业账号创建语义保持兼容；客户端不能通过请求参数关闭已启用策略。

### Requirement: Local registration commits before remote provisioning

系统 SHALL 在本地账号注册事务成功后返回注册成功与正常登录能力，不在注册事务或响应等待中调用 WeKnora；知识能力在对应开通完成前 MUST 显示待开通并拒绝以其他人的连接代用。

#### Scenario: Remote service is unavailable during registration

- **WHEN** WeKnora 不可达而本地邮箱、密码及邀请校验通过
- **THEN** 注册成功且邀请只消费一次，账号可以登录，知识显示待开通；注册请求不会因为远端超时回滚。

#### Scenario: Process stops after registration commit

- **WHEN** 本地账号提交后、开通意图写入前进程停止
- **THEN** 重启后的对账能发现该账号并建立唯一开通任务，不要求员工重新注册。

### Requirement: User and private resources have one stable mapping

系统 MUST 为每个已开通 Octop user_id 保持唯一远端 user_id、个人 tenant 与私有 KB 映射，并阻止远端身份或私有资源被第二个本地账号绑定；邮箱、库名或客户端传入 ID 本身 MUST NOT 构成资源所有权证明。

#### Scenario: Worker receives repeated notifications

- **WHEN** 同一账号被重复通知或多个 worker 同时领取
- **THEN** 只有获准领取者推进开通，已有映射被复用，不能给该员工创建第二份有效映射。

#### Scenario: Existing remote email is not an owned pending creation

- **WHEN** 远端返回同邮箱既有用户，但系统不能证明它属于此次已登记创建意图
- **THEN** 状态进入身份冲突待管理员处理，不重置其密码、不自动接管账号或私有库、不冒充开通成功。

### Requirement: Remote write uncertainty is reconciled before retry

系统 MUST 在远端写入前持久化意图及可恢复关联；超时、断线或崩溃导致结果不确定时，先核对实际远端身份/资源。无法证明首次写入未生效或无法唯一找到自有结果时 MUST 保持待核对，禁止盲目再次创建或自动删除远端资源。

#### Scenario: Knowledge base creation response is lost

- **WHEN** 创建私有 KB 的响应丢失
- **THEN** 系统先核对已登记意图的远端标记和资源；唯一确认后继续，没有充分证明时保持待人工核对，不以再次 POST 解决未知结果。

#### Scenario: Scoped key secret response is lost

- **WHEN** 远端可能已创建 scoped Key，但本地没有可靠保存其明文
- **THEN** 系统不把脱敏列表当作可用凭据，不将知识标为就绪；仅在确认自有 Key 并安全撤销/轮换后恢复，否则要求管理员处理。

### Requirement: Provisioning retries are bounded and observable

系统 SHALL 对可重试故障自动退避重试，每次尝试具有网络/总时限、并发领取与持久检查点；达到连续失败上限、身份冲突或配置缺失时停止自动写入并明确显示原因。员工重试 MUST 仅作用于自己的任务，管理员可以查看和恢复授权范围内任务。

#### Scenario: Employee requests retry repeatedly

- **WHEN** 员工连续点击重试或多个页面同时发起
- **THEN** 任务只被领取一次，其他请求返回当前状态，不并行创建资源、不重置已有远端检查点。

#### Scenario: Administrator token expires

- **WHEN** 远端明确拒绝管理员凭据
- **THEN** 状态显示管理员配置阻塞，停止自动重复鉴权请求，管理员替换有效配置后可以从可靠检查点恢复。

### Requirement: Credentials stay server side and separate from login

系统 MUST 使用受信任服务端管理员认证开通用户，并为新远端用户使用独立随机引导凭据；不得读取或复用 Octop 登录密码。管理员 JWT、引导密码及 scoped Key MUST 加密存储，不出现在公开状态、模型工具、日志或浏览器持久存储中。

短期 Owner 引导凭据 MUST 自首次持久登记起设置不可由重试延长的 24 小时使用截止时间，在到期或开通完成后禁止继续使用。系统 SHALL 在服务运行时清理到期凭据；若停机越过截止时间，恢复后立即启动清理，且在任何恢复登录或外呼前拒绝已过期凭据并转人工恢复。该保障不承诺停机期间离线物理到点删除，不要求独立常驻清理服务。

#### Scenario: Employee opens provisioning status

- **WHEN** 员工读取自己的开通状态
- **THEN** 只收到状态、固定错误码、时间及可用操作，不收到管理员凭据、远端引导密码、Key 或其他员工身份映射。

#### Scenario: Bootstrap credentials expire while the service is stopped

- **WHEN** 服务停机期间短期引导凭据超过其首次登记后的 24 小时截止时间
- **THEN** 恢复后立即启动到期清理，任何恢复尝试在登录或外呼前拒绝该凭据并转人工恢复；重试不能续期或使用它，清理不冒称远端会话已经撤销。

### Requirement: Legacy and changed identities do not silently rebind

历史无邮箱账号 SHALL 保留登录，知识保持待补齐；补齐唯一邮箱后才进入开通。已映射账号修改邮箱、被禁用或被删除时 MUST 重新校验本地资格，禁止按新邮箱自动绑定另一远端用户；不得自动删除远端用户、tenant 或文档。

#### Scenario: Disabled user has a delayed worker result

- **WHEN** 开通网络响应返回前账号已被禁用或邮箱已变化
- **THEN** 迟到结果不能激活知识权限，已产生远端副作用保留对账记录，不提交失配映射。

### Requirement: Minimal provisioning proof survives local user deletion

系统 MUST 在远端外呼前持久化非敏感意图，并保留可靠回执与反向资源占用；这些记录不依赖员工或配置管理员仍存在，插件停用/卸载或删除本地用户不得清除。保留关联 MUST NOT 作为访问权限或接管证明；私人密码、JWT 与 Key 仍按正常账号生命周期清除，不写入保留记录。

#### Scenario: User is deleted before a remote response arrives

- **WHEN** 已登记意图的远端写入返回前本地用户被删除
- **THEN** 正常映射/密钥激活失败；有效世代可另存非授权可靠回执，或在无法保存时保留不确定意图以供核对，不自动再建或删除远端资源。

#### Scenario: Another account reuses the deleted user's email

- **WHEN** 新 user_id 使用与已删除账号相同的邮箱，且原远端资源已有保留占用
- **THEN** 新账号不得认领原资源；配置改名或密钥轮换不能绕过同一真实远端身份域的占用。

#### Scenario: A mixed activation transaction conflicts

- **WHEN** 身份前提、普通/保留记录 revision 或凭据激活/清理任一条件失败
- **THEN** 整个激活事务回滚；后续 retained-only 回执写入不授予 READY 或恢复任何凭据。

### Requirement: Administrative actions use current authority

系统 MUST 在开通管理动作入口、关键本地提交前及远端候选发现返回前，用真实 Host 认证 actor 重新核对数据库当前管理员资格；内存角色或客户端 user_id 不构成授权。后台任务使用已批准的当前配置，不伪造管理员 actor；配置停用、凭据变化或 owner 删除使旧配置 revision 失效。

#### Scenario: An administrator's cached role is stale

- **WHEN** 请求缓存仍标记管理员，但数据库已降权、禁用或删除该用户
- **THEN** 新管理操作或关键提交复核拒绝，不返回核对候选、不按旧角色写配置；不以同邮箱或配置 owner 替换请求身份。

### Requirement: Provisioning UI exposes actionable states

现有邀请注册与用户管理界面 SHALL 提示邮箱必填及冲突；插件设置与聊天 composer SHALL 区分加载、待开通、重试中、待补齐邮箱、管理员配置阻塞、身份冲突、待核对、失败和就绪。状态交互 MUST 提供中英文本地化、键盘操作及服务器时区时间。

#### Scenario: Registration succeeds but knowledge is pending

- **WHEN** 员工完成注册并进入聊天
- **THEN** 显示“账号已注册，知识库待开通”及可用的刷新/重试入口；保留输入草稿，知识提交被明确阻止，不显示成空库或网络检索成功。

#### Scenario: A stale status response belongs to another account

- **WHEN** 用户退出、切换账号或插件卸载后旧状态请求返回
- **THEN** 页面丢弃旧结果并清理操作能力，旧重试按钮不能作用于新账号，卸载后的内置就绪阻断仍生效。

### Requirement: Readiness validates the persisted private retrieval key

系统 MUST 先可靠加密保存并激活精确私库白名单的 retrieve Key 至非授权 KEY_STORED 检查点，再从持久引用读取同一 Key，使用实际私库检索 provider 执行 LIST 与 SEARCH。全部验证成功且当前身份/配置及证明 CAS 有效后，才提交 READY 映射并清理引导凭据；允许真实空结果，不得用 Owner JWT、特殊禁用模型探测或创建假文档代替。该流程 SHALL 共用单次既定网络预算。

#### Scenario: Retrieval fails after the key has been persisted

- **WHEN** KEY_STORED 后真实 scoped Key 检索失败
- **THEN** 状态保持未就绪并保留该恢复检查点，不再新建 KB/Key，不授权普通私库使用；重试验证同一最终凭据。

### Requirement: Global authentication rejection does not fan out across employees

系统 MUST 持久隔离远端明确拒绝的精确管理员凭据 lineage，对其他用户及员工显式重试也不重复发送同一拒绝凭据。仅更改显示名或任务 revision 不得解除；真正替换凭据后才按新 lineage 恢复。

#### Scenario: A rejected token would otherwise be tried for the next employee

- **WHEN** 前一员工尝试已收到该管理员凭据的确定 401/403，扫描推进到下一员工
- **THEN** 下一员工显示配置阻塞，不再使用该精确凭据调用远端；可靠既有资源不被重建。

### Requirement: Provisioning domain identity survives configuration recreation

系统 MUST 将首次 canonical provisioning URL 固定在无 owner FK 的单一保留锚点，配置删除重建、改名和凭据轮换不得替换该锚点以规避反向资源占用；管理员 SHALL 能以有界非敏感摘要识别已删用户的未知意图。摘要不产生接管或访问权限，URL 归一化不冒称服务器指纹识别。

#### Scenario: Configuration administrator is deleted

- **WHEN** 配置管理员删除导致普通配置和密钥消失，另一管理员重建配置
- **THEN** 仅能引用原固定域和 canonical URL，历史 intents/claims 仍保留；没有有效新配置时不会外呼，摘要不恢复已删员工任务。
