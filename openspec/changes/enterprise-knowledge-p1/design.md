# Design

## Context

基线及源码核查见 execution-details.md 与 PRD。已证实 SDK kind 互斥，现有 PluginContext 不包含存储/动作/身份服务；核心新接口均为本变更新增。业务 UI 另见 enterprise-knowledge-plugin-ui。版本 v21 已可能应用，新增迁移 022。

## Goals / Non-Goals

**Goals:** 固定有限 Host facade；使数据和凭据事务可测；插件独立承担业务；每次调用校验而非依赖 UI 开关。

**Non-Goals:** 修改安装目录 SDK、任意路由注册、通用页面平台、新队列、新 WeKnora 或上传/删除能力。

## Decisions

1. 两个现有 kind 包，包内相对导入。SDK确实保留LoadedPlugin.context；公开register_host_setup(ctx, callback)登记回调，Manager在SDK加载完成后将应用作用域facade交给回调，卸载时注销。setup本身不访问DB；不patch SDK、不复制loader或跨插件导入合成模块。
2. 通用表 plugin_records 与 plugin_credentials。记录唯一键 `(plugin_id, namespace, logical_key)`；row.id 为公共字符串、row.pk 为整数。仓储只负责 SQL，domain Host 负责 namespace、加密和能力校验。底层支持传入同一 transaction connection，避免嵌套事务。
3. 只读revision前提通过RecordCheck(namespace, logical_key, expected_revision)进入同一commit；0代表缺失。SQLite复用BEGIN IMMEDIATE，PG在第一条记录操作前由repo获取固定SHARE ROW EXCLUSIVE表锁，覆盖缺失记录并发创建。只在带checks的短事务串行化，不递增被检查来源版本；实际PG验证仍需外部环境。记录 CAS：expected_revision=0 为创建，后续写入要求精确正整数 revision，owner 不可悄悄改变。多写入及 credential activation 在同一事务失败时全部回滚。凭据 staged/active 两态，revision 和 opaque public ID；Fernet 密钥复用 SecretRepo。
4. 新 API 为 `/api/plugins/{plugin_id}/actions/{action}` 和资源路径，Pydantic envelope；身份来自 current_user，plugin payload 不接管 actor。registered input/output schema 与权限说明对人可读。
5. Host 保存 reserved namespace 的 required-extension 记录，普通 plugin facade 不可写。入站/执行接缝位于 hook 之外；无插件上下文的旧线程仍会查 required bindings。每个 facade/generation 在停用后失效。
6. 企业 DTO、五层交集、网络、游标、operation/approval/evidence 和文案均在 knowledge 插件；policy 包只注册 middleware 并通过 Host 版本化服务引用调用。
7. 远程默认HTTPS；用户明确指定测试服务器后，可由服务端OCTOP_WEKNORA_TEST_HTTP_ORIGIN作精确origin例外，仅该来源且报告test-only，禁止客户端通用allow_insecure与重定向。请求用 AsyncClient，预算计数从发起起算，禁重定向和隐式 retry。P01 独立 probe 提供合成合同，实装兼容依赖实际部署证据，不把上游错误正文写日志。
8. 首个代码任务为 P01；随后 P03a 存储、P03b Host、P03c生命周期/API、P03d可信执行和P03e原子来源前提，然后 P04～P15 按依赖推进。执行 brief 与独立审查台账在 self-ignored .superpowers/sdd 中，持久设计与证据归 prd/evidence/enterprise-p1。
9. P07先补通用Registry coordinator与实际graph挂载准入，再接业务tool/hook。进程短锁只保护epoch/in_progress、Host登记与准入task tracking；async reservation不占executor等待长mutex。变更先发布stale、经executor保留真实COMMIT lease失效、跨loop drain旧任务清理，随后才在executor变更SDK/目录；请求取消不得提前释放仍运行worker的reservation。无runtime Host的独立CLI保留同步入口，host=None不能绕过其它app。Agent快照绑定实际成功构建的entry/context/过滤注册、配置与epoch，构建失败或中途变更不发布ready；全局同ID/version不能证明hook已挂载。不改SDK为应用隔离，不承诺多app独立安装隔离；代价为保守全局epoch失效和显式重建恢复。

10. P07b之后先补通用只读工具声明，再接企业tool/hook。受信任setup通过facade.register_read_only_tools(tool_names)声明同一SDK context自己的原始工具名；Host复制为generation/context绑定metadata，非setup、未知/外部名称拒绝。实际成功图把声明映射到启用StructuredTool及最终sanitized名称，处理重名后缀；禁用/未挂载/旧context不贡献能力。公共stream/call用宿主canonical只读列表加当前挂载声明覆盖调用者extra-read输入，沿用原生Ask/Plan/Craft限制。不patch SDK、不在core写企业工具名，不新增schema。resume仍按实际SDK协议验证，不虚构configurable参数。该接口已通过P07b2本地实现和独立审查，属于可信声明，不是恶意插件沙箱或员工数据授权。

