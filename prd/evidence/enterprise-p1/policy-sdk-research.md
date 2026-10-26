# P07：Harness 1.0.4 工具与 middleware 接缝调研

> 时间线说明：本文保留P03d实施前/中源码快照，WIP措辞与行号不是当前运行时状态。P03d现已在三轮修复后独立审查PASS，当前公开合同见plugin-host-v1.md，真实入口/委派/恢复证据见task-P03d-report/review（SDD记录）。P07仍须验证实际两个业务插件挂载到Agent图及每Agent启用状态，不能仅凭全局已加载或本文静态研究称缺hook保护完成。

范围：只读核对本地 `.venv` 中已安装的 `octop-harness` 1.0.4 / LangChain middleware API、Octop 接入点，以及 P07 与 `enterprise-knowledge-access`、`enterprise-controlled-search` 的执行要求。未改 SDK、未运行测试或 live 请求。行号均指本仓库当前工作树源码。

> **状态区分（重要）**：下文 `.venv/Lib/site-packages` 中 `octop-harness` 1.0.4 与 LangChain 的接口、签名和行为属于本地已安装 SDK 源码事实。Octop 的 `ExecutionTicket`、`current_execution()`、required-extension 检查及 chat/Gateway/Manager 接线则是另一代理并行实现中的 **P03d WIP 快照**；尚未独立审查，也未完成入口测试。相关行号和接线只能用于 brief 设计参考，不可视为稳定或已验收合同。P03d 完成 D 审查及支持入口测试后，必须复核这些描述和行号；未通过复核前，P07 brief 应将其列为待确认前置条件。

## 结论

1. **工具发现和实际执行是两处不同接缝。** `PluginContext.tool(name, fn, ...)` 把 `ToolRegistration` 放入插件 registry；Octop 构建 Agent 时将启用的插件工具注入工具列表。Harness `ToolSearchMiddleware` 可以延迟模型侧展示/加载，但源码说明完整工具集仍注册在图上以供受控执行。因此搜索/隐藏不能作为授权门槛；P07 的拒绝必须发生在工具执行前。
2. **Hook 能拦截同一 LangGraph Agent 图经过 ToolNode 的调用。** SDK `ctx.middleware(instance, priority=100)` 接受 middleware 对象，Harness 把它们加入 Agent middleware；LangChain 的真实拦截方法是 `wrap_tool_call` 和 `awrap_tool_call`，收到 `ToolCallRequest`，在不调用 `handler(request)` 时可短路。`awrap_tool_call` 适用于 Octop async chat；若要兼容同步 `invoke/stream`，还需同步对应实现。
3. **工具调用上下文本身不是可信身份。** `ToolCallRequest` 给出 `tool_call`、解析出的 `tool`（也可能为 `None`）、agent `state` 和 `runtime: ToolRuntime`；runtime 可携带图运行 context。身份应从 Octop 的可信执行 ticket/Host facade 取得，而非 tool args、plugin config 或可由模型写入的 state/context。当前工作树有 P03d WIP 的 `PluginHostFacade.current_execution()`，尝试通过 Octop `ContextVar` ticket 取 actor/plugin_context 并重读用户状态；这不是已验收能力。P07 middleware 可按该候选契约设计，但须待 D 审查与入口测试完成后复核。
4. **Host 和策略 hook 必须各自 fail closed。** Host 在公共 turn/call/resume 执行边界校验可信 ticket、agent/thread 与持久 required-extension 版本；缺 ticket/必需插件/版本不符时不启动企业绑定执行。策略 hook 在工具执行层拒绝被允许的遗留搜索、`searchfree` 和旁路工具调用，并将合法知识调用交给知识服务。Hook 本身缺失/停用不可由“没看到企业工具”替代；required-extension 只要声明就应由 Host 阻断。

## 已证实接口与接入

### SDK 插件发现和注册

