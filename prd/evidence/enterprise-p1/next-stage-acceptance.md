# Next-stage acceptance（N08 / M3 本地）

日期：2026-10-26。Worktree：`C:\Users\Administrator\.codex\worktrees\enterprise-p1\octop`。基线起点 HEAD：`6dcdb896`（功能未提交）。执行者：Cursor Composer（非 GPT-6 Luna）。

**结论：** 本阶段本地代码闭环（M1 搜索设置 → M2 KB composer → M3 三模式/批准/主备/证据生命周期）已有非零自动化断言与命令退出码。**未**宣称 Alpha.1 发布完成；**未**把 mock 截图写作真实供应商/WeKnora PASS；OpenSpec 大项仍按原条件未整项勾满。

## 1. 安装与产物核对

| 项 | 状态 |
|---|---|
| 插件源码 `plugins/enterprise-knowledge` + `enterprise-policy` | 已改；UI bundle 经 `npm run build` 刷新 `ui/dist` |
| 搜索设置 UI / Composer / PublicQueryDialog | 已注册 settings + composer surfaces |
| `search.py` / `search_operations.py` | 已存在 |
| 真实安装副本版本摘要 | **人工待验**：需独立 test home 正常认证后核对已安装插件 manifest/版本（本记录不替代） |

## 2. 自动化证据（本次运行）

| 门槛 | 结果 |
|---|---|
| 搜索定向（http/settings/operations/search） | 见 N04/N08 复跑；此前 **154 passed** exit 0 |
| `tests/unit/enterprise` + 搜索定向合并 | **354 passed** exit 0（N08 收集跑） |
| 插件 UI typecheck / test / build | **134 passed**；typecheck/build exit 0 |
| Integration query/policy/actions | **44 passed**, 53 warnings, exit 0（138.96s） |
| 全仓 `make all` | **5710 passed**, 236 skipped, 94 warnings, **exit 0**（732.74s）；日志 `next-stage-make-all.log`（含一则上游/子进程 UTF-8 解码告警，未致失败） |

上游 HTTP 一律 **MockTransport / 测试夹具**（显式标注）。Host/权限/插件注册走真实 facade，未整体 mock 掉。

## 3. 里程碑映射

| 里程碑 | 本地可证明 | 仍待 |
|---|---|---|
| M1 搜索设置 | 保存/CA KEEP·REPLACE·CLEAR/员工白名单/单源诊断确认/recovery/迟到诊断丢弃；UI 组件测试 | 真实供应商诊断；完整 Windows 管理浏览器截图 |
| M2 知识 composer | 选库、revision-zero、清空阻断 prepare、仅 KB_ONLY、operation_id 快照 | 真实 Host+登录浏览器知识发送；私库仍不开放 |
| M3 三模式 | query.approve/revoke；WEB_ONLY 零 WeKnora；HYBRID 内部优先；主备 10/22；证据再授权；取消/重启/停用 | 真实浏览器精确批准路径；远端 SearXNG/Firecrawl/WeKnora |

## 4. OpenSpec 勾选纪律

- 主 5.1 / 5.2 / 5.3 / 5.4、UI 2.4 / 3.x / 4.3 等：**不**因本阶段本地演示整项勾满。
- 私库任务：**不计**完成。
- 主 6.1 / 7.x：仅贡献本阶段证据；完整 P13/发布 Gate 另列。

## 5. 人工待验清单

1. 独立 OCTOP_HOME 安装两插件后管理员浏览器：保存搜索配置 → 分别诊断 → 恢复阻塞。
2. 员工登录：composer 选库 → KB 提问 → 证据续读（上游可 mock，须标注）。
3. 精确公开词批准 → WEB_ONLY / HYBRID → 取消/停用可解释恢复。
4. 真实 SearXNG / Firecrawl / WeKnora 连通（缺输入保持待验）。

## 6. 全仓门槛备注

- 命令：`make 'SHELL=D:/Git/bin/bash.exe' all PYTEST_JOBS=4`，`PYTHONUTF8=1`
- collected/executed：`5710 passed / 236 skipped`（非 testmon 假绿）
- `make all` 含 format 写入；跑后工作区仍为下一阶段功能 diff（预期）
- 未推送、未发布、未勾 OpenSpec 整项

## 7. 场景→测试文件索引（摘要）

| 场景 | 主要证据 |
|---|---|
| 员工 search.get 白名单 | `test_enterprise_search_settings.py` |
| CA 保留/清除 | 同上 |
| 主备领取/计数 | `test_enterprise_search_operations.py` / `test_enterprise_search.py` |
| 三模式图 | `tests/unit/enterprise/test_tools.py` + integration query/policy |
| 设置/诊断 UI | `SearchSettings.test.tsx` |
| Composer/批准 UI | `Composer.test.tsx` / `PublicQueryDialog.test.tsx` |
| WEB 证据展示 | `EvidenceRenderer.test.tsx` / `package.test.ts` |
