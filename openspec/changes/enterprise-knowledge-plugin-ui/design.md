# Design

## Context

既有 loader 支持自包含 ESM 和 renderer/sidepanel；Host 无 settings/composer surfaces。现有 Chat 原生知识库和 ask/plan/craft 有独立语义。业务页在插件，宿主只放容器。

## Goals / Non-Goals

**Goals:** 先确定入口、状态和职责；管理员、专家配置者、员工权限分明；知识与联网过程明确可见。

**Non-Goals:** 新顶级导航、任意插件路由、像素级重绘整个 Dashboard、核心导入企业组件。

## Decisions

1. 两个固定 settings 容器：现有连接器管理区域承载管理员连接/员工授权/搜索配置；专家配置页承载连接绑定、专家允许/默认范围与联网许可。业务设置组件同一插件模块，根据后端返回能力显示对应区域。
2. Plugin UI Host增加 registerSurface(settings|composer)、注销与 get context；可写发送能力通过仅限当前 composer 挂载的 registerComposerPreparation 登记，替代尚未实现的 setTurnContext 占位。登记提供同步不可变选择捕获、真实线程后的异步准备及显式回收恢复；Host固定所属插件命名空间，settings/setup/旧挂载无此能力。React由宿主注入；构建为单入口ESM，blob导入不得残留相对chunk依赖。
3. 管理区采用“字段表单 → 保存 → 分项探测状态”结构；凭据输入write-only，已有密钥显示已配置，提交成功清空。revision冲突保留安全编辑内容并提供重新加载/放弃；不给自动覆盖按钮。
4. UI读取新增插件 settings.get/binding.get/session.get 等sanitized bootstrap动作，返回实际可操作能力；新增的读动作必须与业务actions文档同步。管理动作只供HTTP，不暴露LLM工具。
5. composer：专家/线程切换生成context generation token，丢弃迟到加载响应；默认范围只初始化一次，由服务端裁剪当前授权。active clear持久为空，web-only不发知识ready/probe/catalog请求。
6. 公开词确认使用对话框：原样展示词，可确认/取消，Esc取消、Tab trap、返回触发焦点。编辑词/模式/选库等在服务端撤销旧approval。发送pending时按钮和快捷键均禁止重复提交。
7. 引用使用已有registerRenderer/openSidePanel，结果UI只是表现；后台真实证据是事实源。原文走opaque resource endpoint；不可用保持明确禁用并有原因。
8. 插件en/zh从同一业务bundle构建；宿主generic文案与apiErrors按仓库约束。所有日期从Host给出的服务器时区格式化。

### Layout references

```text
既有设置页面
  [企业知识插件设置]
  连接名称 / 地址 / 凭据输入        [保存]
  企业范围 / 员工授权 / 专家绑定
  LIST 状态 | SEARCH 状态 | READ 状态 [分别探测]
  主搜索源 [配置/测试]  备用源 [配置/测试]

既有聊天页面
  消息与引用卡片                           [检索文本侧栏]
  [企业库多选] [仅知识 | 仅联网 | 混合]     来源/截断/页码状态
  普通 ask/plan/craft                       正文 / 加载更多
  输入框 [发送] → 联网时原样词确认          原文入口/不可用说明
```

## Page Contracts

### Role and action matrix

| 用户角色 | 连接/员工授权/搜索管理 | 专家绑定与默认/联网许可 | 员工聊天与证据 |
|---|---|---|---|
| admin | 可以；服务端admin校验 | 可以；校验目标专家存在 | 按当前会话权限与真实员工上下文 |
| 非admin配置者 | 隐藏，不请求这些管理数据 | 需要agent_plugins权限且拥有目标专家；否则拒绝 | 按员工授权，不以专家所有权扩大知识权限 |
| 普通员工 | 隐藏，零管理bootstrap请求 | 隐藏，禁止写入 | 可访问专家/本人线程且有grant；联网另需search权限 |

### Page action contract

统一envelope携带api_version=1和Host验证的agent/thread context；用户身份不在payload。管理上下文由后端实际权限决定，不能按前端role开关返回数据。