- `PluginContext.tool(self, name: str, fn: Any, *, description: str = "", config_fields: list[dict[str, Any]] | None = None) -> None`：只接受 `manifest.kind == "tool"`，保存 `ToolRegistration(plugin_id, name, fn, description, config_fields)`。来源：`.venv/Lib/site-packages/octop_harness/plugins/context.py:39-60`。
- `PluginContext.middleware(self, instance: Any, *, priority: int = 100) -> None`：只接受 `manifest.kind == "hook"`，保存 middleware registration。不能用一个 manifest kind 同时注册 tool 与 middleware。来源：`context.py:62-71`；计划也记录 kind 互斥：`prd/Octop_第一阶段二开实施计划_v1.0.plan.md:20-26`。
- Loader 顺序是载入模块 → 建 `PluginContext` → 同步调用 `setup(ctx)` → `register_loaded(ctx)`。`LoadedPlugin.context` 保存相同的 context 实例。来源：`.venv/Lib/site-packages/octop_harness/plugins/loader.py:45-85`、`plugins/context.py:152-167`。
- `PluginRegistry.all_tools()` 汇总注册工具；`build_middleware_chain(global_enabled=...)` 过滤全局关闭的插件并按 priority 升序排序。来源：`.venv/Lib/site-packages/octop_harness/plugins/registry.py:126-130,146-155`。
- `build_plugin_tools(*, agent_plugins=None, global_plugins=None)` 遍历注册工具并生成 `StructuredTool`；默认全局插件启用后 agent 可用，agent 配置 `enabled: false` 才跳过。来源：`.venv/Lib/site-packages/octop_harness/plugins/tools.py:27-63`。
- Octop `AgentManager` 计算有效插件，构建插件工具和 middleware，并把 middleware 放在 Harness 配置的 `middleware` 中。来源：`src/octop/infra/agents/manager.py:3838-3865,3890-3892,3915-3932,4053-4072`。这里的 agent middleware 会随该 Agent 图组装；这不是 HTTP/浏览器路由级的全局中间件。

### 实际工具执行 hook 签名

LangChain 安装源码给出的接口：

```python
def wrap_tool_call(
    self,
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
) -> ToolMessage | Command[Any]: ...

async def awrap_tool_call(
    self,
    request: ToolCallRequest,
    handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
) -> ToolMessage | Command[Any]: ...
```

来源：`.venv/Lib/site-packages/langchain/agents/middleware/types.py:674-690,756-779`。文档明示跳过 handler 可短路，middleware 按首个为外层组合；异常是否映射取决于 ToolNode 的 error handling：`types.py:683-696,761-779`。`ToolCallRequest` 字段是 `tool_call: ToolCall`、`tool: BaseTool | None`、`state: Any`、`runtime: ToolRuntime`：`.venv/Lib/site-packages/langgraph/prebuilt/tool_node.py:132-149`。它允许工具为空，因此未注册的模型工具调用也可被 middleware 检查/短路；不要假定 `request.tool.name` 永不为空，应按 `request.tool_call["name"]` 检查。

LangGraph `Runtime` 有运行期 `context`，但不含 `RunnableConfig`；后者需显式注入或 `get_config()`：`.venv/Lib/site-packages/langgraph/runtime.py:125-141`。Harness 的 `ToolSearchMiddleware` 另有 `wrap_tool_call/awrap_tool_call`，且其说明指出完整实际工具集仍注册在图中：`.venv/Lib/site-packages/octop_harness/middleware/tool_search.py:103-149`。所以 deferred 搜索清单不是执行 allowlist，策略检查不能只放在 `wrap_model_call` 或依赖模型可见工具集合。

### 可信当前执行上下文和 Host 阻断（P03d WIP，待复核）

- **WIP 快照**：Dashboard turn 适配器从认证用户构造 `PluginActor(user_id, is_admin, permissions, agent_id, thread_id, locale)`，生成进程内 `ExecutionTicket` 并放进内部 message metadata；ticket 类型要求真实 agent/thread。来源：`src/octop/api/routers/chat/turn.py:354-374`、`src/octop/infra/agents/plugins/execution.py:20-52,55-63`。
- **WIP 快照**：Gateway 消费内部 ticket，在 agent 处理期间激活 `ContextVar`，结束后 reset；缺失/无效票据时调用 `authorize_execution`，失败即不进入消息处理。来源：`src/octop/infra/gateway/process/processor.py:642-674,980-1012`。
- **WIP 快照**：`AgentManager.stream/call/resume_hitl` 再调用 Host 的 `authorize_execution`；resume 明确使用 `execution_kind="resume"`。来源：`src/octop/infra/agents/manager.py:1856-1885,1888-1912,1917-1935`。
- **WIP 快照**：`PluginHost.authorize_execution(ticket, *, agent_id, thread_id, execution_kind=None)` 校验 ticket 的 Host key、agent/thread 和类型；读取 Host 保留 `required_extensions` 记录，存在声明时要求真实 ticket、重新授权 actor，并要求所有 required plugin 的已启用版本精确匹配，否则 denied/unavailable。来源：`src/octop/infra/agents/plugins/host.py:313-377`。
- **WIP 快照**：`PluginHostFacade.current_execution()` 只在当前 Host 的 ticket scope 下可用，先重跑执行校验，再重新读取有效 user 状态并返回当前插件的 plugin_context 副本。来源：`host.py:378-405`（返回构造在 405 行后）。
- 如果经 D 审查与入口测试确认，上述接缝可供 Host 做公共入口 fail closed，并供策略 hook 读取可信上下文；在此之前不要把它们当已完成接线。不要让策略插件自行解码 `ticket`，也不要把 ticket 放进 Harness 的 `configurable`/模型参数。

