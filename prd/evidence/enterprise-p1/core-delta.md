# 核心修改清单

状态：设计已核查；逐任务实施。基线 dd9dce4，用户已要求插件化。C0=无需核心变更；C1=组合已有接口；C2=需通用桥接。仅下表允许进入核心；不得把 WeKnora、scope、public_query、预算或业务页面写入宿主。

| 级别 | 文件/接缝 | 缺口及职责 | 验证与回退 |
|---|---|---|---|
| C2 | migrations/022_plugin_host_storage.sql/.pg.sql；repos/plugin_records.py、plugin_credentials.py；db/services.py | v21 无命名空间 CAS/暂存凭据；仅 SQL 与 DI | fresh/21→22/幂等、CAS、rollback、namespace；回退插件保留表 |
| C2 | agents/plugins/host_contracts.py、host.py | SDK 无 Host 事务、身份、注册动作/资源、配套世代 | 绑定/越权/依赖/停用/旧引用；普通插件无变化 |
| C2 | agents/plugins/manager.py、infra/server.py | 加载后绑定 Host、动态 services provider、注销及在途取消 | setup失败清理、服务重绑定、reload/disable/uninstall |
| C2 | api/routers/plugin_actions.py、api/app.py、api/openapi_meta.py | 通用认证 envelope/schema目录/资源代理 | 未登录、伪actor、未知动作、schema、资源再授权、api/docs |
| C2 | api/routers/plugins.py 的 load/install/enable/uninstall 调用点 | Host 生命周期可能等待存储提交锁，已有 async router 必须把相关同步操作放 executor | HTTP停用/卸载时loop进度与在途action取消；既有权限/响应不改 |
| C2 | agents/plugins/execution.py、agents/manager.py、gateway/process/processor.py、gateway/process/stream_project.py、gateway/steer/coordinator.py；gateway/ws/ws_channel.py的_continuation_inbound；agents/teams/team_manager.py的现有wire_host_dispatch adapters与通用异步peer准入middleware（manager组合注入） | 逐轮可信上下文及 hook 之外必需插件检查 | 共享身份/普通会话、resume、停用、省略plugin_context |
| C2 | api/routers/chat/models.py、turn.py、ws.py、routes.py；后续serialize/history与history/projection.py | 认证入站、通用plugin_context和证据引用透传、终态接缝 | session_key所有权、入队/steer/HITL、历史/取消/迟到/原生知识库兼容 |
| C2 | dashboard 插件 host/types/loader/registry、plugins/surfaces；计划指定容器 | 两类固定 surface、通用上下文、清理与发送约束 | 旧renderer、卸载、键盘、tsc/build；回退恢复容器入口 |
| C2 | 通用 errors/i18n en/zh 与 dashboard apiErrors | 仅 Host 通用错误码 | key parity与用户可读错误 |
| C1 | SDK ctx.tool/ctx.middleware、renderer/sidepanel、host.request、Fernet、JWT | 复用而非替换 | 原有测试和回归 |
| C1 | tests/unit/db/{test_agent_profile_columns,test_clip_thread_title,test_skill_package_icons,test_skill_packages_repo,test_published_experts_repo,test_repo_knowledge}.py；backup/test_system_archive.py；desktop/test_capture_display.py | 022后当前水位断言与DISPLAY默认前提隔离；仅测试，不改历史fixture或生产语义 | 首轮make all的10项失败逐项定向复验；最终集成再全仓验证 |
| C1 | tests/unit/api/test_chat_hitl_resume.py两个resume stubs | 新on_admitted协议的既有fake适配，保留状态断言 | 准入后错误/后续HITL及准入前撤权状态回归 |
| C0 | WeKnora/搜索/授权/阅读/业务UI | 全部在两个插件，不能进入核心 | 插件ZIP独立升级及边界审查 |

本地初次实施先开放 P03a 存储行；其它 C2 依任务 brief逐项实施，不把清单当作任意改动授权。

P07 consumer integration refinement（2026-10-25，通用接缝已本地实现/独立审查PASS）：既有 manager/Host/server/plugins router 行支持进程共享 SDK Registry 的通用变更协调；短状态 epoch、所有相关 Host 失效、跨 loop drain 完成后才变更 SDK/目录，保留真实 COMMIT lease。agents/manager.py 核实实际 AgentEntry/context/图中 required 扩展挂载，不能仅以全局已加载代替。coordinator、mount、只读能力子任务均已审查；业务hook仍在实施，不修改 SDK 源码，不承诺多 app 独立插件安装隔离，3.6 仍未完成。

