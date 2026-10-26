# Octop 已写 UI 的真实安装验收：功能实现与执行计划

日期：2026-10-26。代码起点：`10fe84d2`，分支 `codex/enterprise-p1`。唯一开发目录：`E:\git\octop`。

## 1. 本轮目标与规格

完成现有共享知识/搜索插件的真实目录安装、正常登录及浏览器验收；补齐实际接线和失败状态，不继续开发私库、远端部署或发布。功能/测试代码由 GPT-6 Luna subagent 实施，控制器负责环境、浏览器与独立 review。用户已授权本计划编写后继续执行，不在批次间重复请求启动确认。

采用现有 OpenSpec `enterprise-knowledge-plugin-ui`（spec-driven，本轮开始时 5/13），关联主变更的知识/受控搜索规格。CLI 已确认项目根 E 盘、规划齐备、apply ready。沿用已审页面行为；新增产品行为先修规格再实现。原任务只有全部条件满足才勾选，实机源和私库不因本轮通过而计 PASS。

本地测试使用实际 OctopServer、数据库、认证、PluginManager、两个真实插件、SDK 图、Dashboard loader/surfaces 和 SSE；只替换 WeKnora/搜索的 HTTP 上游与最终 LLM。HTTP mock 不等于真实供应商验收；不得写入伪造 evidence、跳过 Host、注入浏览器 token 或关闭权限检查来通过页面。

## 2. 已发现的接线风险

1. `Composer.refresh` 在保存模式为 WEB_ONLY 时仍可能加载知识目录；需证明进入 WEB_ONLY 的页面与发送路径均零知识 HTTP。
2. 无原生 thread 时 composer 捕获 uninitialized，prepare 当前直接拒绝；须按宿主已解析的 target 读取/初始化真实会话再准备，避免首条消息永远无法发送。
3. 初始默认、显式空选择、context generation、批准弹窗与发送快照必须覆盖真实调用；不能只测持有预建 thread 的组件。
4. 旧本地页 5188 显示较早设置区，不能据此验收新包。新实例核对安装后的 plugin.yaml/UI manifest/bundle SHA-256，并使用隔离 home。

## 3. 批次和代码边界

### B01 可复现的真实安装验收运行器

**作者：** GPT-6 Luna。**允许路径：** 新建 `tests/support/enterprise_ui_runtime.py`、`tests/unit/enterprise/test_ui_runtime.py`；确有必要的同名前缀 fixture/helper 最多另两文件。禁止修改生产认证、Host/Core、既有用户 home 或真实远端。

**实现描述：**

- 命令 `uv run python -m tests.support.enterprise_ui_runtime --home <全新目录> --port 5190`，仅绑定 127.0.0.1；拒绝复用非空 home，避免改现有实例。设置本进程 OCTOP_HOME，正常启动/关闭服务。
- 用实际 PluginManager 安装仓库中 enterprise-knowledge 与 enterprise-policy（真实 hook，不是 synthetic companion）。校验 UI entry/manifest 及源与安装副本哈希；缺 dist 明确失败，不悄悄退回文本工具。
- 在隔离数据中创建专用 admin/employee 和专家，通过正常管理能力配置连接、grant、binding、两搜索源；使用真实 probe action 得到 ready，不手填成功探测行。浏览器正常登录，测试凭据仅限该临时实例，不能取真实用户环境密钥。
- 两个知识库含不同可见范围，测试文档足够长，支持 chunks 连续阅读；固定公开搜索与可触发主源 503 的合成测试词。所有上游 httpx 客户端只用 MockTransport，意外请求失败，不出网。
- SDK 使用确定性模型，但工具调用/证据/权限由真实图产生；不造 ToolMessage/evidence。记录非敏感 provider/路径/次数/顺序、canary 是否出现，不记录 JWT、Key、完整私密 query 或正文。
- 输出本地 ready 元数据：端口、用户名、专家/连接 ID、安装摘要与状态文件路径。凭据如需文件仅写隔离 home，不提交。正常处理 SIGINT/停止并 drain 本轮任务。

**验收：** 新 home 可启动，已安装真实两插件且 bundle 一致；登录前仍需正常认证；知识/公开上游和 LLM 不出网；可观察三模式调用数与 fallback。

