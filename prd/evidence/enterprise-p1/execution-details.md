# 第一阶段执行细则

执行基线：dd9dce470844c25d59effb50615bb3c667ea25cd。工作区：`C:/Users/Administrator/.codex/worktrees/enterprise-p1/octop`，分支 `codex/enterprise-p1`。远程 `git ls-remote --heads origin develop` 无匹配，因此从已核查 main 基线创建独立功能分支；后续 PR 的目标分支需在实际提交前重新核对。禁止提交、推送及发布。

## 授权和职责

用户已授权“创建执行细节后，功能代码分派给 GPT-6 Luna subagent”。此授权包含先写规格再继续实施，不重复请求启动 apply。主代理负责规格、接口、任务切分、审查和集成；功能与测试实现由显式 `model=gpt-6-luna` 的子代理承担。每次只运行一个功能实现代理，避免共享文件冲突；实现后另行审查，未解决的问题返回原实现代理修复。

原 PRD 为产品需求来源；实施计划的插件边界及服务器时区取用户已确认方向。所有需求标记、超时、预算、默认拒绝、敏感数据限制保持一致。

## 执行顺序与完成定义

| 任务 | 输入和产出 | 本地完成证据 | 外部完成条件 |
|---|---|---|---|
| P01 | 只读兼容 probe、合成响应合同测试、缺输入清单 | 缺输入/无 live 零外呼，响应校验和脱敏测试 | 真实 WeKnora 版本/digest、两库权限、文档及远程运行证据 |
| P01b | 用户明确指定的测试HTTP origin例外 | 仅环境精确origin匹配，其余HTTPS默认/脱敏/预算不变 | 非TLS验收，不能替代完整远程合同 |
| P02 | core-delta、Host v1 契约、runtime-boundary、OpenSpec 页面规格 | 源码接缝核查与能力清单 | 无 |
| P03a | 通用存储与凭据仓储、迁移、DI、CAS/事务 | SQLite 初始化/升级/并发/回滚及密文测试 | PostgreSQL 相同约束实测 |
| P03b | Host DTO、动态存储 facade、服务与动作注册 | 注册 schema、隔离、凭据事务、旧 facade 失效测试 | 无 |
| P03c | Manager/server 生命周期与受认证动作、资源分发 | 认证/权限/schema、取消、卸载、DB 重绑定及普通插件回归 | 无 |
| P03d | 可信 turn、持久 required-extension 与公共执行检查 | 省略上下文、伪造身份、共享线程、恢复和子调用拒绝绕过 | 支持入口真实运行 |
| P03e | 供P04 session保存使用的原子record revision前提 | 同事务校验来源与保存会话，包含缺失revision0；跨连接SQLite竞态 | PG条件事务并发实测 |
| P04a/P04b | 先包/配置/授权/绑定动作，再会话快照/operation/evidence基础 | 两批各自审查；配置CAS、空授权、凭据故障、来源原子前提及身份隔离 | 无；两批都通过才完成P04 |
| P05a/P05b1/P05b2 | 先范围策略，再传输/响应映射/预算，最后授权目录与分项探测持久化 | 各批独立审查；MockTransport 合同、零越权请求、共同预算、来源revision | P01 远程响应确认 |
| P06 | 连续阅读、cursor、原文资源 | 8,000 字符边界、摘要、撤权和资源代理测试 | 远程原文能力确认 |
| P07 | tool+hook 配套、真实 turn 身份、必需扩展 | Agent/线程 fail-closed、旧工具旁路与并发测试 | 支持路径真实运行 |
| P08 | 通用 settings/composer 挂载与插件设置 UI | 已评审 UI spec、组件/加载/卸载/构建 | 管理界面实机 |
| P09 | composer、证据卡片、阅读侧栏及历史 | UI spec 场景、原生功能兼容、完整插件 ESM | 实机 A-02～04 |
| P10 | 受控主备搜索、operation 去重 | 10/22 秒、仅 timeout/429/5xx 切换、最多两请求 | 两家供应商及凭据配置 |
| P11 | 搜索设置与诊断 UI | revision、脱敏、权限及组件测试 | 两源实测 |
| P12 | KB_ONLY/WEB_ONLY/HYBRID、原样公开词批准 | 模式计数、逐字符、canary、并发与批准消费 | 实机交互 |
| P13 | 取消、重启、禁用/卸载/重载 | 迟到提交零、停用后外呼零、引用失效 | 实机生命周期 |
| P14 | 幂等迁移、配套升级与回退 | origin 唯一事务、T0 与未知记录保留 | 真实副本恢复 |
| P15 | 两 ZIP、bundle、发布门禁与 A-01～07 证据 | 包结构、摘要、缺证据拒绝 | Windows 桌面、PG、远程和 20 题 Oracle 全部通过 |

每一子任务：brief 固定可写文件和接口 → Luna 先写有意义的失败测试 → 实现 → 定向测试和 lint/typecheck → 独立审查 → 修复 → 更新执行台账。新任务不得省略上游契约或把失败当成功。全仓 make all 在集成阶段执行，格式化产生的无关变更需撤回到执行前内容。

P04b初审的调用方来源snapshot缺失已由原Luna修复并通过独立复核；原M1/M2/N1同步关闭。session能力响应M3由紧接的P05a实现并独立确认关闭，P04/3.1已完成本地验收；不据此推断远程可用性。

## 接口与核心改动控制

企业模型/算法/网络/页面在 `plugins/enterprise-knowledge/`，hook 在 `plugins/enterprise-policy/`。插件仅使用版本化 Host facade；核心不引入企业专属参数或业务 UI。宿主候选修改严格遵循实施计划 2.3，并逐项登记 core-delta。

Host 记录的唯一键为 `(plugin_id, namespace, logical_key)`，公共 ID 为字符串、整数为内部 PK。创建要求 expected_revision=0，更新要求精确当前 revision；所有变更先验证后事务提交。通用凭据保存 Fernet 密文和 opaque 引用，服务端解析时限定插件与 owner；暂存后提交失败不能留下可生效的新引用。

迁移采用 022 成对文件：基线已有完整 v21 和版本断言，不能证明所有本地数据库未应用 v21。新编号确保已在 v21 的实例不会跳过新表；保持 SQLite/PG 等价。

## 页面先规格

页面变更单独位于 `openspec/changes/enterprise-knowledge-plugin-ui/`：入口和角色、状态/校验、动作 API、插件与宿主责任、键盘焦点、en/zh、服务器时区及验收逐项明确。相应 OpenSpec artifacts 完整并经主代理对照 PRD/计划评审后才分派 UI 功能代码；用户本轮已经授权后续执行。

## 当前外部输入

用户已报告WeKnora 0.8.3和远程HTTP地址，已选企业SearXNG + Firecrawl；非敏感输入与协议核查见provider-decision.md。用户另明确授权指定HTTP测试origin（详见provider-decision）；已只读发现工作空间10000/0库。仍缺digest、已索引测试库/文档权限和真实SEARCH/READ fixtures，搜索部署地址/版本/API合同，以及真实PG/Windows桌面试点环境。对应验收保持 BLOCKED；允许继续本地实现和合成合同测试。所有 live 探测必须由显式开关触发，默认零外呼；密钥不进入 argv、报告、fixtures 或页面持久存储。