| 页面事件 | action/resource | 最小输入 | 关键响应与UI更新 |
|---|---|---|---|
| 管理设置打开 | settings.get | surface、可选agent context | capabilities、脱敏连接/探测摘要；只返回可管理分区 |
| 连接保存 | connection.save | connection_id、expected_revision、地址/范围、可选write-only Key | revision/status；清Key并使旧probe失效 |
| 列库选择 | connection.catalog | connection_id、配置revision、actor上下文 | 过滤过的ID/名称；拒绝旧revision和越权metadata |
| 分项探测 | connection.probe | connection_id、revision、LIST/SEARCH/READ | observed_revision、分项status/time；迟到不覆盖 |
| 员工授权保存 | grant.save | connection_id、目标员工、allowed IDs、expected_revision | grant revision；不自动扩大已存选择 |
| 专家设置打开 | binding.get | agent context | binding/default/web_allowed/revision；owner校验 |
| 专家绑定保存 | binding.save | connection_ref、expert_allowed/default IDs、web_allowed、expected_revision | 新revision；无效默认拒绝 |
| 搜索设置打开/保存 | search.get/search.save | 管理上下文；主备及expected_revision、可选write-only Keys | 脱敏配置/revision；保存后探测待确认 |
| 主备检测 | search.test | 当前search revision、source角色 | observed_revision、source status；不得改session mode |
| 进入/切换会话 | session.get | agent/thread | selection_present、selected IDs、mode、revision、source revisions、可用能力 |
| 初始化/更改会话 | session.save | selected IDs/mode、expected_revision、必需source_revisions对象（connection_id可null、config_revision/binding_revision/grant_revision为显式非负整数，缺失来源为0） | 保存state/newrevision；旧来源snapshot拒绝，rev0竞态失败reload胜者 |
| 准备一次提问 | operation.prepare | payload为version=1、必需expected_session_revision；当前agent/thread由Host上下文携带 | OperationOutput.operation含服务端UUID；本地校验零来源HTTP，冲突reload后显式重试 |
| 联网词确认 | query.approve | 原样public_query、operation、会话/配置revision | approval_id及绑定；取消不调用，变更作废 |
| 引用/历史读取 | evidence.list | payload为version=1、operation_id；当前agent/thread由Host上下文携带 | EvidenceListOutput含已鉴权operation/evidence；使用当前或保留的历史operation引用 |
| 阅读/续读 | evidence.read | 来源operation_id、evidence_id、可选continuation | content/truncated/continuation；失败不追加丢序 |
| 打开原文 | evidence.original → Host resource | 来源operation_id、evidence_id、当前context | availability及仅在可用时返回的opaque resource token；auth资源代理 |

session.get的selection_present由持久记录是否存在决定，不以数组长度判断。无记录时返回当前授权快照裁剪的默认与revision=0；session.save同时校验snapshot revisions。context generation变化后的初始化/加载结果全部丢弃。已有record即使为空也不初始化；权限缩小时标记不可用并要求显式调整，不增加范围。

session.get现有平面connection_id/config_revision/binding_revision/grant_revision字段提供来源快照；客户端仅把这些字段映射到session.save.source_revisions。服务端先比较调用方快照与当前来源，再将期望revision送入同一保存事务的checks。遗漏或非法snapshot拒绝，不能以服务端新读值替代旧页面的期望值。

operation.prepare使用当前session.get/save结果的revision，包括隐式revision=0。payload不含question、actor、scope或客户端operation_id；返回的operation_id只作为该发送项的plugin_context关联，授权由服务端重新判断。prepare不触发列库、探测、检索或阅读；后续实际检索才记录凭据可见范围。联网批准仍由P12实现，不因本契约表存在而视为已实现。

点击或快捷键提交时复制本次text与attachments快照；异步原生建thread、初始化session和prepare均属于该发送项。若同一context generation内用户继续编辑草稿，旧项成功只能清除仍对应旧快照的草稿，不能擦除新内容；失败也不能把旧快照恢复到新草稿之上。context切换继续遵循generation丢弃规则，排队、回收、编辑重发不得借用另一项的operation或approval。

