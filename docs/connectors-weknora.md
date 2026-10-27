# WeKnora 连接器（私有知识库）

Octop 有两种方式接入 WeKnora，都只读（检索 + 文档阅读），不写、不代答：

| 形态 | 配置位置 | 存储 |
| --- | --- | --- |
| 内置连接器卡片 `weknora` | 控制台「连接器 → 内置连接器 → WeKnora」 | 连接凭证由插件 Host 加密持久化 |
| 企业知识插件 `enterprise-knowledge` | 插件连接记录（`connection`） | 同上，另受 `allowed_kb_ids` 白名单约束 |

元数据源：[`src/octop/infra/connectors/catalog.json`](../src/octop/infra/connectors/catalog.json)（`kind: weknora`，`mcp_mode: gateway`）。
插件实现：[`plugins/enterprise-knowledge/`](../plugins/enterprise-knowledge/)。

## 本机部署参数（内网，2026-10-27）

> 以下为 `smc-weknora` 实例的实际值，属于内网信息，不要转发到公开渠道。

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `base_url` | `http://192.168.102.104:18080` | WeKnora 前端 Nginx；不含 `/api/v1`，Octop 会自动补齐 |
| `tenant_id` | `10001` | 企业共享库所在工作空间 |
| `knowledge_base_ids` / `allowed_kb_ids` | `805b7515-6cff-4db4-978a-cad5b762a9fd` | 知识库「工作流程」 |
| `api_key` | 只读 retrieve Key（id=3） | 明文见下方「凭证从哪来」，不要写进本文件或仓库 |
| WeKnora 版本 | `0.8.3-dev.e1f9a616` | commit `e1f9a6166a593a4804a2f99f1d942919c9cc8ea7` |
| 镜像 digest | `sha256:3944258a304861984ce0e94f7a33bdca33c485c6280e0e24944084d6976368cc` | `wechatopenai/weknora-app` |

个人知识库不共用这组参数：每个用户有自己的 `tenant_id`、知识库和 Key，见本文末「个人知识库」。

## 必须设置的环境变量

```bash
# 非 loopback 的 http:// base_url 必须精确声明 origin，否则连接被拒绝
OCTOP_WEKNORA_TEST_HTTP_ORIGIN=http://192.168.102.104:18080
```

规则（[`models.py`](../plugins/enterprise-knowledge/enterprise_knowledge/models.py) 的 `normalize_base_url` / 连接校验）：

- 只接受 `http` 的 loopback 地址（`localhost`、`127.0.0.0/8`、`::1`）或 `https`；
- 内网 IP 走 `http` 时，必须让 `OCTOP_WEKNORA_TEST_HTTP_ORIGIN` **精确等于**该 origin：不带 `/api/v1`、不带查询串或片段、不带用户名密码、不支持通配；
- 这是**Octop 进程**的环境变量，配置在 WeKnora 服务器上不生效；
- 该变量只用于测试入口，报告里会标记 test-only。

可选：

```bash
# HTTPS 使用私有 CA 时，填 Octop 主机上的 PEM 路径
OCTOP_WEKNORA_CA_BUNDLE=/etc/octop/certs/weknora-ca.pem
```

## 连接器卡片字段

在控制台填：

| 字段 | 必填 | 本实例取值 |
| --- | --- | --- |
| Service URL / `base_url` | 是 | `http://192.168.102.104:18080` |
| API Key / `api_key` | 否（无鉴权部署可空） | 企业 retrieve Key |
| Tenant ID / `tenant_id` | 否 | `10001` |
| Default knowledge-base IDs / `knowledge_base_ids` | 否 | `805b7515-6cff-4db4-978a-cad5b762a9fd` |

知识库留空表示「当前凭证可见的全部库」。企业场景建议显式填写，配合 Key 的白名单双重限定。

## 企业知识插件连接记录

`ConnectionSaveInput` 字段（[`models.py`](../plugins/enterprise-knowledge/enterprise_knowledge/models.py)）：

| 字段 | 说明 |
| --- | --- |
| `name` | 展示名，1–120 字符 |
| `base_url` | 同上，规范化后保存 |
| `tenant_id` | 目标工作空间 |
| `allowed_kb_ids` | 允许检索的库；空列表表示不额外限制 |
| `api_key` | 只写不读，保存后只显示 `credential_configured` |
| `enabled` | 关闭后探针标记 `DISABLED` |

## 凭证从哪来

- **企业检索 Key**：WeKnora 服务器上 `/root/.octop-secrets/weknora-enterprise-retrieve.key`（root 600）。能力仅 `retrieve`，白名单仅企业库。通过安全渠道导入 Octop，不要粘贴到工单或仓库。
- **平台 Key**（WeKnora 里名为 `Octop` 的那把）：只能访问 `/system/admin/settings`、`/system/admin/runtime/*`、`/system/admin/audit-log`；**不能开户**，不要拿它当企业检索 Key，也不要塞进 `OCTOP_WEKNORA_API_KEY`。
- **开户**：`POST /api/v1/system/admin/users/create` 只接受系统管理员的登录 JWT（24 小时有效），不接受任何 API Key。相关记录见 WeKnora 服务器的 `octop-handoff/`。

## 自检（在 Octop 主机执行）

```bash
B=http://192.168.102.104:18080/api/v1
K=<企业 retrieve Key>

# 只读列出被授权的库
curl -s -H "X-API-Key: $K" -H "X-Tenant-ID: 10001" "$B/knowledge-bases"

# 检索（Octop 的实际请求形态）
curl -s -X POST "$B/knowledge-search?resource_urls=handle" \
  -H "X-API-Key: $K" -H "X-Tenant-ID: 10001" -H 'Content-Type: application/json' \
  -d '{"query":"工作流程","knowledge_base_ids":["805b7515-6cff-4db4-978a-cad5b762a9fd"],"match_count":8}'
```

两条都应返回 `application/json`，且不走 30x 跳转。

## 常见错误

| 现象 | 原因 |
| --- | --- |
| `Unauthorized: missing authentication` / `invalid API key` | Key 错、被撤销，或没带 `X-API-Key` |
| `API key scope does not allow one or more knowledge bases` | 请求的库不在 Key 白名单里（批量请求任一越界即整体 403） |
| `insufficient permissions to access target workspace` | `X-Tenant-ID` 与 Key 所属租户不一致 |
| `remote HTTP requires the exact configured test origin` | 没设 `OCTOP_WEKNORA_TEST_HTTP_ORIGIN`，或值与 `base_url` 的 origin 不完全相同 |
| 检索 200 但 `data: []` | 库内没有文档，或该租户没有 Embedding 模型（个人库常见） |

## 个人知识库

由 `enterprise-user-private-knowledge` 变更实现：开户时给每个用户在 WeKnora 建独立 tenant + document 库 + 单库 retrieve Key。本机实测结论（2026-10-27）：

- 用户 → 个人空间 → 个人库 → 单库 Key 的授权链与隔离成立；
- 新租户**不会**继承任何模型，Embedding 需由 Octop 在开户流程中创建，否则入库与命中都无法完成；
- 企业共享库与个人库用不同的 Key，互不复用。

## 已知边界

- 只读：`retrieve` 不含 ingest / chat / 管理动作；写操作需要另建 Key。
- 不跨租户：同一把 Key 不能读其他工作空间的数据。
- 不做代答：WeKnora 只提供检索结果，回答仍由 Octop 自己的 LLM 生成。
