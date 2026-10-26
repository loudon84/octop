---
title: Octop 企业智能体工作台产品定义与第一阶段二开实施计划
prd_id: PRD-OCTOP-ENTERPRISE-P1
version: 1.0
status: REVIEW
created_at: 2026-10-24
updated_at: 2026-10-24
template: 需求PRD工程模板.md v1.0
---

# Octop 企业智能体工作台产品定义与第一阶段二开实施计划

## 0. Document Meta

| 项目 | 定义 |
|---|---|
| 产品 | 基于 Octop 二开的企业智能体工作台，正式品牌名由企业确定 |
| 文档用途 | 产品边界评审、第一阶段排期、开发与验收依据 |
| 上游 | https://github.com/TencentCloud/Octop |
| 核查源码基线 | `dd9dce470844c25d59effb50615bb3c667ea25cd`；不表示企业已部署此版本 |
| 二开仓库与分支 | 项目启动登记，作为 M0 的输入；不假定已存在企业 Fork |
| 产品负责人 | 企业产品/架构负责人；实施人员由项目启动分配 |
| 评审角色 | 产品、前端、后端、测试、运维及试点业务负责人 |
| 目标版本 | Enterprise Desktop Alpha.1，企业内部试点 |
| 变更类型 | BROWNFIELD_CHANGE、INTEGRATION、NEW_FEATURE |
| Golden Consumer | 企业实际 Octop 桌面客户端与专家运行实例、实际 WeKnora 服务 |
| 状态说明 | 产品方向已明确；具体需求为评审稿，代码未实施、实机未验收 |

本文件按《需求PRD工程模板.md》第 34 节最小完整结构编排，并补充功能实施路线、任务依赖和证据清单。MUST/MUST NOT 为强制要求，映射到验收编号；SHOULD 为默认建议。文中的周期为资源假设下的估算。

## 1. Goal 与产品定义

### 1.1 一句话目标

让企业员工在已登录 Octop、已获知识源权限的前提下，通过统一桌面工作台调用专家，检索企业知识与获准的互联网信息，获得可核对来源的答案，同时保证知识范围受控、错误可识别、操作可追溯。

### 1.2 产品定位

产品是“员工使用专家智能体完成知识工作的统一入口”。Octop 承担专家、会话、工具执行及桌面交互；WeKnora 承担企业知识的集中解析、索引和检索；搜索供应商提供外部信息检索。

第一阶段业务价值：员工不必切换多个页面即可查询制度、产品和供应商资料，并能核对专家答案的原始依据。工作闭环为：选择专家与资料范围 → 提问 → 获取证据 → 专家回答 → 查看引用与原文。

| 用户 | 核心任务 | 第一阶段价值 |
|---|---|---|
| 员工 | 查询制度、产品参数、公开供应商信息 | 统一入口、明确来源、减少重复检索 |
| 专家配置者 | 配置专家可用知识源和联网能力 | 确定专家默认范围和允许调用能力 |
| 知识管理员 | 在 WeKnora 维护文档与权限 | 复用既有知识管理入口 |
| 系统管理员 | 配置连接、凭据、搜索源和诊断 | 能定位连接、认证、检索与生成故障 |
| 测试与运维 | 回归、发布、回退和证据归档 | 可判断是否具备试点条件 |

### 1.3 已确认方向与建议边界

已确认：基于 Octop 二开；知识库强化与搜索源强化为本轮主线；优先验证原生 WeKnora 连接器。

本稿建议：第一阶段采用一个企业 WeKnora 后端、一个主搜索源、一个备用搜索源；优先覆盖 Windows 桌面与共享 Web 界面；先完成只读知识使用闭环。macOS/Linux 保持上游兼容，独立安装器认证不纳入本阶段发布门禁。

### 1.4 路线选择

| 路线 | 收益 | 成本 | 决策 |
|---|---|---|---|
| 深化 Octop 原生知识引擎 | 单系统部署 | 企业解析与知识治理需持续自研 | 保留已有个人库，本阶段不扩建其引擎 |
| Octop 原生个人能力＋WeKnora 企业知识源 | 复用已存在连接器与独立知识后端 | 需要补范围、引用及错误语义 | 本阶段采用，先以验证结果决定补强工作量 |
| 同时集成多个企业知识平台 | 多后端覆盖 | 测试与权限映射范围成倍增加 | 不纳入 Alpha.1 |

## 2. Background

### 2.1 已核查现状

Octop 官方文档描述 React Dashboard、Python 服务、桌面客户端、知识库及连接器体系。已读取的 WeKnora adapter 提供 `list_knowledge_bases`、`search`、`read_document` 三个只读工具；通过 HTTP 请求 WeKnora REST API，而非要求部署独立 MCP 服务。

当前 adapter 的关键行为：

- 认证头为 `X-API-Key`，可选 `X-Tenant-ID`；请求超时为 30 秒，不跟随重定向。
- 搜索范围优先使用工具参数，再使用连接配置；均为空时列出凭证可见库。
- 搜索最多返回 8 个片段；每个片段截取前 1,200 字符。
- 文档阅读默认每页 20 个分块，最大 50；返回的每个分块同样受 1,200 字符截断。
- 检索片段保留文档 ID、标题、分块序号、分数、正文和截断标记。
- 连接探测只调用知识库列表；未提供上传、删除、索引管理工具。

### 2.2 可验证问题

1. 默认知识库 ID 可被工具参数覆盖，不能作为强制授权白名单。
2. 只通过列库测试不能证明搜索、文档阅读与专家调用成功。
3. 长条款可能在检索和阅读两个环节均被截断。
4. 现有返回字段不足以证明桌面支持原文打开、页码定位及引用卡片。
5. 企业实装版本、WeKnora 响应结构和权限行为尚未取得实测证据。

### 2.3 影响

业务：答案可能缺少关键条款或无法核对来源。工程：前端、工具与知识后端缺少统一证据契约。权限：宽权限服务密钥可能扩大员工可检索范围。运维：连接成功与业务可用容易混淆。AI Coding：若不区分默认范围和硬权限，容易错误实现授权逻辑。

