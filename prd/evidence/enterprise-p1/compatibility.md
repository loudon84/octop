# 兼容性证据状态

本文件记录本地源码/依赖事实，不是 A-01 通过证明。实际命令运行日期为 2026-10-24，版本见 manifest.json。

| 范围 | 已验证事实 | 未验证项 |
|---|---|---|
| Octop | 固定基线 dd9dce4，独立 codex/enterprise-p1 分支，改动未提交 | 全阶段集成与发布验收 |
| Harness | 本地 octop-harness 1.0.4；LoadedPlugin.context、tool/hook kind 和相对导入路径已核查 | 企业配套插件实机完整运行 |
| Gateway | 本地 octop-gateway 1.0.3，Dashboard/gateway/共同执行接缝已追踪 | 可信执行桥接的后续集成测试 |
| WeKnora | probe 使用显式 --live、无输入默认 requests=0；本地合同测试通过 | 用户报告0.8.3，指定HTTP测试例外；已发现tenant10000/0库，待digest、索引测试资源/权限、SEARCH/READ真实响应 |
| 数据库 | SQLite 022 初始化/升级/CAS/事务本地测试通过 | PostgreSQL 同约束运行 |
| 搜索 | 用户已选企业SearXNG + Firecrawl；接口文档见provider-decision.md | 两家实际协议与真实请求验收 |
| 桌面 | 页面 OpenSpec 已评审，未实现 UI | Windows 完整业务与 20 题 Oracle |

凭据仅通过运行环境提供，不写入清单、源码或报告。未运行真实 probe，因此没有生成冒充 PASS 的 EVID-A-01.json。

P05/P06 合同复核见 weknora-contract-research.md。当前 probe 主要检查 envelope 和路径，SEARCH 合成用例是 EMPTY，尚未校验真实命中必需字段；CHUNKS 的 envelope 分页元数据也未留证。因此即使以后单次 probe 输出 PASS，也不能单凭该状态解除 A-01 或认定分页/归属/原文兼容；还须有已索引测试文档的非空字段、授权差异和分页 fixtures。缺 kb_id、源版本、原文 URL 一律不猜测。

当前脚本的单次正向运行只接受一个环境 Key，并要求两个配置库均可见；它不能独自证明“两库两种权限”的负向矩阵。实机 A-01 还需要分别配置不同受限身份的验证记录和预期结果，不能把一次正向报告复用为多身份验收。

用户报告版本后的源码补充见weknora-v083-source-contract.md：固定官方v0.8.3端点/DTO足以定义synthetic合同，仍不等于部署实证。缺命中KB归属可按实际knowledge_id查详情，ID必须匹配且knowledge_base_id存在并获授权；不得从单库参数或任意metadata猜测。原文preview为只读候选，download要求更高角色，不通过提升权限补齐验收。