11. 全局policy middleware不能把current_execution的全部denied当成普通无身份执行。P07b2增加显式optional检查：仅ticket不存在可返回None，present但foreign/失效/撤权仍拒绝；PluginExecution携带fresh authorize_execution已计算的required_plugins不可变副本。普通无ticket或已认证空required-map在解析业务配套服务前通过，绑定turn才解析严格配套服务。业务工具默认仍要求可信身份；None绝不构成访问权限。该能力不暴露ticket/DB、不新增持久配置；具体API尚待P07b2实施验证。

12. P07c的operation.prepare仅本地检查actor/thread/session和来源revision及隔离状态，不为准备动作发目录/探测请求。插件abefore_agent以VALIDATING->RETRIEVING CAS领取唯一检索批次，在首次模型前按本轮HumanMessage文本检索；不能拼接历史或无文本时偷取上一问。空/拒绝/失败通过真实SDK跳转或合成响应跳过inner model，不能在已流出tokens后才替换回答。显式search复用已登记证据，图aafter_agent成功后才GENERATING->COMPLETED；完成不代表交付确认。所有来源查询共用既有预算，当前操作观察的目录可供list重算本地交集，页面阅读仍是独立有界read。新turn不能重用已领取operation；取消/恢复/重启的完整终态在P13继续验证。具体source消息和Octop合成响应输出必须在真实无网络编译图验证，不能仅假hook证明。

13. P07b3真实无模型图纠正了先前仅检查updates的静态判断：安装版LangGraphProtocol的messages通道已把before-agent写入状态的AIMessage投为native token。公共AgentManager.stream→protocol→compiled graph→gateway实验证明模型调用0、可见文本一次、call/history单条。撤回额外custom-to-token转换和octop.assistant_text.v1候选协议，避免与原生消息双发；只保留原生链路回归，不新增核心接缝。P07c异步before-agent以合成AI state+jump_to=end输出本地化空结果/拒绝，不再发第二个writer事件。

P07b2时序补充：只读extra名单在fresh async授权后的最终短准入guard内取当前snapshot，并用既有Host task refs为声明来源增加临时generation lease，即使无持久required binding也在provider失效时取消旧invocation；不写持久requirements、不赋予员工权限。否则预备request名单与await之后选中的新图可能错配。无声明普通Agent保持原行为，普通旧/未知snapshot退回canonical名单。原始注册名与函数对应关系必须在sanitize前捕获，避免同函数不同alias误扩只读能力。

## Risks / Trade-offs

P04a恢复补充：`stage_credential(owner_user_id, payload, *, credential_id=None)`允许受信任插件预分配服务端UUIDv4并先持久写入恢复意图；默认仍由Host生成opaque ID。指定ID只允许新建，重复ID拒绝且不覆盖任何既有凭据。SQL保留在repo，校验/加密和executor/世代lease留在Host，不新增迁移或业务字段。恢复意图首次写入还须同事务检查实际连接revision，防止并发保存使T0过期。成本是小幅扩展通用stage接口；没有公开任意客户端指定引用能力。

- SDK 和注册表进程全局 → Host/context/generation 必须按应用区分，关闭与重新加载需有隔离测试。
- SQLite 同步事务 → Host async wrapper 经 executor；不把网络请求放进 DB 事务。
- 未确定远程响应/供应商 → 本地实现不作为实机兼容证明；有证据后修订 adapter 合同。
- 页面/API 不一致 → UI spec 先冻结行为与动作，再实现；接口漂移先更新规格。

## Migration Plan

P07保守全失效后的发起方恢复：显式变更成功后，在同一reservation/worker permit内重绑发起manager仍启用且实际当前的LoadedPlugin context，包括未替换的存活插件；关闭/卸载项不恢复，其它app不自动恢复。否则同一manager先装知识包、再装配套包，会丢失第一包的Host动作。精确已知ID可缩小实际Host失效集合，但epoch仍可保守使图快照过期。重绑不等于图已就绪，P07b成功重建前仍拒绝旧required执行；upload/market闭包也须明确携带发起manager，不能仅按boundmethod推断。

