# Octop Composio Personal Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. User explicitly requested GPT-6 Luna subagents; all delegated workers and reviewers use `gpt-6-luna`.

**Goal:** 按 PRD v1.1 补齐现有 Composio 个人连接实现，完成自动验证，并准确区分已实现与仍待真实授权的能力。

**Architecture:** 保留当前 `composio-connect` 目录类型、远程 MCP 与服务端 OAuth 凭据保存路径。修复现有实现的协议、授权事务、个人权限与运行时隔离缺口；前端经 Octop typed API 管理个人连接。以 PRD 为准，不切换为统一项目 Key。

**Tech Stack:** Python 3.12+/FastAPI/asyncio/sqlite3/psycopg，React 18/TypeScript/Vite，现有 octop-harness MCP 和 OAuth helpers。

**Spec:** [需求 PRD v1.1](Octop_Composio公网连接扩展_需求PRD_v1.0_2026-10-27.md)。文件名含 v1.0，正文已更新为 v1.1，实施以正文为准。

## Global Constraints

- 每位 Octop 用户连接自己的 Composio 账户；不要求普通用户配置公司统一项目 API Key。
- 个人连接不得共享；管理员对他人的 Composio 连接只能查看脱敏状态与强制断开。
- Composio 授权回调在服务端直接保存凭证，浏览器不经手 token。
- 新建连接默认不随每次对话自动携带；首期不开放 Composio 的默认开启开关。
- 连接器用户解析、工具加载与缓存按“实际调用者 + 本轮”隔离；共享专家可安全使用调用者自己的连接。
- 页面初始加载、搜索和授权状态刷新不得自动触发授权创建／重置或业务写操作。
- 用户未确认的真实外部写入不得执行；真实账户登录由账户持有人完成，不索取或输出秘密。
- 工作目录为 `E:\git\octop`，继续当前分支，不建 worktree、不提交、不推送；保留既有未提交修改。
- 执行子代理使用 GPT-6 Luna；用户指定的模型优先于技能的自动模型升级建议。

## Review Focus

1. 把 ContextVar 当作运行时隔离但仍修改共享 agent 工具集：测试必须从并发调用实际执行到 A/B 不同工具，而非只断言 ContextVar 值。
2. 旧授权回调晚于停用／断开／新授权到达：旧事务必须失效，失败回调不能覆盖有效凭据。
3. 用 base64 解码未验证 JWT 即信任邮箱：无可信验证证据时身份显示未知；更换账户不得保留旧邮箱。
4. 猜测 Composio 元工具参数或无 initialize 直接 JSON-RPC：使用实际 schema 与规范 MCP 会话，不支持的只读状态查询必须降级。
5. 前端授权窗口关闭、回跳刷新、重新连接后的旧搜索结果：清理 listener/timer 和旧账户状态，不虚报可用。

## 当前基线与执行规则

2026-10-27 工作区已存在 `composio.py`、`ComposioTab.tsx`、探测脚本、测试及多处 connector 修改，同时有知识插件和聊天修复等其他工作。先保存本任务开始时修改文件的只读快照和清单到 git 忽略的执行目录；后续 review 比较该快照与当前文件，不使用 `HEAD~1` 冒充任务 diff。

进度、brief、report、review package 放在 `.superpowers/sdd/Octop_Composio实施执行计划_2026-10-27/`；正式验证报告仍在 `prd/`。当前既有修改也是待核验实现，不能只 review 本轮新增行而漏掉其安全缺口。

实施者只修改 brief 指定文件；跨任务接口变更先报告协调者。协议验证、审查可并行；修改相同文件的任务串行。每个实施任务先用能复现缺口的失败测试，再做最小修复并验证；不写只测试 mock 或 ContextVar 自身的形式测试。

用户已明确要求“制定计划后分派执行”，因此保存计划后直接按任务执行，不另加重复批准步骤。真实授权未完成时继续能独立完成的测试与代码修复，将 live 验收标为待完成，不把缺少人工登录解释成协议不支持。

## Task 0: 现有实现与 M0 协议复核

**Files:** `scripts/composio_probe.py`、`prd/Octop_Composio技术验证报告_2026-10-27.md`；只读检查 `infra/connectors/composio.py`、OAuth 与当前 PRD。

**Interfaces:** 输出 V-01～V-07 真实能力矩阵、协议缺陷清单及后续任务的调用约束；不能输出 token 或伪造真实认证证据。

