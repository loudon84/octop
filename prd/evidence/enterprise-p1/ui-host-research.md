# P08 前端宿主接缝调研

工作区：`C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop`。本次为只读核对，唯一新增此证据文档；未改功能/测试、未运行测试、未提交。

## 结论摘要

现有动态插件 UI 已有一个可复用的自包含 ESM loader、插件级 host 工厂、tool renderer 注册表、认证 API request、聊天 agent/thread context，以及 renderer sidepanel 生命周期。`settings` / `composer` surface 的类型、注册表、卸载和 React 容器均尚未存在；P08 需补一个通用 surface 注册/卸载机制，并在两个既有页面及聊天 composer 内加固定容器。现状符合设计文档“Host 无 settings/composer surfaces”的描述（`openspec/changes/enterprise-knowledge-plugin-ui/design.md:5,15-16`）。

## 已存在：可复用接缝

- **Plugin Host：** `OctopPluginUIHost` 当前只有 `registerRenderer/getToolContext/patchResult/request/openSidePanel/closeSidePanel`（`dashboard/src/plugins/toolRenderers/types.ts:62-75`）。`createPluginUiHost(pluginId)` 返回这些能力；agent/thread 来自共享 `contextOverride`，locale 取 i18n，theme 读 document（`dashboard/src/plugins/toolRenderers/host.ts:15-43,45-78,81-87`）。故 `registerSurface(settings|composer)`、surface 卸载接口、`getContext/setTurnContext` 均待新增。
- **动态载入：** 插件从 `ui.entry` 经认证 fetch，源码包装 Blob URL 后 `import()`，再执行 `setup(host)`（`dashboard/src/plugins/toolRenderers/loader.ts:11-21,23-49,52-68`）。源码明确要求单个自包含 `ui/dist/index.js`、不留相对 import/chunk 依赖；React/JSX 由 `window.__OCTOP_REACT__` / `__OCTOP_JSX__` 提供（`loader.ts:23-25`；`plugins/README.md:71-75`）。loader 跟踪 `loadedPlugins` 和 blob URL；重载先 reset，卸载 renderer 并 revoke blobs（`loader.ts:8-9,71-90`）。
- **Renderer 卸载与侧栏：** renderer registry 按 `pluginId::id` 存储，支持 `unregisterPluginRenderers(pluginId)` 并通过 version listener 通知订阅者（`registry.ts:8-11,28-38,46-71`）；loader 在 setup 前先注销该插件 renderers，reset 时注销已加载插件（`loader.ts:59-63,81-90`）。chat 通过 `setPluginUiDockHandlers` 将 `openSidePanel/closeSidePanel` 接到 dock tab（`pages/Chat/index.tsx:520-533`）；dock 内容由 callId 查原工具消息，再渲染 `ToolDetailsInline`（`components/ChatDockToolUiContent.tsx:13-43`），多个 tool UI tab 可同时驻留并隐藏非活动项（`components/ChatDockPanel.tsx:639-657`）。新增 surface 要复用插件级注册/注销路径，并在卸载时移除对应已挂载 UI；只注销 renderer 不会自动删除 dock 中已打开的 callId tab。不要改变既有 `openSidePanel` ID/行为。
- **固定 settings 容器 1：** 路由 `/connectors` 指向 `ConnectorsPage`（`dashboard/src/routes/index.tsx:155`）。页面 `PageShell.Tabbed` 下按 `custom` / `enabled` 显示现有连接器区（`pages/Agent/Connectors/index.tsx:2343-2360`），enabled 区渲染实例卡片（`:2421-2428`）。当前无插件 surface mount；合适的固定容器是此页已启用连接器管理区域内的一个稳定插件区，不需新增 route/nav。
- **固定 settings 容器 2：** `/personalization/*` 由 `PersonalizationPage` 处理，`/personalization/plugins` 是现有 tab（`routes/index.tsx:58-65,163`；`pages/Agent/Personalization/index.tsx:75-81`）。`plugins` tab 内现有 `AgentPluginsPanel(agentId={activeAgentId})`（`:174-185`），页面带 `agentScoped`，并通过 `expertOnly` 包裹内容（`:118-125,180-184`）。可在此既有 expert/plugin 配置页挂第二个固定 surface；当前并无 registerSurface 挂载。权限仍由插件 action/服务端校验，不应把前端 tab 可见性当授权事实。
- **Chat composer 与发送调用链：** `ChatInput.submitMessage` 是 UI 提交点，常规发送调用 `onSend(wireText, attachments)`；排队消息在此处构造 `composerContext`（`pages/Chat/components/ChatInput.tsx:609-648`）。`useChatSend.handleSend` 构造 composer context 后调用 `sendMessage(...)`（`pages/Chat/hooks/useChatSend.ts:141-177`）。`useChat.sendMessage` 把该 context 放进 optimistic `ChatMessage` 并调用 `chatStore.sendTurn`（`pages/Chat/hooks/useChat.ts:884-925`）。当前 `UserComposerContext` 只有 skills/connectors/knowledgeBaseIds/targetAgents/model/reasoning 字段，无插件扩展字段（`pages/Chat/hooks/sseHelpers.ts:57-66`）。底层 WebSocket `user_turn` payload 发 thread/model/mcp/knowledge/target/reasoning/mode/hitl 等，但不发 `plugin_context`（`pages/Chat/hooks/chatStore.ts:2834-2864`）。因此 composer surface 内容不能仅加在 UI 本地 state；需明确定义从 Host 受控 turn context → ChatInput/useChatSend/useChat → `chatStore.sendTurn` → socket payload 的字段通道，并让队列/重试保留快照。当前普通 message `composerContext` 主要是 dashboard 历史/气泡显示路径，不能视作已可透传插件 turn context。
- **服务器时区：** `useServerTimezone()` 经 `octopSettingsApi.timezone()` 请求 `/api/settings/timezone`，无值/失败使用 UTC fallback（`dashboard/src/hooks/useServerTimezone.ts:4-5,28-63`；API wrapper 在 `api/modules/settings.ts:50`）。Host context 不包含时区（`toolRenderers/types.ts:20-25,62-75`），故目标方案需由宿主 context 注入已取出的 server timezone（或提供等价只读 getter）；插件不应读取浏览器本地时区作为展示值。`design.md:22` 与页面 spec 也要求使用 server timezone。
- **认证和身份上下文：** `host.request` 转到通用 `request()`（`host.ts:69-70`）；通用 request 从 local/session storage 取 JWT（`api/request.ts:85-92`），注入 `Authorization: Bearer` 与 `Accept-Language`（`:243-269`），实际执行 fetch 并统一处理 401/403（`:382-415`）。chat tool context 设置只有 agent/thread（`usePluginToolUis.ts:14-28`），Host 不提供 user ID；用户身份应由服务端根据已认证请求推导，符合 P08 设计“payload 不带 user identity”（`design.md:53`）。请勿为了业务插件扩展而把 token 暴露给插件或在 context 增加可伪造身份字段。