**验证：** 新 helper 的定向 pytest、限定 Ruff/format/mypy；运行器实际启动和健康检查。进程由控制器统一管理，作者不遗留后台服务。

### B02 Composer 首发、模式与快照修复

**作者：** 新 GPT-6 Luna。**依赖：** 本计划；可与 B01 并行，因为文件不共享。

**允许路径：** `plugins/enterprise-knowledge/ui/src/Composer.tsx`、`composerApi.ts`、`Composer.test.tsx`；必要的插件双语资源。其他生产路径先报告控制器，不能自行改核心 Host。

**实现描述：**

- 先用失败测试证明 WEB_ONLY 初始加载不应调用 connection.catalog，及无 thread 捕获 uninitialized 后真实 target 准备的行为。
- 选库目录仅在知识模式按授权加载；WEB_ONLY 不触发 catalog/probe/知识 ready HTTP，仍显示服务器模式与阻塞原因。
- 利用已有 `ComposerPreparationHandler.prepare(input.target)`。uninitialized 只允许在该宿主解析出的同专家真实 target 下读取 session；selection_present=false 才 CAS0 初始化服务器裁剪的默认值，CAS 冲突只重读胜者，不重填空选择。
- 对该 target 的动作 envelope 可在插件 API helper 增加明确 target 参数；不增加新的 Host runtime API，不从 history/display 元数据猜 scope，不接受 actor/question/自造 operation_id。
- 保留已捕获的 saved revision/selection/mode；有明确未保存编辑、上下文失效/abort 时阻止准备；任何异步响应不能覆盖新上下文或较新草稿。批准通过现有真实 action，取消/改词/切换不得复用旧批准。

**验收：** 首条 KB 消息能准备真实 operation；已保存空选择仍阻止知识发送；WEB_ONLY 初始/发送路径不发知识请求；两页 revision0 竞态、迟到结果、单次批准和取消有断言。测试通过不能代替本轮真实浏览器步骤。

**验证：** 覆盖以上边界的新增组件/API测试（保留 RED/GREEN）、插件 UI typecheck/test/build、限定格式检查。

### B03 真实浏览器验收与发现后的修复

**执行：** 控制器。**依赖：** B01、B02 作者自查与独立 review 通过，安装重新构建的最终 UI。

| 场景 | 操作与必须证据 |
|---|---|
| A 管理设置 | 正常登录 admin；保存配置→分别诊断两源；明确成功/失败、版本和 CA/Key 状态，旧结果不覆盖新保存 |
| B 首条知识问答 | employee 正常登录；从新聊天第一条消息开始，服务器创建真实 thread/operation，出现服务端登记证据卡片 |
| C 范围与阅读 | 选库/清空不回填；打开检索文本侧栏、续读、原文不可用原因；历史重开重新授权，无伪页码/URL |
| D 三模式 | KB_ONLY web=0；WEB_ONLY WeKnora=0；HYBRID 内部先行；原样公开词确认后才请求，取消为0，主源503时只备源一次 |
| E 交互隔离 | 快捷键/双击、编辑或队列项、切线程、刷新历史；异步期间新草稿保留，不错绑 operation |
| F 生命周期 | 正常管理操作停用/恢复配套插件，控件清理和阻塞；撤权后读/续读拒绝，不继续显示可信新结果 |
| G 展示 | en/zh、服务器时区、键盘焦点/确认对话框，控制台无未处理错误 |

浏览器工具没有 Chrome DevTools 连接器时，使用 CUA 的已公开浏览器 API，不安装替代依赖。浏览器只做正常 UI 操作；不读取登录 token、不用 evaluate 修改页面/发请求。遇到 CAPTCHA 时按当前工具规则在操作前取得该次确认；其他独立工作继续。不得绕过 CAPTCHA 或修改认证保护。

发现具体阻塞先形成精确复现、路径/失败断言，再派 GPT-6 Luna 修复一批，完成后独立 review 与对应浏览器回归。允许范围优先插件 UI/业务模块；通用 Host 缺口须先补 core-delta 并限定通用修复。

**证据产物：** `prd/evidence/enterprise-p1/ui-installed-acceptance.md`、截图与脱敏请求摘要；对缺能力或未跑场景明确记录，不以组件测试/截图替代真实行为。

