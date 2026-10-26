# Plugin Host API v1 执行合同

状态：P03a/b/c/d/e 基础能力及P04a预分配凭据扩展已本地实现并通过独立审查；P07生命周期协调、实际图准入及只读能力补强也已本地审查PASS；原生合成输出也已通过本地验证及独立审查；业务tool/hook和P14只读迁移能力仍待实施验收。具体证据见execution-status.md。SDK事实见host-integration-research.md；候选能力不能仅凭本文存在就视作已实现。

## 绑定

Harness 1.0.4 的 LoadedPlugin.context 确实保存同一个PluginContext。公开辅助函数 `register_host_setup(ctx, callback)` 只把注册回调附着到可扩展的ctx；Octop PluginManager在SDK load完成后绑定应用作用域facade并调用回调。SDK原有setup仅做tool/hook注册，不读数据库。不要patch SDK类、不导入别的插件合成模块、不复制loader。

Host在server创建manager时建立，services通过provider逐次取得；DB deferred/rebind不能捕获旧pool。独立CLI仅安装包而无runtime Host时保持不可执行Host能力，不伪造可用服务。普通未使用新callback的插件兼容。

## DTO 与 facade

- `PluginActor`：不可变的实际user_id/admin/permissions、agent_id/thread_id/operation_id可选、locale。由HTTP认证或可信turn适配构造，LLM参数不能构造身份。
- `RecordWrite(namespace, logical_key, owner_user_id, payload, expected_revision)`：JSON payload、创建rev0/更新精确rev，owner不可变；Host强制绑定plugin_id和保留namespace限制。
- `CredentialActivation(credential_id, owner_user_id, expected_revision)`：仅staged→active，和RecordWrite在同事务；失败无部分提交。
- `get_record/list_records`、`stage_credential`、`resolve_credential`、`commit`、`release_staged_credential` 均async；同步SQL/加密经run_in_executor。读密钥只能active/plugin+owner匹配；释放只影响staged，active的受管清理由显式后续管理完成。
- facade包含plugin_id、Host API=1、当前generation；停用/关闭/重载后旧facade所有动作拒绝。不暴露RepoBundle/conn/SQL/Request。

记录的owner_user_id是创建者/不可变管理归属，并非该插件所有记录的通用读取分区：同一企业插件必须能读取管理员创建的共享连接、员工grant和专家binding。Host低层记录能力只交给受信任插件，不能作为HTTP或LLM直接CRUD开放；业务actions必须用可信actor校验各namespace的资源权限，尤其sessions/evidence/thread归属。SQL仓储按plugin/namespace/key过滤，不在SQL层复制业务授权。凭据则额外按plugin+owner+opaque ID匹配；插件解析当前已授权连接的凭据归属后才能请求使用。

## 注册

注册发生于callback、允许数据库尚未就绪；只登记描述与异步handler，不执行读写。

- `register_service(api_version, factory, required_plugins)`：版本化服务工厂；required_plugins指定配套插件精确兼容版本。循环配套只做就绪集合检查，不递归构建服务。
- `register_action(name, input_model, output_model, handler, summary, permission)`：只允许固定动作名，Pydantic输入输出，handler显式取得可信actor。permission为authenticated/admin/agent_owner；插件仍负责五层业务校验。
- `register_resource(handler)`：opaque ref由插件解析，每次actor和线程再鉴权；仅返回受控数据/stream，不把供应商signed URL返回浏览器。
- P07b2（本地实现/独立审查PASS） `register_read_only_tools(tool_names: Sequence[str]) -> None`：仅在真实SDK context的Host setup中声明本插件原始注册名，拒绝字符串伪sequence、空/未知/其它context名称及setup后调用；复制冻结输入，随context/generation失效。实际成功图按工具对象对应关系转为最终sanitized运行名，仅启用且实际挂载者贡献原生Ask/Plan只读能力；公共stream/call的extra-read仅取宿主canonical列表与当前声明，不接受调用者扩权。不新增DB、业务名称或SDK补丁，本地实现见P07b2报告，仍需兼容Host构建和最终门禁。
- 不接受任意路由、Python属性路径或同步网络handler。

## HTTP 和上下文

`POST /api/plugins/{plugin_id}/actions/{action}`：envelope `{api_version:1, context?:{agent_id?,thread_id?,operation_id?}, payload:{...}}`，extra字段拒绝。context IDs仍不可信，宿主校验Agent访问/owner及thread所属，actor.user_id始终取current_user。GET动作目录公开注册summary/schema和权限描述，只供已认证可访问用户；不能返回secret/配置正文。

