# Tasks

## 1. 规格与Host契约

- [x] 1.1 对照PRD、PLG-01～11和后端actions评审三个页面spec/design；交付无未决行为的检查记录。
- [x] 1.2 实现通用requirements只读就绪、settings/composer surface注册、清理、上下文/发送约束；验证共享员工读取、停用缺失/跨会话刷新、未知surface拒绝、旧renderer兼容、首发/队列/编辑/退出快照及异步准备不覆盖新草稿。

## 2. 设置流程（P08/P11）

- [x] 2.1 插件独立UI package构建单入口ESM和manifest；验证无相对chunk、复用Host React及目标机无需npm install。
- [x] 2.2 实现连接/员工授权/专家绑定与sanitized bootstrap；测试角色限制、默认空、保存/探测分离、Key清空与无浏览器持久密钥。
- [x] 2.3 实现revision冲突、迟到probe、空/加载/错误/停用提示；组件测试对应场景并检查键盘焦点。
- [ ] 2.4 实现主备搜索设置和诊断；验证只有一源时禁用联网、两源状态与实际降级信息。

## 3. 聊天与证据（P09/P12）

- [ ] 3.1 实现会话选库/default、expected_session_revision本地operation.prepare与context generation；测试主动清空不回填、revision0创建竞态、跨用户/专家/线程切换及web-only零知识调用。
- [ ] 3.2 实现独立三模式与精确query确认；测试逐字符、取消/焦点、双击/快捷键、失效批准和不可用模式不静默切换。
- [ ] 3.3 实现真实证据卡片/侧栏；按operation_id读取evidence.list，测试伪引用、历史引用鉴权、截断/续读、无页码/无原文、撤权和来源变化。
- [ ] 3.4 实现history重开和生命周期提示；验证后台再授权、HITL原上下文、企业忙时追加独立下一轮及插件停用后的控件清理/阻止发送。

## 4. 联调与验收

- [ ] 4.1 插件en/zh和服务器时区接入；验证locale parity、时间显示、键盘导航和读屏状态提示。
- [ ] 4.2 运行插件typecheck/lint/test/build与Dashboard tsc/lint/test/build；记录命令、结果和对应spec场景。
- [ ] 4.3 真实Windows走完管理→选库→提问→引用→续读→原文、三模式与禁用恢复；保存截图/操作证据，文本回退不作为UI完成。
