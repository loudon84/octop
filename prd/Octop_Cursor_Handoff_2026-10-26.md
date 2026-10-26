# Octop 第一阶段交接与后续开发规划（Cursor Handoff）

> 2026-10-26 后续更新：本文主体保留 `74dc70d8` 时点的历史交接。当前已新增提交 `6dcdb896`，完成搜索设置后端及 pre_send；“未实现 search settings”“P10b1 NOT READY”等旧描述不代表现状。下一阶段以 [Octop_下一阶段开发计划与实施明细_v1.0.md](./Octop_下一阶段开发计划与实施明细_v1.0.md) 和 execution-status 最新段为准。本次更新只编写规划，未启动后续功能编码。

更新日期：2026-10-26。代码基线：`74dc70d81753488cce48dc2e107e19f46dc5a741`，分支 `codex/enterprise-p1`。

本文用于接手已有实现、识别剩余工作和安排下一批开发。需求与完整验收场景仍以已评审 OpenSpec 和实施计划为准；本文不另建任务事实源，也不宣布第一阶段交付完成。

## 1. 先打开正确的代码目录

| 项目 | 当前事实 |
|---|---|
| 实际开发 worktree | `C:\Users\Administrator\.codex\worktrees\enterprise-p1\octop` |
| 开发分支及提交 | `codex/enterprise-p1`；`74dc70d8`，本机已提交，尚未推送 |
| 提交内容 | 企业插件、通用 Host、UI 桥接、私库基础、规格与测试等累计实现；297 个文件 |
| 主仓目录 | `E:\git\octop` 当前位于 `main`，另有未跟踪的 `.agents/`、`openspec/`、`prd/`；不要直接覆盖、清理或在这里重做功能 |
| 当前工作状态 | 功能阶段按用户指示暂停；提交阻塞已修复；本次只创建交接文档 |
| 文档位置 | 开发 worktree 的 `prd/` 为后续维护位置；主仓 `prd/` 同名副本供导航，本次内容一致 |

Cursor 优先直接打开上述开发 worktree。其 Git 元数据依赖主仓，保留 `E:\git\octop`。如以后需要迁回主仓或换机器，先核对分支、未跟踪文件和提交传递方式，不假定远端已经有此提交。

接手先执行只读检查：

```powershell
Set-Location 'C:\Users\Administrator\.codex\worktrees\enterprise-p1\octop'
git branch --show-current
git rev-parse HEAD
git status --short
```

代码提交完成时工作区干净。本交接文档是在该提交之后新建，尚未提交；看到它未跟踪属于预期。本文链接指向本机开发 worktree；换机器后按链接标签中的仓库相对路径定位。

## 2. 先读哪些材料

按以下顺序建立上下文，不必重新阅读全部对话或逐份重复历史审查。

| 顺序 | 入口 | 用途 |
|---|---|---|
| 1 | [AGENTS.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/AGENTS.md) | 模块依赖、持久化、i18n、服务器时区、Windows、提交门槛 |
| 2 | [prd/evidence/enterprise-p1/execution-status.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/execution-status.md) | 最新验证与各已接受子批；先读 2026-10-26 最新段 |
| 3 | [prd/Octop_第一阶段二开实施计划_v1.0.plan.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/Octop_第一阶段二开实施计划_v1.0.plan.md) | PLG-01～11、P01～P15、代码归属与验收映射 |
| 4 | [openspec/changes/enterprise-knowledge-p1/tasks.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/openspec/changes/enterprise-knowledge-p1/tasks.md) | 主阶段任务；同目录 proposal/specs/design 给出行为要求 |
| 5 | [openspec/changes/enterprise-knowledge-plugin-ui/tasks.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/openspec/changes/enterprise-knowledge-plugin-ui/tasks.md) | 页面行为与尚未完成的组件/实机任务 |
| 6 | [openspec/changes/enterprise-user-private-knowledge/tasks.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/openspec/changes/enterprise-user-private-knowledge/tasks.md) | 用户邮箱、独立私库、共享来源增量 |
| 7 | [prd/evidence/enterprise-p1/core-delta.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/core-delta.md)、[plugin-host-v1.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/plugin-host-v1.md)、[runtime-boundary.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/runtime-boundary.md) | 核心白名单、Host 接口、真实执行边界 |
| 8 | [prd/Octop_产品定义与第一阶段二开实施计划_v1.0.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/Octop_产品定义与第一阶段二开实施计划_v1.0.md) | 产品需求、A-01～A-07、质量集、发布门禁 |

