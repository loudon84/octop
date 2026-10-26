# Spec Delta

## Purpose

为同一员工提供自己已开通的私有知识库和明确授权的企业共享知识库，在会话选择、远端认证、检索证据及后续阅读中保留真实来源与权限边界，使跨租户资源不能因显示名称或库 ID 相同而混用。

## ADDED Requirements

### Requirement: Private scope derives from the registered owner

系统 MUST 从当前受信任 Octop 身份与已就绪映射确定唯一私有来源，再与远端凭据可见范围、专家允许的私有来源能力及会话选择相交；任何客户端邮箱、远端 user_id、tenant_id、库名或相同 KB ID 都不能替代所有权校验。

#### Scenario: Employee supplies another employee's private source

- **WHEN** A 在请求或模型参数中提交 B 的私有来源或证据
- **THEN** 请求整体拒绝，在向 B 的 tenant 发送内容请求前终止，不把 B 的库加入 A 的目录。

#### Scenario: Private knowledge has no enterprise grant

- **WHEN** A 的个人映射就绪、当前专家允许私有来源且会话选中 A 私有库，但 A 没有任何企业共享库授权
- **THEN** A 可以使用自己的私有库；系统不伪造企业 grant，也不要求给所有员工同一个企业连接。

### Requirement: Shared enterprise scope remains explicit

企业共享来源 MUST 继续使用凭据可见、企业允许、员工授权、专家允许与会话选择的交集；私有库开通、加入个人 tenant 或拥有共享专家 MUST NOT 自动扩大企业权限或其他人的私有权限。

#### Scenario: Enterprise access is revoked while private access remains

- **WHEN** 管理员撤销 A 的某个企业库 grant
- **THEN** 新操作不能使用该企业库，旧操作/续读按来源变化规则拒绝；A 对自己的私有库权限不因此扩展或转移。

### Requirement: Session selection distinguishes private and enterprise sources

会话 SHALL 展示私有与企业来源分类及库名，以服务端可验证的来源标识加库标识表达选择，允许同一提问选择自己私有库和已授权企业库。主动清空 SHALL 保持为空，不用默认值回填；开通未就绪时知识提交 MUST 明确阻止。

#### Scenario: Identical remote IDs exist in different tenants

- **WHEN** 个人 tenant 与企业 tenant 存在相同 KB ID
- **THEN** 页面分别展示来源，服务端按各自来源解析，不合并权限、请求或证据。

#### Scenario: Provisioning is pending after registration

- **WHEN** 员工进入知识模式但用户开通尚未完成
- **THEN** 显示待开通状态并保留草稿，不能静默使用企业管理员连接代替个人身份。

### Requirement: Selected sources share one operation budget

同一次知识操作 MUST 固定已选来源及各自身份、配置/授权/映射 revision，分别用对应 tenant 与凭据检索，合计遵守既有 90 秒、10 次物理请求及最多 8 条每条 1200 字符片段限制；单次 HTTP 仍不超过 30 秒，不因来源数倍增预算。

#### Scenario: One question selects private and shared knowledge

- **WHEN** 员工提交同时选中两类来源的问题
- **THEN** 每个远端调用只携带其自身 tenant 和已授权库集合，证据合并后仍受全局上限约束，最终回答只在所有选中来源完成授权检索后开始。

#### Scenario: A selected source fails before answer generation

- **WHEN** 一个已选来源无法验证身份、返回失败或耗尽共同预算
- **THEN** 操作明确失败且不进入最终模型，不把另一来源的部分结果冒充完整检索成功，也不自动转联网模式。

### Requirement: Evidence and reading preserve source identity

证据、历史引用与续读 MUST 保存不可由模型替换的来源关联及原操作映射；每次读取重新校验当前用户与对应来源，沿用真实 evidence UUID、来源变更拒绝、截断和 8000 字符阅读限制。原文不可用时 SHALL 明确降级。

#### Scenario: Private mapping changes before continuation

- **WHEN** 历史私有证据的来源映射或凭据已变更
- **THEN** 续读拒绝并提示重新检索，不把同名文档或相同远端 ID 解析到新 tenant。

#### Scenario: Forged source label appears in retrieved text

- **WHEN** 文档内容或模型输出包含私有/企业标签、他人 evidence UUID 或嵌套渲染指令
- **THEN** 它们只作为非受信文本；可信引用必须经服务端证据接口重新鉴权后展示。

### Requirement: Plugin UI shows readiness and source without granting authority

插件 composer 和证据界面 SHALL 展示私有/企业来源、加载/空/无权/待开通/错误状态，并使用当前 locale 与服务器时区；核心只挂载通用容器。切换用户、专家、线程或插件世代时 MUST 清理旧选择能力，队列项保留自己的已提交来源快照。

#### Scenario: Source selection changes while preparation is pending

- **WHEN** A 修改下一轮选库，而上一轮的异步准备或队列项尚未发送
- **THEN** 上一轮使用自身冻结快照或明确冲突失败，不能读取新选择；新草稿不被旧结果覆盖。

#### Scenario: Personal library exists but has no documents

- **WHEN** 个人映射与鉴权已就绪，所选私有库检索为空
- **THEN** 显示正常空结果，区别于未开通、无权限和供应商失败，不自动联网补答。