P06续读使用服务端UUID4 cursor记录，仅保存身份/证据及来源operation、connection/document/KB、当前来源/会话revision、有序内容摘要及下一块内字符位置，不持久缓存全文。固定有效期为首次读取cursor族创建后的30分钟，后续token继承相同过期时间，不增加配置旋钮。cursor允许重放同一逻辑窗口，但每次重鉴权、重扫摘要；失败不推进旧cursor。完整扫描无法在共同预算内验证时明确失败，不返回成功半页。摘要仅描述已观察的有序内容，不能证明上游跨HTTP原子快照或排除ABA。

evidence.read/original明确携带来源operation_id和evidence_id，并核对当前actor/agent/thread与持有证据。Host actor.operation_id只是关联引用，不能代替插件持有权校验。初次读取历史证据可按当前grant/binding/session重鉴权，但连接身份及配置revision必须匹配来源operation；源配置改动需重新检索，不能把相同文档ID静默解析到新地址。历史已存片段仍可按历史权限展示。当前原文能力未实装验证，original响应明确unavailable、不发resource token、不外呼；不能通过猜测preview/download路径制造可用代理。

P05b2动作细则：`connection.catalog`输入connection_id/expected_revision，管理员可读取凭据可见目录以配置初始空范围；员工仍按专家/线程当前四层交集，连接disabled或WEB_ONLY时零请求。`connection.probe`仅管理员，输入connection_id/expected_revision/component（LIST、SEARCH或READ），SEARCH另需query与非空knowledge_base_ids，READ另需knowledge_id与非空knowledge_base_ids；缺少组件输入返回BLOCKED且零请求。测试库限制在凭据可见与企业允许交集，不能用探测扩大企业范围。每次显式组件探测独立共享30/90/10预算，不复用员工会话操作。

分项记录保留observed_revision、checked_at（UTC）、status、固定error_code；不保存query、正文、Key或上游错误体，不递增连接配置revision。探测写入使用配置与恢复intent的RecordCheck及分项CAS，无自动重试。旧revision记录只可历史保留，不参与当前状态。enabled连接当前三项SUCCESS才READY；LIST或SEARCH空数据不能冒充非空SEARCH/READ证明。disabled连接总体保持DISABLED，但管理员可见当前分项结果；启用保存产生新配置revision，需重新探测。会话knowledge_ready只根据当前本地授权、非隔离和持久当前READY派生，读取能力不外呼，web_ready仍等待搜索实现。

v21→v22 新增通用插件表，双方言测试；安装两个默认关闭包→配置空授权→探测→逐用户授权→知识闭环→搜索模式→试点 Gate。回退插件数据版本和配套包，不降级主数据库结构。

## Open Questions

实际部署版本、CA 与原文能力；搜索供应商；真实 PG 和 Windows 试点证据。它们阻止对应实机验收，不阻止本地默认拒绝路径。

### Busy ingress and frontend context handoff

通用plugin_context按实际提交/队列项独立复制，不与composer_context展示metadata混同。当前native steer的PendingSteer只有text/content_blocks，不保存新plugin_context；本阶段受required extension约束的忙时追加采用独立下一轮，未支持逐消息扩展合同的steer由宿主拒绝，普通会话不变。HITL使用服务端原continuation再授权，不引入客户端恢复批准替换。具体薄桥接与组件行为由UI change定义，代码仍待P08/P13实施验证。

### P07b2 actual native deferred discovery refinement

Real compiled Ask/Plan graphs also require the installed SDK client `tool_search` metadata operation. Add that generic built-in name to canonical HOST_READ_TOOLS, retaining native execution checks for the tools it discovers. Reserve `tool_search` during all plugin/MCP runtime-name sanitization in AgentManager, so a foreign registration cannot inherit the canonical allowance. No SDK patch or business names. This generic capability passed P07b2 Fix1 independent review; P07c business consumption remains pending. P07c policy must permit only the actual SDK-owned metadata discovery callable over its already filtered inventory, never arbitrary same-name tools or an external search provider; real graph tests must prove that boundary. Existing user authorization to execute the plan covers this necessary implementation refinement.



### P07c1 producer freeze

P07c splits sequentially into the flat service/retrieval/action producer and actual tool/policy graph consumer; both remain required for task3.5/3.6. API1 factory returns only a narrow flat KnowledgeService with async methods; no public nested/raw ConnectionService, provider, session, operation or reader objects escape the generation proxy. Configuration and admin probes remain independent. Employee operation.prepare/evidence.list/read/original resolve the paired service. Prepare requires expected_session_revision and remains entirely local, including absent session revision0 CAS; it never claims remote effective scope. Initial turn retrieval uses fresh current_execution and server-validated prepared operation correlation, CAS-claims VALIDATING before one bounded provider batch. Success atomically commits evidence, observed knowledge-base metadata and GENERATING; EMPTY atomically commits observation and EMPTY without evidence. Existing Host RecordWrite/RecordCheck suffices, no new table/lock. Source/session/intent checks accompany final publication and graph completion.