原计划中“当前 main”“产品代码尚未实施”“最终全仓待跑”等文字有历史背景；当前实现和最近验证以本文基线及 execution-status 最新段为准。产品规则则继续按已审规格执行，不用状态描述替代需求。

本机 `.superpowers/sdd/Octop_第一阶段二开实施计划_v1.0.plan/` 保存了各批 brief、编辑前基线、独立 review 和完整门禁日志，但属于未入 Git 的工作记录。它可辅助查证，不是 Cursor 接手的必需隐藏依赖；不要假定其他机器存在该目录或能恢复旧 Codex 子代理。

## 3. 已确认的用户需求与边界

1. 本机只开发 Octop；WeKnora 已在其他服务器部署，不在本机重复部署。
2. 企业业务以 Octop 插件交付：`enterprise-knowledge` 为 tool 插件，`enterprise-policy` 为配套 hook 插件。Host 核心只提供通用能力。
3. 页面变动先更新并审查 OpenSpec，再实施；settings/composer 为固定挂载点，证据使用 renderer/侧栏，不扩建任意页面插件平台。
4. 新账号以唯一规范邮箱，1:1 对应 WeKnora 用户与独立私库；同时保留显式授权的企业共享库。
5. 本地注册成功不等待远端开通；知识功能可以待开通，后台对账与重试。既有远端用户不能被接管，也不能靠重置密码“恢复”关系。
6. 短期引导凭据到期立即禁用；运行时清理，停机则恢复后立即清理。实际清理 worker 仍待完成。
7. 搜索固定为企业 SearXNG 主源、Firecrawl 备用源；部署地址、版本、认证等由操作者配置。
8. WeKnora 无法完成的远端验证转人工；这不删除私库需求，也不把未实现代码视为已交付。恢复开发时，优先推进可本地验证的搜索、模式及 UI。
9. 用户偏好功能/测试代码由 GPT-6 Luna 子代理实现，控制器负责契约、任务拆解和独立审查。Cursor 若不具备相同模型/子代理能力，应如实说明能力差异，不伪造调用记录。
10. 当前暂停没有因创建本文而自动解除。后续按用户在 Cursor 中给出的启动和范围指令执行；提交、推送、发布分别遵循用户授权。

## 4. 完成进度：区分子批接受与产品交付

计数于本交接生成时重新核对 OpenSpec 复选框：

| 范围 | 已勾选 / 总数 | 含义 |
|---|---:|---|
| 主计划 `enterprise-knowledge-p1` | **15 / 29** | 通用基础与共享知识主链已有实现；搜索、模式、生命周期收尾、迁移和交付门禁未完成 |
| 插件 UI `enterprise-knowledge-plugin-ui` | **5 / 13** | 通用页面桥接、设置包和共享设置已完成；业务 composer、搜索设置、完整证据流程及实机验收仍有缺口 |
| 私库增量 `enterprise-user-private-knowledge` | **6 / 18** | 邮箱、可信身份、后台工作框架、保留记录及当前权限复核完成；实际开通生产者、联合来源与页面未交付 |

这些是大任务计数，多个已接受子批可能仍不足以勾选一个大任务。不能由“全仓测试通过”直接推导所有产品任务完成；例如主任务 7.3 还要求插件 Python/UI 的完整联合质量门槛。

### 4.1 已有实现，可复用