## 3. Scope / Non-goal

### 3.1 第一阶段范围

| Scope | 内容 | Requirement |
|---|---|---|
| SCOPE-01 | 固定双方版本并完成连接器兼容验证 | REQ-01 |
| SCOPE-02 | 连接管理、分步探测与专家绑定 | REQ-02 |
| SCOPE-03 | 授权范围内的知识库选择与检索 | REQ-03 |
| SCOPE-04 | 引用卡片、分块阅读与原文访问 | REQ-04 |
| SCOPE-05 | 搜索源配置与主备切换 | REQ-05 |
| SCOPE-06 | 会话三种搜索模式与证据融合 | REQ-06 |
| SCOPE-07 | 诊断、兼容迁移、试点与发布证据 | REQ-07 |

### 3.2 明确排除

Alpha.1 MUST NOT 新建上传/删除知识工具、目录自动同步、网页自动入库、CRM/OA 写入、企业流程分派、Runtime 替换、页面级插件平台、全新 SSO 或企业安装器。上述能力不进入本阶段代码与发布包；范围核对归 A-07。

知识管理员在 WeKnora 完成导入与索引管理。桌面原有上传聊天附件功能不等同于上传企业知识库，不改变其已有语义。联网搜索不能隐式读取或携带附件内容。

## 4. Architecture Boundary

| Domain | Owner | Input | Output | 边界 |
|---|---|---|---|---|
| 产品契约 | 本 PRD | 已确认目标 | REQ/AC/范围 | 不替代运行配置 |
| 实现规格与任务 | 研发负责人 | 评审后的 PRD | 模块设计、开发任务 | 不擅自扩大范围 |
| 桌面/UI | Dashboard 与薄 Wails 外壳 | 用户操作 | 范围选择、引用、错误展示 | 不持有供应商明文密钥 |
| 执行与适配 | 专家所在 Octop 后端 | 身份、策略、问题 | 工具结果与答案 | 不重写 WeKnora 索引 |
| 知识与权限 | WeKnora | 凭证、目标库、检索请求 | 授权数据与片段 | 不管理 Octop 会话 |
| 联网搜索 | Octop 搜索适配层 | 用户批准的公开查询词 | 来源结果 | 不接收内部文档上下文 |
| 发布治理 | 测试/运维 | 实装版本与测试结果 | Evidence、发布结论 | 不以测试文件存在代替执行 |

目标数据流：员工 → Octop 界面 → 专家所在后端 → WeKnora 或搜索供应商 → 统一证据记录 → 专家答案 → 引用查看。

## 5. Terminology

- 知识源：一个受管理的 WeKnora 连接实例；知识库是该实例下的资源。
- 凭证可见范围：WeKnora 对当前凭证实际授权的资源集合。
- 企业允许范围：管理员为此连接设定的硬限制，区别于专家默认范围。
- 会话选择范围：员工当前选择的知识库集合，不能扩大硬限制。
- 有效范围：凭证可见范围 ∩ 企业允许范围 ∩ 员工授权范围 ∩ 专家允许范围 ∩ 会话选择范围。员工身份来自服务端认证会话，员工授权由管理员在 Octop 后端维护，不接受工具参数指定用户身份；默认无授权。会话选择必须为前四者的子集；工具参数只能进一步缩小会话范围，不能扩大。企业会话的所有 WeKnora 调用统一经过该策略层，包括旧连接器工具，禁止从旧工具入口绕过。
- 引用：指向一次真实检索证据的标识；模型输出的自由文本 URL 不自动成为可信引用。
- 原文：WeKnora 的源文件/源链接；分块阅读必须标注为“检索文本”。
- PASS：规定 Oracle 与运行证据同时成立。BLOCKED：缺环境、版本或必要输入，未完成验收。
- 第一阶段：M0 至 M4，包含知识强化与受控联网搜索；M0/M1 是优先落地部分。

## 6. State / Source of Truth

| 状态 | 分类 | 事实源 | 写入者 | 覆盖规则 |
|---|---|---|---|---|
| 文档、分块、知识库权限 | OBSERVED_STATE | WeKnora | 知识管理员/WeKnora | Octop 只读，不覆盖 |
| 连接与允许范围 | DESIRED_STATE | Octop 配置存储 | 有权限管理员 | revision 校验后更新 |
| 员工知识库授权 | DESIRED_STATE | Octop 授权存储 | 管理员 | revision 校验后更新 |
| 专家绑定、默认范围 | DESIRED_STATE | Octop 专家配置 | 专家配置者 | 不覆盖企业硬限制 |
| 会话模式与选择 | DESIRED_STATE | Octop 会话配置 | 会话用户 | 服务端验证后保存 |
| 有效范围 | RESOLVED_STATE | 后端计算 | 策略层 | 每次工具调用重新计算 |
| 已生效配置 revision | LAST_APPLIED_STATE | Octop 配置存储 | 配置事务 | 成功提交后变更 |
| 探测与查询进度 | RUNTIME_STATE | 当前操作 | 后端 | 旧响应不能覆盖新操作 |
| 引用与验收记录 | EVIDENCE_STATE | 会话证据/发布目录 | 后端/测试 | 追加，保留版本关联 |

## 7. State Machine

连接状态：UNCONFIGURED → CONFIGURED → PROBING → READY / DEGRADED / AUTH_FAILED / UNREACHABLE。保存只进入 CONFIGURED；列库成功但检索或阅读失败进入 DEGRADED；三项探测成功才为 READY。凭证修改使历史 READY 失效；禁用进入 DISABLED，重新启用后需重新探测。

查询状态：IDLE → VALIDATING → RETRIEVING → GENERATING → COMPLETED；无命中进入 EMPTY；认证/权限失败进入 DENIED；网络/协议错误进入 FAILED；用户取消进入 CANCELLED。终态不可被迟到响应改写，重试生成新 operation_id。

模式为 KB_ONLY、WEB_ONLY、HYBRID。默认 KB_ONLY；从任何模式切换到联网模式必须已有可用搜索源和管理员授权，否则保持原模式并返回错误。

