# Enterprise knowledge UI spec review

状态：**FAIL — 暂不满足开始 P08/P09 UI 实现的规格门槛。** 页面范围和安全意图基本齐全，但几个影响权限、数据流和可验收行为的决定仍需落到 OpenSpec。这里是只读规格审查，不代表运行或产品验收 PASS。

审查范围：`openspec/changes/enterprise-knowledge-plugin-ui/{proposal.md,design.md,tasks.md,specs/*/spec.md}`；实施计划 PLG-01～11、P08/P09/P11/P12；PRD selected_kb_ids、REQ-06、页面交付范围；Host v1 action 目录；`dashboard/src/plugins/toolRenderers` 现有接口。未修改其他文件，未运行测试。

## 通过项

| 检查点 | 结果 | 依据 |
|---|---|---|
| 插件 UI 边界与固定挂载 | PASS | Proposal 将业务组件放插件；Design 限定 settings/composer 两类固定 surface、禁止任意页面/路由；与 PLG-03/04、P08/P09 一致。 |
| 后端作为权限事实源 | PASS | Settings spec 限制普通员工读取管理数据；Chat/Evidence spec 要求服务端重新鉴权、真实 evidence ID、opaque 原文引用；与 PLG-09 一致。 |
| 查询词安全与状态隔离 | PASS | Chat spec 覆盖精确字符串、审批绑定、清空范围、切换迟到响应、WEB_ONLY 不触发知识 ready；对应 PRD REQ-06、P09/P12。 |
| 证据真实性与阅读边界 | PASS | Evidence spec 覆盖伪引用、截断续读、无页码/原文、撤权、源版本变化、历史重开；与 P09 和 PRD A-04 对齐。 |
| 管理保存/探测语义 | PASS | Settings spec 明确 write-only secret、revision 冲突、保存与 LIST/SEARCH/READ 探测分离、未探测不能 READY；与 P08/P11 对齐。 |
| Host 已有 renderer 能力判断 | PASS（对现状描述） | `toolRenderers/types.ts` 暴露 `registerRenderer`、`request`、`openSidePanel`、`closeSidePanel`；`host.ts` 实现认证 API request 和侧栏转发。它不含 settings/composer surface 注册，故 Design 将其列为新增通用桥接是准确的。 |

## 必须补齐的规格问题

### F1. P08 管理角色和各区动作边界未定义

Settings spec 概述称“系统管理员和有管理权限的专家配置者”均可使用设置区，但 Scenario 只规定普通员工不能看管理输入/动作。没有说明专家配置者能否读/改连接、员工授权、搜索源；也没有区分其可管理的专家绑定/默认库与系统管理员专属的连接/员工 grant。实施计划 §4.1 和 Host v1 明确连接配置仅管理员、专家绑定须已有管理权限并通过所有权校验；P08 还要求角色限制测试。

**要求：** 在 Settings spec 明列角色 × 页面分区/动作矩阵（系统管理员、具备专家管理权的专家配置者、普通员工），并写出无权时是否隐藏/禁用、是否完全不请求对应 bootstrap 数据。当前“不展示行政输入”不足以约束部分有管理权角色。

### F2. UI 操作没有落到可核对的动作/数据合同

Design 只点名 `settings.get/binding.get/session.get` 等 bootstrap 动作；各 spec 未列调用动作、发生时机及关键字段。Settings 未说明连接/员工授权/专家绑定保存及 catalog 读取各自对应何动作；P11 未明确 `search.save/test` 与 revision/test result 的展示映射；Chat/Evidence 未把 `session.get/save`、证据登记/历史解析、`evidence.read/original` 的调用及资源引用传递写成页面流程。实施计划 §4.2/§2.4 已列出动作目录和 `connection.save/probe/catalog`、`grant.save`、`binding.save`、`session.save`、`query.approve`、`evidence.list/read/original`、`search.save/test`，Host v1 也给出 action envelope。规格目前要求完成这些页面，但不能据此核对调用链和成功/冲突/失败后的数据更新。

**要求：** 各 requirement 或设计附一份页面动作表：surface/用户动作 → action/resource → 最小输入（expected_revision、thread/agent context 等）→ 关键响应状态/字段 → UI 更新；与 P04/P06/P11/P12 注册的 schemas 对齐。无需在 OpenSpec 复制完整 Pydantic schema。

