# P09b 插件证据卡片/阅读器实施合同（独立规划PASS，P09a已接受，实施READY）

本草案只细化既有 `enterprise-evidence-ui/spec.md`、插件 UI design 和已审 P09a `tool-dock-ui-contract.md`；业务后端证据合同冻结。P09a 已实现并经 Fix2 独立 Spec/Quality PASS，root 已核实际公开接缝。P09b 独立 Spec/Builder 规划已通过，开工前置已满足：P09a 实际验收、精确13路径白名单与编辑前基线均已核对；此文不勾选 UI3.3/4.3 或完整原文/WEB/私有源验收。

## 实际接缝与入口

- `plugins/enterprise-knowledge/ui/src/index.ts` 现只登记 settings；setup 同步再 `registerRenderer({id:"enterprise_knowledge_evidence",tools:["enterprise_knowledge_search"],component:EvidenceRenderer})`。`tools.py` 的 `octop_ui.renderer` 确为该 ID、version=1、工具名为 `enterprise_knowledge_search`。`registerRenderer` 返回 void，现有 settings disposer 仍作为 setup 返回；loader 所属 token 负责注销 renderer。不能捕获 setup Host 作阅读请求/开 dock。
- P09a 接受后插件仅镜像公开结构：`PluginSetupHost` 为原 `PluginHost` 的 settings 能力加 `registerRenderer`；`PluginToolHost` 单独包含 `request/getToolContext/openSidePanel/closeSidePanel`；`PluginToolProps` 镜像 `ToolRenderProps` 的真实字段及新 `presentation?:{location:"inline"|"side-panel";active:boolean;state?:PluginJson}`。不强迫所有旧 Settings `PluginHost` fixture 实现工具方法。组件使用 **挂载的 `props.host`**；旧 `props.agentId` 可能是团队 speaker，不作为 action 目标。工具 Host 的 `getContext()` 为 null，不能沿用 `api.action` 的 surface 上下文推导。
- `ToolRenderProps` 现有 `pluginId/toolName/callId/status/data/textFallback/host`；仅在完成且 `pluginId===enterprise-knowledge`、`toolName===enterprise_knowledge_search`、非空 callId、有效工具上下文时激活阅读。`props.data` 必须为 object，`schema_id="octop.enterprise.knowledge.v1"`、`version===1`、UUID4 `operation_id`；`data.evidence/reused` 和模型文字只供无信任提示，不参与卡片、正文、链接或预选。无有效引用展示安全空/错误，不调用 action。
- P09a `openSidePanel({callId,state})` 的 state 仅 `{operation_id,evidence_id}` JSON 安全临时选择，来源必须是当前 `evidence.list` 返回的 ID；无选择时可只存 operation_id。Core 只搬运 opaque state，不解释 ID。点击卡片复制选择后调用**当前挂载 Host**；同 call 新选择由 P09a 新 activation 重挂 reader。dock reader 在 `presentation.location==="side-panel" && active` 下加载；inline 卡片与 dock 不并发读取。关闭/整 dock 关闭的焦点归还由 P09a 稳定工具组处理，插件不传回调、不直接控制 Core DOM。

## 真实 action / DTO / 数据校验