### B04 Review 与最终门槛

- 每个作者完成后独立检查规格和代码质量；重点是 target/身份、零知识外呼、CAS、线程/草稿、可信证据和 fixture 不绕过生产控制。
- 所有本轮功能代码仍由 GPT-6 Luna 完成；控制器整合审查、记录与浏览器操作。
- 完成限定检查后执行仓库 `make all`，插件 Python 限定静态检查、插件 UI typecheck/test/build；涉及 Dashboard 改动再执行其 tsc/lint/test/build。记录真实退出码与跳过项；本轮不提交/推送/发布。
- 总结实际完成场景及未完成的远端/私库/发布范围。只有满足原 OpenSpec 整项全部条件才更新复选框，并再次用 CLI 确认进度。

## 4. 执行约束与已作选择

### B04a 已复现的 Dashboard 质量门槛修复

2026-10-26 执行 Dashboard lint 后发现两处既有 error：`Channels/components/constants.test.ts` 的未使用导入，以及 `Settings/Models/components/modals/ModelListEditor.tsx` 的条件中冗余 Boolean。由 GPT-6 Luna 删除该导入、直接使用原条件值；不改变条件语义、不清理其他警告。先运行限定 ESLint，再跑全量 lint、相关测试与 tsc；控制器独立 review 此小差异。

全量 Dashboard 测试如有失败，先按实际错误归类并判断是否运行环境或基线问题。只有确认根因、写明最小修复和断言后才扩展代码路径，不自动扩大为全 Dashboard 重构。

### B04b 已诊断的 Dashboard 测试修复

全量测试最终 7 文件失败、276 通过；9 用例失败、1544 通过。独立限定复现见本轮 workspace 的 `B04b-diagnosis.md`。GPT-6 Luna 仅修改下列测试，不修改业务实现或全局质量门槛：

- `DocumentPreviewCore.docxSanitize.test.ts`、`skillMarkdown.test.ts`、`SkillDrawer.test.ts`：在纯函数/元数据测试边界 mock 无关 PDF 预览依赖，避免 jsdom 加载需要 DOMMatrix 的 PDF.js；保留原行为断言。
- `knowledgeBases.test.ts`：补现有真实 `bridgeAgentHeaders` 契约，验证有/无 agent 的 header 行为。
- `publishedExperts.test.ts`：断言 refresh 请求携带调用者实际传入的 JSON body。
- `useChat.history.test.ts`：只给已复现约六秒冷动态导入的该测试文件配置 15 秒上限，保留所有历史/游标/失败断言，不更改全局 timeout。
- `AuthGuard.test.tsx`：显式等待认证 shell 出现再导航，断言导航前后壳仍挂载、认证状态请求仍只调用一次；不修改 AuthGuard。

验证：七文件限定测试、ESLint、tsc；独立 review mock 不屏蔽被测行为，之后控制器重新跑全量 Dashboard 测试并记录实际结果。

### B03a Windows 静态模块 MIME 修复

真实浏览器停在加载页，控制器对页面实际引用的 `/assets/index.Cjkl2BMC.js` 发起 HTTP 检查，返回 200 但 `Content-Type: text/plain; charset=utf-8`。`api/app.py` 的 `_dashboard_response` 直接依赖系统 MIME 推断，本机 Windows 映射不能保证浏览器可执行模块。该问题属于通用 Host 传输兼容性，记录在 core-delta 后实施。

GPT-6 Luna 仅修改 `src/octop/api/app.py`、`tests/unit/api/test_dashboard_cache.py`，必要时补 `tests/integration/test_dashboard_serve.py`：对实际静态 `.js` / `.mjs` / `.css` 明确返回合法 MIME，其他资源保留原推断；不修改注册表、认证、CSP、缓存、路径边界或 SPA fallback。先模拟错误系统 MIME 得到 RED，再验证 JS/MJS/CSS 类型、缓存及缺失 asset 404。独立 review 通过后重启隔离实例并重新加载真实页面。

### B04c 跨盘 Harness 测试配置修复