动作权限来自注册metadata而不是客户端permission。输出按output_model再验证。Validation/permission/notfound/conflict/unavailable使用通用宿主错误，业务错误码和文案归插件。资源API同样认证，禁止用户指定任意远程URL。

settings.get / binding.get / session.get / search.get为页面只读bootstrap，返回当前actor有权读取的脱敏数据和可操作能力；连接和搜索Key永不读取到前端。管理动作与LLM工具分开。

## 必需扩展与生命周期

宿主保存保留namespace required-extension记录，普通facade不能访问/修改。入站和执行接缝在hook外，绑定不依赖客户端plugin_context。缺任一配套、兼容版本或真实主体都拒绝企业查询。

disable/uninstall/reload先使旧generation失效并取消在途任务，再注销/替换callback；关闭所有facade。完成写入前再次核对generation/operation终态。binding和历史数据保留；解绑只允许显式管理。

生命周期失效与存储副作用以数据库实际 COMMIT 为线性化边界：失效完成后，旧 worker 不得再提交。不能只在 transaction body 的最后检查，因为 context manager 随后才真正 COMMIT。此前已提交的数据保留；取消不等于撤销已完成事务。可能等待同步提交锁的生命周期操作在异步入口经 executor 执行。

版本化服务代理在每次调用前后校验服务自身和依赖的精确版本、generation；同版重载也使旧引用失效。配套 hook 通过 facade.resolve_service 取得公开服务，不能访问 facade 私有 Host 或合成模块。

### P03d 可信执行合同（本地实现/审查通过）

以下公开接口已由P03d实现并经三轮修复后独立审查通过。facade.commit 可同时接受 RequiredExtensionWrite，将业务 binding 和宿主保留记录原子提交。每个 facade 只变更自身声明；解除要求是受管理权限与 revision 约束的显式操作，不能覆盖其它声明。Agent 和线程要求取并集。

专用管理读能力 `await facade.get_requirement(actor, agent_id, thread_id=None)` 只返回自身声明的required_plugins与Host revision，不存在为revision0。它核对当前管理权限，供binding保存/恢复取得正确CAS前提；普通records仍不能访问保留namespace，业务record与Host声明revision不可假定相同。

认证入口产生仅进程内可用、绑定当前应用 Host 的 ticket，携带不可变 actor、Agent/线程和通用 plugin_context 副本。ticket 不能从 JSON、数值 subject 或 request.user 恢复；传入普通 JSON 的 actor 不构成身份。Gateway 在验证 ticket 后建立 ContextVar scope，公共 stream/call/resume_hitl 接缝再次校验持久要求。facade.current_execution 只读取该次 scope 的身份和本插件上下文；没有可信 scope 就拒绝。ticket 不进入 checkpoint、历史或日志。

`await facade.current_execution()` 每次重新读取用户当前角色/permissions/disabled，并校验当前Agent和线程访问；ticket的权限字段只是签发时快照，不能在整个turn内反复当作最新授权。返回的PluginExecution含新actor和插件上下文副本，不暴露repo。这样用户在生成期间被撤权后，下一次工具授权会看到当前状态。

PluginExecution同时提供服务端execution_kind（turn/resume/continuation），公共resume接缝固定为resume。插件据此拒绝将重启后的旧恢复隐式变成新查询；业务operation状态仍在插件内校验，客户端不能指定身份或execution kind。

P07b2（本地实现/独立审查PASS）：`await facade.current_execution(*, optional=False)`保持默认强制身份；显式optional=True仅在ticket不存在时返回None，present但foreign/失效/撤权仍抛错。PluginExecution增加只读required_plugins map，取本次fresh authorize_execution返回并冻结；空map的已认证普通turn可在解析业务服务前保持兼容。None不授予任何业务权限；全局middleware不得捕获任意denied并当成无ticket。既有三参数构造保持兼容，不能把测试默认空map当真实授权证明。

Dashboard 入队/steer/HITL 和公共执行接缝都检查。已有 session_key 解析到线程时同样核对 user/Agent。企业父 turn 的跨 Agent 子调用本阶段拒绝，防止调用未绑定 Agent 绕过；普通无绑定团队保持原行为。不同应用、共享 Agent 并发员工、取消后上下文都不得串用。实施与入口验证记录于 P03d brief/report。