- `api.ts` 保留现有 surface `action()` 行为；另加窄 `toolAction(host, capturedTarget, name, payload, signal)`：从 `getToolContext()` 取得且要求非空 `agentId/threadId`，与挂载时捕获目标一致，每次发出前及 await 后再核对；通过 `props.host.request` 向既有 `/plugins/enterprise-knowledge/actions/${name}` POST `{api_version:1,context:{agent_id,thread_id},payload}`，`RequestInit.signal` 传入。目标不符/Host 失效即丢弃结果，不将 transport body 直接显示。服务端逐次授权仍是事实源；拒绝不可由本地历史缓存掩盖。
- `evidence.list` 输入**仅** `{schema_id:"octop.enterprise.knowledge.v1",version:1,operation_id}`；响应 envelope `api_version===1`、`result` 对应 `EvidenceListOutput`：schema/version、`operation.operation_id/agent_id/thread_id` 与当前目标匹配，`evidence` 为数组。逐项校验 `evidence_id/operation_id`、source_kind=`KNOWLEDGE|WEB`、title/content 字符串、truncated 布尔、retrieved_at 可解析 UTC、source_page 为 null 或非负整数、source_version/locator 可空；operation_id 逐项相等。失败是安全错误态，不降级使用 `tool.data.evidence`。只从本次 list 响应创建可信卡片与选择映射；ID 只当 opaque 引用，标题/内容/locator 只作 React 文本，不插 HTML 或导航 URL。
- 卡片展示真实来源类型、title、retrieved_at、截断状态、可用的 source_page；null 页码明确“无页码”，不补页 1。KNOWLEDGE 可打开检索文本 reader；WEB 仅展示已登记摘要并标明当前无连续阅读/原文能力，不向仅接受知识来源的 `evidence.read/original` 发送 WEB ID。当前 producer 尚未产出 WEB 证据，保留其验收门槛。
- `evidence.read` 输入**仅** `{operation_id,evidence_id,continuation?}`，无 schema/version 字段；首次不传 continuation，续读仅传上一成功页的 opaque UUID。校验 ReadPage schema/version、operation/evidence 对应、`status=SUCCESS|EMPTY`、content 长度≤8000、truncated 与 continuation 同真值、source_version===null。仅 SUCCESS 且同 selection/sequence 时按返回顺序追加；首次 EMPTY 显示空态；续读 EMPTY 保留已成功文本并结束游标，显示无更多内容；网络/预算等可恢复失败不推进 cursor、不丢已成功文本，可按同 cursor 显式重试；READ_CURSOR_INVALID/READ_SOURCE_CHANGED 等终止错误按下述专门规则清 cursor，仅可显式重启。`READ_SOURCE_CHANGED` 立即停追加、清旧 continuation，提示“来源已变”并提供明确从无 continuation 重新读取（先重新 list/授权），不自动拼接旧正文。阅读内容标为“检索文本”，不得宣称完整原文。
- `evidence.original` 输入**仅** `{operation_id,evidence_id}`；仅在知识卡当前授权且用户操作时查询。实际 `OriginalOutput` 固定 `available:false,reason:"CAPABILITY_UNVERIFIED",resource_token:null`，显示明确不可用与原因，无资源请求、下载、provider URL、浏览器导航。未来若后端真返回可用，须另有经审 Host opaque authenticated resource 合同才能接线；本草案不猜 endpoint/token 用法，不以模拟可用值完成完整原文验收。

## 生命周期、时间与可访问性

- 对每个 renderer mount 和每次选择维护单调 sequence 与 `AbortController`；改变 `operation_id/evidence_id/presentation`、失活、卸载时先递增/abort，再清本 mount 正文与游标。每个 list/read/original/timezone 请求的完成和拒绝均核对 sequence、当前 Host/目标、当前 selection；迟到结果/错误不发布。Host 异步成功及拒绝由 P09a 再验证认证 epoch；401/403 或 `ACCESS_DENIED/SCOPE_INVALID` 使当前 reader 停止新请求并显示 en/zh 权限变化，不泄露错误正文。隐藏再开、新选择、历史重新挂载均 fresh list/re-read，不能复用旧授权或正文。
- 历史工具项与实时项执行同一 `operation_id → evidence.list` 流程；只凭当前真实 `props.host.getToolContext` 的 Agent/thread，禁止全局 contextOverride、null thread 或消息作者身份回退。服务端保留 operation 是否仍可用以 list 结果为准；历史文本里的模型 ID 不生成卡片。
- 时间通过同一挂载 Host 的已认证 `GET /settings/timezone` 读取 `{timezone:string}`；仅接受非空且 `Intl.DateTimeFormat` 可识别的显式 IANA/有效时区，再格式化 `retrieved_at`；请求失败/无效时显示 en/zh“服务器时区不可用”，不显示格式化或原始时间值，只保留明确不可用提示，不用浏览器默认时区。不同 mount/target/认证不得共享旧时区结果。settings 原有 `context.timeZone` 逻辑不动。
- i18n 在现有 `ui/src/i18n.ts` 同步补 en/zh：列表/阅读/更多进度、空、错误/重试、授权变化、来源变化/重启、截断/无页码、原文不可用、WEB 来源、时间不可用。加载/错误/权限/追加状态用可读 `role=status`/`aria-live`，错误焦点可达；卡片与更多/重试/重启用原生 button、可键盘 Tab/Enter/Space，禁用态有文字原因。选择重挂后将焦点移至 reader 标题/状态，关闭由 P09a 回到来源工具区域；不抢走背景 tab 焦点。