连接配置持久化，探测结果记录检查时间与对应 revision；查询进度为运行态，结果进入会话证据存储。重启后未终结操作标记 INTERRUPTED，不伪造完成。上述状态验收归 A-02、A-06、A-07。

## 8. Data / Schema Contract

新增 schema ID 为 `octop.enterprise.knowledge.v1`，version=1；新增对象拒绝未知字段。上游原始响应可有额外字段，但由 adapter 显式映射，缺必要标识返回 UPSTREAM_SCHEMA_INVALID。旧连接通过兼容层读取，不修改其原始字段名。

| 对象/字段 | 类型/必填 | 默认值与语义 |
|---|---|---|
| connection_id / revision | string / integer，必填 | ID 服务端生成；revision 从 1 递增 |
| base_url | string，必填 | 仅 HTTP(S)；移除尾部斜线，规范到单个 /api/v1 |
| credential_ref | string，必填 | 指向后端凭据存储，前端仅显示掩码 |
| tenant_id | string，可选 | 无值不发对应请求头，不作为员工授权凭证 |
| allowed_kb_ids | string[]，必填 | 新配置默认 []，表示不允许任何库；不表示全部 |
| user_allowed_kb_ids | string[]，必填 | 按服务端用户身份查询；默认 []，共享专家不能扩大员工授权 |
| expert_allowed_kb_ids | string[]，必填 | 新绑定默认 []，管理员显式选择 |
| selected_kb_ids | string[]，必填 | 新会话复制专家默认集合；空集合拒绝知识查询 |
| mode | enum，必填 | KB_ONLY；另有 WEB_ONLY、HYBRID |
| public_query | string，联网必填 | 用户输入/确认的公开查询词，外发 query 必须逐字符等于确认值；主备沿用同一值；模型改写或新增查询必须重新确认，不能自动拼接内部上下文 |
| evidence_id / operation_id | string，必填 | 服务端 UUID；引用与操作绑定 |
| source_kind | enum，必填 | KNOWLEDGE / WEB |
| knowledge_id / kb_id | string，知识结果必填 | 上游标识；缺 kb_id 时经授权元数据查询补齐，不能猜测 |
| title / locator | string / object | title 可为空并显示文档 ID；locator 含分块序号或 URL，页码仅在上游提供时存在 |
| content / truncated | string / bool，必填 | 保留截断事实；不能补造被截断内容 |
| retrieved_at / source_version | ISO8601 / string或null | UTC 时间；版本缺失保留 null |
| status / error_code | enum / string或null | 成功空结果与失败严格分开 |

新接口的具体 URL、组件拆分、数据库表名属于实现设计；认证、字段语义、状态和权限规则不得变更。新增原文读取输出支持 continuation；单次最多 8,000 字符，按分块内偏移继续读取，禁止静默丢弃剩余内容。分块分页 page 与源文件页码是不同字段。

## 9. Requirements

### REQ-01 — 版本与兼容验证

**Goal：**版本与兼容验证，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 登记 Octop SHA、WeKnora SHA/镜像 digest、部署拓扑；分别验证列库、搜索、阅读三个 API；MUST NOT 将静态核查或 mock 结果记为实机通过。

**Inputs：**双方实际版本、服务地址、受限凭证、测试文档。

**Preconditions：**双方服务可达；凭证仅在环境配置。

**Authoritative State：**SOT=版本登记与服务响应；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**UNVERIFIED → 检查 → VERIFIED/BLOCKED。

**Allowed Side Effects：**只写验证证据。

**Forbidden Side Effects：**业务文档和生产配置。

**Ownership Scope：**EVIDENCE_STATE，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**版本或接口缺失时 BLOCKED；补齐后重新执行。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**接口返回结构符合 fixture 且实际链路留证。

**Invariants：**INV-01：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**DEPENDENCY_UNAVAILABLE / UPSTREAM_SCHEMA_INVALID。

**Acceptance：**A-01（含正常与负向用例）。

**Evidence：**TEST-A-01 执行结果、EVID-A-01.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

### REQ-02 — 连接管理与专家绑定

**Goal：**连接管理与专家绑定，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 复用现有 WeKnora 连接入口，展示配置、列库、检索、阅读四步状态；MUST 以 revision 校验更新；MUST NOT 在提交后的 API 响应、浏览器持久化存储、普通员工界面或日志暴露密钥；管理员首次输入与提交请求允许短暂持有，跨主机传输使用 HTTPS，提交成功后清空输入。

**Inputs：**连接配置、测试问题与测试文档 ID、专家 ID。

**Preconditions：**用户拥有连接管理及专家配置权限。

**Authoritative State：**SOT=连接配置、专家绑定；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**CONFIGURED → 探测 → READY/DEGRADED。

**Allowed Side Effects：**配置记录、后端凭据及探测记录。

**Forbidden Side Effects：**WeKnora 文档写入、其他用户配置。

**Ownership Scope：**RECORD，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**保存失败还原 T0；探测失败保留配置并标明失败步骤，不自动切换宽权限凭证。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**专家仅使用明确绑定且启用的连接。

**Invariants：**INV-02：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**CONFIG_CONFLICT / AUTH_FAILED / UPSTREAM_TIMEOUT。

**Acceptance：**A-02（含正常与负向用例）。

**Evidence：**TEST-A-02 执行结果、EVID-A-02.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

### REQ-03 — 受控知识检索

**Goal：**受控知识检索，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 在工具执行前计算有效范围，显式传入越界 ID 时拒绝整个请求；MUST 对直接文档读取重新鉴权；MUST 按员工、连接和专家权限交集过滤员工列库返回（包括名称、描述与数量），管理员配置页仅对有管理权限者展示凭证可见库；MUST NOT 将默认知识库当作硬白名单；MUST NOT 在空范围时回退全部库。

**Inputs：**用户身份、专家、会话选择、查询词。

**Preconditions：**连接启用且策略完整。