P04a I2 修复扩展（已本地实现/独立审查PASS）：Host.stage_credential 与 plugin_credentials repo.stage 增加可选预分配 opaque UUIDv4 ID，以便插件先持久恢复定位再暂存；复用现有表和加密/lease，无厂商字段。仅 host.py、repo插件凭据 stage 及对应定向测试纳入该通用变更；不允许插件访问SQL。P04a Fix2联合51测试通过，PG实库不据此计PASS。

P07b2 Fix1追加通用白名单：`infra/agents/conversation_mode.py`仅将SDK原生metadata工具`tool_search`纳入canonical只读名单；`infra/agents/manager.py`在现有plugin/MCP四处工具名sanitization保留该名称，不能让外来同名工具继承能力。真实Ask/Plan编译图证实发现接缝需要此项；仍保留mutating工具执行限制，无业务名称/SDK修改。P07b2 Fix1已本地实现并独立审查PASS；P07c业务消费仍待实施。


P07c2a输入前提已实施的通用白名单：`infra/gateway/process/message_keys.py`仅定义通用USER_INPUT_KEY，`processor.py`现有Dashboard请求构造从InboundMessage.text服务端创建版本化Human元数据。复用现有Human kwargs载体，不改harness_request/SDK、Host facade/ticket、DB或业务路由；不让显示/附件内容替代当前问题。真实processor的6项测试与独立审查通过，业务hook消费者另由P07c2实现。

P08 UI就绪前提（P08a本地实现/独立审查PASS，完整前端联合验收仍待完成）：既有host_contracts.py/host.py及plugin_actions.py行允许增加通用只读requirements响应，返回当前用户可访问Agent/本人thread的持久required_plugins及unavailable IDs；缺失插件不丢失声明，不放宽管理/写权限，末端执行门禁不变。普通共享员工不能被owner-only设置路由阻断。Dashboard builtin shell负责缺UI模块时的可见blocker与30秒可见会话刷新、focus/lifecycle/提交前刷新，不嵌企业ID、来源探测或业务表单。精确实现/测试白名单另随任务冻结；本条不代表功能已验收。

Gateway兼容gate2测试白名单（本地实现/独立审查PASS）：`tests/unit/gateway/test_{attachment_hints,dashboard_ws,processor_resource_errors,processor_trajectory_observe,versioned_history}.py`。一次gateway gate实测21失败/494通过/2跳过；九处普通fixture明确`_plugin_host=None`，原21失败节点及完整五文件89测试通过，SpecPASS/QualityApproved。业务断言和生产门禁未改；最终make all另行验证。

邮箱/私有库增量 I02a（已审设计、接口冻结，尚未实施）：新增通用 `infra/users/registration_policy.py`，接入 `infra/users/{manager,invites}.py`、`infra/auth/ldap/service.py`、`cli/support/offline_ops.py`、`cli/commands/init.py` 的首次创建；`api/routers/{auth,settings}.py` 与 `api/deps.py` 仅 typed 通用策略读取/严格管理员保存及精确 JWT 豁免；`infra/errors.py`、两后端 locale 与两前端 apiErrors 只增加对应通用错误。数据库唯一索引不变，InviteRepo 原子兑换不拆分，旧绑定身份登录保留，无供应商字段/网络/后台开通进入核心。精确20路径含相关测试已在 task-I02a-email-policy-brief.md 冻结并捕获基线，等待当前功能 writer 冻结后派发；其它新增身份/worker/凭据能力仍为合同草案，不能凭本条实施。

I02b 身份/短期凭据清理（合同冻结，未实施）：仅 `infra/agents/plugins/{host_contracts,host}.py`、`infra/db/repos/{users,plugin_credentials}.py` 增加 setup-only 身份消费者、窄分页/当前值指纹和同事务 identity_checks/active credential_deletions。UserRepo 仅选择 id/email/disabled，PG FOR SHARE 与 SQLite BEGIN IMMEDIATE 提交前提，既有无检查提交保持兼容；无用户 schema、网络、业务身份字段或浏览器能力。精确九路径含测试与基线见 task-I02b-identity-brief.md，待当前任务及基础审查修复完成后派发。