| 领域 / 子批 | 已有能力 | 接手时保留的限制 |
|---|---|---|
| P03 通用 Host | 命名空间记录、CAS/原子前提、加密凭据 stage/commit、认证动作/资源/schema、生命周期与旧 facade 失效 | 不等于全部业务 operation 的重启恢复和 P13 验收完成 |
| P03d / P07 执行接线 | 可信 actor/ticket、required-plugin、实际 SDK 注册/图准入、只读能力；配套真实知识工具与 policy hook | 不能以提示词或 UI metadata 替代权限，不放开旧工具旁路 |
| P04 / P05 共享知识 | 连接、员工 grant、专家 binding、分项探测、五层授权交集、严格 WeKnora HTTP/DTO 映射 | 有本地/mock 证据，缺真实部署非空检索和双身份隔离证明 |
| P06 连续阅读 | 证据来源校验、cursor、8,000 字符窗口、源变化及撤权处理 | 原文目前可鉴权后返回不可用降级；不能声称实际下载/预览已验证 |
| P08a/b/c、P09a | 通用 requirements/readiness、settings/composer surface、发送与队列/编辑/草稿快照、工具侧栏 Host 桥接 | 通用 composer 桥接不等于插件业务选库/三模式组件完成 |
| P08d 共享设置 UI | 独立 UI 包、连接/员工授权/专家绑定、Key 清空、revision/迟到响应、部分真实浏览器操作证据 | 尚无完整搜索设置、私库管理与全流程无障碍/Windows 验收 |
| P09b 证据阅读器 | 实际插件 renderer、服务端 `evidence.list`、卡片/侧栏、续读、撤权锁存、源变化重启、WEB 摘要显示 | 本地独立审查通过；UI 3.3 与 4.3 仍待真实挂载、历史及完整流程验收，复用现有实现 |
| P09c1 展示上下文 | locale/theme 实时刷新，保留 Host 身份、挂载和 reader 状态；旧目标 Host 失效 | 不是刷新授权或切换线程的捷径 |
| 私库 I01/I02 | 通用邮箱策略、身份分页/提交前提、Host 工作生命周期、保留记录、fresh actor 权限复核 | 这些是框架基础，尚无完整远端开通 worker |
| 私库 I03a | 严格 DTO、开通配置动作、状态/重试动作及记录前提 | 私库 tasks 3.1 仍未完整交付，不能视作用户/KB 已自动创建 |
| P10a 单源搜索 adapter | `search_http.py` 与 MockTransport 测试已提交；作者定向 125 项通过，当前全仓也通过 | **尚缺独立功能 Spec/Quality 验收**；没有 search actions、全操作去重/主备编排或员工联网就绪 |

### 4.2 尚停在规划或验收阶段

- **P10b1 搜索设置服务：NOT READY 草案。** `search.get/save/test` 的管理员诊断语义、物理发送前权限复核和残留凭据恢复尚需冻结，不能直接照候选 DTO 编码。
- **I03b1 私库远端 wire：合同规划已审，代码未实施。** 按用户此前优先级后移；精确合同见 [provisioning-wire-contract.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/provisioning-wire-contract.md)。
- **P09c2 知识浏览器 fixture：草案未就绪，未启动实现。** 不把计划、MockTransport 或组件渲染当作真实部署联调完成。
- `search.py`、`search_settings.py`、`provisioning_wire.py` 等可能出现在规划中；实现前检查文件是否已存在，不将拟建路径当成现有接口。

## 5. 当前代码导航

