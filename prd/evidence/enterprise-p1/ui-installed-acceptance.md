# 已写插件 UI：真实安装与浏览器验收记录

日期：2026-10-26。唯一开发目录：`E:\git\octop`。功能基线：`10fe84d2`；分支：`codex/enterprise-p1`。本轮没有 commit、push 或发布。

## 结论与边界

**OpenSpec UI：13/13，CLI状态complete，strict校验通过。** 主计划15/29、私库6/18保持；没有归档change或发布。

已完成现有知识/受控搜索 UI 的本地真实安装、正常登录、主要浏览器链路及发现缺陷的实现和独立 review。功能和测试代码均由 GPT-6 Luna subagent 完成，控制器编写计划、执行浏览器和整合验证，另由独立 reviewer 检查。

实际运行 OctopServer、SQLite、认证、PluginManager、enterprise-knowledge / enterprise-policy、SDK graph、Dashboard 插件加载器与原生聊天。仅 WeKnora / SearXNG / Firecrawl HTTP 和最终 LLM 使用闭合的本地合成替身。**这些结果不代表真实供应商、私库自动开通或生产部署验收通过。**

计划见 [功能实现与执行计划](../../Octop_UI真实安装验收_功能实现与执行计划_v1.0.md)。脱敏机器摘要见 [验收摘要](ui-installed-acceptance-summary.json)；不包含 Key、JWT、密码、完整查询或正文。

## 安装证据

- 使用全新隔离 `browser-home-05`，只监听 `127.0.0.1:5190`，浏览器访问 `http://localhost:5190`。正常 setup、管理员/员工认证与真实插件安装；匿名管理接口仍为 **401**。
- fresh-home 自检在同一线程跑两轮真实 KB_ONLY graph：**COMPLETED / COMPLETED**，不同 operation，每轮各 1 条实际登记 evidence；history 保留两条最终回答，消息 ID 唯一。员工初始只授权 A，B 未授权；自检不扩权。
- UI 构建后通过正常 `/api/plugins/upload` ZIP 安装接口更新，返回 **200**；30 个运行文件包含 Python 包、locale、UI bundle/manifest。安装副本与仓库的 manifest、main.py、bundle SHA-256 均一致。
- 最终 bundle：`2eb1b69ab58d93796795a9bf345922eb7b50aaf41b713147b79f2f8dd0d6bf29`。刷新真实历史 WEB 卡片后显示 **Firecrawl / 已切换至备源**，英文显示 **Firecrawl / Switched to secondary source**。
- 正常重装可能重载测试进程中的上游 factory；重装后只检查本地元数据、展示与生命周期，没有继续执行供应商查询。执行证据来自重装前同一实例，最终安装与展示证据来自重装后；没有复制 token、直接写 evidence/operation 或覆盖安装目录伪造结果。

## 浏览器场景与结果