I02c 后台生命周期/注册通知（本地实现与独立复审通过）：仅 `infra/agents/plugins/host.py`、`infra/server.py`、`infra/users/manager.py`、`infra/auth/ldap/service.py` 提供 setup-only worker、Host owner-loop Event/start/wake/task tracking、真实协调器取消drain与每server注入的成功建号通知。既有 InviteService 原子事务不改、SharedServices 不变；通知不是可靠队列，插件后续扫描修复遗漏。精确十二路径含测试在 task-I02c-worker-brief.md（补充两条真实 SDK callback fixture）；原基线保留，94 项主定向及相关 API/旧夹具、两轮取消证据修复复审通过。无供应商字段/网络/业务调度进入核心，不凭此改写 PluginManager。

I02a Fix1 测试白名单追加：`tests/integration/test_postgresql_registration_policy.py`，针对独立服务/事务预查后最终邮箱唯一约束的真实竞争及失败邀请不消费。审查已确认 SQLite 同 Manager 锁测试不证明此路径，故新增 gated PG 用例；只能使用 root 新建核对的任务临时库，不重置 schema，不改 Repo/DDL。其余改动限原 I02a20路径，精确21路径基线已在修改前捕获。

I02d 通用保留记录（本地实现与独立复审通过）：新增 `infra/db/repos/plugin_retained_records.py`，仅改 `infra/db/services.py` 与 `infra/agents/plugins/{host_contracts,host}.py`，在内部 settings 版本化命名空间提供隔离/分页/CAS/check 与现有 commit 原子整合。普通 records/credentials owner 级联保持，保留记录无 owner FK、无 secret、无 delete/purge/TTL；不改 022/schema、SettingsRepo/AuditRepo/UserRepo 删除、API/UI。含 retained 的 mixed 提交必须在 identity_checks 覆盖全部普通 write/activation/deletion owner，缺失及非空 requirements 在任何写前 invalid；纯 retained 无身份前提，旧 no-retained 不变。PG 全部覆盖身份行锁在前，plugin_records/settings 表锁顺序固定，SQLite immediate；取消/世代沿用实际 lease。第八节 user-provisioning-contract.md 为完整接口，实际八路径含测试白名单、原始及 Fix1 基线均保留；Fix1 真实 SQLite 43 项/PG 4 项及静态通过，三个 Important 关闭，独立 Spec/Quality 验收 task2.4。M1 requirements 错误优先级与原 retrospective RED 过程偏差留最终审查，非全仓/远端验收。

I02e 当前 actor 权限复核（本地实现与独立复审通过）：仅 `infra/agents/plugins/host.py` 增通用 facade.reauthorize_actor，复用现有持久授权 helper、DI/executor/真实 generation lease 与安全错误；新 `tests/unit/plugins/test_host_reauthorization.py` 测 DB 当前角色/disabled/范围、严格输入、故障与世代失效。无新 SQL/DTO/schema/HTTP、不改全局 invoke_action 或现有 action 语义、不加供应商字段。可信请求 actor 来自实际 Host 入口，不能从 payload/配置 owner 拼造。完整合同第九节；源基线在 I02c 接受后捕获，25 项定向与静态及独立 Spec/Quality 审查通过，已在 I02d 前验收；实际业务调用仍由后续3.1实现。

I02c 测试白名单补充：`tests/unit/plugins/test_host_contracts.py` 与 `test_host_lifecycle.py` 仅将两条实际失败的 callback fixture 改用真实 SDK PluginContext，保留原 callback/并发世代断言及 plain 无 callback 兼容。root 核对 before-I02b/before-I02c 两基线已有实际类型校验，非 C 新增；两路径 PRE-EDIT 快照已追加至原十路径内容不变的十二路径审查基线。不修改生产身份校验、不跳过或弱化测试。

P08b2 固定挂载/就绪刷新（本地实现与独立复审通过）：Dashboard仅增加Connectors/expert固定settings容器、Chat通用composer容器/独立就绪提示、泛型requirements客户端、共享inventory顺序与同窗口事件。使用实际Agent和真实thread、服务器时区与Host React，不导入企业业务组件、不请求业务bootstrap/供应商。Fix2后无未关闭Important；精确baseline/差异及原始29、Fix1 32、Fix2 6项测试证据在计划scratch同名brief/report/review中。UI1.2仍待C发送/auth/queue联合验收。