Observation is a bounded list of kb_id plus source-provided optional name, nullable until retrieval. It never grants authority: cached search/list recheck current local sources/session/quarantine against the pinned operation, and cannot open a new provider. Search tool takes only optional knowledge_base_ids narrowing for the current question; it accepts no replacement query, actor or operation id. No plaintext question or question hash is persisted for reuse. Initial question comes from the current native HumanMessage at the policy before-agent hook. Runtime tool names and real policy consumer remain P07c2.

SearchHit retains source knowledge_title, or title from an attribution detail already requested because KB ownership was missing; missing title remains explicitly unknown (empty evidence title/optional transport title), never guessed from ids or file names. No extra request solely to invent a title. A provider-instance detail cache may reuse the same verified ownership/title with fresh allowed-ID validation; all requests keep the same10/90/30 budget. Verified nonnegative integer source_locators.page maps to evidence page; textual locators such as p.2 remain unknown, never inferred from chunk_index. Source version remains unknown unless upstream provides a verified field. These mappings remain synthetic source-contract tests until real deployment fixtures exist.

### P07c2 input provenance prerequisite (C2a locally accepted; business consumer pending)

Concrete gateway source inspection disproved that the last Human content equals only the current question: attachment hints, extracted documents, group context and localized empty placeholders are concatenated before Harness. Use existing Human.additional_kwargs, not a new Host facade/ticket/schema/cache. Dashboard processor creates USER_INPUT_KEY=octop_user_input with {version:1,text:InboundMessage.text or empty string} while assembling message_kwargs, always server-created and exact normalized inbound text, before rendered content reaches build_harness_request. Ignore same-name client metadata. This metadata can persist in checkpoints/history under existing Human visibility rules; do not call it transient or raw wire bytes. Ordinary content/composer/attachments remain unchanged.

Initial supported stamping is the authenticated Dashboard HTTP/WS common processor. Unsupported enterprise-bound CLI/IM/Bridge/cron/delegation remain rejected by accepted identity gates, not newly enabled. P07c2 policy first preserves ordinary unrelated/unbound turns, then requires the marker on the CURRENT last Human for bound new-turn query: strict version integer1 (not bool), text string and nonempty after whitespace test, no historical backwards search or content/placeholder fallback. Empty/missing/malformed marker denies before source/model; query passes original string unchanged. Direct trusted service tests can provide their internal question argument, but that is not an additional HTTP query input. The original turn operation remains identity/source authority; this field only identifies input text.

### P07c2 knowledge-tool producer freeze

Split C2 into the knowledge tool/ownership/output producer followed by the actual policy hook and default-chain dispatch consumer. Register enterprise_knowledge_list(), enterprise_knowledge_search(knowledge_base_ids=None), enterprise_knowledge_read(evidence_id, continuation=None) as three distinct async SDK functions. Register their original names as Host read-only capabilities; tools call only the paired flat service and receive fresh execution authorization. No model argument supplies actor, operation, question replacement, connection or provider URL.

KnowledgeService gains only async owned_tools_current(tools) returning a role-to-final-name string map for list/search/read. The main factory supplies its own registered callback identities privately; selected async coroutine (or sync func only when no coroutine exists) must match, so a foreign coroutine cannot borrow an owned sync function. Returned names are not standalone authority; the hook must match the actual ToolCallRequest tool and its call name. Service execution kinds remain turn-only until P13 explicitly implements continuation; current premature resume/continuation allowance is removed with this producer.

The actual search tool returns JSON with octop_ui={renderer:enterprise_knowledge_evidence,version:1}, data={schema_id:octop.enterprise.knowledge.v1,version:1,operation_id,reused:true,evidence:[...]}, and localized text. Each projected evidence has only evidence_id, source_kind, title, content, truncated, retrieved_at, source_version and source_page from validated records. Envelope metadata excludes user/agent/thread/connection/credential/approval fields and provider configuration URLs; retrieved content remains untrusted data. Unknown metadata stays unknown. The renderer must reauthorize via evidence.list using operation_id before trusting citation metadata. List returns versioned operation_id/knowledge_bases JSON; read returns the accepted ReadPage JSON. Neither adds a new renderer hint in this producer.

Native default-chain dispatch uses the separately verified public handler/model override approach; eager short-circuit alone does not initialize deferred ToolSearch. Its final production policy behavior is frozen separately after the runtime probe review. Tool producer tests use a clearly labelled synthetic companion for service metadata and do not claim actual policy enforcement or graph-end completion.