### F3. 首次会话默认值与已保存空选择的区分不够可验收

PRD schema §8（`selected_kb_ids`）规定新会话复制专家默认集合，空集合拒绝知识查询；Chat spec 说新会话从授权裁剪后的专家默认初始化，并规定明确清空不回填、跨 context 只载入其服务端状态。缺少判定“新会话尚无 session selection”与“已有 session selection 为空”的事实字段/加载顺序，以及专家默认或授权在初始化/会话载入期间变化时采用哪个快照。Design 的“默认范围只初始化一次”是方向，但没有给出初始化判据或触发保存的界线。

**要求：** 明确 bootstrap 是否返回 selection-present/初始化状态或等价标记；仅缺少会话值时复制授权交集后的专家默认，已存在的空数组视为用户清空；写明初始化与 context generation 竞态的胜者。这样才能验证“不自动补回”同时保留 PRD 新会话默认。

### F4. P09 键盘行为只在 Design 叙述，没有完整可验收条款

Design §6 写 Esc 取消、Tab trap、关闭返回触发焦点，Tasks 3.2 有焦点测试；Chat spec 的 query cancel Scenario 只要求取消后回到 trigger，未要求确认对话框的 Esc/Tab/焦点、初始焦点、读屏标签和双击/快捷键的可操作反馈。P09/PLG-11 把键盘交互列为实现前页面规格要求，当前详细行为留在非规范性设计段落。

**要求：** 将确认对话框的键盘与焦点行为列入 Chat requirement/scenario；为选库、多选、模式切换、发送禁用/错误提供键盘和可访问名称/状态的最低验收。视觉表现可后延，但行为合同不能只靠 tasks 推断。

### F5. Chat 与证据状态的 locale/timezone 验收未形成闭环

Proposal/Design 和 Tasks 承诺 en/zh 与服务器时区；Settings、Evidence spec 有对应要求，但 Chat spec 未明确本地化其加载、拒绝、模式不可用、空范围、配置冲突提示。PRD §18 写“用户本地时区”，实施计划 §1/§138 已记录该冲突并选择按 AGENTS.md 使用服务器时区；本 OpenSpec 选服务器时区是正确决议，但应在审查记录/相关验收维持此决议，避免以 PRD 旧表述回退。

**要求：** Chat spec 明确用户可见文案 en/zh、错误理由随服务器业务响应本地化；证据时间始终采用 Host 服务器时区（保持当前 OpenSpec/仓库规则，PRD §18 为已登记待同步的冲突）。

## 可延后到实现阶段的视觉细节

- 卡片、表单、侧栏的具体间距、颜色、图标、断点与响应式排布；当前线框已足以表达入口/主要区域，不阻塞功能实现。
- 通用组件拆分、表单控件样式与空态插画；只要后续不改变固定 surface、信息层级和已定义状态。

## 进入实现前的最小关闭条件

更新本 OpenSpec 的角色/动作表、selected_kb_ids 初始化判据和 Chat 键盘/locale 行为；然后重新对照 P08/P09/P11/P12 与 Host action schemas。视觉稿不作为本次阻塞项。

## 重审：F1–F5

范围严格限定为上一轮 findings F1–F5；检查更新后的 UI specs、Design 中新增的角色/动作矩阵与 session 初始化条款。未审查或提出范围外要求。

| Finding | 结果 | 核对依据 |
|---|---|---|
| F1 管理角色及分区边界 | **ADDRESSED** | Design 的 Role and action matrix 区分 admin、非 admin 专家配置者和普通员工；Settings spec 明确连接/员工授权/搜索仅 admin，专家绑定要求 `agent_plugins` 权限及目标专家 owner，普通员工不取管理 bootstrap。符合给定的现有权限规则。 |
| F2 页面动作/数据合同 | **ADDRESSED** | Design 的 Page action contract 列出 settings、connection save/catalog/probe、grant/binding/search、session 初始化/保存、query approval、evidence list/read/original 的页面事件、动作/资源、最小输入及响应更新；Settings 与 Evidence specs 又将相应动作变为规范要求。已覆盖指定 action schema 提案。 |
| F3 新会话默认与已保存空选择 | **ADDRESSED** | Design 明确 `selection_present` 由持久记录存在性决定、缺记录用授权快照初始化、`revision=0` CAS 失败方 reload、已有空数组不回填；Chat spec 将这些规则和双 tab 竞态、默认变化场景列为规范要求。 |
| F4 Chat 键盘行为 | **ADDRESSED** | Chat 新增 Keyboard accessible localized composer requirement，规定可访问名称、键盘选择、对话框初始焦点/Tab/Escape/返回焦点、禁用原因与 pending 去重，并新增键盘取消和快捷键场景。 |
| F5 locale/timezone | **ADDRESSED** | Chat requirement 规定所有 composer 状态/错误文案支持 en/zh、显示时间使用服务器时区；Settings 与 Evidence requirements 也明确各自文案 locale 和服务器时区。PRD §18 的用户本地时区差异沿用实施计划已登记的服务器时区决议。 |