### P03e 只读来源 revision 前提（本地实现/审查通过）

为已评审session.save提供RecordCheck(namespace, logical_key, expected_revision)，facade.commit在同一事务核对checks后执行全部变更。0代表记录缺失；检查不更新来源payload/revision/timestamps，错误保持T0。命名空间仍绑定当前插件，不允许保留namespace。

SQLite复用现有BEGIN IMMEDIATE。PG仅在有checks的短事务中由repo先获取固定SHARE ROW EXCLUSIVE表锁，再检查/写入并持有到COMMIT，覆盖不存在行被并发创建；成本是条件事务串行化。[PostgreSQL锁文档13.3.1](https://www.postgresql.org/docs/current/explicit-locking.html)说明该锁与数据修改锁冲突且自互斥。SQL不进入Host或业务插件，实库PG并发测试未完成前不记验收PASS。

## 分批接口交接

P07b3原生输出验证（本地3测试/独立审查PASS，零宿主产品增量）：真实SDK messages通道已把合成AI状态写入投影为native token，模型调用0、聊天/IM文本一次且call/history单条。先前octop.assistant_text.v1候选协议及额外转换器已撤回，未形成可用API；插件直接使用原生合成AI state+jump_to=end，不再发送重复writer事件。普通模型和custom保留原行为，不从全历史快照重放。最终插件业务行为仍需P07c真实集成验收。

P03a仅SQL/DI；P03b实现DTO、存储/凭据facade与纯注册/生命周期状态测试；P03c接Manager/server和认证API；P03d接可信turn、required bindings与执行门槛；P03e补足有实际session消费者的原子来源前提。每批独立测试/审查，不把局部完成登记成整个P03完成。

P03d团队接缝：实际ask_agent经通用异步middleware fresh授权，实例submit在enqueue前消费Host/父/目标/source-thread匹配的一次性permit，再清除父ticket；裸同步内部SDK submit无准入拒绝。每job和fallback再检查实际目标，已知target_thread_id精确匹配Agent级与该thread，未知thread按可能绑定拒绝。普通未绑定公开团队工具链保持兼容，企业跨Agent委派本阶段不支持。

P03e的RecordCheck/checks已交付公开facade并通过本地审查；验证后的输入snapshot控制executor中的锁/校验流程，调用者改动原列表不改变提交前提。SQLite独立Pool并发已测，PG仍仅固定锁SQL/顺序结构证据。

### P04a recovery stage contract（本地实现/独立审查通过）

`await facade.stage_credential(owner_user_id, payload, *, credential_id=None)` 增加可选预分配 opaque ID。插件在服务端产生 UUIDv4，先通过公开 commit 把该 ID 和真实 T0 持久写入 intent/recovery，随后 stage 使用该精确 ID；后续登记和清理均失败时仍能定位已暂存凭据。指定 ID 必须 canonical UUIDv4，严格拒绝重复，不能覆盖、复用或激活已有凭据；默认调用保持原生成方式。repo 只承担 insert SQL，Host 负责校验/加密/插件+owner隔离/原有worker lease，不新增DB迁移。ID从不成为前端输入或输出。初始intent commit同时检查connection revision（新建为0），确保备份对应首次恢复元数据mutation前的真实T0。

P07b2 Fix1补充（本地实现/独立审查PASS）：SDK客户端`tool_search`仅搜索已过滤工具元数据，纳入canonical只读工具；plugin/MCP工具sanitizer保留此runtime名，外来同名注册重命名。允许发现不授予被发现工具执行权。业务策略只可识别真实SDK-owned discovery callable并保持自身inventory过滤，不能仅信字符串；不将该metadata工具混为企业外部搜索。




P07c2a输入前提（本地实现/独立审查PASS，6项测试）：Dashboard服务端在当前Human.additional_kwargs写`octop_user_input={version:1,text:当前InboundMessage.text或空字符串}`，保留原始网关规范化文本，不混附件提示/正文或本地化占位，客户端同名metadata不能覆盖。可按既有Human元数据规则持久化，不声称transient。仅输入来源，不授权；身份/范围仍由current_execution及业务源检查。后续P07c2受控插件须只消费当前Human的严格marker，缺失/空值不回看历史；该消费者尚待实施，普通会话和已禁用bound旁路边界不变。