- [ ] 阅读现有报告，区分匿名发现、已实测登录及设计推测。
- [ ] 用现有匿名 discovery 有界核对官方 metadata；检查脚本是否会打印秘密或阻塞等待，必要时修复并写针对性测试。
- [ ] 结合官方 MCP／Composio 文档确认元工具 schema 来源与安全查询边界；没有认证时保留 unknown。
- [ ] 报告可继续的独立工作与真实授权前不能宣称完成的项目；如个人路线确实不可行，按 PRD 停止依赖路径。

**Verify:** `uv run python scripts/composio_probe.py discover`；脚本测试只覆盖必要脱敏行为；没有账户登录时报告不能写“个人协议完整兼容”。

## Task 1: 个人连接领域服务与 MCP 协议

**Files:** `src/octop/infra/connectors/composio.py`、`tests/unit/connectors/test_composio.py`、必要的 `tests/unit/connectors/test_composio_protocol.py`；不修改 manager 或 router。

**Interfaces:** 保留 `ComposioService.get_connection(user_id)`、`check_connection(user_id)`、`search_apps(user_id, query)`、`start_app_auth(user_id, toolkit)`、`check_app_status(user_id, toolkit)` 入口，返回第 7 节 typed API 所需脱敏结构；实现细节采用现有 MCP SDK client/session。

- [ ] 写失败测试：真实样式搜索 schema、MCP initialize/session、未知能力不声称支持、身份未验证不信任邮箱、更换账户清除身份、搜索不触发应用授权。
- [ ] 修复 direct JSON-RPC／SSE 解析与 schema 猜测；优先复用标准 SDK 会话，使用已发现的 schema 构造参数。
- [ ] 状态读取无可验证的只读方法时返回 unknown/unavailable，不用 manage-connections 假造 action=list。
- [ ] 敏感返回使用字段白名单；授权 URL 只向当前持有人展示且不写日志；保留其完成授权所需数据。
- [ ] 并发幂等建立一个个人连接，防止顺序创建后重复记录；不删除用户无关记录。
- [ ] 跑聚焦测试、Ruff 和对该模块的必要类型检查，报告结果。

## Task 2: 实际调用者与本轮运行时隔离

**Files:** `src/octop/infra/agents/manager.py`、`src/octop/infra/connectors/mcp_tool_cache.py`、必要的连接器运行时 helper；`tests/integration/test_agent_mcp_gateway.py` 及新增针对性测试；跨层调用点只在证明必要时调整。

**Interfaces:** 保持外部 `prepare_chat_mcp(... connector_user_id=...)` 与聊天执行入口兼容；调用者身份随本轮作用域传递，不能通过全局 `_connector_user_override[agent_id]` 或共享 agent 的工具挂载传递。

执行裁定：当前 Harness 没有 per-call 工具注册接口，采用每智能体执行租约，覆盖工具准备、完整 stream/call/HITL 与工具恢复；后台 reload 也遵守该租约。代价是同一智能体调用串行，包括模型等待时间。允许最小调整 `api/routers/chat/turn.py`、`infra/gateway/process/{processor.py,stream_project.py}`、`infra/cron/delivery.py`，将实际调用者和已校验选择带到执行边界。不得只在预加载阶段加锁后立即释放。

- [ ] 先写共享 agent 并发 A/B 真实样式工具执行失败测试，以及 A→B→A 串行回归。
- [ ] 根据现有 harness 执行接口选择最小的每轮工具上下文／受控隔离方案；禁止仅增加 ContextVar 而保留跨调用共享可变工具集。
- [ ] 工具执行前复查 owner、连接启用状态与凭据代次；停用／断开后旧工具拒绝后续新调用。
- [ ] 明确请求未选中连接时不能调用旧挂载工具；覆盖内置、自定义及 Composio。
- [ ] 验证共享专家、IM/Cron 显式选择与原有连接器兼容，报告锁范围及并发行为。

**Verify:** `uv run pytest tests/integration/test_agent_mcp_gateway.py <新增运行时测试路径> -q`；最终报告列出失败复现与通过证据。

## Task 3: OAuth 生命周期、权限与 HTTP 合同

**Files:** `src/octop/api/routers/connectors.py`、`src/octop/infra/connectors/service.py`、`src/octop/infra/connectors/oauth/mcp.py`、`src/octop/infra/db/repos/connectors.py`、必要的 i18n/error 文件；`tests/integration/test_connectors_api.py` 与新 OAuth 回归测试。