session.capabilities为只读派生对象：can_save_selection、can_select_knowledge、knowledge_ready、web_ready。本地允许集合非空只支持选择，不代表上游READY；未验证adapter/probe时后两项false。空范围或缺失来源仍允许按真实snapshot显式保存[]，隔离状态则禁止新保存。UI不得把能力当授权，提交仍由服务端逐次校验；读取能力不得触发WeKnora探测。

目录/探测输入统一使用connection_id与expected_revision。管理员目录显示凭据可见库以配置初始空范围；员工目录只显示四层授权交集，不能调用管理探测。connection.probe每次提交一个component（LIST/SEARCH/READ）；SEARCH需query与非空knowledge_base_ids，READ需knowledge_id与非空knowledge_base_ids。缺输入展示BLOCKED，未验证或过期分项不得合成READY。管理员可测试saved-disabled连接，整体仍显示DISABLED；启用保存更新revision后重新探测。响应分项提供observed_revision、checked_at、status和固定error_code，UI按服务器时区展示时间并丢弃旧revision/旧context响应。query不作为探测状态持久保存。

阅读cursor固定在首次读取族创建后30分钟过期，续读不会刷新有效期；过期或来源改变时停止追加，提供明确重新开始入口。网络/预算失败不推进位置，重试同一continuation前仍需后端鉴权与摘要检查。服务端无法在共同预算内验证完整块集合时展示失败，不把失败内容追加为成功半页。当前目标部署原文入口未验证，显示原文不可用且不发resource请求；检索文本仍可按权限阅读。

实时工具结果可能先于 operation 终态到达。evidence.list 返回身份、scope、revision 合法但状态仍 VALIDATING/RETRIEVING/GENERATING 时，插件只显示现有本地化加载态，不把部分 evidence 当可信卡片，也不显示通用失败。仅重取本地鉴权 action，采用 250ms 起、最多2秒间隔、30秒自动等待上限；上限后保持明确待完成状态并提供手动刷新。完成态才校验并展示证据；403及DENIED/FAILED/CANCELLED/INTERRUPTED终态、context或工具身份变化、隐藏卸载均停止并清理定时请求；未知状态作为非法响应，不轮询。不得通过放宽服务端 operation 状态或证据权限校验解决 UI 时序问题。

## Risks / Trade-offs

- 挂载点缺失 → UI实现前完成固定Host契约，不复制企业表单到核心页面。
- stale上下文/授权漂移 → generation+revision校验，后端逐次授权；失效时提示而非静默改模式。
- 纯文本回退掩盖缺UI → UI验收单独运行，回退只保障可读性。
- 两个OpenSpec变更共享actions → 后端动作变动先同步这里的交互合同和场景，再实现。

## Migration Plan

先settingssurface+管理表单，再知识composer/阅读，最后搜索设置/模式确认。停用插件清除注册控件并阻止企业会话继续执行，保留历史和服务端记录。

### Generic shell readiness

The shell uses GET /plugins/requirements with current Agent and optional real thread, returning api_version=1, required_plugins and unavailable_plugins. It is distinct from business action envelopes and owner-only plugin settings. The response contains only merged names/versions and unavailable IDs; it does not reveal declaration provenance or confer permission. Null thread reads Agent scope only, then first send obtains its native thread and refreshes that scope before business preparation.

A builtin generic status remains renderable after plugin unload. Reconcile UI modules with the fresh global installed/enabled list on same-window lifecycle events, focus/visibility and before submission. Visible chat refreshes once per 30 seconds; hidden chat pauses that timer and refreshes on return. No push infrastructure or configurable interval is added. Pending/error/unavailable state cannot silently become an empty requirement set. Use opaque context generations to discard late responses and clear stale turn state; final backend admission still handles changes after the read. This is generic shell behavior, with no enterprise IDs or business/source probes in Dashboard.

### Native thread and turn preparation

