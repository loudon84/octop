# Tasks

## 1. 基线与契约（P01/P02）

- [x] 1.1 实现显式live只读probe、配置校验和脱敏报告；验证 test_probe 中路由、零外呼、schema及预算场景。
- [ ] 1.2 取得远程版本/digest、两库权限及文档输入，显式运行probe；核对真实LIST/SEARCH/READ证据，无输入保持BLOCKED。
- [x] 1.3 完成 core-delta、Host v1、runtime-boundary 和执行brief；对照源代码与插件SDK审查全部提案接口。

- [x] 1.4 按用户授权增加精确测试HTTP origin例外（P01b），验证默认HTTPS及host/port隔离、零泄密与既有预算。

## 2. 通用宿主（P03）

- [x] 2.1 增加022双方言插件表、记录/凭据仓储和DI；验证fresh/21升级/幂等/CAS并发/原子回滚及owner隔离。
- [x] 2.2 增加版本化Host contracts、动态services、密文stage/commit和注册入口；验证命名空间、异步executor、旧facade失效及无密钥输出。
- [x] 2.3 将Manager load/disable/reload/uninstall和server关闭接入Host；验证setup失败清理、DB重绑定及ordinary插件兼容。
- [x] 2.4 增加认证动作/资源router及schema目录；验证伪actor、未知action、schema错误、管理员限制及 /api/docs 可读性。
- [x] 2.5 加入可信turn和持久required-extension入站检查；验证共享Agent、thread owner、缺hook和省略plugin_context均不能旁路。

- [x] 2.6 增加plugin-scoped只读revision前提；验证source改变/缺失创建冲突、来源不变和跨连接事务边界（P03e）。

## 3. 知识插件（P04～P07）

- [x] 3.1 实现严格业务DTO、空默认、配置/grant/binding/session动作；补足Host预分配opaque凭据ID及恢复意图来源原子前提；验证revision冲突、非法字段、凭据暂存恢复和身份隔离。
- [x] 3.2 实现五层范围与过滤catalog；验证显式越权整单拒绝、无未授权metadata及零search请求。
- [x] 3.3 实现异步WeKnora与分项probe；验证8/1200截断、30/90/10预算、schema/EMPTY/权限错误分离。
- [x] 3.4 实现连续阅读/cursor/原文；验证8000边界、长块拼接摘要、源变化及撤权零新请求。
- [ ] 3.5 实现versioned evidence/真实UUID引用；验证伪引用、恶意文档和history权限。
- [x] 3.6 实现通用Registry/实际图准入及只读工具声明，再实现tool与policy hook配套注册；验证Ask/Plan实际挂载工具发现/执行、伪extra-read不扩权、旧搜索/MCP/browser/exec等旁路拒绝。

## 4. 插件页面桥接（P08/P09）

- [x] 4.1 完成并审查 enterprise-knowledge-plugin-ui 全部规格/design/tasks；逐项对照PRD与PLG边界，无页面行为歧义再下发UI任务。
- [ ] 4.2 完成该UI变更的settings/composer/证据阅读任务；验证独立ESM包、宿主兼容、组件测试、tsc及构建。

## 5. 受控联网（P10～P12）

- [ ] 5.1 固定主备供应商合同与环境输入并记录决策；对所选两源运行合同和真实探测，缺输入不计PASS。
- [ ] 5.2 实现主备执行与operation去重；验证10/22秒、最多两请求、错误切换矩阵和取消。
- [ ] 5.3 实现search配置动作和已评审UI；验证revision、密钥脱敏、两源就绪及权限限制。
- [ ] 5.4 实现三模式和原样query批准消费；验证逐字符、跨线程/双击、canary不外发及web-only零WeKnora。

## 6. 生命周期与迁移（P13/P14）

- [ ] 6.1 实现取消/重启/禁用卸载终态；验证迟到提交零、停用外呼零、旧approval/实例失效。
- [ ] 6.2 实现显式legacy迁移和原子origin marker；验证重复/并发只创建一次、旧配置不改。
- [ ] 6.3 实现T0、摘要、配套版本升级与回退；验证未知数据保留和故障恢复，备份保留至少7天且恢复通过。

## 7. 交付门禁（P15）

- [ ] 7.1 打包两个单根ZIP和bundle摘要/兼容manifest；验证无密钥、UI齐备、隔离安装与独立升级。
- [ ] 7.2 对照core-delta与UI spec复核实际diff；归档边界证据，无白名单外业务进入宿主。
- [ ] 7.3 运行make all、插件Ruff/mypy/UI quality与build；保存真实命令、退出码和基线问题。
- [ ] 7.4 补齐PG、两搜索源、WeKnora和Windows实机/A-01～07及20题证据；验证缺证据/FAIL/SKIPPED/BLOCKED时Gate非0。

## Workflow follow-up

- 用户要求时才提交、推送或发布；达到所有条件后再按OpenSpec归档，不把本地代码完成视作Alpha.1已发布。