### P07c2 policy consumer freeze

The enterprise-policy hook0.1.0 uses only public Host/current execution and the paired KnowledgeService API1, never imports the knowledge plugin's private modules. Ordinary absent/unrelated requirements exit before service resolution. Present invalid execution is denied; either enterprise ID activates exact-pair enforcement. Only the current final Human's server marker supplies the unchanged retrieval question. Successful before-agent retrieval writes an internal operation receipt in plugin graph state; every refusal or ordinary entry clears it. This metadata is excluded from graph input and is not model/user authority. No actor or per-turn phase is retained on middleware instances.

Host execution context is plugin-scoped: the policy facade does not receive the knowledge plugin's operation context. Retrieval returns the admitted ID; fresh receipt comparison uses KnowledgeService.search_current's validated current operation, without another provider batch. Only the knowledge service accesses its own operation context. Do not duplicate client operation IDs under policy context or expose private execution tickets/raw operation services.

Native jump-to-end routes through after-agent hooks, so after-agent cannot blindly complete the context operation. It requires this graph's admitted receipt, matching fresh context and a successful real cached-search ToolMessage. Tool phases use stable operation-derived call IDs and actual current-Human ToolMessages. An error response cannot qualify. Old history is excluded. KnowledgeService deny_current(code) may CAS only owned VALIDATING to DENIED, and fail_current(operation_id,code) only owned GENERATING to FAILED after matching the server receipt to its current context ID. Both require fresh execution/ownership, accept closed error-code sets and preserve terminal or differently active states. Neither fetches source content; a revoked source grant does not authorize content but need not block recording a failure. Invalid Host identity still blocks writes. Cancellation/restart/resume remain P13.

Every model wrapper filters actual enabled callable identities via owned_tools_current; only genuine installed ToolSearch bound methods are extra discovery capability. Actual tool execution checks that the request tool object belongs to that set and its runtime name matches the requested name. Legacy search, MCP, browser, exec and peer/delegation tools cannot run inside bound turns. Independent manual browser endpoints remain unchanged. Ask/Plan/Craft use the accepted Host read-only declarations.

For bootstrap, delegate public handler(request.override(model=fresh local deterministic BaseChatModel,tools=permitted)) in the installed default middleware order. The local model's bind_tools examines the downstream schema: full owned search emits the real cached-search call; deferred empty schema emits genuine discovery first. Discovery that completed without making the required tool available fails rather than looping. After real cached-search success, handler(request.override(tools=permitted)) preserves the original model object and configuration. Do not reorder or patch the SDK, force external provider tool choice, fabricate ToolMessages, or add custom streaming adapters. Source text remains untrusted; trusted cards still require server evidence reauthorization in P09.

### P08 generic readiness prerequisite

The current installed-plugin list is not persisted execution requirements, and its Agent variant is owner-only. Add a minimal generic PluginHost.read_requirements(actor) returning an immutable merged required_plugins map and unavailable_plugins IDs, plus GET /api/plugins/requirements?agent_id=...&thread_id=... with api_version=1. The server builds actor from current_user; the Host refreshes persistent identity and validates shared-Agent access plus own-thread membership. Without a thread, read Agent scope only. Do not use declaring-facade get_requirement's management permission for this read, return declaration provenance/owner IDs, or accept a client requirements map.

Read the same reserved records as final admission, including declarations whose plugin is gone. A narrow shared parser may serve readiness and authorize_execution without changing ticket/identity requirements; malformed maps or conflicting versions fail unavailable. Missing/disabled/wrong-version active plugins and explicit per-Agent disable are unavailable. The response is a local UI hint, not actual-graph or business/source readiness. Final mounted-graph and operation checks remain independent. No remote probe, schema change, write, business identifier or permission relaxation enters this prerequisite.


### P10a selected search HTTP adapter refinement

The independently reviewed `prd/evidence/enterprise-p1/search-http-adapters-contract.md` freezes the two-path plugin-only adapter slice: SearXNG form POST/search and Firecrawl JSON POST/v2/search; exact approved query, local8-result/1200-codepoint projection, strict full-array validation, one send per attempt and one10-second source deadline including pure executor parsing. TLS/parser cleanup ownership and NEW/RUNNING/finished close semantics are explicit. This slice does not consume approval or implement cross-call durable request accounting,22-second orchestration, fallback, settings or modes; those remain subsequent P10b/UI tasks. Per user priority on2026-10-25, unverified WeKnora live knowledge checks move to manual handoff and do not block this local slice.
