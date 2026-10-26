# P03 Host 接缝研究

范围：只核查仓库 `AGENTS.md`、`execution-details.md` 和实现所需源码。以下“已证实”描述当前实现；“提案”是给 P03 Host v1 的设计建议，不代表代码已实现。

## 结论

Octop 的插件加载入口集中在 `PluginManager._load_plugin_dir()`，启动时于数据库绑定前调用；Harness 1.0.4 的 loader 在 SDK 内部创建 `PluginContext` 后同步执行 `setup(ctx)`。不改 `.venv` 的最小低耦合接法，是 Octop 包装加载结果、在 `setup()` 返回后把具名 facade 绑定到 `LoadedPlugin.context`，供插件闭包保存的工具/钩子在后续执行时使用。该接法不支持 `setup()` 本身调用 Host。若 P03 要求 setup 阶段即能调用 Host，就需要 Octop 自有 loader（复制或适配 SDK loader）或未来 SDK 扩展点；运行时 monkeypatch SDK 的 `PluginContext` 全局符号会引入并发及升级风险，不建议作为正式契约。

建议接口拆为 `src/octop/infra/agents/plugins/host_contracts.py`（纯 DTO/Protocol，不依赖 API 或 SDK）与 `host.py`（绑定插件身份、服务、授权及停用态的实现）。插件通过已注入的 `ctx.host` 获取 facade；插件保留闭包里的 `ctx`，只在工具/钩子被执行时访问该属性。Octop 在 loader 返回后、发布/重载该插件前完成注入。此为提案；Harness 的 `PluginContext` 当前没有 `host` 属性。

## 已证实的接缝

### Loader、setup、插件停用和卸载

- Octop 在 `PluginManager.load_installed()`、`load_missing()` 及安装路径调用 `_load_plugin_dir()`；后者仅先装 legacy import alias，随后直接委托 Harness `load_plugin_dir()`：`src/octop/infra/agents/plugins/manager.py:78-82,411-481,643-669`。
- 服务启动时先创建 manager、seed 插件并 `load_installed()`，之后才打开/迁移控制面数据库并构造 `SharedServices`：`src/octop/infra/server.py:397-465`。因此 setup 阶段不能假定 DB 或用户/线程身份已就绪。
- Harness 1.0.4 `load_plugin_dir()` 导入插件模块，创建 `PluginContext(manifest, source_path)`，调用 `setup(ctx)`，再 `register_loaded(ctx)`；后者将同一个 context 存入 `LoadedPlugin.context`：`.venv/Lib/site-packages/octop_harness/plugins/loader.py:45-87`、`plugins/context.py:163-167`。`PluginContext` 有 `plugin_id`、工具/钩子注册等能力，但没有 Host 属性或 loader 回调：`plugins/context.py:18-48,109-160`。
- `PluginManager.set_enabled(False)` 写入全局配置、缓存已有工具/技能目录后调用 `unload_plugin(plugin_id)`；API `PATCH /plugins/{plugin_id}` 随后刷新 channel 注册并 `reload_all()` agents。启用时则从磁盘加载：`manager.py:549-593`、`src/octop/api/routers/plugins.py:260-290`。
- `uninstall()` 先 `unload_plugin()`、清缓存，再递归删除插件目录；API 随后刷新 channels 和 agents：`manager.py:753-762`、`plugins.py:293-303`。Harness 的 unload 从进程 registry 移除插件并删除其模块及子模块：`.venv/Lib/site-packages/octop_harness/plugins/loader.py:102-113`。
- `load_installed()` 清空全局 registry 后重载；插件 reload、安装也会重载 agent：`manager.py:411-450`、`plugins.py:92-123,138-180`。Host facade 若保存 manager 创建时的 DB/service 对象，DB rebind 后会过期；`AppRuntime.replace_services()` 会替换多个服务的持久层：`server.py:296-313`。

### 身份、agent/thread 所有权入口

- HTTP 身份入口是 `api.deps.current_user`，从 request 的 JWT / request state 解析用户；`get_server` 返回当前 server，`require_permission()` 是模块权限依赖：`src/octop/api/deps.py:69-75,155-170,183-204`。Host 的产品动作若来自 API，应由适配层把已认证用户显式交给 facade；不可让插件伪造 `user_id` 就获得授权。
- Agent 行级访问复用入口为 `require_agent_row()`，其组合了存在性、用户所有权/共享权限、admin 及 `as_user` 规则；写操作常用 `require_agent_owner_row()` / `assert_agent_owner()`：`src/octop/api/common/agent.py:18-32,39-75`。workspace 专用适配器为 `require_running_workspace()`，内部同样先做 agent 授权：`src/octop/api/common/workspace.py:37-67`。
- Thread 所有权检查目前是 chat history router 私有函数 `_require_thread()`：先 `require_agent_row()`，再检查 thread 存在、属于同一 agent、且 `row.user_id` 等于当前或经 admin 指定的 effective user：`src/octop/api/routers/chat/history.py:87-99`。它不能作为 infra Host 的直接依赖，因为仓库边界禁止 infra 导入 API；建议将“从可信执行上下文取得 identity + 按 agent/thread 做授权”放在 API/turn adapter，或把可复用策略下沉为不依赖 FastAPI 的 infra service，再由 API 与 Host 调用。不能只校验 thread ID 与 agent ID。

### 数据库事务和方言