### 重审结论

**PASS — F1–F5 均已关闭；按本轮限定范围，OpenSpec 满足 UI 规格门槛，可进入实现。** 本结论仅表示规格审查通过，不代表代码或运行验收通过。未修改产品规格或代码；本轮仅追加本审查记录。


## P08 发送链路补充规格复核（root，2026-10-25）

F6：只读精确审计发现首发异步创建线程绕过useChat.sendMessage，queue/reclaim/edit重发不含执行快照，busy steer不保存新plugin_context，HITL只接受服务器continuation。已在UI Chat spec新增Immutable per-turn extension context及四个场景，Design/tasks与后端Host scenario同步；采用原生建会话→真实thread初始化→准备→提交，失败留草稿，忙时企业追加独立下一轮，HITL不接受UI替换。

root规格复核：与现有默认裁剪/空不回填、精确批准、context generation、pending去重及PLG通用Host边界一致；不新增页面/路由、企业专属宿主字段或逐steer协议，普通会话保持原有行为。F1–F5结论保留，补充行为可进入后续已冻结brief的实现；这只表示规格一致性审查，不是运行PASS。实际发送/steer与页面仍未实施。源码依据：useChatSend.ts首发分支，useChatMessageQueue.ts，chatStore.ts的sendTurn/sendSteerWhileBusy/resumeHitl；ws.py:_maybe_admit_steer与steer/types.py:PendingSteer，后者仅存text/content_blocks。

## F7：本地操作准备与发送快照（独立复核，2026-10-25）

**SpecPASS / QualityApproved，零Critical/Important/Minor。** 四份既有UI artifact增量已通过严格校验和独立审查；与实际models/actions核对：operation.prepare携带version=1和expected_session_revision，使用当前Host agent/thread，返回服务端operation UUID且零来源HTTP；evidence.list按operation_id读取，read/original按operation_id和evidence_id读取。首发准备期间同generation的新草稿编辑不会被旧项成功清空或失败回填覆盖。

本结论只接受F7规格，与此前F1～F6共同约束后续页面实现。C1编排仍在独立修复/复审；实际代理鉴权、revision竞态交由C1验收。UI草稿、队列/HITL、键盘、locale/timezone和Windows现场效果仍由UI未完成任务验证。query.approve留给P12，当前未验证原文能力继续明确不可用。没有页面运行完成或Alpha.1通过的结论。

## F8：插件移除后的通用就绪提示（独立复核，2026-10-25）

**SpecPASS / QualityApproved，零问题项。** 六份既有规划文件增量通过两个变更的strict校验及独立复核。通用Host只读响应返回当前用户可访问Agent和本人thread的合并required_plugins及unavailable IDs；不依赖卸载后的插件、owner-only设置接口或客户端权限。无thread时先读Agent范围，真实thread创建后再读其范围。非法/冲突/读取失败不当空ready，末端准入仍独立生效。

Builtin聊天shell负责缺插件时的提示与清理；可见页面每30秒及focus/visibility/lifecycle/提交前刷新，隐藏暂停、返回重查，迟到结果按generation丢弃。无企业ID、业务探测、声明owner/provenance或新增推送基础设施。接口尚未实现：P08a负责认证读取/解析/权限，P08b负责surface/loader及刷新清理，P08c负责首发、队列与草稿竞态；各自必须提供运行证据。F1～F7既有行为保留，UI任务勾选不变。C1和C2c现已本地实现并复核，真实策略由C2d继续，不推断页面或远程验收通过。