## 路径覆盖边界

| 路径 | 本地证据与可否拦截 | P07 应如何处理 |
|---|---|---|
| knowledge plugin `ctx.tool` 注册的 list/search/read | 经 `build_plugin_tools` 注入 Agent 工具清单，并经过该图 ToolNode；可由同图 hook 在执行前拦截。 | 可绑定为企业知识工具；Host service 每次重算五层范围。 |
| Harness `browser_use`、`web_fetch` 和内置 web search 工具 | Harness `_build_tools()` 明确组合 `browser_use`、`web_fetch`、`load_web_search_tools(...)`：`.venv/Lib/site-packages/octop_harness/agent.py:1361-1372`；作为该图工具，可由该图的 tool-call middleware 按名称/registration 执行检查。Octop 工具目录含 browser/searchfree/搜索项：`src/octop/infra/agents/settings/tool_catalog.py:22-30,64-76`。 | 对当前 Agent 图上明确命中的旧搜索工具可拒绝；未加入 Agent 图的浏览器 HTTP/API 路径不因此受保护。 |
| `execute` / shell backend 工具 | Octop 工具目录确实列出 builtin `execute`：`tool_catalog.py:51-62`；Harness 的 Agent 构造还将配置 backend 交给 graph builder（Harness `agent.py:1700+`）。对于经当前 Agent ToolNode 执行的该工具，通用 hook 有调用级拦截点。 | 可按实际注册工具名拒绝图内调用；不得推断它覆盖进程直接执行、独立工具接口或所有 shell 实现。先用运行时精确工具清单建立覆盖证据。 |
| MCP tool | Harness `MCPToolMiddleware` 只改模型暴露列表；MCP tools 实际仍来自 MCP server tools。Harness 明确注明 subagents 不继承 parent middleware，并在子代理组装处静态隐藏 MCP：`agent.py:1493-1500,1670-1693`。同一主图中的 MCP tool call 可由外层 `awrap_tool_call` 拦截。 | 主图可按工具名/来源拒绝不受控 MCP 搜索；不能声称对所有子图、直接 MCP gateway 请求或其他 MCP 执行入口全覆盖。缺证据的 enterprise-bound delegation/MCP 路径应 Host 侧禁止。 |
| ToolSearch / provider 原生延迟发现 | 只改变模型请求中的工具展示/载入；SDK 文档写明完整工具仍在图上正常 guarded execution。 | 必须在 `awrap_tool_call` 做执行策略，单独过滤发现目录无效。 |
| Harness subagent / team / `task` delegation | Harness 指明 subagents 不继承 parent middleware：`agent.py:1671-1674`；故父图 hook 不自然覆盖子图。 | P07 对企业绑定 turn 的委派应依计划默认拒绝，除非单独证明所有派生图继承可信 ticket、required checks 和策略 middleware。普通未绑定团队不应据此改变。 |
| 独立 Browser session/router | **Hook 覆盖事实**：这些路由不经 Harness Agent ToolNode，因此不能证明受策略 middleware 控制。**业务关联事实**：当前路由由 `current_user` 认证并按 `user_browser_profile(user.id)` 操作；harness-sessions、handoff、shutdown、record/replay 和 WS screencast 没有 agent/thread/turn 参数（例如 `src/octop/api/routers/browser/harness.py:292-295,298-303,342-359`，`stream.py:335-373,382-400`，`record_replay.py:142-165`）。helper 虽有可选 `agent_id` 参数，当前这些路由未传入（`harness.py:65-70,119-131`）。因此本次证据不表明它能执行 enterprise-bound Agent/turn。 | **不要把用户手动 Browser profile 当作企业 Agent 策略旁路，也不要新增全局禁用。** 仅当某条 Browser 接口实际接受并执行 enterprise-bound Agent/turn，才需由该 Host/route 在执行前校验可信 agent/thread/ticket 与 required extension；仍不能声称 Agent tool hook 覆盖了独立路由。 |
| 直接 HTTP/API、外部插件/MCP 调用 | 不一定经 Harness Agent ToolNode；本次未发现可证明插件 middleware 会包住这些调用的合同。 | 对能执行 enterprise-bound Agent/turn 的入口，需由对应 Host/route 在执行前校验；缺少这种关联的独立接口不因此自动归入企业查询旁路。 |