**Authoritative State：**SOT=WeKnora 权限与 Octop 三层范围；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**VALIDATING → RETRIEVING/ DENIED。

**Allowed Side Effects：**只读请求、会话检索证据。

**Forbidden Side Effects：**越权外部请求、知识内容缓存复用到其他用户。

**Ownership Scope：**RESOURCE，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**认证或权限失败不重试；范围变化后重新验证；禁用后禁止新请求。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**每条结果位于有效范围；权限撤销后新查询和引用打开失败。

**Invariants：**INV-03：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**SCOPE_REQUIRED / ACCESS_DENIED / AUTH_FAILED。

**Acceptance：**A-03（含正常与负向用例）。

**Evidence：**TEST-A-03 执行结果、EVID-A-03.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

### REQ-04 — 引用展示与连续阅读

**Goal：**引用展示与连续阅读，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 为真实返回片段建立 evidence_id 并校验答案引用；MUST 展示来源、标题、检索时间和截断状态；MUST 支持 8,000 字符分页连续读取；MUST NOT 将分块序号作为文件页码；原文不可用时 MUST 明示并提供检索文本阅读。

**Inputs：**检索结果、引用 ID、文档 ID、continuation。

**Preconditions：**调用者当前仍有资源访问权限。

**Authoritative State：**SOT=WeKnora 原文/分块与会话证据；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**引用点击 → 重新授权 → 文本/原文/明确不可用。

**Allowed Side Effects：**会话引用、只读原文请求。

**Forbidden Side Effects：**公开化私有资源、自动执行文档指令、任意本地路径读取。

**Ownership Scope：**RECORD，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**不存在或已失权时不显示新正文；打开失败允许重试；不生成猜测 URL。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**长文本连续读取可还原 fixture；答案引用均能关联检索证据。

**Invariants：**INV-04：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**REFERENCE_NOT_FOUND / ACCESS_DENIED / SOURCE_UNAVAILABLE。

**Acceptance：**A-04（含正常与负向用例）。

**Evidence：**TEST-A-04 执行结果、EVID-A-04.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

### REQ-05 — 主备搜索源

**Goal：**主备搜索源，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 配置一个主源一个备用源并分别测试；仅超时、429、5xx 时切换一次备用源；MUST NOT 对认证错误或成功空结果自动切源；MUST 在 UI 显示实际来源与降级状态。

**Inputs：**管理员搜索源配置、公开查询词。

**Preconditions：**两个源通过连通及响应结构验证。

**Authoritative State：**SOT=搜索源配置与实际响应；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**PRIMARY → 成功；可降级失败 → SECONDARY → 成功/失败。

**Allowed Side Effects：**供应商只读检索、调用计数。

**Forbidden Side Effects：**无界重试、跨用户响应缓存。

**Ownership Scope：**RECORD，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**单源总 deadline 10 秒；两源含调度总 deadline 22 秒；取消停止后续请求；无额外重试。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**单操作最多两次外部搜索；失败可定位到供应商。

**Invariants：**INV-05：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**SEARCH_AUTH_FAILED / SEARCH_UNAVAILABLE / SEARCH_TIMEOUT。

**Acceptance：**A-05（含正常与负向用例）。

**Evidence：**TEST-A-05 执行结果、EVID-A-05.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

### REQ-06 — 会话模式与证据融合

**Goal：**会话模式与证据融合，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 默认 KB_ONLY，服务端执行模式限制；WEB_ONLY 仅用公开查询词；HYBRID 内部检索后仅以用户确认的公开查询词联网；MUST 将两类证据分组展示；MUST NOT 发送内部片段、附件、会话历史或客户标识作为自动附加搜索上下文。

**Inputs：**mode、selected_kb_ids、用户确认的 public_query。

**Preconditions：**有对应模式权限与配置。

**Authoritative State：**SOT=会话配置与工具策略；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**VALIDATING → 检索 → 生成/EMPTY/FAILED。

**Allowed Side Effects：**会话模式与结果。

**Forbidden Side Effects：**知识库无命中时静默联网。

**Ownership Scope：**RECORD，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**HYBRID 任一源不可用时明确标记部分结果；两源都失败则停止有据回答；取消后丢弃迟到响应。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**联网请求 payload 可证明只有批准的查询字段；引用可区分内部/外部。

**Invariants：**INV-06：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**MODE_NOT_ALLOWED / PUBLIC_QUERY_REQUIRED / CANCELLED。

**Acceptance：**A-06（含正常与负向用例）。

**Evidence：**TEST-A-06 执行结果、EVID-A-06.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

### REQ-07 — 迁移诊断与发布

**Goal：**迁移诊断与发布，使第一阶段形成可验收业务闭环。

**Normative Requirement：**MUST 记录脱敏操作事件并输出绑定版本的验收证据；MUST 对新增企业功能默认关闭；MUST 保留旧连接与用户资料；MUST NOT 自动扩大旧配置权限或将 SKIPPED/BLOCKED 算 PASS；MUST 执行范围核对与实际 Windows 桌面回归。

**Inputs：**旧配置快照、功能开关、测试报告。

**Preconditions：**数据库备份可读，发布基线已固定。

**Authoritative State：**SOT=配置快照与验收证据；Observed=实际操作返回；Derived=校验后的显示状态。

**State Transition：**关闭 → 迁移/验证 → 灰度启用 → 保留/回退。

**Allowed Side Effects：**受管新增配置、日志、报告。

**Forbidden Side Effects：**修改原始知识文档、删除未知归属文件。

**Ownership Scope：**GENERATED_ONLY，限当前 connection_id/operation_id 和调用者资源。

**Idempotency：**读取重复执行不改变业务源；相同配置重复提交返回既有值，不新增绑定；证据按 operation_id 独立记录。

**Failure Semantics：**迁移失败事务回滚；回退失败保留备份和失败清单；关闭新开关恢复旧入口。错误不转换为空结果；回滚范围遵循第 13 节。

**Postconditions：**Required AC 全部 PASS 才允许试点发布。

**Invariants：**INV-07：上述权限、范围及数据归属在成功、重试、失败路径均保持。