首次企业发送由宿主沿用原生建会话流程，取得真实thread后再按当前generation加载/初始化插件session与准备operation；loading/pending阻止按钮及快捷键重复提交。准备失败保留文本/附件，不用null/pending thread伪造会话，也不让插件组件直接操作原生路由。已经有真实thread的每次提交/入队复制独立plugin_context；排队、flush/reclaim和异步回调使用该项自己的快照，再交服务端验证。消息编辑需要新operation和必要的新确认，历史展示metadata不能代替执行输入。

企业忙时追加内容进入独立下一轮，沿用原生内存队列并保持该消息快照；本阶段不建立逐steer的批准/operation协议。后端对受required extension约束而不具备逐消息上下文合同的忙时steer拒绝，普通无required会话保留既有steer行为；follow_up与忙时竞态拒绝保留项目供稍后重试。HITL HTTP DTO不新增客户端plugin_context，继续由服务端原continuation恢复并重新授权。明确logout/401和用户主体变更清除旧可写context，不把内部身份标记暴露为插件授权。


### Native preparation implementation contract (P08c)

完整 builder 合同见同计划 scratch 的 task-P08c-native-send-brief.md（独立审查前为 PREPARED，不是已实现）。仅细化既有 first-send、draft、queue、auth 约束，不新增业务权限或来源字段。

- registerComposerPreparation 登记 captureSelection/prepare/restoreSelection；同步捕获 selected（含显式空数组）或 uninitialized，JSON验证后深复制冻结，真实 Agent/thread 确定后准备。最终 plugin_context 由Host按登记owner组装，公开查询词不经原生输入 trim/materialize。
- 有插件准备的首发在准备期间保留原挂载和路由；同上下文的新草稿仍可编辑。本次旧快照成功只能清仍属于原revision的草稿；有新版草稿时不自动导航、不覆盖。切上下文取消未接受任务；原生新线程可供同一origin显式重试复用，不自动删除。
- pending submission 阻止重复点击/快捷键，typing 保持可用；队列每项持有独立瞬态快照，flush 只重查该项实际目标，不借用当前选择。in-flight head仍占容量，拒绝/不确定结果保留为显式恢复，不自动重放，回收/编辑需要新准备。
- 实际前端新轮走既有 WS user_turn 的 plugin_context 和 follow_up；不增加 HTTP 新轮端点或逐steer批准协议，HITL HTTP 使用原 continuation。turn.started 不是插件准入证明；通用错误可能发生在部分执行后，不宣称失败就一定未执行。
- 显式token清除/替换与主体变化同步撤销旧挂载/待发送/队列能力；同主体滑动续期不清草稿。旧401或续期结果不得修改新的登录状态，主体仅用于内部失效，不作为插件授权字段。

### Existing tool dock implementation contract (P09a/P09b)

先交付通用 P09a 桥接，再交付插件 P09b 证据卡片/阅读器。精确且已独立规划审查的合同见 `prd/evidence/enterprise-p1/tool-dock-ui-contract.md`；规划通过不代表源码实现或 UI3.3/4.3 完成。

- 复用现有 toolUi tab，仅搬运插件定义的 JSON-safe 临时 state；核心不解释 operation/evidence/KB ID，不保存正文、cursor 或授权结果到 tab/history/localStorage。同 call 换 state 创建新 activation 并重挂 reader，新 Host 接手，旧 Host 永久失效。
- ToolDetailsInline 注入绑定真实会话 Agent/thread、当前 renderer、call 与认证生命周期的 props.host；团队 speakerAgentId 和全局 contextOverride 不作 API 目标。非活动工具 reader 卸载，重新激活重新鉴权读取；其它 dock 页保持现有 keepalive。隐藏/切换/替换事件在同一调用栈先撤销旧 presentation binding，再提交 React 更新，不能等待 cleanup 才失效。
- 旧 renderer props、内建视觉及无 state 通用打开按钮保持兼容；setup/surface/global 插件 Host 无工具 binding，不能打开或关闭有操作能力的工具 tab，旧 setup Host dock 调用须改用挂载 props.host。插件不能自行提供内部 owner/scope/焦点 callback。
- scoped patch 只更新捕获的非空 thread/call，目标不存在即失败，不回退搜索别的线程。关闭当前工具 tab/整个 dock/当前 Host close 后，焦点回来源工具区域；非活动 tab 关闭、切 tab、路由或认证失效不抢焦点。稳定父壳独立于 renderer/placeholder，按钮定位 ref 保持原职责。
- 证据列表、已登记 ID、正文/续读、错误与来源变化、服务器时区及原文状态全部由 P09b 插件组件实现。可用原文仍需真实 opaque resource 合同；当前不可用不拼造 provider URL，也不据此完成原文验收。

