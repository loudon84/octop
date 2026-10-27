# 本机 Octop → 远程 WeKnora 真实连接记录

日期：2026-10-27。按用户要求在 `E:\git\octop` 运行真实 Octop，连接公司局域网 WeKnora。没有使用合成上游或 LLM 替身。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 本地地址 | `http://127.0.0.1:5190/`，HTTP 200，标题 Octop |
| 运行时 | 实际 OctopServer、SQLite、Plugin Host、FastAPI 与 uvicorn；未替换 HTTP transport |
| 插件 | enterprise-knowledge / enterprise-policy，均为 0.1.0，已真实安装并加载 |
| WeKnora API 基址 | `http://192.168.102.104:18080/api/v1` |
| 本次 tenant | `10000`，来自本机已有环境配置 |
| 凭据来源 | 本机已有 `OCTOP_WEKNORA_API_KEY`，通过插件 connection.save 写入本地 Host 密文存储；没有回显明文 |
| 连接 ID | `31a3dc74-8ce3-4bb8-8170-461730696d4b` |
| 插件 LIST 探测 | **SUCCESS**，error_code 为 null |
| 管理员目录发现 | 成功；当前 Key + tenant 可见知识库 **0 个** |
| SEARCH | 未执行，原因 NO_VISIBLE_KNOWLEDGE_BASE |
| READ | 未执行，原因 NO_DOCUMENT_ID |
| HTTP 认证 | 匿名访问本地 `/api/plugins` 仍返回 401 |
| 远程写操作 | 无；未创建或修改远程用户、空间、知识库、文档或 Key |

这证明本机实际插件可以使用当前凭据访问该远程入口并完成 LIST，不代表其它 tenant 没有库，也不代表非空检索、全文阅读、1:1 自动开户或浏览器聊天已通过。WeKnora 0.8.3 仍为此前用户报告，镜像 digest 未在本轮核验。

## 本地运行资料

- 独立数据目录：`E:\git\octop\dist\weknora-live-2026-10-27\home`。
- 状态：同目录上一级 `status.json`，仅保存连接状态、ID 与脱敏结果。
- 管理员用户名：`weknora-local-admin`；随机临时登录密码只保存在同目录上一级 `local-login.json`，未写入 Git 或此记录。
- 当前由工具管理的前台会话运行，监听 PID 在本轮检查时为 `40832`，仅绑定 loopback。后续判断是否仍运行须检查实际监听，不以历史 PID 当实时状态。
- 运行资料位于被 Git 忽略的 `dist/`，不属于功能代码交付。

先通过正常 CLI init 创建隔离实例管理员并安装两个插件，然后启动 OctopServer 与实际 HTTP 监听。诊断使用该本地管理员的真实身份通过 embedded 方式调用同一 Plugin Host 管理动作；没有伪造 HTTP 登录或 JWT。浏览器使用管理页时仍需正常登录与验证。

Windows 启动辅助脚本的子进程输出最初按 GBK 解码，出现输出读取线程错误；初始化和插件安装仍成功，服务与真实 LIST 已完成。已将辅助脚本的捕获编码明确为 UTF-8。该改动仅涉及忽略目录的本轮运行辅助文件，没有修改产品源码。

后台 Start-Process 方式被自动审批拒绝且未返回具体原因；实际改用工具管理的前台会话启动。没有通过其它后台助手绕过该拒绝。

## 后续复用与验证

停止当前实例后，可在项目 PowerShell 中用正常入口复用同一数据目录：

```powershell
Set-Location 'E:\git\octop'
$env:OCTOP_HOME = 'E:\git\octop\dist\weknora-live-2026-10-27\home'
$env:OCTOP_WEKNORA_TEST_HTTP_ORIGIN = 'http://192.168.102.104:18080'
uv run octop run --host 127.0.0.1 --port 5190
```

仅对该测试 origin 使用此前用户授权的 HTTP 例外。已有 connection 的 Key 已在本地加密保存，不必把 Key 写进这段命令或文档。