上述“可拦截”限定为 `AgentMiddleware` 确实位于实际执行图、目标操作确实作为 ToolCall 通过其 ToolNode 的情况。代码注册的 middleware 并不等于跨图/跨 API 的系统级拦截器。

## P07 最小实现方式与边界建议

1. `enterprise-knowledge` 保持单独 `kind: tool`，`setup(ctx)` 用 `ctx.tool("<固定名>", async_fn, description=...)` 注册 list/search/read；函数在真正执行时 `await` 从注入 callback 捕获的 Host facade 获取 `current_execution()`，不得在 setup 时取得员工身份。
2. `enterprise-policy` 保持单独 `kind: hook`，`setup(ctx)` 通过 `ctx.middleware(PolicyMiddleware(), priority=<明确值>)` 注册。async middleware 实现真实 `awrap_tool_call(self, request, handler)` 签名，先获取 Host 版本化 policy service；对受保护操作先校验 Host execution，再根据 `request.tool_call["name"]` 识别遗留 web/MCP/browser/exec/delegation，拒绝时返回带相同 `tool_call_id` 的 error `ToolMessage`，允许且经 policy authorize 后才 `await handler(request)`。如同步入口仍在支持矩阵内，成对实现 `wrap_tool_call`，不能把 async-only 当作可同步执行。
3. Hook 不应因“别的插件没注册”而隐式跳过。知识工具/策略 hook 的精确 ID+版本由 Host required-extension 记录在 agent/thread 绑定上；Host 在 `stream`、`call`、`resume_hitl` 及确认适用的 IM/Bridge/子调用入口做必需扩展与 ticket 检查。缺少持久要求/认证主体时，保持普通未绑定 turn 兼容；声明过企业扩展但未安装、禁用、错版或无 ticket 时拒绝。
4. 把受保护 tool 名单限制为**源码证实且 runtime 真实暴露**的条目；启动/Agent 构造阶段生成实际图工具清单，P07 回归验证旧搜索/MCP/`searchfree`/browser/execute 名称。对不经当前图 Hook 的 route/service/agent delegation 明确 deny 或留给对应 Host 入口任务，不能以“理论上能看到 request.tool”覆盖。

## P07 brief 应包含的最小验收

- Hook 对 list/search/read 正常传递，对真实 actor 的越权/无 ticket/Host service 缺失拒绝；伪造 tool args、plugin_context、user id 不得影响身份。
- 直接调用 `awrap_tool_call` 时，旧搜索、`searchfree`、受限 MCP、browser、exec 和未注册 ToolCall 都在 handler 前拒绝；可允许工具才调用 handler 一次。重复/并发调用的 operation 去重由知识/搜索服务实现，不由 middleware 通过重试实现。
- 工具发现仅用于 UX；隐藏后直接发出的遗留 tool call 仍拒绝。测试主图工具实际清单与名字归一规则，不能只测 registry 里手工构造的 tool。
- Host required-extension 集成：策略 hook 未加载、disabled、版本不符、ticket 缺失，以及 resume/IM/Bridge/子代理等未覆盖入口分别验证 Host 在执行前 fail closed；普通未绑定 Agent 回归保留。
- 明确 `browser_use` graph tool 的覆盖不等于 Browser API 覆盖；对独立浏览器/API 只在其实际能执行 enterprise-bound Agent/turn 时要求 Host guard。当前用户手动 browser profile 路由不因此新增全局禁用。对直接 MCP 和派生 graph，要有拒绝/不支持的可执行证据。禁止在未覆盖工具里执行 network canary。

## 不能承诺的接口

本地 SDK `PluginContext` 没有内建 host facade 字段、turn actor、policy hook registry API 或 required-extension 概念；`ctx.middleware` 仅接受任意 middleware instance。企业插件 Host 注册 callback 与 `current_execution()` 属 Octop 新增契约（当前工作树实现在 `src/octop/infra/agents/plugins/host.py`），不属于 octop-harness 1.0.4。不要在 brief 中写成 SDK 原生能力。

当前工作树的 P03d WIP 快照包含 chat turn ticket、Gateway ContextVar 和 AgentManager public call gate（见上），但它们尚未独立审查或完成入口测试，不能记为已解决的 Octop 支持入口。即使后续确认这些接线，也不证明所有 ACP、Bridge、后台 job、浏览器 API 或 Harness 派生 subagent 都进入同一 scope。凡未由审查后的源码接线和测试证实的路径，应列为 P07 拒绝绑定边界或独立宿主接缝任务，而不是猜测新增 hook 签名。