**Interfaces:** 与 Task 1 服务入口一致；保持已有 Composio typed routes 的调用契约，必要变更通知 Task 4；pending 结果只含 applied/kind/instance_id 等脱敏数据。

- [ ] 写失败测试：owner/admin 权限矩阵、通用 create/PATCH/test/refresh 绕过、默认开启与共享拒绝、回调重放/超时/断开后晚回调、查询不返回 token。
- [ ] Composio 凭证仅服务端保存，MCP 初始化确认后才显示 reachable；失败不能覆盖现有可用连接。
- [ ] 授权事务有效期与最新 generation 在消费／应用时核验；取消、断开、新授权使旧事务失效。
- [ ] admin 他人连接仅脱敏读取与强制断开，其余修改／调用均拒绝；状态查询不接收任意调用者 user_id。
- [ ] 状态与凭据变更通知 Task 2 缓存失效，不通过全局重载污染其他调用者。
- [ ] 运行 API 和 OAuth 相关测试，确保其他目录连接器维持既有兼容行为。

## Task 4: 页面闭环与交互验证

**Files:** `dashboard/src/pages/Agent/Connectors/ComposioTab.tsx`、`index.tsx`、`index.module.less`、`dashboard/src/api/modules/connectors.ts`、`dashboard/src/pages/Chat/components/ConnectorPickerPopover.tsx`、`dashboard/src/locales/{en,zh}.json`、页面测试。

**Interfaces:** 使用 Task 3 提供的 typed API；前端不直接接触 MCP secret；capabilities 驱动 unknown/unsupported 降级。

- [ ] 写失败测试：四页签与 query 回跳、Composio 在已连接只计一个实例、连接归属与状态展示。
- [ ] 修复 OAuth popup 预开启、回调 state/source/origin 绑定、取消／超时／组件卸载 listener/timer 清理，回跳后只刷新脱敏结果。
- [ ] 未连通不显示正常；缺少 MCP 能力不提供虚假管理动作；切换／断开清除应用搜索与详情缓存。
- [ ] 应用授权链接必须为已验证安全的 HTTP(S) URL，明确点击后才跳转；禁止把任意上游 JSON 直接当 HTML 或授权状态。
- [ ] 停用／断开确认文案与范围告知完整，保持 zh/en、服务器时区及现有视觉规范。
- [ ] 运行必要前端测试、`npx tsc -b`、`npm run lint`，浏览器检查未连接与模拟状态页面；真实 OAuth 另记。

## Task 5: 审查、全量验证与交付

**Files:** 修复各任务发现的直接问题；`docs/connectors.md`、M0 报告和本计划记录；不修改无关 OpenSpec 或既有 PRD。

- [ ] 每个任务结束生成任务实际 diff 与报告，派独立 Luna reviewer 核对需求及代码质量；按审查结果修复并复核。
- [ ] 补齐目录表与 JSON 一致性，运行 i18n、connector/API/runtime 聚焦回归。
- [ ] 执行 `make all`、前端 `npx tsc -b`／`npm run lint` 及 `make build-frontend`；保留完整日志，失败定位具体原因，不跳过检查。
- [ ] 检查 `/api/docs` 可读性；确认代码未泄露凭据且没有无关格式化变更。
- [ ] 若账户持有人可配合，完成两用户登录、只读调用及受控合成数据写入；否则将 AC-02/07/08 的真实证据明确标为待验收。
- [ ] 保存实际完成项、命令结果、未完成原因及用户下一步；禁止用测试通过替代真实集成验收。

## 验收映射与任务依赖

| 任务 | 依赖 | PRD 验收项 |
| --- | --- | --- |
| Task 0 | 既有 PRD 与匿名探测 | V-01～V-07、AC-16 的外部证据边界 |
| Task 1 | Task 0 协议判断 | AC-02、06、07、11、12、13 |
| Task 2 | Task 0；与 Task 1 文件不重叠 | AC-04、09、10 |
| Task 3 | Task 1 和 Task 2 的接口／失效机制 | AC-02、03、04、05、06、11、14 |
| Task 4 | Task 3 合同；可先只读审查现有页面 | AC-01、06、07、09、12、13、15 |
| Task 5 | 全部代码任务结束 | AC-01～16 与真实链路剩余项 |