| 场景 | 实际结果 | 可复核证据 |
|---|---|---|
| A 管理设置 | admin 正常登录；保存搜索配置 revision 1→2，分别诊断主/备源，主源 EMPTY/0条、备源 SUCCESS/1条，均对应 revision2；展示 CA/凭据状态而不回显密钥。最终 ZIP 后再次正常 admin 登录加载设置表单。 | `screenshots/ui-admin-settings.png`；`screenshots/ui-final-admin-settings.png` |
| B 首条知识问答 | employee 从“新建对话”首条输入开始，由服务端创建真实 thread/session/operation；最终回答出现，证据卡片自动加载，无手动重试。默认只有获准的 A。 | `screenshots/ui-final-knowledge-reader.png`；摘要中 KB_ONLY COMPLETED |
| C 范围与阅读 | 清空后 revision2、空选择不回填；知识发送被拒绝且草稿保留；WEB_ONLY 空范围可保存，切回知识模式可重新选择 A。侧栏续读成功；无页码明确显示，无原文显示“能力尚未验证”。 | reader 截图；`screenshots/ui-empty-selection.png`（前轮同一行为）；真实 reader/chunks 请求计数 |
| D 模式与外发 | KB_ONLY 不增加两搜索源请求；WEB_ONLY 批准前请求总数24，Escape取消后仍24；快捷键再次准备、双击批准后仅24→26（SearXNG503一次、Firecrawl一次），全部 WeKnora 计数不变。WEB_ONLY 第二轮正常 COMPLETED，显示备源卡片。 | 脱敏摘要；`screenshots/ui-final-web-fallback.png`、`ui-final-installed-composer.png` |
| D 混合模式 | HYBRID sequence31 为 WeKnora search，32 为 SearXNG，33 为 Firecrawl；最终同时出现知识/联网证据和回答。内部 canary 仅出现在知识请求，公开请求为0。 | `screenshots/ui-final-hybrid.png`；摘要 recent_requests |
| E 交互与历史 | 新草稿在读取证据时保留；空范围/取消/必需插件缺失均保留草稿；Ctrl+Enter 进入批准弹窗，批准双击没有重复外呼。刷新历史重新执行 evidence.list，来源变更的旧引用显示拒绝/变化提示；当前有效 WEB 证据可重新展示。 | 原生 UI 操作、请求增量、最终历史卡片；相关 CAS/上下文/竞态组件测试 |
| F 生命周期 | 通过正常管理 HTTP API 停用配套策略，浏览器显示 required plugin 不可用并拒绝发送；恢复后新的 KB 查询正常完成。通过 grant.save 撤权后，已打开 reader 的续读显示 SOURCE_CHANGED、停止加载；再正常恢复原 A 授权。知识插件本身停用后控件卸载、显示必需插件缺失，重新启用后控件恢复。 | `screenshots/ui-final-policy-disabled.png`、`ui-final-revoked-reader.png`、`ui-final-plugin-disabled.png` |
| G 双语/时区/焦点 | en/zh 实际切换，品牌和备源文案正确；时间使用服务器 Asia/Shanghai（中文21:06对应英文9:06 PM）。取消批准后焦点回到发送按钮，原始公开词输入为空，不把内部问题预填；本轮末读取浏览器 warn/error 为 []。 | `screenshots/ui-final-installed-composer-en.png`；正常 UI 语言设置与 DOM 焦点观察 |

A 的保存/双源诊断在本轮先前隔离实例完成；最终实例没有把旧诊断记录冒充新探测。生命周期的 admin 操作用正常认证 HTTP API 执行，员工浏览器观察结果，不是跨角色 token 注入。用户已分别确认对应批次 CAPTCHA 后才操作本机滑块。

## 本轮发现并关闭的问题

1. Windows 静态 JS 被系统 MIME 映射成 text/plain：仅在通用静态响应明确 JS/MJS/CSS 类型；实际浏览器正常加载，没有修改注册表或认证。
2. Composer 首发 target、WEB_ONLY 零目录请求、已保存/未保存选择及迟到初始化覆盖草稿：使用 Host 解析的 target、保存 revision 和状态版本校验，保留 CAS。
3. 工具返回早于操作完成时误报证据错误：合法 VALIDATING/RETRIEVING/GENERATING 显示 pending，自动重查有30秒实际截止/abort；完成才信任证据，拒绝/卸载/上下文改变停止；pending不再同时显示 empty。
4. 等值 context 对象引起重复加载和草稿重置，以及异步批准取消丢焦点：改为语义 scope 依赖、同步捕获本地焦点引用。
5. 合成模型复用消息 ID 导致第二轮 graph 循环：测试模型每轮生成新消息，兼容 factory keyword；真实两轮与 WEB_ONLY 浏览器复验通过。自检扩权/旧revision问题经独立 review 发现后移除。
6. 联网卡片漏实际 provider/degraded：显示已验证 DTO 的真实供应商及中英备源提示；不增加外呼或 URL。
7. HITL 输入框答复丢失服务端 pending ID：保留具体 pause 标识，经通用 route callback 显式映射现有 hook 第五参；修复同用户同线程旧问题可能误答最新问题，不改变后端授权。新集成 HTTP 替身的 revision 字段问题也经独立 review 发现后修正为真实字段和严格 CAS，并增加外层精确断言。

