# Proposal

## Why

第一阶段需要管理员能看见配置/授权/探测状态，员工能选择知识范围、批准联网词并核对证据。先定义可验收交互，避免插件功能直接散入核心页面或实现后再补状态规则。

## What Changes

- 在既有连接器/专家配置页增加通用 settings 容器；企业表单由插件 UI 加载。
- 聊天 composer 挂载插件选库、三模式与原样查询确认；员工无权选择的库不出现。
- 真实证据工具卡片与检索文本侧栏，支持续读、原文及历史重开。
- en/zh、服务器时区、键盘焦点、加载/空/失败/冲突和停用状态明确。

## Capabilities

### New Capabilities

- `enterprise-settings-ui`: 管理员连接/授权、专家绑定、分步探测与主备搜索设置。
- `enterprise-chat-ui`: 员工选库/三模式/确认和会话切换。
- `enterprise-evidence-ui`: 真实引用卡片、阅读/续读/原文及历史。

### Modified Capabilities

无已有 OpenSpec capability。普通聊天及原生知识库交互保持兼容。

## Impact

业务组件位于 enterprise-knowledge 插件独立 UI 包。核心仅 loader/Host/context、固定 settings/composer 容器及已有 tool dock 的通用挂载/临时状态/焦点桥接；不解释企业证据字段，不注册任意页面、菜单或路由。服务端权限、schema、operation 和资源能力由 enterprise-knowledge-p1 变更提供。