**Error Codes：**MIGRATION_FAILED / ROLLBACK_FAILED / RELEASE_GATE_FAILED。

**Acceptance：**A-07（含正常与负向用例）。

**Evidence：**TEST-A-07 执行结果、EVID-A-07.json、脱敏网络/状态记录及第 12 节定义的前后摘要；UI 需求另有操作截图。

## 10. Side Effects

| 操作 | Octop DB | 本地文件 | 网络 | 缓存 | WeKnora 业务源 |
|---|---|---|---|---|---|
| 保存配置/专家绑定 | 受管记录写 | 凭据后端按现有机制 | 无探测则无 | 失效旧配置缓存 | 不写 |
| 连通探测 | 状态记录 | 脱敏日志 | WeKnora 只读 API | 不缓存正文 | 不写 |
| 检索/阅读 | 会话证据 | 脱敏日志 | WeKnora 只读 API | 仅当前操作内存 | 不写 |
| 联网检索 | 调用统计/会话证据 | 脱敏日志 | 公开查询 | 仅当前操作内存 | 不访问 |
| 模式切换 | 当前会话设置 | 无 | 无 | 无 | 不写 |
| 迁移/回退 | 新增受管配置 | 备份/证据 | 无 | 失效配置缓存 | 不写 |

“只读”限定为不修改 WeKnora 文档、分块与授权，不等于无日志或无会话记录。检索正文仅随正常会话权限保存，不额外进入全局日志。历史会话已有正文不会因源权限撤销自动抹除；本阶段保障撤销后新的检索、阅读和引用打开被拒绝，历史数据保留沿用会话策略。

## 11. Ownership

连接配置是管理员管理的 RECORD，员工选择是会话 USER_OWNED FIELD；专家默认值是专家配置 RECORD；知识资料始终由 WeKnora 管理。升级只迁移本阶段新增字段。用户修改使 revision 递增，不由后台探测覆盖。

发现 current_revision != expected_revision：BLOCK，返回 CONFIG_CONFLICT，0 配置 mutation。未知归属配置 PRESERVE 并报告，不删除。删除连接只删除其受管引用及撤销后端凭据绑定，不删除 WeKnora 知识库或既有会话。

## 12. Identity / Hash

资源身份使用 `(connection_id, kb_id, knowledge_id)`，禁止只按标题合并。UUID 使用服务端生成的随机 UUID v4；标题不是主键。

证据文件摘要为 SHA-256(file_bytes)，无换行转换。目录摘要为：相对 POSIX 路径按 UTF-8 字节序排序，逐项拼接 `path UTF-8 + NUL + sha256(file_bytes) ASCII + LF` 后计算 SHA-256；拒绝越界路径和符号链接。文件内容含原有 metadata，不纳入文件系统 mtime/权限。凭据不加入可分享摘要输入。业务文档摘要取固定测试文件原始字节；上游版本 ID 缺失时记 null，不能把抓取时间作为源版本。

## 13. Transaction

配置保存事务包含连接字段、策略、绑定与 revision；不包含网络探测。凭据优先使用既有后端存储：新凭据先写新引用，DB 事务成功后切换引用，旧凭据保留至确认无绑定后清理；DB 失败删除新引用，旧引用保持。

T0=首次 mutation 前的受管配置与凭据引用快照。提交顺序：权限/字段校验 → revision 比较 → 凭据暂存 → DB 事务 → 提交 → 探测独立执行。探测失败不回滚已保存配置，不显示 READY。

失败时受管配置等于 T0。若凭据清理或恢复失败，返回 ROLLBACK_FAILED、保留备份与恢复清单，禁用该连接新调用；人工恢复后再次验证。恢复备份至少保留 7 天且至恢复验收通过，之后由管理员清理；未成功恢复不得删除。验收归 A-02、A-07。

## 14. Failure

| 错误 | 用户状态 | 重试规则 | 副作用 |
|---|---|---|---|
| AUTH_FAILED / ACCESS_DENIED | 凭证失效/无权限 | 修正权限后手动重试 | 不回退更宽凭证 |
| UPSTREAM_TIMEOUT | 知识源超时 | 知识调用不自动重试 | 已有会话保留 |
| UPSTREAM_SCHEMA_INVALID | 版本/协议不兼容 | 修复适配后重试 | 不把响应当空结果 |
| SEARCH_TIMEOUT / 429 / 5xx | 主源异常 | 只切备用一次 | 记录实际源 |
| 成功但无结果 | EMPTY | 用户修改问题后重试 | 不切源、不自动联网 |
| CONFIG_CONFLICT | 配置已被更新 | 刷新后重新编辑 | 0 写入 |
| CANCELLED / INTERRUPTED | 已取消/中断 | 新 operation_id 重试 | 迟到结果不写最终答案 |

## 15. Conflict

相同连接 ID 并发编辑按 revision 拒绝后提交者；相同端点不同凭证作为不同连接，不自动合并；同名知识库显示 ID/来源区分；缺失引用只标记失效，不绑定同名新文档；模型显式请求越界库直接拒绝，不悄悄缩小范围后声称完成原请求。

## 16. Migration 与外部依赖

旧 WeKnora 连接不直接转换为新企业授权配置。启用企业功能时复制为待配置记录，allowed_kb_ids=[]，经管理员选择后激活；旧连接继续按旧功能运行，但不得被新企业会话绕过授权调用。迁移脚本重复执行不得复制第二份记录；通过 origin_connection_id 唯一约束保证。

| 依赖 | 固定要求 | 必需接口/能力 | 不可用行为 |
|---|---|---|---|
| Octop | 上述 SHA 为核查参考；实装 SHA 在 M0 登记 | 连接器、专家绑定、会话与 UI | 未固定版本则 M0 BLOCKED |
| WeKnora | M0 固定实装 SHA 或镜像 digest | GET /knowledge-bases；POST /knowledge-search；GET /knowledge/:id；GET /chunks/:id | 缺接口/授权验证失败则知识发布 BLOCKED |
| 搜索主备供应商 | M2 固定 API 版本与 adapter SHA | 返回标题、URL、摘要和明确错误 | 无主备配置则联网模式不开放 |
| Wails/Desktop | 跟随冻结的 Octop 依赖锁 | 加载共享 UI、打开可信链接 | 桌面回归失败阻止试点 |