P08c 发送接线（合同独立审查PASS，Luna实施中，尚未验收）：Core只允许同计划task-P08c-native-send-brief.md列出的36路径（含原34路径及同主体刷新相关settings surface/测试），原基线保留且派发前fresh compare通过。另3个插件入口/测试/产物路径只修mounted Host转发，留在插件侧，不属于Core白名单；该入口子切片Fix1已独立验收。新增composerPreparation/authSession仅做瞬态挂载登记/不可变选择捕获/真实线程后准备/按项队列与显式认证失效；业务字段和动作留插件。继续使用既有WS plugin_context/follow_up，不新增HTTP新轮端点、不改HITL/逐steer/后端权限或SDK，不建持久作业框架。上述声明不是功能完成证据。

P08c Fix1 通用白名单补充（2026-10-25，实施中、未验收）：原36个 Core路径追加 dashboard/src/utils/locale.ts 与 locale.test.ts，合计38个 Core路径，另有已接受的3个插件 entry路径。仅修复 AuthGuard 延迟语言应用的所有权：locale helper 接受通用 caller isCurrent 与最新应用序号，不导入 authSession/API；AuthGuard 传验证租约。所有41路径首次编辑前基线已保留。全部 bound 新轮使用既有队列恢复项，不新增持久队列/协议或企业业务到 Core。初审15项仍须原 Luna 修复并独立复审，不能按白名单条目计完成。

P09a 通用工具侧栏（独立Spec/Builder规划PASS，尚未实施）：精确合同见tool-dock-ui-contract.md。仅允许以下11个Core源码路径：dashboard/src/plugins/toolRenderers/{types.ts,host.ts}；dashboard/src/pages/Chat/{ChatToolDockContext.tsx,index.tsx,chatMessages.partial.less}；hooks/{useChatDockPanel.ts,chatStore.ts}；components/{MessageBubble.tsx,ChatDockToolUiContent.tsx,ChatDockPanel.tsx,ChatDockPanels.tsx}（hooks/components相对Chat）。对应7测试：toolRenderers/host.test.ts；Chat/hooks/{useChatDockPanel.test.ts,chatStore.pluginToolPatch.test.ts}；Chat/components/{ChatDockPanels.keepAlive.test.tsx,ChatDockPanel.addTab.test.tsx,ToolDetailsInline.pluginDock.test.tsx}；Chat/ChatToolDockContext.test.tsx。合计18路径，实际PRE-EDIT基线及READY brief后才派Luna，不以本条授权同时开第二writer。

职责仅当前工具Host/opaque临时tab state、真实Agent/thread/auth作用域、一次性activation/隐藏卸载、同call替换及关闭焦点；chatStore仅新增可选精确session patch并禁止该路径fallback，旧两参行为保持。setup/global插件Host的无来源dock副作用须迁移到mounted props.host，旧renderer props/内建视觉兼容，不能伪造owner或扩大server授权。Core不解释企业证据字段、不调用业务actions、不加入正文/续读/原文逻辑，不新增API/schema/注册表/持久缓存。P09b企业卡片和阅读器全部在插件，UI3.3/4.3及原文仍待完整验收。

P08c当前状态补充：前述实施中为历史记录；实际第三轮Fix3独立Spec/Quality PASS，原15项与后续R1–R5/N1关闭，UI1.2已验收、前端生产构建成功。全仓make all/完整UI与远端门禁仍待完成。


P09c1 通用工具展示上下文修正（独立Spec/Builder规划PASS，尚未实施）：只改dashboard/src/plugins/toolRenderers/host.ts、dashboard/src/pages/Chat/ChatToolDockContext.tsx、dashboard/src/pages/Chat/components/MessageBubble.tsx，新增components/ToolDetailsInline.presentationContext.test.tsx（components相对Chat）。内部可选getter仅更新locale/theme；捕获agent/thread/auth/registry/lease不变，Host引用、reader状态和continuation不因展示更新而重建。精确合同tool-presentation-context-contract.md；四路径编辑前基线已捕获，当前P09b唯一功能作者完成冻结后再派发。