## 最小 Host/生命周期增量建议（供执行 brief）

1. **类型/API：** 扩展 `types.ts`，增加受限 `PluginSurface = "settings" | "composer"` 与 surface props/注册参数；Host 提供 `registerSurface(...)`、受控注销（返回 disposer 或统一 `unregisterPluginSurfaces(pluginId)`）、只读 `getContext()` 和 `setTurnContext(...)`。 context 至少涵盖 agent/thread/locale/theme/serverTimezone；turn context 限定 composer 允许写入的插件命名空间，不暴露身份。以上皆为待新增；现有 `getToolContext()` 只能返回四项 context。
2. **Registry + loader：** 用独立 surface registry（按 pluginId/surface/id）通知 React slot 更新；在 loader 的重载前、reset/uninstall 时与 renderer 一起 unregister；注册项应能定位卸载其 React 树。避免把 surface 复用成 renderer/toolName 伪注册，以免改变工具解析语义。
3. **固定 mount：** 宿主只在 `/connectors` enabled 面板、`/personalization/plugins` 的 expert 插件设置区、以及 ChatInput composer 控件行读取注册项并 render；页面路径和既有 route/tab 不新增。连接管理表单和知识业务逻辑留在插件。
4. **发送通道：** 在 composer mount 给插件传入当前 turn context 的读写回调；在提交与排队处 snapshot，按 plugin id 合并到发送载荷的 `plugin_context`；必须同时覆盖常规发送、queued send/reclaim 和 async context。前端 `chatStore.ts` 当前缺此字段。服务端 `ChatTurn` 型号虽有 `plugin_context`（`src/octop/api/routers/chat/models.py:46`）且 turn 层消费它（`turn.py:307,364-365`），但该后端正由 P03d 修改，不能据此认定端到端契约已验收。

