# Proposal

## Why

用户要求 Octop 新注册账号以唯一邮箱 1:1 对应 WeKnora 用户及独立私有知识库，同时保留显式授权的企业共享库。现有第一阶段只读共享连接与可选邮箱不能提供该用户隔离及自动开通体验。

## What Changes

- **BREAKING**：启用企业用户开通策略后，新人类账号创建必须使用归一化且唯一的邮箱；覆盖邀请注册、管理员创建与 SSO 首次创建，保留既有邀请准入，不新增公开注册入口。
- 注册的本地提交与远端开通分离。Octop 先注册成功，知识能力显示待开通；企业知识插件负责有界重试、并发领取、持久检查点与未知写入结果核对。
- 建立稳定 Octop user_id → WeKnora user_id / personal tenant / private KB 的 1:1 映射。远端管理员 JWT 与用户引导凭据在服务端加密管理，不复用 Octop 密码。
- 个人库与企业授权库分别验证身份、tenant、来源和证据；同一提问可选择个人库及已授权企业库，各来源共享一次操作总预算。个人库不被当作企业 grant，企业 grant 也不授予他人个人库。
- 页面在邀请注册、现有用户管理和插件知识 composer/settings 内显示邮箱校验、待开通/重试/冲突/就绪及私有/企业来源。沿用通用插件容器，不新增 Dashboard 供应商业务页面。
- 历史无邮箱账号保留登录并提示补齐邮箱后开通知识；邮箱变化不自动重绑远端身份，既有同邮箱远端账号不自动接管。
- 删除本地账号后仍保留远端写入意图、可靠回执与反向占用的最小非敏感记录，用于恢复和防止再次认领；私人凭据沿用账号删除级联，不保留密码、JWT 或 Key。

## Capabilities

### New Capabilities

- `enterprise-user-provisioning`：唯一邮箱、插件开通生命周期、凭据与远端映射恢复、员工/管理员状态交互。
- `enterprise-private-knowledge`：用户私有来源与显式企业共享来源的会话范围、分来源检索、证据鉴权及就绪行为。

### Modified Capabilities

无已归档主规格。此变更依赖进行中的 enterprise-knowledge-p1 与 enterprise-knowledge-plugin-ui；其既有企业共享库五层交集、实际 graph 准入、证据与来源检查继续有效。此增量仅扩展账号/KB 开通写入与私有来源，明确取代先前对此范围的非目标，不授权文档上传/删除、远端部署或发布。

## Impact

功能与适配位于 plugins/enterprise-knowledge；enterprise-policy 仍通过窄版本化服务消费检索结果。核心候选改动限通用邮箱策略、可信用户身份快照/变化、受 Host 生命周期管理的后台工作、通用保留记录 CAS 与可信 actor 的当前数据库权限复核；SQL 仅在 repos，路由保持薄。保留记录复用 settings 的内部命名空间，不新增 schema 或供应商字段。最终核心白名单与精确 API 在设计审查及实施 brief 冻结，不能把此提案作为任意核心改动授权。

WeKnora 官方 v0.8.3 用户创建需要 SystemAdmin JWT，现有 API Key 不足。实际管理员凭据、部署策略和模型可用性尚未配置/验证；缺失时显示管理员配置阻塞，不阻止本地注册，也不得冒充开通成功。方案与本地 Mock 合同验证不能替代远端验收。