| 职责 | 当前主要位置 |
|---|---|
| Host 协议、身份、记录/凭据与执行 | `src/octop/infra/agents/plugins/{host_contracts,host,execution}.py`；[docs/plugin-host.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/docs/plugin-host.md) |
| 仓储、依赖注入和迁移 | `src/octop/infra/db/repos/plugin_*.py`、`src/octop/infra/db/services.py`、`022_plugin_host_storage.sql/.pg.sql` |
| HTTP 薄适配 | `src/octop/api/routers/plugin_actions.py`；聊天 models/turn；`src/octop/infra/bridge/peer_turn.py` 的字段映射 |
| 知识插件 | [plugins/enterprise-knowledge/main.py](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/plugins/enterprise-knowledge/main.py)、`enterprise_knowledge/` 内 connections/policy/weknora/probes/service/tools/reading/evidence/sessions/operations |
| 私库基础 | 同包 provisioning_models/provisioning_records/provisioning/provisioning_state/provisioning_actions |
| 搜索现有 adapter | [plugins/enterprise-knowledge/enterprise_knowledge/search_http.py](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/plugins/enterprise-knowledge/enterprise_knowledge/search_http.py) |
| 策略 hook | [plugins/enterprise-policy/main.py](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/plugins/enterprise-policy/main.py)、`enterprise_policy/policy.py` |
| 插件 UI | `plugins/enterprise-knowledge/ui/src/{index,api,host,i18n,Settings,EvidenceRenderer}.*`；包入口 `ui/dist/index.js` |
| Dashboard 通用桥接 | `dashboard/src/plugins/toolRenderers/`、`pages/Chat/ChatToolDockContext.tsx`、ChatInput/MessageBubble/chatStore/useChatSend 等 |
| 测试 | `tests/unit/enterprise/`、`tests/unit/plugins/`、`tests/integration/test_enterprise_plugin_*.py`；插件 UI 自身测试在插件包中 |

`src/octop/dashboard/` 是构建产物，改界面用 `dashboard/` 或插件 UI 源码。当前数据库源码断言为 **v22**；AGENTS 中 v20 示例落后于本分支，不能据此降水位或重复创建迁移。后续迁移遵循仓库规则，并核实实际已应用水位和发布情况。

## 6. 不可绕过的实现约束

- 企业知识/搜索/授权算法、供应商 URL、业务 timeout、业务 DTO 和页面放插件；核心只增加已说明的通用薄桥接，改白名单外文件前先补 `core-delta.md` 与设计依据。
- 插件使用 Host 取得可信身份、存储、凭据和事务；不导入 Octop 私有 repo/manager，不直接读写主库或密钥文件。
- Python 插件采用包内相对导入；实际 loader 使用动态包上下文。不要修改 `.venv`，不要跨插件按 `harness_plugin_*` 模块名导入。
- 共享库范围每次重算：凭据可见 ∩ 企业 ∩ 员工 ∩ 专家 ∩ 会话选择。显式越界整单拒绝；列库、直读、续读、历史打开重新授权。
- 私库来源要按真实 owner、tenant、凭据区分；远端相同 KB/文档 ID 不等于同一来源，不能直接并入原生 `knowledge_base_ids`。
- WeKnora 每请求 30 秒、每操作总 90 秒、最多 10 次 HTTP、无自动重试；检索最多 8 条 × 1,200 字符；阅读每次 8,000 字符。缺元数据时明确降级/失败，不猜页码或原文 URL。
- 联网单源 10 秒、操作总 22 秒、最多两次供应商请求；只对 timeout/429/5xx 切一次备用；401/403、协议错误和成功 EMPTY 不切。单 adapter 的“一次请求”不等于全 operation 的最多两次。
- 默认 KB_ONLY；WEB_ONLY 对 WeKnora 调用/探测为零；HYBRID 顺序由服务端编排保证。插件模式与 Ask/Plan/Craft 独立。
- 外发 query 逐字符等于用户确认值；不 trim、改写或拼接历史、附件、内部片段。批准与真实用户/线程/专家/operation/revision 绑定，一次消费。
- schema 为 `octop.enterprise.knowledge.v1`、version=1；新增 DTO 严格校验，服务端生成真实 UUID v4 引用。UI `patchResult` 不产生可信证据。
- 企业功能默认关闭；缺配套 hook、版本不匹配、禁用/卸载时拒绝新企业调用，不能自动退化成宽权限普通会话。
- 服务端/插件 UI 保持 en/zh；用户时间按服务器 `default_timezone`。凭据不进前端持久存储、Agent 明文配置、日志或提交。