后端全量 5714 通过、237 跳过、7 失败。七项均发生在三个直接构造 Harness 的测试 helper：默认 backend root `/` 在本机解析为 E 盘根，而临时工作区在 C 盘，SkillsMiddleware 被正确的越根检查拒绝。生产 AgentManager 已按 Windows workspace 约束 root，测试遗漏了该配置。

GPT-6 Luna 仅改 `tests/unit/agents/test_plugin_mount_snapshot.py`、`tests/unit/agents/test_plugin_stream_text.py`、`tests/integration/test_enterprise_plugin_tools.py` 中对应图构造 helper，显式配置以该测试 workspace 为根的 local_shell backend（与生产语义一致），不改生产、路径防护、断言或 skip。复现结果使用首轮 make all 的七项失败；修复后限定三文件通过、Ruff/mypy 通过，并经独立 review。最后控制器重跑 `make all`。日志中独立 stub 关闭噪音不在此批扩展处理。

### B04d 插件已有格式差异收尾

补充全插件格式检查发现 Python `search_settings.py` 一文件和插件 UI 六文件的既有格式差异。仅由 GPT-6 Luna 运行 formatter 整理：UI 的 `api.test.ts`、`host.ts`、`PublicQueryDialog.tsx` / `.test.tsx`、`SearchSettings.tsx` / `.test.tsx`，以及 Python `enterprise_knowledge/search_settings.py`。Windows UI 检查显式采用 `--end-of-line auto` 以保留仓库的既有换行；不更改全局 formatter 配置。核对 Python AST 和 UI 编译产物等价，完整插件格式/Ruff 通过；重新 build 并比较最终 bundle 哈希。纯格式变更不额外重复测试，若输出不等价则停止并报告，不能夹带功能修复。

### B03b 工具早于操作终态的证据加载修复

浏览器实际首发完成，但卡片显示“无法加载证据”；手动重试后正确出现登记卡片，侧栏续读与原文不可用均可操作。当前 renderer 仅接受 COMPLETED/EMPTY，而真实工具可能在 GENERATING 阶段已返回。先独立审查上述 evidence spec/design 的补充，再由 GPT-6 Luna 核对根因并实现。

仅开放 `plugins/enterprise-knowledge/ui/src/EvidenceRenderer.tsx` / `.test.tsx`、必要插件 `i18n.ts`。复用现有鉴权 action 和 scope，严格区分已验证 pending operation 与非法响应：250ms起指数等待、2秒间隔上限、30秒自动等待上限；上限后明确 pending+手动刷新，完成才显示可信卡片。403、失败/取消、切scope/工具、隐藏卸载立即停；不修改后端权限/终态检查或Host。先用真实GENERATING响应→COMPLETED的组件失败测试复现，补等待上限/卸载/拒绝/迟到/WEB_ONLY无知识调用回归。完成插件全套typecheck/test/build/格式，独立 review 后正常重装并复跑浏览器首发，不以手动重试掩盖错误。

### B03c 等值上下文刷新与批准取消焦点

浏览器在知识模式停留数分钟，目录请求累计262次；切WEB_ONLY后请求数稳定。独立组件诊断证实整个context对象identity变化触发refresh：同值新对象rerender5次令session/catalog各1→6次。另Escape取消公开词后焦点落BODY；异步prepare之后才采样activeElement，原发送按钮已被Host禁用。上述均是现有scope/草稿/焦点约束的缺陷。

GPT-6 Luna仅修改插件 `Composer.tsx` / `.test.tsx` 与同样存在对象依赖的 `SearchSettings.tsx` / `.test.tsx`：网络加载按语义scope primitive（generation、agent、thread及必要locale）触发；同值快照、theme/timeZone/readiness更新不重复session/catalog/settings动作，不覆盖未保存草稿，真实scope变更仍重新加载并隔离迟到响应。同步captureSelection时只在插件本地ref捕获当前HTMLElement；不序列化DOM、不查询Host私有节点、不加Host API。取消后在Host解锁时恢复仍连接且当前scope的触发元素；新capture无有效元素则清空，不复用陈旧焦点。

先RED同值对象5次更新、未保存搜索设置草稿保持、真实scope变更加载、capture后Host禁用/blur再Escape的焦点；GREEN包含旧CAS/两个异步顺序/模式/批准全部回归。全插件typecheck/test/build和格式后独立review，再真实安装观测空闲请求数与焦点。