继续非空检索前，需要在当前 Key 有权限的 tenant 下准备已索引的库/文档，或提供实际有库的正确 tenant 与相应权限 Key。不要通过给所有用户开放所有库来消除空列表。本轮没有运行 pytest、全仓门禁或重复上一阶段浏览器验收。

## 本机页面“加载中”恢复（2026-10-27）

- 本机 127.0.0.1:5190 服务正常，首页及所引用的 JS/CSS 通过直接 HTTP 请求均返回 200。
- 内置浏览器仍停留在启动画面。CDP 观测到 vendor-react、vendor-antd、vendor-utils 三个脚本从磁盘缓存读取，缓存响应头为 `Content-Type: text/plain; charset=utf-8`，响应日期为 2026-10-26，且带一年 immutable 缓存。
- 使用一次忽略缓存的页面刷新，未删除登录信息、未重启服务、未修改产品代码。
- 刷新后上述脚本均经网络重新获取并为 `text/javascript`；实际 DOM 和截图确认正常显示登录表单及滑块。
- 截图：`dist/weknora-live-2026-10-27/login-restored.jpg`（Git 忽略）。本次仅验证页面打开，未操作 CAPTCHA 或执行登录，未运行测试套件。

## 用户完成模型/专家后的 WeKnora 入口核查

- 浏览器已观察到 `my-researcher` 的真实普通模型回复，普通对话链路已通。
- 实际 `/connectors` 的“插件设置 → 企业知识设置”已挂载。既有连接 `WeKnora LAN live connection` 指向 `http://192.168.102.104:18080/api/v1`、tenant10000，凭据已配置，LIST历史探测SUCCESS；保存状态DEGRADED不能当作SEARCH/READ已通过。
- 本轮点击“加载授权知识库”（只读目录动作），界面加载完成但允许知识库列表仍为空，无错误提示。当前凭据的可见范围仍为0，不能推断远端全部租户没有库。
- 实际 `/experts` → 经营分析专家 → 编辑 → 抽屉底部“企业知识设置”显示绑定版本0、未选择知识连接、“启用绑定”禁用；没有进行保存。截图 `dist/weknora-live-2026-10-27/expert-binding-entry.jpg`。
- 后续顺序：远端同租户提供可见且解析完成的测试库/文档 → 连接中明确允许该库并保存 → 当前Octop用户获得该库员工授权 → 专家绑定该连接、允许/默认该库并启用 → 刷新聊天选择库，以仅知识模式检索并核验真实证据。先完成LIST/SEARCH/READ分项探测。
- 本轮仅查看入口和读取目录，未改授权/绑定/模型，未发送新消息、运行代码测试或写入远程数据。

## 目录按钮反馈与插件缓存修复验收

用户继续反馈按钮无响应后，在真实已登录浏览器捕获 `connection.catalog` HTTP200，`knowledge_bases=[]`，`observed_revision=3`。本次修复完成之前的“仅查看入口”说明不再涵盖后续UI开发。

Luna完成局部加载/成功数量/空/失败反馈以及Host脚本fetch的no-store缓存选项。插件Settings定向27项通过、插件typecheck/build与Dashboard build通过；未重跑全仓suite。根代理review与真实浏览器验收完成，详情见主排查计划末节。

本机插件已用CLI覆盖安装，保留连接revision3及用户数据。恢复浏览器默认缓存后正常reload实际获取149758字节新版插件脚本（非磁盘/SW缓存），点击后显示“当前连接凭据没有可见的知识库。请检查来源空间、租户和访问权限。”，没有Console error。截图 `dist/weknora-live-2026-10-27/catalog-empty-feedback.jpg`。临时验收tab已关闭；没有操作原标签页表单，没有上游写入或权限变更。剩余对接问题仍是当前凭据/tenant10000下可见目录为空，不推断整个WeKnora实例没有库。