每批都有独立 Spec / Quality review；初审的必须修复项已关闭。测试门槛修复仅涉及已复现的 Dashboard 测试依赖/时序、两处 lint，以及 Windows 测试 Harness root，未降低断言、权限或全局质量要求。

## 最终质量门槛

| 命令 | 实际结果 |
|---|---|
| `make all SHELL=D:/Git/bin/bash.exe REPO_ROOT=E:/git/octop PYTEST_JOBS=4`（补 Git 工具 PATH、PYTHONUTF8=1） | **exit0；5728 passed、236 skipped、94 warnings；767.31秒** |
| 插件 UI `npm test` | **exit0；9文件、162 tests** |
| 插件 UI `npm run typecheck` / `npm run build` | **exit0** |
| 插件 UI 本地 Prettier3 `--check --end-of-line auto src` | **exit0** |
| 插件 Python Ruff check / format check | **exit0；24文件格式正确** |
| 运行器定向 pytest / Ruff / strict mypy | **14 tests，全部exit0** |
| Dashboard `npx tsc -b` / `npm run lint` | **exit0；0 errors、67既有warnings** |
| Dashboard `npm test` / `npm run build` | **exit0；284文件、1568 tests（npm test -- --maxWorkers=4，299.30秒）；构建成功** |

make all 在最终后端/运行器实现上执行；后续新增功能为 B03e 插件 UI 展示及 B03g 通用前端 HITL 目标透传，分别经独立 review 和插件162项、Dashboard相关29项回归/typecheck/build/格式。没有为纯展示改动重复后端大套测试。格式器造成的1183份无内容差异 Dashboard 换行变动已逐批核对后恢复，保留9份实际修改。

## 保留的验收范围

- HITL/忙时队列的剩余组合证据已由真实企业 Composer→Host→原生输入/队列/发送的集成测试补足：两条队列消息各自准备 operation，保留 revision/模式/选库快照并 FIFO 派发；HITL 通过实际 resume HTTP 保留原 thread/pending_id，不新增 operation/user_turn 或客户端 plugin_context。HTTP/FakeWebSocket 为测试边界，后端原上下文/再授权复用已有精确集成测试；不将其称为新跑过真实SDK或浏览器HITL。
- 真实供应商部署/Key/CA/版本与私库自动开通、恢复、私有及共享来源联查保持后续独立工作；真实原文能力仍待人工对接，当前验证的是明确不可用降级。
- 不因本地截图或合成上游通过而勾选真实远端/生产验收。OpenSpec最终计数以本轮任务核对及CLI输出为准。

本轮临时浏览器已关闭，5190 已无监听进程。Windows shell 的 Ctrl+C 返回1，fresh05状态文件仍保留最后ready快照，不能以此判断进程还在运行；关闭状态以实际监听/进程检查为准，未手工改写ready数据。最终安装的供应商中英标签通过可访问DOM核验；对应 composer 截图展示当前模式/选择区域，标签文本另见本记录，不把视口外内容冒充截图所见。

B03f/B03g 的单文件前端组合证据见 `dashboard/src/pages/Chat/enterpriseComposerIntegration.test.tsx`，结合 `ChatInput.prefill.test.tsx` / `pendingHitl.test.ts` 为29项定向通过；严格CAS修复后新文件2项通过。B03fg R1和B04e冷加载隔离均通过最终独立 Spec/Quality复审。默认并行的新增后全量曾因history冷编译超时失败（283文件通过、1文件6项失败）；B04e移除本轮15秒timeout覆盖，静态预载真实useSessions，原6项在默认5秒限时下通过。最终完整复跑结果单独记录，不隐藏这次失败。


最终收尾：Dashboard最后全量 `npm test -- --maxWorkers=4` 为 **284 files / 1568 tests passed，exit0，299.30秒**；包含修正后的企业Composer队列/HITL与默认超时的历史测试。全量lint为0 errors/67既有warnings，最新tsc与生产build通过。B03fg/B04e最终独立Spec/Quality均PASS，无必须修复项。