## 7. 恢复开发后的建议顺序

以下为对现有任务的执行排序，不重置原复选框，不承诺新的日历工期。每个批次继续拆成有精确文件范围、输入输出、失败语义和验收的薄片。

| 批次 | 开发需求 / 对应任务 | 前置与完成门槛 |
|---|---|---|
| R0 基线接管 | 核对提交、差异、OpenSpec 和本文；形成下一薄片 brief | 不重做已接受 Host/reader/locale-theme；先核对当前代码，避免复用过期草稿 |
| R1 P10a 验收 | 独立审查现有 SearXNG/Firecrawl adapter；主任务 5.1 的本地部分 | 对照 [search-http-adapters-contract.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/search-http-adapters-contract.md)，关闭真实问题后再给后续调用；真实供应商验证单列 |
| R2 搜索配置后端 | 冻结诊断规格，分片实现 `search.get/save/test`、revision/凭据与诊断；主 5.3 | 先收敛下节四项决策；真实 Host/SQLite + MockTransport，管理员限制、KEEP/REPLACE/CLEAR、迟到结果/CAS、安全错误均可证明 |
| R3 受控搜索编排 | operation 持久去重/领取、主备错误矩阵、10/22 秒和最多两次、WEB evidence；主 5.2 | 不依赖模型控制重试；同 operation 并发/重复调用不重新外发；取消、权限变更和迟到结果有明确终态 |
| R4 搜索设置 UI | 插件设置中配置、分别诊断两源、revision 与就绪提示；UI 2.4 | 后端 action DTO 已冻结；一源不能联网，诊断不授予员工搜索许可；独立 ESM/typecheck/test/build |
| R5 Composer 与三模式 | 会话选库、来源快照、三模式、精确公开 query 确认/消费；主 5.4、UI 3.1/3.2 | 复用通用发送/队列桥接；覆盖主动清空、跨线程/身份、双击/快捷键、canary、WEB_ONLY 零 WeKnora；KB_ONLY 先形成可验证切片 |
| R6 私库生产者 | I03a 剩余映射/领取、I03b wire、后台对账/退避/重试/清理；私库 1.2、3.1～3.3 | 先完整冻结 action/Host 合同；真实 HTTP mock 覆盖未知结果、不接管、不盲建、24h 到期；实装认证由人工对接，不能以缺远端跳过本地实现 |
| R7 私有/共享来源与页面 | 来源 DTO、联合检索、证据/续读关联、邮箱表单及开通/员工页面；私库 4.1～4.3、5.1～5.3 | 依赖生产者/来源合同；先审 UI specs；无企业 grant 仍可用自己私库，不能读取他人库或串 tenant，联合调用共享既定预算 |
| R8 证据及真实交互收尾 | 主 3.5/4.2、UI 3.3/3.4/4.1～4.3 | 复用已有 reader，补真实已安装包、历史重开、撤权、来源变化、键盘/读屏、中英文与服务器时区证据；不要重写阅读器 |
| R9 生命周期与迁移 | P13/P14；主 6.1～6.3 | operation 取消/重启/禁用终态、旧 approval/实例失效；显式 legacy 导入幂等；T0/未知资料摘要、配套升级/回退，备份至少 7 天且恢复通过 |
| R10 打包与交付 | P15；主 7.1～7.4、私库 6.1/6.2 | 两个单根 ZIP、UI 产物/版本/摘要/兼容 manifest、核心边界复核、完整 PG/Windows/人工远端证据、20题 Oracle 与非零失败 gate |

R6/R7 的远端验证已转人工，可以在明确合同下做本地 mock 开发；当前优先级仍是先搜索/模式/UI。协议冻结后可安排独立文件的测试或 UI 工作；共享 contracts、Settings、sessions、policy 等文件由一个功能 writer 串行修改。每轮审查只复查本轮增量与具体验证，避免反复重跑已经通过的大范围用例。

### 7.1 P10b1 开始编码前必须收敛的四项问题