- `DatabasePool` 协议公开 `dialect`、`connect()`、`transaction()`，两者均是 context manager，yield 底层连接：`src/octop/infra/db/pool.py:21-30`。
- SQLite `transaction()` 在锁内执行 `BEGIN IMMEDIATE`，yield `sqlite3.Connection`，成功 commit、异常 rollback：`pool.py:60-77`。PostgreSQL 通过 pool connection 和 `conn.transaction()`，yield `_PgConnectionProxy`：`pool.py:115-127,166-173`。proxy 提供 sqlite 风格 `.execute()` 并将 `?` 改写为 `%s`；这个替换器未理解 SQL 字符串字面量：`pool.py:15-18,115-123`。
- `SharedServices.db` 是到 `RepoBundle.db` 的属性，且 RepoBundle 暴露多组 Octop 私有 Repo：`src/octop/infra/db/services.py:40-99,100-111`。这些是内部适配细节，不应成为插件公开契约。若 Host 需要多表原子写入，可内部用 `services.db.transaction()`；不要把连接、Repo 或任意 SQL 能力透给插件。

## 最小 Host v1 提案

**文件与职责（建议名）：**

- `host_contracts.py`：`HostFacade` / `HostTurnContext` 等 Protocol 与不可变 DTO。只表达稳定业务动作，例如按已授权上下文读取 agent/thread 标识、持久化插件 namespace 下的 revisioned setting、凭据 opaque ref 解析、受控资源调用；不暴露 `OctopServer`、FastAPI `Request`、Repo、数据库连接、SQL 或 Harness 类型。
- `host.py`：`PluginHostFacade(plugin_id, service_provider, lifecycle_registry)` 实现。以闭包固定 plugin_id/namespace；每次敏感动作向当前 `OctopServer`/`SharedServices` 获取依赖，重新确认插件启用/注册状态，并执行作用域与权限校验。异步网络/外部动作入口应可传取消信号并在停用后拒绝新调用。
- 插件接入：Octop 的 `_load_plugin_dir()` 包装器取得 `LoadedPlugin` 后，取 `loaded.context` 并绑定 `host` 属性，再将其视为可用于后续执行的插件。插件 setup 只注册闭包，例如 `ctx.tool(..., lambda ...: ctx.host....)`；Host 不在 setup 中运行。若插件需要正常类型检查，应在插件自身定义 `Protocol`/cast，或 P03 后续提供公开、稳定、可类型检查的 `octop_host` package facade。`ctx.host` 作为支持路径必须由 Octop 文档及版本化接口承诺，当前 Harness 不提供它。

**生命周期建议：**

1. loader 完成 setup → Octop 立即注入 Host → registry 发布/Agent 构建；注入失败就拒绝加载该插件，不能让工具悄悄无 facade 运行。
2. disable/uninstall/reload 入口首先将 `(plugin_id, generation)` 标为 inactive 并拒绝新动作，然后 unload/删除/替换 registry，再刷新 channel 与 agents。可由 `set_enabled(False)`、`uninstall()`、`load_installed()`、强制安装覆盖共同调用一个幂等的 host lifecycle invalidation 方法。
3. Host 每次动作校验 generation 与 enabled/on-disk/registry 状态；异步动作在开始、外呼前和提交前再检查取消/世代。卸载不能撤销已经完成的远端副作用；至少要保证旧 facade 无法提交本地结果或发起新外呼。
4. services 通过动态 provider 获取，覆盖 setup deferred DB 和 `replace_services()` rebind；不在插件闭包中捕获老 DB pool。

上述生命周期协调属于建议。当前实现确证 unload + Agent reload，不保证已运行的插件异步操作取消或停止，也没有通用插件 Host invalidation hook。

## 设计风险和需 P03 定案点

- **setup 注入时序：** 事后给 `ctx.host` 可满足被注册回调之后调用，不能让 `setup()` 直接完成 Host 注册/读取。若真实插件需求依赖 setup 阶段 Host，最小接法需改为 Octop 自己装载并调用 setup，维护 SDK loader 行为差异；应先确认需求再选。
- **共享进程与越权：** Python 插件代码和 facade 在同一进程内，facade 不是安全沙箱。凭据、DB、网络只能经受限业务方法；插件仍可直接 import Octop 内部模块，运行时 API 只能提供约束而不能防恶意插件绕过。部署/信任模型需承认这一点。
- **线程身份语义：** Harness 工具的运行参数不应被当作认证身份。turn context 应由 Octop 的 chat/agent 执行边界构造，并绑定 user/agent/thread；请求型 API 再从 `current_user` 派生。system/cron/IM turn 需显式主体和权限策略，不应默认 admin。
- **停用竞态：** 删除 registry 不会移除已构造 agent graph 中捕获的工具引用，也不能取消网络请求。当前 API 后续 `reload_all()` 是可用的刷新动作，但并未证明并发 turn 的取消或迟到写入安全。
- **事务范围：** 插件不应自带 SQL；Host 方法内部用现有 pool transaction。SQLite 的 `BEGIN IMMEDIATE` 会占用单连接写锁，facade 不得跨 await 持有事务。两方言提交语义应以 Repo/测试为准，不能把 psycopg proxy 当 sqlite3 的完整替身。
- **接口命名/分层：** `host_contracts.py` 放纯 contract 可降低耦合；`host.py` 放实现且避免 `infra → api`。若 thread/agent 访问规则仍留在 router 私有函数，则提案需要新增中性领域策略入口，不能在 `infra/agents/plugins/host.py` 反向 import `api.common.agent` 或 `api.routers.chat.history`。

## 本次未覆盖

未读完整 PRD 或实施计划；未修改产品代码、SDK、测试，也未执行测试或探测外部服务。本报告是源码接缝调研，不是 P03 Host 契约的最终批准规格。
