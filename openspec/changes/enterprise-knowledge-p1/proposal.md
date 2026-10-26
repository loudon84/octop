# Proposal

## Why

企业员工需要从已部署的远程 WeKnora 获得有权限、有证据的知识答案，并在明确批准后使用主备联网搜索。既有连接器的默认范围、截断阅读和连接级身份不足以支撑该需求；采用可独立升级的 Octop 插件交付业务能力。

## What Changes

- 两个配套包 enterprise-knowledge（tool）与 enterprise-policy（hook），企业功能默认关闭。
- 新增通用 Host v1：可信身份、命名空间事务记录、密文引用、注册动作/资源和生命周期约束。
- 五层范围、异步只读 WeKnora、真实证据、连续阅读和受控原文。
- 精确公开词批准、主备搜索及三模式策略，取消和重启后拒绝迟到提交。
- 幂等迁移、ZIP 配套版本和默认拒绝的发布门禁。
- 页面行为由独立 enterprise-knowledge-plugin-ui 变更定义；核心 UI 只提供 settings/composer 容器。

## Capabilities

### New Capabilities

- `plugin-host-services`: 通用插件身份、存储、凭据、动作、资源和配套生命周期。
- `enterprise-knowledge-access`: 默认拒绝的知识范围、远程合同探测、证据、阅读和原文。
- `enterprise-controlled-search`: 原样批准查询、三模式、主备错误语义与预算。
- `enterprise-release-safety`: 幂等迁移、配套包、回退和验收证据。

### Modified Capabilities

无已有 OpenSpec capability；原生知识库和普通插件语义保持兼容。

## Impact

业务源码在 plugins/enterprise-knowledge 和 plugins/enterprise-policy；宿主改动限实施计划 2.3 白名单及 core-delta.md。SQLite/PG 增加通用插件存储表，现有 v21 实例通过 022 升级。无 WeKnora 部署、文档写入、SSO、任意插件页面或运行时替换。