1. **管理员诊断与员工批准不同。** 设置页 `search.test` 在无聊天上下文时如何绑定“当前管理员、保存来源、配置 revision、明确确认的公开 query”，需要在规格中明确；不能伪造线程/专家或消费员工 approval。一次只测所选源，不 fallback、不发布证据、不调用模型。
2. **实际发送前的授权时点。** 当前 P10a 在异步 TLS 准备后发送，没有已冻结的 trusted pre-send 回调。调用前和返回前检查不能证明等待期间降权会在物理发送前阻止请求。若要求该保证，先单独审查最小兼容 callback 增量、原 10 秒预算、拒绝/超时零发送测试；不能让 settings 调用不存在的接口。
3. **凭据准备记录的残留恢复。** staged/active、取消、提交未知和清理失败需明确可恢复状态；不能假定 Host 有自动 GC，也不能把无法证明的 release 标成成功。未决状态下的 get/save/test 阻断与人工/安全恢复入口须形成合同。
4. **DTO 和可信部署输入。** 固定主备角色、expected_revision、KEEP/REPLACE/CLEAR、独立诊断记录、版本仅 reported、后端同源关系 UNKNOWN、CA 由可信入口提供等都要冻结。候选字段不是已接受 API，不把浏览器任意文件路径或环境变量自动加载塞入现有 Host。

本机完整草案位于 `.superpowers/sdd/Octop_第一阶段二开实施计划_v1.0.plan/task-P10b1-search-settings-draft.md`，状态 NOT READY。上面四项已提取到本文，接手不依赖该未跟踪草案存在。

## 8. 人工配置与远端验收交接

详见 [prd/evidence/enterprise-p1/weknora-manual-handoff.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/evidence/enterprise-p1/weknora-manual-handoff.md)、[prd/config/enterprise-dev.env.example](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/config/enterprise-dev.env.example) 和 [prd/config/README.md](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/prd/config/README.md)。本文不复制真实地址、API Key、管理员 JWT、密码或登录 session。

- WeKnora 版本目前是用户报告的 **0.8.3**，真实 digest/commit 未核验。既往只读证据仅 4 GET、可达但列表 0 库；没有非空搜索/阅读、实际用户/KB/Key 开通和两身份隔离证据。
- 人工需补 digest/CA、两组有索引资料的库/文档及身份、Owner 与最小 scoped Key、非空 API 响应、权限负例、连续阅读和质量集结果。
- 共享检索 Key 不等于系统管理员 JWT。注册/KB/最小 Key 开通能力和身份凭据必须按实际服务验证。
- 过去仅授权了指定测试 origin 的 HTTP 例外，变量为 `OCTOP_WEKNORA_TEST_HTTP_ORIGIN`；其余远程入口保留 HTTPS 默认，不能改成全局允许 HTTP。
- WeKnora 变量包括 `OCTOP_WEKNORA_BASE_URL/API_KEY/VERSION/IMAGE_DIGEST/CA_BUNDLE/TENANT_ID/TEST_KB_IDS/TEST_DOCUMENT_IDS/PROBE_QUERY/CONSUMER_IDENTITY`，完整名称以模板为准。历史记录表明 Key 配置过 Windows 用户环境，但 Cursor 进程是否已继承需由操作者确认存在性，不打印值。
- 搜索输入为 `OCTOP_SEARXNG_BASE_URL/VERSION/CA_BUNDLE`、`OCTOP_FIRECRAWL_BASE_URL/VERSION/CA_BUNDLE/SEARCH_BACKEND/API_KEY` 对应完整变量名。它们是手工配置入口，不等于已完成运行时设置/Host 凭据接线。
- 模板中的 `OCTOP_SEARXNG_API_KEY` 是历史预留；**当前 P10a 不支持 SearXNG 凭据**，不得据此发明认证 header。确有企业网关认证时先补合同。Firecrawl 只有配置时才使用可选 Bearer。
- 人工验证不能替代尚未实现的私库 worker。报告只回填非敏感 ID、状态码、请求数、版本和脱敏结构；没有证据的条目继续待验证。