P09b 已独立规划审查的精确合同见 `prd/evidence/enterprise-p1/evidence-reader-ui-contract.md`，只允许插件13个源码/测试/构建产物路径。实时及历史引用均先用当前会话 context 调 evidence.list，只有其已登记 ID 能成为卡片；opaque dock state 不能替换原 tool operation，所有请求使用挂载 props.host，时间只按有效服务器时区显示。

reader 同时最多一个 read 在途，并在 await/React提交前同步占位；旧 finally 不能释放新请求。仅网络/预算等可恢复失败保留同一 continuation；READ_CURSOR_INVALID/READ_SOURCE_CHANGED（含原文动作）终止续读、清游标并供显式 fresh list/read 重启，拒绝授权则清当前授权数据。8000字符按 Unicode codepoint 校验，实际 DTO 关联/必需字段与原文固定不可用闭集校验，不把 undefined 相等当有效关联。上述规划不替代 P09a 实际源码验收、完整浏览器链路或真实原文/WEB/私有源门禁。


### Live Tool Host presentation metadata (P09c1)

Existing live locale/theme behavior is refined by the independently reviewed `prd/evidence/enterprise-p1/tool-presentation-context-contract.md`: an internal optional getter supplies only current locale/theme while captured agent/thread/auth/registration/one-use authority remains fixed. A reactive ThemeProvider and current language notify mounted memo renderers through an ordinary presentation prop update; Host/activation/reader/continuation do not reset. Exact Core delta is three source paths and one integration test, frozen separately. This planning acceptance does not complete UI4.1 or actual browser verification.

HITL输入框答复保留findPendingAsk取得的服务端pending_id，并通过通用路由显式映射至现有resume hook的pendingId参数。已展示旧pause不得隐式改答最新pause；不加客户端plugin_context，服务端仍从原continuation恢复并重新授权。无ID的legacy调用保持兼容，过期ID由既有后端拒绝。
### 2026-10-27 empty Agent regression refinement

ChatComposerTools gates the readiness notice and composer PluginSurface on the resolved Agent ID. An absent target is not an in-flight requirements request. The readiness hook keeps its existing non-ready semantics, and the native child/input remains mounted so selecting an Agent preserves drafts. This is a generic Host presentation rule with no enterprise IDs. A selected Agent still displays pending/error/unavailable readiness normally. No Agent/provider is created automatically.

### 2026-10-27 catalog feedback regression refinement

The plugin Settings catalog controls display request feedback adjacent to the initiating control, separately from the selected allowed-set validation. Loading, loaded count, empty credential-visible catalog, and sanitized retryable failure are distinct states for connection and expert binding targets. Request state is bound to target connection/revision and semantic generation; equivalent presentation renders preserve it while actual target/revision changes invalidate it. No new backend endpoint, scope expansion, automatic save, or WeKnora writes are introduced.

Browser acceptance exposed an additional deployment obstacle: the same-version plugin entry was served from disk cache after overwrite (145188 old bytes versus 149758 current bytes), including after Page.reload(ignoreCache=true). The generic Host fetch for an installed UI entry therefore uses cache:no-store. This affects only executable plugin UI text fetches and retains authentication, inventory lifecycle, and module isolation. No cache-clearing or permanent browser setting is required.

### Connector tab presentation isolation

The fixed generic settings slot belongs to the connected tab. Keep the settings subtree mounted inside a native hidden wrapper while another tab is selected, matching ordinary retained tab-panel behavior and preserving unsaved drafts. The slot's contents and business behavior remain plugin-owned. The existing tab selection/query handling and Composio implementation are preserved; this correction does not migrate knowledge settings or change backend selection.