知识请求默认维持 30 秒单请求超时；一次操作最多 10 次 WeKnora HTTP 请求，总 deadline 90 秒，任一先触及即失败或返回明确部分阅读状态，不继续生成“完整文档结论”。搜索仅开放已通过合同测试的主备源；具体供应商作为 M2 环境配置输入，不预设购买服务。

## 17. Security

| 威胁 | 控制 | 验收 |
|---|---|---|
| 宽权限凭证导致跨部门可见 | WeKnora 受限 Key＋Octop 有效范围；读文档再次校验 | A-03 |
| 前端/日志泄露凭据 | 后端引用、掩码、事件字段白名单 | A-02/A-07 |
| 文档提示注入 | 证据作为数据，不提升为系统指令；工具仍由服务端策略限制 | A-04/A-06 |
| 任意 URL/SSRF | 端点仅管理员配置；访问限定配置主机、端口及原文受控代理；拒绝重定向跳出允许主机 | A-02/A-04 |
| 路径穿越/符号链接 | 原文通过知识 ID 取回，不接受本地路径；证据写目录拒绝越界及 symlink | A-04/A-07 |
| 任意执行 | 连接器无 shell/代码执行工具；文件预览不执行宏或脚本 | A-04 |
| 内部数据外发 | 仅传用户确认 public_query，不带附件、片段、历史 | A-06 |
| 依赖来源不明 | 固定 SHA/digest，记录构建与版本 | A-01/A-07 |

## 18. Observability

阶段：VALIDATE、AUTHORIZE、LIST、SEARCH、READ、GENERATE、CANCEL、MIGRATE、ROLLBACK、CHECK。

记录 operation_id、connection_id、expert_id、用户内部 ID、stage、status、timestamp、duration_ms、实际 provider、结果数量、error_code、配置 revision。日志不记录 Key、完整问题、原文片段和签名下载 URL。UI 分离“连接成功”“检索可用”“阅读可用”，时间显示为用户本地时区，持久时间统一 UTC。

## 19. Acceptance

所有 A-01 至 A-07 为 Required。每组含下列子用例；子用例任一失败则该组 FAIL。命令由实施者在真实测试环境登记，文档不伪造已存在的测试路径。

| AC / Requirement | Given | When | Then / 机器 Oracle | Evidence |
|---|---|---|---|---|
| A-01 / REQ-01 | 固定版本、两个库、已索引 fixture | 分别列库/检索/阅读 | HTTP 成功且 schema 通过；文档 ID 匹配；缺版本/错误 schema 时 BLOCKED/明确错误 | TEST-A-01、版本 manifest、脱敏响应 |
| A-02 / REQ-02 | 正确/错误凭证、两个相同 revision 的编辑请求 | 探测、并发保存、事务故障注入 | 全探测成功才 READY；错误认证不得 READY；并发仅一条提交；失败配置摘要等于 T0；提交后响应/浏览器持久化存储/普通员工界面/日志 Key 出现次数=0 | TEST-A-02、状态与前后摘要 |
| A-03 / REQ-03 | A 用户仅允许库 A；库 B 含唯一标记 | 正常检索、空选择、越界 ID、直读 B、撤权后重读 | 正常命中 A；空范围拒绝；返回 B 内容、库名、描述次数=0；共享专家也不能扩大用户范围；越界搜索外呼次数=0；撤权后新读拒绝 | TEST-A-03、策略决策与请求计数 |
| A-04 / REQ-04 | 长分块 12,000 字符、失效引用、无页码文件、恶意指令文档 | 展示引用、连续读取、打开原文 | 连续读取拼接摘要=fixture；假引用接受数=0；无页码时不生成页码；禁止路径与宏执行；私有原文访问经过授权 | TEST-A-04、UI 记录、fixture 摘要 |
| A-05 / REQ-05 | 主备 mock 与实际供应商 | 分别注入 timeout/429/5xx/401/空结果 | 可降级错误最多 2 次请求；401/空结果仅 1 次；总 deadline≤22 秒＋测试调度容差1秒；标注实际源 | TEST-A-05、请求轨迹与耗时 |
| A-06 / REQ-06 | 三种模式、内部敏感 canary、公开查询词 | 提问、无命中、取消、混合部分失败 | KB_ONLY 联网调用=0；WEB_ONLY 知识调用=0；外发 query=已确认 query，payload 不含 canary/历史/附件；迟到答案提交=0；部分失败标记存在 | TEST-A-06、外发请求快照、状态轨迹 |
| A-07 / REQ-07 | 旧配置、备份、冻结发布版本 | 迁移两次、回退、桌面回归、运行门禁 | 重复迁移新增数=0；未知资料摘要不变；回退失败备份存在；任何非 PASS 时退出码非0；无范围外新工具 | TEST-A-07、迁移报告、Windows 实机报告、Gate 输出 |

真实问题质量集：20 题，其中至少 5 题精确型号、5 题制度条款、4 题长文、3 题无答案、3 题跨库。业务负责人预标注可接受文档 ID 和关键依据；有答案 17 题中至少 15 题命中预标注证据；3 题无答案均不得编造引用。该计数与 A-03/A-04 共同归档，不能用语言流畅度替代。

## 20. Edge-case Matrix

