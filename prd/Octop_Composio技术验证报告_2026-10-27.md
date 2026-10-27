# Octop Composio 技术验证报告（M0）

## 当前真实验证状态（实施阶段更新）

2026-10-27，隔离 Octop 测试环境通过产品 OAuth 流程保存了一个个人 Composio 连接，MCP SDK 的 `initialize` 与 `tools/list` 成功，当前上游返回 11 个工具。页面显示已认证、MCP 可连接；未验证远端账户身份，因此没有显示邮箱。后续曾观察到无效 state 回调页，但已保存连接仍有效，重复访问／页面回跳原因尚待核查，不能仅据该错误页断言首次交换失败。

实际 `COMPOSIO_SEARCH_TOOLS.inputSchema` 使用 `queries[*].use_case`，并支持 `session.generate_id`。以公开关键词 GitHub 调用成功，返回 MCP text content 包裹的 JSON：`data.results[*].toolkits` 为字符串数组，`data.toolkit_connection_statuses` 为对象数组。当前页面解析器漏掉字符串标识，正在据真实结构修复；这证明搜索协议可调用，尚不能宣称页面搜索已验收。

| 验证项 | 实际结果 |
| --- | --- |
| 个人 OAuth、token 交换及服务端保存 | 已成功形成一个个人连接，凭据走产品加密存储路径 |
| MCP initialize / tools/list | 成功，11 个工具 |
| 应用搜索工具调用 | GitHub 搜索成功，页面结果解析待修复 |
| token 刷新、应用授权、业务只读调用 | 尚未验收 |
| 两个真实账户隔离、受控写入 | 尚未验收 |

下文保留的是最初匿名 M0 探测时的证据快照；其中“未登录／schema 未知”仅描述当时状态。最新完成度以本节与[实施验收记录](Octop_Composio实施验收记录_2026-10-27.md)为准。未记录 token、授权 code、完整一次性链接或原始账户响应。

## 2026-10-27 实施阶段补充证据

官方文档 HTML 提供 `rel=alternate` 的 Markdown 地址。普通页面抽取未展开的参数，在以下官方 Markdown 端点中可读取；这补充了参数文档，不代表已经取得当前个人账户的真实 `tools/list` schema：