## 精确插件白名单与验收

- 仅源码：`plugins/enterprise-knowledge/ui/src/index.ts`、`host.ts`（窄结构镜像）、`api.ts`（工具 action adapter）、`i18n.ts`、新增 `EvidenceRenderer.tsx`（卡片与既有 dock reader 可同文件，只有确实变大才拆一个 `EvidenceReader.tsx`）、`ui/build.mjs`（manifest renderer 列表）。`plugin.yaml` 已指向 `ui/dist/index.js` 和 manifest，无需修改；不改 backend、Core 或 `Settings.tsx`。如 P09a 实际 props 与合同不符，先重审白名单，不静默扩大。
- 仅测试：新增 `ui/src/EvidenceRenderer.test.tsx`、`api.tool.test.ts`；调整 `index.test.tsx` 仅 setup fixture 的窄 `PluginSetupHost`/renderer 登记，`package.test.ts` 断言真 renderer manifest/打包。既有 `Settings.test.tsx` fixture 不补无关方法；`api.test.ts` surface 回归保留。无需新依赖或测试读 live a3 文件。
- `build.mjs` 继续单入口 bundled ESM、无相对 import/chunk、Host React shim、ES2022、无 install；manifest 精确为 `{renderers:[{id:"enterprise_knowledge_evidence",tools:["enterprise_knowledge_search"]}]}`，不得声称 loader 根据 manifest 登记组件（实际 setup 登记）。
- 有意义的 RED/通过矩阵：工具身份/引用缺失和伪造 data IDs/URLs 不产生卡片；list 响应关联/字段无效安全失败；实时与历史都先 list；知识与 WEB 分流；null 页/UTC 时区不可用；卡片选择传 JSON state 并用新 activation 取得 fresh Host；切卡/隐藏/卸载/认证失效/迟到成功与拒绝无旧内容；read SUCCESS→续读顺序、EMPTY、可恢复失败同 cursor 重试、READ_SOURCE_CHANGED 显式重启；original 固定不可用且零 resource/download/provider 请求；中英与键盘/状态/焦点；settings setup 保持 mounted props.host、bundle manifest 正确。
- 实施后跑 `ui` 定向 Vitest、`npm run typecheck`、`npm run build` 与 package test、差异格式/空白检查；按仓库 ship bar 再评估 `make all`。这些尚未运行；P09a 完成前不得以 mock Host 测试代替真实 dock/焦点集成验收。

## Root 具名精度补充（仍待独立规划审查）