不自动运行 live probe、远端创建/删除、部署或读取全部环境变量。必要实机操作沿用用户明确的对接范围，并使用独立测试资源。

## 9. 页面接手要求

页面的需求归属与入口、权限、加载/空态/错误/成功/冲突、退出/切换、en/zh、键盘/读屏，都先在相应 OpenSpec change 里写明并审查。已有 change 可增量修订，不按每个组件重复新建 spec。

当前源码里已有共享 Settings 与 EvidenceRenderer；不要因大任务未勾选就重新实现它们。需要补的是业务 composer/搜索设置/私库状态与来源，以及现有证据链的真实挂载、历史与联合来源接线。

源码构建成功不等于测试服务器已经加载新插件。上一轮浏览器环境曾加载旧插件副本；重新验收前核对实际安装的两个插件和 UI manifest/版本。临时端口、旧登录 session 与后台进程不作为交接前提；使用独立测试 home，走正常认证和真实 Host/renderer 路径。可替换上游 HTTP 为 mock，但不能绕过后端授权或直接伪造可信 evidence 来宣称全链路通过。

## 10. 已通过的验证与后续验证方式

### 10.1 本次提交的真实结果

- `make all`：**退出码 0**；Ruff/format、mypy 605 个源文件通过；全量非 live **5,674 passed、236 skipped、88 warnings**，746.32 秒，4 workers。
- 该全量运行启用 testmon 依赖记录、明确关闭增量筛选，执行全部 5,910 个非 live 用例。随后原提交钩子正常筛选为 0 changed / 1,142 unchanged，未重复选中测试；不是跳过钩子。
- 原钩子的 `make precommit` 和 dashboard `tsc -b && vite build` 均通过；生产构建 30.55 秒。提交后工作区干净，未推送。
- 88 条告警包括第三方弃用和 Windows 浏览器安装测试的子进程解码告警，已记录。部分 PG 用例以前有实库证据，但完整 PG/远端/Windows 产品验收仍未完成；236 skipped 不能记成这些能力通过。
- `make all` 不自动覆盖插件 Python 的全部静态检查、插件 UI 独立质量门槛或真实远端；不能直接勾选 P15 的全部任务。

### 10.2 修改后的检查

先运行与实际改动相符的定向检查；关闭问题后执行仓库必需门槛。接手只读/写文档时不用再次跑全仓。

本机 Windows 的已验证 Make 环境：

```powershell
$env:PYTHONUTF8 = '1'
$env:PATH = 'D:/Git/usr/bin;D:/Git/bin;' + $env:PATH
make 'SHELL=D:/Git/bin/bash.exe' all PYTEST_JOBS=4
```

其他机器以实际 Bash/Make 路径为准。保留 `uv run pytest`，不使用裸 `pytest`。新增后端用户文案时检查 en/zh；涉及通用 ErrorCode 时同步 Dashboard apiErrors。

插件代码另外按变更路径执行 Ruff/format/strict mypy。例如现有 P10a 的精确测试文件为 `tests/unit/plugins/test_enterprise_search_http.py`。不要照抄旧计划中尚未创建的 test 文件或发布脚本当作现有门槛。

插件 UI 的当前可用脚本：

```powershell
npm --prefix dashboard ci
npm --prefix plugins/enterprise-knowledge/ui run typecheck
npm --prefix plugins/enterprise-knowledge/ui run test
npm --prefix plugins/enterprise-knowledge/ui run build
```

插件 UI 的 typecheck/test 复用 `dashboard/node_modules` 工具。当前插件 package.json **没有 lint 脚本**；格式/ESLint 按实际配置与变更路径核查，不直接执行旧计划中的不存在命令。宿主 UI 变更仍需 Dashboard tsc/lint/适当测试及构建；`src/octop/dashboard/` 由构建生成。