- [Search Tools 参数文档](https://docs.composio.dev/toolkits/meta-tools/search_tools.md)：`queries` 数组项包含必需的 `use_case` 字符串，`session` 用于关联搜索工作流；返回结构记载 `data.results`、`data.toolkit_connection_statuses` 和 `data.session`。
- [Manage Connections 参数文档](https://docs.composio.dev/toolkits/meta-tools/manage_connections.md)：使用 `toolkits` 数组，可选 `reinitiate_all` 和 `session_id`；缺少连接时会启动授权，因此不能用作自动状态轮询。

实现应将文档中的已知形态与每次认证后获取的 schema 比对，不再使用猜测的 `action=create/list` 参数。不能把该补充文档视为个人账户实际搜索、授权或邮箱身份已通过验收。

本轮用户已确认可以配合一个个人账户登录。已启动隔离本地 Octop 与前端测试入口；真实授权证据在完成后写入实施验收记录，不记录 token、授权 code 或完整一次性链接。

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-10-27 |
| 需求 | [PRD 正文 v1.1](Octop_Composio公网连接扩展_需求PRD_v1.0_2026-10-27.md) |
| 匿名探测 | `uv run python scripts/composio_probe.py discover`，本轮执行一次 |
| OAuth 登录 | 未执行；未注册客户端、未登录账户、未请求或查看用户凭证 |

## 结论与证据边界

**个人 Composio Connect 路线可继续做独立代码修复和自动测试；个人账户 OAuth/MCP 兼容性及完整 P0 验收仍未确认。** 匿名 discovery 只证明端点元数据可读取以及未授权 MCP 返回 401。它不证明动态客户端注册、浏览器回调、token 刷新、工具调用或多用户隔离在真实账户下可工作。不得据此称“个人协议完整兼容”。

官方 Connect 文档把 `https://connect.composio.dev/mcp` 描述为适用于已有 MCP 客户端的共享入口，并列出七个元工具；文档页面没有提供 JSON 输入 schema。需要实际认证 `tools/list` 的 `inputSchema` 才能确认调用参数。项目 sessions via MCP 是另一个面向应用构建者的路径，不能作为个人 Connect 行为的证明。[Composio Connect](https://docs.composio.dev/docs/composio-connect) · [Using sessions via MCP](https://docs.composio.dev/docs/sessions-via-mcp)

## 匿名实测结果

`discover` 于 2026-10-27 成功：issuer 为 `https://connect.composio.dev`，resource 为 `https://connect.composio.dev/mcp`，authorization endpoint 为 `https://connect.composio.dev/oauth/authorize`；token 与动态注册端点分别位于 `https://login.composio.dev/oauth2/token` 和 `/oauth2/register`。元数据声明 PKCE `S256`、token endpoint auth method `none` 和 scopes `openid profile email offline_access`。未授权 `POST /mcp` 返回 401，响应带指向 protected-resource metadata 的 `WWW-Authenticate`。

这仅确认匿名端点发现。虽然 metadata 公布注册 endpoint，并声明客户端认证方式 `none`，本轮没有 POST 注册请求，因而注册是否会接受匿名动态注册、其必填字段/限制及注册后的 redirect URI 约束均**未知**。没有使用账户或 OAuth 凭证。

## V-01～V-07 状态

| 项 | 当前结果 | 证据边界 / 待确认 |
| --- | --- | --- |
| V-01 OAuth 发现与注册 | **匿名发现部分确认** | endpoints、S256、`none` 和 scopes 如上；真实 DCR、授权登录、回调、换 token 和刷新均待账户持有人验收。 |
| V-02 MCP 会话与工具列表 | **未知（认证门控）** | 匿名 MCP 401；已修订 probe 会在拿到 access token 后走 initialize / initialized / tools-list 流程，但本轮未执行。须以标准 MCP 会话确认版本、session 行为及响应。 |
| V-03 多账户隔离 | **未知（认证门控）** | 需两个真实 Composio 账户及 Octop 用户执行端到端隔离检查。 |
| V-04 元工具及只读边界 | **名字/高层职责已由官方文档确认，schema 未知** | 官方列出 `COMPOSIO_SEARCH_TOOLS`、`COMPOSIO_GET_TOOL_SCHEMAS`、`COMPOSIO_MULTI_EXECUTE_TOOL`、`COMPOSIO_MANAGE_CONNECTIONS`、`COMPOSIO_WAIT_FOR_CONNECTIONS`、`COMPOSIO_REMOTE_WORKBENCH`、`COMPOSIO_REMOTE_BASH_TOOL`。Manage connections 明确含 create/list/rename/remove，属于可能产生写入的工具；不能用于自动状态刷新。官方文档没有 JSON schema；搜索、状态查询参数及工具级只读属性待认证 `tools/list` 与受控验证。 |
| V-05 远端账户身份 | **未知** | scopes 中有 `openid`/`email` 不证明 token endpoint 必返回 id_token、email claim 已验证或邮箱与个人 Connect 账户绑定。未经验证的 JWT payload 不能当可信身份。 |
| V-06 已连接应用/授权管理与远程工具暴露 | **部分文档可知，其余未知** | 官方 Connect 描述按需授权和远程 Workbench/Bash 元工具；具体账户应用列表、状态只读查询、选择行为及 Octop 是否暴露这些能力必须由实际 schema/认证响应确认。Octop 不应因此向用户宣称可固定账户选择。 |
| V-07 回调与安全约束 | **本地设计部分确认** | Octop OAuth helper 对 localhost 回调有例外并要求公网 HTTPS；当前 probe 默认 `127.0.0.1`。真实 Composio 回调接受情况、事务生命周期及安全边界仍待验收。 |

## 可独立推进与真实验收门槛

可以继续：修复 MCP 生命周期/transport 使用、元工具参数猜测、未知状态降级、owner 隔离、凭证保护、授权事务和 UI 声明等本地代码缺口；为这些行为编写 mock/集成自动测试。此类测试只说明 Octop 代码行为，不证明 Composio 服务端接受该流程。

真实账户操作前不能宣称完成：DCR 可用、授权回调和刷新成功、初始化与 `tools/list` 可用、应用搜索和应用授权、远端身份/email可信、账户间隔离、只读应用调用及完整 AC-02/07/08。AC-08 必须由账户持有人使用自己的账号验收。不得自动调用 manage/create/remove、应用授权或任何业务写工具。

## 工具调用约束（供实现）

1. 不硬编码猜测 `COMPOSIO_SEARCH_TOOLS` 的 `{query: ...}` 或 `COMPOSIO_MANAGE_CONNECTIONS` 的 `{action, toolkit}`；只采用认证 `tools/list` 返回的 schema。
2. 不将 `COMPOSIO_MANAGE_CONNECTIONS` 作为只读状态接口；官方说明其具备连接创建、重命名、删除能力。缺少可确认的只读状态 API 时返回 `unknown`。
3. 页面加载、连接检查和轮询只能执行经确认只读的操作；远程 workbench/bash 与 multi-execute 均不得自动调用。
4. 仅凭 OAuth metadata 或未验证 id_token claim 不显示 Composio 账户邮箱。
5. MCP 请求必须遵循协商出的协议生命周期；对传统握手版本执行 initialize、initialized 后再调用工具。实际服务端协议/session行为须从认证连接确认。参考 [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-03-26/basic/lifecycle)。

## Probe 凭证处理

`scripts/composio_probe.py oauth` 只向终端给出脱敏客户端标识、token 存在布尔值和工具名；不打印、持久化 access/refresh/id token 或 client secret。不再输出未验证解码的 id_token email。请在专用测试环境运行交互 OAuth；不要把完整浏览器 URL、token 或未经脱敏响应粘贴进报告。

## 下一步账户持有人验收

在用户可操作且确认时运行 `uv run python scripts/composio_probe.py oauth --redirect-uri http://127.0.0.1:8765/callback`，记录脱敏结果：注册是否成功、回调/换 token/刷新结果、MCP 初始化协商版本、工具名和 schema（去掉任何敏感值），再以第二个账户检查隔离。只调用经过 schema 和语义确认的只读工具。当前没有账户授权证据，因此本报告不标记完整 P0 通过。