- state 中 operation_id 必须等于 props.data 的真实有效 operation_id，evidence_id 必须存在于本 mount fresh list。未知/错 operation 的 state 不读取、不回退猜第一项，显示安全错误或授权后列表。inline 不自动 read；side-panel 无选项只列出授权证据供选择。
- EvidenceListOutput/每个实际使用的 nested operation/evidence 都校验固定 schema/version（存在且 version===1，不接受字符串/布尔）；每条 evidence 的 agent_id/thread_id 与捕获目标一致，operation_id 与所读 operation 一致，user_id 与同响应 operation.user_id 一致（只作响应内部一致性，不由浏览器决定权限）。证据 UUID 重复拒绝。选定显示字段显式投影，不 spread 原始 backend payload 到 DOM/links。
- ReadPage.content 的8000字符按 Python后端 Unicode codepoint语义校验，不能用 JS UTF-16 .length 把合法非BMP正文误拒；页 operation/evidence/knowledge_id 必须匹配选定实际注册条目。EMPTY 内容/cursor/truncated闭集同后端模型。来源变化保留已读文字但标明失效、清cursor/禁止继续；显式重启时清旧正文后fresh list/read，不混拼新旧版本。
- toolAction 只接受 evidence.list/read/original 三个已冻名称；可用最小 optional signal参数复用既有 action envelope序列化，但现有 surface调用/管理null context语义不变。只转发当前挂载Host请求，不暴露transport body。
- 精确白名单固定13路径（不是可临场再拆）：源7为 ui/src/{index.ts,host.ts,api.ts,i18n.ts,EvidenceRenderer.tsx,EvidenceReader.tsx} 和ui/build.mjs；EvidenceReader.tsx为可选已列路径，不使用则无变更。测试4为ui/src/{EvidenceRenderer.test.tsx,api.tool.test.ts,index.test.tsx,package.test.ts}。产物2为ui/dist/{index.js,manifest.json}，只能构建生成。上述均相对plugins/enterprise-knowledge/。不新增其它helper/style/test/dependency路径，Settings不改。package.test现有setuphost fixture也须增registerRenderer，避免产物真实setup测试假失败。
- 最终 sourcefreeze后 package typecheck、build一次，再运行本UI包完整test（含实际built package/entry和既有Settings/API回归；source/api/host/index/i18n均改动，相关包回归有具体理由），记录命令/结果；随后才实际Dashboard+安装包联合浏览器验收。P09a已独立接受，仍不得以mock Host替代最后实际安装包和浏览器联合验收。全仓make all仍独立最终门禁。

## 首轮独立规划审查三项修正

1. `READ_CURSOR_INVALID` 与 `READ_SOURCE_CHANGED` 均进入终止续读、清 cursor、保留已成功文字但标失效的状态，只能用户显式重启；禁止继续重试旧 cursor。重启先清旧正文，再 fresh list/无 continuation read。网络/预算等可恢复失败才保留同 cursor 重试。original 也可能返回 READ_SOURCE_CHANGED，同样终止当前选择族的继续操作并显示来源变化，不归普通重试。401/403 或 ACCESS_DENIED/SCOPE_INVALID 清除当前授权卡片/正文/cursor、使在途请求失效，只显示本地化权限变化，不从 tool.data 回填；重新挂载后再 fresh list。
2. 每 reader 同时最多一个 read 在途，第一事件处理调用栈、任何 await 前以本地 ref/request ID 占位，更多/重试按钮禁用并呈现加载状态。响应仅对应当前 slot/selection/sequence 时原子追加文本与替换 cursor；只有网络/预算等可恢复失败保留原 cursor，终止类错误按第1项清 cursor；两者均只释放自己的 slot。旧请求 finally 只能释放自己的 slot，不能释放显式重启后的新 slot。重启/切换先 invalidate/abort 并释放旧 slot，新 request ID 独立；双击和未提交 React 状态更新前的重复调用都不得发第二个 read。测试同 cursor 双击、可恢复失败后单次重试、终止失败不重试旧 cursor、旧响应与旧 finally 迟到。
3. 实际消费 DTO 闭集：status 必须恰为 done 才启动；KNOWLEDGE evidence 的 connection_id 必须显式为有效 UUIDv4，kb_id/knowledge_id 必须显式为非空 opaque string（不自创 UUID 格式），WEB connection_id 必须显式 null；缺字段不以 undefined 相等接纳。ReadPage.knowledge_id 先验证显式非空 string，再与所选注册条目比较，并核 source_kind 为 KNOWLEDGE。OriginalOutput 必须逐项核 api_version/schema/version、operation/evidence ID 及固定 available===false/reason===CAPABILITY_UNVERIFIED/resource_token===null；缺失/错绑/available:true/非空 token 全部安全错误、零下载或资源请求。补畸形 list/read/original、游标过期与来源变化区分、原文来源变化、非BMP8000 codepoint及双击续读测试；不增加源码/测试白名单路径。