提交时保留 `core.hooksPath=.githooks`。本机 hook 内调用 Make 需要进程环境 `MAKEFLAGS='SHELL=D:/Git/bin/bash.exe'`；允许用已支持的 4 workers，不改 hook 来回避失败。不要把全量记录用的 `--testmon-noselect` 长期遗留给后续正常增量提交。仅在用户要求时提交/推送。

### 10.3 避免重复踩坑

- 动态插件内用 `.models` 等相对导入，真实目录安装测试不可省略。
- API 增加通用聊天字段后，核对 Bridge 字段白名单及实际 mapper；现有 `plugin_context` 同步修复已在基线中。
- 带 setup callback 的 Host 测试使用真实 SDK `PluginContext`，不能用 SimpleNamespace 伪装。
- 真实 agent entry 的 graph refresh 测试要满足当前配置 token 绑定，不能删 guard 让夹具通过。
- SDK `PluginRegistry` 是进程全局；依赖空 registry 的测试必须隔离并恢复。cron-only/search-only 的有序污染回归已修复，不放宽精确工具集合。
- 中断 shell/工具不保证 pytest 子进程退出。核对并只清理本次拥有的进程；保留其他开发服务。不要仅因一段时间无输出就终止全仓或留下后台 pytest。

## 11. 第一阶段交付与之后的候选范围

第一阶段交付仍需 A-01～A-07 的真实证据、完整插件包/宿主兼容、生命周期与回退、Windows 流程和人工对接结果。20 题 Oracle：5 型号、5 制度、4 长文、3 无答案、3 跨库；17 道有答案题至少 15 道命中依据，3 道无答案题均不得出现假引用。缺证据、FAIL、SKIPPED 或 BLOCKED 的必需验收项必须让发布 gate 非 0。

第一阶段验收之后的候选来自 PRD 第 29 节：桌面资料导入、目录增量同步、网页人工确认入库、文档版本维护、更多知识后端、业务系统受控 Action。这些尚未获本期实施授权，不计入当前剩余任务，也没有日期承诺；选定后需单独描述权限/副作用、评审 OpenSpec、估算与验收。

当前仍排除批量上传/删除远端知识、自动索引同步、CRM/OA 写入、替换 Runtime、引入新 SSO、重做安装器及任意页面插件平台。

## 12. 推荐技能与接手提示词

推荐使用项目已有的 `openspec-apply-change`、`openspec-update-change`、`openspec-explore`；真正完成后再用 archive/sync。项目入口见 [`.agents/skills`](C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop/.agents/skills)。优先调用 Cursor 当前实际可用的技能；不能假设 Codex 全局技能、工具 ID 和代理会话也在 Cursor 中存在。

功能修复采用 systematic-debugging 和有行为 RED 的测试流程；界面用 frontend-ui-engineering；提交用 git-workflow-and-versioning；任务执行保持精确文件范围、一次功能 writer 和独立 Spec/Quality review。使用新证据推进，不重新执行已经验收的整批任务。

可在 Cursor 中使用以下启动文本，并按此次要继续的范围修改最后一段：

```text
请先阅读 prd/Octop_Cursor_Handoff_2026-10-26.md、AGENTS.md、最新 execution-status 和三个 OpenSpec change。
代码基线为 codex/enterprise-p1 的 74dc70d8；确认打开的是开发 worktree，保护现有修改。
保持企业业务插件化、核心薄桥接、页面先 spec、唯一邮箱 1:1 私库与显式共享、注册成功待开通重试。
WeKnora 远端验证转人工，不把它作为本地搜索/模式/UI 的阻塞，也不宣称私库整体已完成。
先列出此次要继续的薄片、精确文件/输入输出/验收，复用已接受的 Host、Settings、EvidenceRenderer 和发送链。
若本次获得继续开发指令，优先从 P10a 独立验收、搜索诊断合同未决项和一条可验证的搜索设置链路开始。
每批完成后记录实际命令、结果及 review，只据证据更新 OpenSpec tasks；不自动推送、发布或重写已完成模块。
```