| 输入类型 | 测试输入 | 预期 |
|---|---|---|
| normal | 已授权已索引文档 | 返回可核对片段 |
| empty | 空选择/成功零命中 | 分别 SCOPE_REQUIRED / EMPTY |
| missing | 文档已删除 | SOURCE_UNAVAILABLE |
| existing | 旧连接存在 | 保留，复制待配置记录 |
| duplicate | 重复知识库 ID、重复迁移 | ID 去重、记录不重复 |
| drifted | revision 已变化 | CONFIG_CONFLICT，0 写入 |
| partial | HYBRID 一个源失败 | 分组保留成功结果并标明部分失败 |
| invalid | 非 HTTP URL、非法响应 | 拒绝配置/UPSTREAM_SCHEMA_INVALID |
| boundary | 8/9 条结果、1,200/1,201、8,000/8,001 字符 | 检索上限明确，截断有标记，连续阅读不丢字 |
| retry | 主源超时、备用也失败 | 两次后终止 |
| failure | Key 撤销、服务断网 | 无新正文，不宽化授权 |
| rollback | 配置写中断 | 等于 T0 或保留恢复材料并禁用 |

## 21. Negative Acceptance

所有 DENY 规则均加入对应 AC：A-01 禁止 mock 冒充实机；A-02 禁止凭据暴露/越权配置；A-03 禁止空范围全库查询及直读越权；A-04 禁止假引用、猜测页码、任意路径与文档指令执行；A-05 禁止无界重试与认证失败切源；A-06 禁止静默联网和自动附加内网内容；A-07 禁止范围外功能、未知数据删除与未验收发布。

## 22. Failure Injection

| 注入点 | 确定后置条件 | AC |
|---|---|---|
| 暂存凭据前失败 | 配置= T0，无新凭据 | A-02 |
| 暂存后、DB提交前失败 | 配置=T0，新凭据清理；清理失败保留恢复清单 | A-02 |
| DB提交后探测失败 | 配置已保存，状态非 READY | A-02 |
| 检索后读取前撤权 | 阅读拒绝，不展示新正文 | A-03 |
| 第一个阅读页后网络中断 | 已返回内容标注不完整，保留 continuation | A-04 |
| 主源超时/备用失败 | 两次后 FAILED，记录两次错误 | A-05 |
| 用户取消后响应到达 | 不写最终答案 | A-06 |
| 迁移期间或回退期间失败 | T0 恢复，或 ROLLBACK_FAILED＋备份保留 | A-07 |

## 23. Evidence

每个 EVID-A-xx.json 包含：acceptance_id、requirement_ids、test_ids、status、command、exit_code、oracle.expected、oracle.actual、evidence_files、repo、branch、commit_sha、timestamp、tool_versions、fixture_digest、consumer_identity。文件摘要按第12节计算。凭据与内部业务原文不进入可外发报告。

当前所有实机 AC 状态为 NOT_RUN（发布检查时按 BLOCKED 处理），本文不是 PASS 证据。Golden Consumer 需记录实装 SHA、clean/dirty、变更前后快照、测试业务源摘要与用户资料摘要。mock 覆盖错误分支，实机覆盖服务兼容、真实权限和桌面体验，两者均需存在。

## 24. Traceability

| Requirement | Invariant | Acceptance | Test | Evidence | Gate |
|---|---|---|---|---|---|
| REQ-01 | INV-01 | A-01 | TEST-A-01 | EVID-A-01.json | Required |
| REQ-02 | INV-02 | A-02 | TEST-A-02 | EVID-A-02.json | Required |
| REQ-03 | INV-03 | A-03 | TEST-A-03 | EVID-A-03.json | Required |
| REQ-04 | INV-04 | A-04 | TEST-A-04 | EVID-A-04.json | Required |
| REQ-05 | INV-05 | A-05 | TEST-A-05 | EVID-A-05.json | Required |
| REQ-06 | INV-06 | A-06 | TEST-A-06 | EVID-A-06.json | Required |
| REQ-07 | INV-07 | A-07 | TEST-A-07 | EVID-A-07.json | Required |

## 25. Release Gate

Alpha.1 发布要求：A-01～A-07 全部 PASS、20题质量集达到阈值、Windows 实机回归通过、无未关闭权限越界问题、回退演练通过。FAIL/SKIPPED/BLOCKED 均不等于 PASS，发布检查进程返回非0。

如果知识闭环先完成，可交付内部验证构建且关闭联网入口；不能把该构建标记为完整 Alpha.1 验收通过。

## 26. Plan Generation Gate

本稿状态为 REVIEW。用户已选择 WeKnora 优先路线，不等于所有本稿新增细节已批准。实施路线用于排期评审；正式机器执行 `.plan.md` 在本稿 APPROVED_FOR_PLAN 且对应工作包无 SPEC_SEMANTIC_GAP 后生成。

M0 可开展只读源码与环境核查。M1 开工输入：企业 Fork/分支、实际双方版本、WeKnora 地址、凭证配置完成、测试文档完成索引、连接管理权限可用。M2 开工输入：两家搜索源的有效环境配置与已验证接口。缺输入时只阻塞对应工作包，禁止凭空填写版本或密钥。

实现者可决定组件拆分、测试框架与数据库表名；不能自行改变范围交集、默认空集合、外发策略、失败状态与验收阈值。

## 27. Definition of Done

产品文档：范围、SOT、状态、Schema、REQ、负向用例、事务和证据已定义；工程评审通过后登记 APPROVED_FOR_PLAN。

开发完成：功能代码、兼容迁移、合同测试和故障注入测试完成，标记 implemented。

验证完成：真实环境与 synthetic fixture 均有执行证据，所有 Required AC PASS，标记 verified。

发布完成：冻结构建与版本，试点用户清单、回退包、诊断说明齐全。未完成任何 Required AC，不标记 VERIFIED。

## 28. 第一阶段功能实施计划

### 28.1 工作包与依赖

资源假设：1名前端、1名后端、测试与运维兼职支持。总历时估算约 4～6 周；M0发现接口或权限不兼容时重新估算，不把固定工期作为承诺。