## 测试/打包工具现状

- dashboard 脚本有 `npm test` → `vitest run`；Vitest 使用 jsdom、setupFiles 和 `src/**/*.test.ts(x)`（`dashboard/package.json:6-17`；`dashboard/vitest.config.ts:20-25`），可覆盖 surface registry/卸载和页面 slot 生命周期。
- `toolRenderers` 当前无 loader、host、registry 专属测试；文件列表只有 `isPinnedToolUi.test.ts`、`parseToolOutput.test.ts`、`remotePluginUi.test.ts`。`parseToolOutput.test.ts` 有注册 renderer 的测试，但不是 loader 生命周期测试。这里是覆盖空白，不代表需要扩大本次 UI feature 的测试范围。
- 单入口 ESM 约束来自 blob import：entry 以外的相对模块路径会按 blob URL 解析失败；README 另规定 React/JSX 走宿主全局注入。构建检查需验证产物没有 `import "./..."`/动态 chunk 引用，并确认 React externals 与当前 dashboard 全局名一致。

## 白名单与风险

### 执行 brief 文件白名单（按需求可增删）

- **通用宿主：** `dashboard/src/plugins/toolRenderers/types.ts`、`host.ts`、新增的 surface registry（可单独文件）、`loader.ts`、`index.ts`。
- **固定挂载：** `dashboard/src/pages/Agent/Connectors/index.tsx`、`dashboard/src/pages/Agent/Personalization/index.tsx`、现有 Chat composer 的最小 slot 文件（大概率 `dashboard/src/pages/Chat/components/ChatInput.tsx`；若抽小型通用 slot，则仅增加一个组件）。
- **受控 turn context：** `dashboard/src/pages/Chat/hooks/sseHelpers.ts`、`useChatSend.ts`、`useChat.ts`、`chatStore.ts`，以及它们已有的 queue/reclaim 适配点（变更范围取决于 P03d 实际契约；先查看当前接口后再纳入）。
- **必要验证：** 新增/改动的 dashboard Vitest 文件；无必要不动 `MessageBubble`、`ChatDockPanel` 既有 renderer 行为或后端文件。

### 风险（需 brief 显式交代）

- registry unregister 不等于已打开 sidepanel tab 自动关闭；surface 清理要通过 React slot 卸载，renderer/dock 保持现有语义。
- `contextOverride` 是单例共享 context，当前只在 Chat mount 更新 agent/thread；在非 Chat 页面使用插件 setup 不应意外依赖当前 route 的陈旧 thread。上下文必须由 slot 提供/刷新，并用 generation 处理异步切换。
- `request()` 自动 agent header 只按已识别路径策略附加；注释特别说 composer/knowledge host GET 不自动 scoped（`api/request.ts:220-229`）。含 agent 的插件请求需通过规范化 agent scoped path 或明确 Host 支持的 context/query/header 约定，不能默认认为任意 plugin action path 自动携带 agent id。
- 已装载插件模块使用 `loadedPlugins` 去重；reload 会 revoke Blob URL 但已实例化 React 元素仍需通过 registry 更新卸载。请验证时序避免更新中闪烁/旧组件继续响应。
- 当前 Chat WS payload 不传 `plugin_context`；P03d 后端 work-in-progress 不是前端可依赖的已验收接口。执行前对照 P03d 实际契约，避免双边字段名/快照规则分叉。
- `composer_context`（消息展示 metadata）与 `plugin_context`（turn 执行输入）是不同通道；前者不能作为授权或服务端插件执行输入的替代。