### B03d 合成模型的多轮契约修复

浏览器第二轮 WEB_ONLY 来源HTTP完成但操作仍GENERATING。只读诊断确认合成 FakeMessagesListChatModel 重复返回同一AIMessage对象及已分配id，LangGraph reducer会覆盖原消息；这是spinner的强因果候选，尚不能排除其它问题。另 factory shim 不接受现行 max_retries 等keyword，导致可复现的memory降级。

原 GPT-6 Luna 运行器作者仅修改 `tests/support/enterprise_ui_runtime.py` 和 `tests/unit/enterprise/test_ui_runtime.py`：确定性模型每次调用产生新的消息对象/id，保持bind_tools、invoke/stream可用；factory shim接受现行可选keyword，不造成真实LLM外呼。保留真实图/工具/operation/evidence与全部权限流程。先RED两次调用id复用/reducer覆盖及keyword错误，再GREEN；在独立新home、独立端口5192正常安装并验证同一thread连续两轮真实图完成、不同operation/消息id、保留两轮历史。来源只用MockTransport，不造终态/ToolMessage/evidence、不扩大生产代码或关闭权限；关闭自己的实例，不碰控制器5190。

限定pytest/Ruff/strict mypy后独立review。全批插件修复齐备后控制器重启最终隔离实例并正常重新登录，对第二轮spinner再验；如果仍失败则继续按实际根因修复，不将此候选直接当作产品缺陷已关闭。

- 沿用 E 盘现有 checkout，保留已有五份迁移文档修改，不另建 worktree；功能基线差异在本轮 ledger 保存。
- 使用全新临时 home 和本机 5190 端口，已有 5188 页面/会话不作新包验收依据，不修改用户正式实例。
- B01/B02 文件独立可并行；B03 的安装更新等待 B02 build 完成，期间不让运行服务加载半写的安装副本。
- 正常登录和服务端授权真实；只有外部资料源与 LLM 合成。原文当前不具备能力时验证明确不可用，不为了演示扩展读取权限。
- 本轮只闭合已审 UI 行为；远端真实供应商、私库生产者和正式交付保持原有待办。

### B03e 联网证据的实际供应商与备源标识

最终浏览器 WEB_ONLY 已正常完成且记录主源503一次、备源一次，但卡片仅显示“联网来源”，遗漏 DTO 已严格验证的 provider/degraded；现有 evidence spec 已要求 fallback 显示实际来源。GPT-6 Luna 仅修改 `EvidenceRenderer.tsx` / `.test.tsx` 与必要 `i18n.ts`：联网卡片显示实际 SearXNG/Firecrawl，degraded=true 时明确标记“已切换至备源”，知识卡片不变，不增加 URL/网络动作或后端字段。先 RED 实际 Firecrawl/fallback 中英标签与 SearXNG 非降级控制，再 GREEN；保留未知 provider 拒绝及 WEB_ONLY 无知识 read/original 的断言。全插件测试/typecheck/build/格式后独立 review。通过正常 ZIP 安装更新本地插件，核对源/安装副本哈希，再刷新历史的真实登记卡片复验；不改浏览器 token 或复制安装目录文件掩盖安装过程。

### B03f 企业 Composer 与队列/HITL 的集成证据

UI任务3.4的Host/native-send原有实现和精准接缝测试已通过，但独立核对未找到真实企业Composer贯穿发送/队列/HITL的组合断言。本批只补测试证据，优先不改生产代码；不建立新队列、steer协议或浏览器会话。

GPT-6 Luna允许新建 `dashboard/src/pages/Chat/enterpriseComposerIntegration.test.tsx`（若组织需要同前缀helper最多一文件）：挂载实际插件setup/Composer、PluginSurface scoped Host和preparation registry，实际ChatInput/useChatSend/useChat/useChatMessageQueue/dispatchPreparedQueueItem/chatStore路径，只在HTTP、FakeWebSocket及无关媒体/浏览器API边界使用替身。不要mock被测capture/prepare/send/queue/resume。