| 包 | 时间估算 | 功能交付 | 依赖 | 责任角色 | 完成门槛 |
|---|---|---|---|---|---|
| M0 基线验证 | 3～5工作日 | 双方版本、三个工具联调、权限/截断差距报告 | 环境输入 | 后端＋运维＋测试 | A-01；确定能复用的路径 |
| M1 企业知识闭环 | 7～10工作日 | 连接分步诊断、专家绑定、范围选择、引用与连续阅读 | M0 | 前后端＋测试 | A-02～A-04 |
| M2 搜索源强化 | 4～6工作日 | 主备源管理、测试与降级、来源显示 | 供应商输入；复用 M1 配置基础 | 后端＋前端 | A-05 |
| M3 会话融合 | 3～5工作日 | 三模式、公开查询确认、混合证据及取消 | M1＋M2 | 前后端＋测试 | A-06 |
| M4 企业试点 | 3～5工作日 | 迁移、回退、Windows打包回归、质量集与证据 | M1～M3 | 测试＋运维＋业务负责人 | A-07及全部Release Gate |

### 28.2 开发任务登记

以下为产品级工作分解，不是已批准的可执行 `.plan.md`。所有任务当前为 planned；实际环境未配置的运行验证为 blocked。

| 任务 | 需求/验收 | 文件或模块入口 | 实施目标/状态转移 | 副作用与失败 | 验证/证据 |
|---|---|---|---|---|---|
| T01 | REQ-01/A-01 | gateway/adapters/weknora.py；部署配置 | 固定基线并完成 UNVERIFIED→VERIFIED | 仅证据；缺版本 BLOCKED | 三 API 实机报告 |
| T02 | REQ-02/A-02 | connectors/catalog.json、probe.py；连接设置 UI | 分步探测、revision 与专家绑定 | 配置事务；失败回 T0 | 状态与并发/回滚测试 |
| T03 | REQ-03/A-03 | gateway adapter 与工具执行前策略层 | 有效范围交集；文档 ID 再授权 | 只读；越界拒绝 | 双用户/双库请求轨迹 |
| T04 | REQ-04/A-04 | adapter 阅读接口；Chat 引用展示 | 标准化证据、连续阅读、受控原文 | 会话证据；原文不可用降级文本 | 长文本摘要、UI回归 |
| T05 | REQ-05/A-05 | 搜索 provider 配置/调用层，具体入口 M0 定位 | 主备管理、deadline与错误分类 | 网络与统计；只切换一次 | 错误注入与实源证据 |
| T06 | REQ-06/A-06 | Chat 工具栏、会话策略、证据输出 | 三模式与公开查询确认 | 会话设置；取消丢弃迟到结果 | 模式矩阵、外发快照 |
| T07 | REQ-07/A-07 | 配置迁移、功能开关、发布流程 | 老配置保留、试点与回退 | 新增受管字段；失败保留备份 | Gate、Windows与恢复报告 |

现有路径是改造定位入口，不保证企业 Fork 文件完全一致；新策略层、证据 DTO、连续阅读输出是拟新增设计。T02前需读取现有认证/凭据存储代码，选取其事务机制；不得另存明文 Key。

### 28.3 页面交付范围

| 页面 | 必需操作 | 可见状态 |
|---|---|---|
| 连接器设置 | 新增/编辑 WeKnora、选择允许库、分步测试、禁用 | 配置有效性、三步结果、最近探测时间 |
| 专家设置 | 绑定连接、允许库、默认库、联网许可 | 未配置/可用/失效 |
| 聊天工具栏 | 知识库选择、模式切换、输入确认公开查询词 | 当前范围、当前模式、不可用原因 |
| 回答引用区 | 内部/外部分组、点击证据、显示截断 | 真实来源、检索时间、部分失败 |
| 阅读面板 | 分块连续阅读、存在时打开原文 | 加载/续读/无权限/原文不可用 |
| 搜索源设置 | 主备配置、测试、查看降级 | 可用/失效/限流/超时 |

### 28.4 第一轮验证输入清单

- Octop 实装版本及 SHA、企业 Fork/分支、桌面连接本地或服务器专家的拓扑。
- WeKnora 实装版本及 digest、从专家运行主机可达的服务地址。
- 两个知识库、两组不同权限凭据、10份代表性测试资料及20题质量集。
- 凭据在部署环境配置；报告只记录凭据引用和权限范围。
- 主备搜索源在 M2 前配置完成，不阻塞 M0/M1。

### 28.5 阶段性交付件

M0：兼容矩阵、实机请求结果、差距清单。M1：知识功能验证构建、权限与引用测试报告。M2/M3：搜索与三模式验证构建、外发/降级证据。M4：Alpha.1 安装包、版本清单、迁移与回退说明、验收证据包、业务试点结论。

## 29. 后续路线与本期变更控制

后续候选：桌面导入企业知识、目录增量同步、网页人工确认入库、文档版本维护、更多知识后端、业务系统受控 Action。候选功能不承诺日期，不计入本期验收。

增加范围须记录需求 ID、业务收益、权限与副作用、对工期及验收的影响；评审后更新文档版本。代码审查顺序为需求、状态事实源、副作用、失败、所有权、验收证据，再审代码质量。

## 30. 参考与证据边界

1. [Octop 固定基线 WeKnora adapter](https://github.com/TencentCloud/Octop/blob/dd9dce470844c25d59effb50615bb3c667ea25cd/src/octop/infra/connectors/gateway/adapters/weknora.py)：已读取源码，支撑第2节工具行为。
2. [Octop 连接器目录](https://github.com/TencentCloud/Octop/blob/dd9dce470844c25d59effb50615bb3c667ea25cd/src/octop/infra/connectors/catalog.json)：连接配置字段。
3. [Octop 官方连接器说明](https://github.com/TencentCloud/Octop/blob/main/docs/connectors.md)：gateway 模式与目录入口。
4. [WeKnora 知识 API 文档](https://github.com/Tencent/WeKnora/blob/main/website-docs/04-api/02-api-knowledge.md)：权限与接口验证参考，实装兼容以 M0 为准。
5. [WeKnora 检索 API](https://github.com/Tencent/WeKnora/blob/main/docs/api/knowledge-search.md)：检索参数参考。
6. 《需求PRD工程模板.md》v1.0：已读取，采用其最小完整结构与证据门禁。

产品方案与数值上限除第2节既有实现外，均是本稿目标契约；未声称其已存在于上游或企业部署。