- 忙时顺序提交两条消息：分别实际准备operation并保存独立快照；当前轮不cancel、不steer；按原生队列在idle后逐项发送，断言两个WS user_turn具有不同operation_id、各自选库/模式快照、delivery=follow_up、prepare各一次，不借当前选择、不丢消息/重复flush。
- HITL：从实际初始企业operation和WS pending ask进入恢复分支，ChatInput响应通过真实resumeHitl发出HTTP/SSE；沿用原thread/pending_id，不新增operation、不发第二个user_turn，也不在HITL DTO增添客户端plugin_context。后端原context/新ticket/再授权复用本轮make all已通过的 `tests/integration/test_plugin_turn_guard.py` 精准断言。
- 新增覆盖不要求人为制造产品缺陷。若出现失败，先核对测试边界和根因；确认生产缺陷后报告控制器，修改生产路径前另补计划并review，不能降低断言来绿灯。

执行限定新文件测试、Dashboard tsc/限定ESLint/Prettier；既有全Dashboard1566与后端5728结果可复用，不为纯新增测试重复大套。独立审查实际贯通边界和断言后方可判断3.4闭合，不把FakeWebSocket称为新的真实SDK或浏览器HITL验收。

### B03g HITL 具体 pause 标识透传修复

B03f实际串联前确认通用UI缺口：`findPendingAsk`要求已有pending_id，却未返回该字段；routeGuardedChatSend只传actions/thread给resume。后端无ID选最新pending是legacy兼容。同用户同thread的另一tab可能已把A答复推进到B；当前旧A输入缺ID会答到B，不是跨用户越权。独立只读核对已确认；需用B03f真实HTTP断言复现。

GPT-6 Luna额外允许仅修改 `dashboard/src/pages/Chat/utils/pendingHitl.ts`、`utils/pendingHitl.test.ts`、`index.tsx`、`components/ChatInput.prefill.test.tsx`，以及已授权新集成测试。PendingAsk保留服务端pendingId，route callback第三参明确携带该ID，ChatPage用显式wrapper映射到现有useChat.resumeHitl第五参，不能误映射dismissed参数。其他card/hook/backend协议不变，无IDlegacy仍兼容；不增加客户端plugin_context或企业业务规则。先RED缺ID、精确恢复目标及旧ID不被当前选择替换，再GREEN；有ID只resolve对应pause、另一pending不被乐观关闭。更新此前真实路由测试的预期调用，不删除断言。

限定新集成及pendingHitl/ChatInput/HITL相关测试、Dashboard tsc/ESLint/Prettier/build；独立review通过后方可接受。生产改动只属通用Host白名单的HITL目标选择；不触碰后端授权、pending查找或插件业务。实现前同步chat spec/design/core-delta并审查，随后继续原B03f。

### B04e 历史测试冷加载隔离

B03g后全Dashboard284文件复跑，283通过；history测试第一项在动态import阶段15秒超时，后续5项受未结束act影响返回null。旧B04b的15秒文件上限并不稳定，不能继续加timeout或降低行为断言。根因线索：loadHistory动态引入useSessions，首次调用把模块冷编译计入行为测试。

GPT-6 Luna仅修改 `dashboard/src/pages/Chat/hooks/useChat.history.test.ts`：核对实际动态依赖，把无关模块冷加载放到测试收集/装配阶段（例如静态预载真实useSessions，保持被测loadHistory的动态import异步边界），移除不再必要的15秒testTimeout；不mock被测history/store/hook，不改生产或全局Vitest配置、断言、skip。若预载不能隔离根因应报告具体错误，不能靠延长阈值变绿。先限定六项回归、tsc/ESLint/Prettier；独立review后最终全Dashboard以明确maxWorkers=4运行，记录这一资源配置，不把默认并行失败隐藏。


## 5. 最终执行结果

2026-10-26：本计划B01–B04及发现修复B03a–g/B04a–e完成；功能/测试代码由GPT-6 Luna实施，独立Spec/Quality review无剩余必须修复项。OpenSpec UI 13/13、strict校验通过；主15/29、私库6/18不变。后端make all 5728通过/236跳过；插件UI162通过；Dashboard最终284文件1568项通过（4workers），typecheck/lint/build/格式通过。具体分段证据、人工边界和截图以ui-installed-acceptance.md为准。本轮未提交/推送/归档/发布，临时浏览器/5190已关闭。
