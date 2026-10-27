# WeKnora 服务器配置与 Octop 对接：Codex 执行提示词

日期：2026-10-26。适用对象：在 WeKnora 所在服务器运行的 Codex。

## 一、结论与已核实的配置

用户提供的五个变量均存在于官方 **v0.8.3**。除写入环境变量，还需要检查容器是否接收、数据库设置是否覆盖、管理员权限、模型与索引、限定权限的 Key，以及实际 API 响应。当前部署版本由用户报告，镜像 digest、有效配置和以下验收尚需服务器执行者确认。

| 配置 | 目标 | 生效检查 |
| --- | --- | --- |
| `WEKNORA_AUTH_DEFAULT_TENANT_MODE=create_personal` | 新账号创建个人空间，并成为 Owner | 同时检查运行时 `auth.default_tenant_mode`，只影响后续开户 |
| `WEKNORA_TENANT_ENABLE_RBAC=true` | 强制空间角色鉴权 | 检查后端进程实际环境、启动配置和权限负例 |
| `WEKNORA_TENANT_ENABLE_CROSS_TENANT_ACCESS=false` | 关闭跨空间超级访问 | 与 SystemAdmin 身份独立；不需要为开户开启跨空间内容访问 |
| `WEKNORA_TENANT_AUTO_CREATE_API_KEY=false` | 新空间不自动下发全权限 Key | 同时检查运行时 `tenant.auto_create_api_key` |
| `DISABLE_REGISTRATION=true` | 禁止密码注册 | 同时检查运行时 `auth.registration_mode=invite_only`；该值连邀请新注册也禁止，已有用户仍可接受邀请 |

以上变量与含义依据 [v0.8.3 环境变量模板](https://github.com/Tencent/WeKnora/blob/v0.8.3/.env.example)、[启动配置源码](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/config/config.go)和[运行时设置注册表](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/system_setting.go)。已落库的对应运行时设置优先于环境变量；不能仅凭 `.env` 判断已经生效。

### 需要明确的三个边界

1. **个人空间不等于个人知识库。** 创建用户时分配 tenant，之后还需以该用户身份创建 document KB、生成单库 retrieve Key，并验证权限。空库创建成功也不能证明文档解析、索引和非空检索已可用。参见[用户服务](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/user.go)和[知识库服务](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/knowledgebase.go)。
2. **开户凭据与检索凭据分别管理。** v0.8.3 的 `POST /api/v1/system/admin/users/create` 要求 SystemAdmin 登录 JWT，不接受 platform API Key；空间 Owner 也不自动拥有 SystemAdmin。关闭密码注册不阻止管理员开户。参见[系统 API](https://github.com/Tencent/WeKnora/blob/v0.8.3/website-docs/04-api/02-api-system.md)与[平台管理](https://github.com/Tencent/WeKnora/blob/v0.8.3/website-docs/03-features/20-platform-admin.md)。
3. **服务器配置完成不等于 Octop 自动开通功能交付。** 当前 Octop 已有共享知识插件与 UI，以及私库开通的部分本地配置/状态基础；远端开户适配、后台开通恢复、私有与共享联合来源及其 UI 尚未完整交付。参见 [私库任务清单](../openspec/changes/enterprise-user-private-knowledge/tasks.md)和[人工对接清单](evidence/enterprise-p1/weknora-manual-handoff.md)。

本轮另发现：本地待实现的 `provisioning-wire-contract.md` 表格将开户成功响应写作“原始 user”，容易被误读为顶层用户字段。官方固定 tag 实际为 **`{"user": {...}, "generated_password"?: "..."}`**，没有 `success/data` 包装；201 新建、200 已存在。后续 Octop 实现前必须澄清该合同和 fixture，不应让服务器改接口迎合错误解析。本轮仅记录差异，未修改已冻结合同或功能代码。依据：[CreateSystemUserResponse 与 handler](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/system.go#L1583-L1715)。

## 二、复制以下提示词给服务器上的 Codex

从下面的“任务开始”复制到“任务结束”。无需复制本机项目历史，也不要附带真实密码、JWT 或 API Key。

---

### 任务开始

请在当前这台服务器完成已有 WeKnora 部署的检查、必要配置和 Octop 对接准备，并生成脱敏交接报告。不要停留在建议清单；对可以安全完成的配置实际执行，完成后用少量真实 API 请求验证。

#### 1. 已知环境与目标

- 已有 WeKnora，用户报告版本 **0.8.3**，请核实实际运行版本、commit 和镜像 digest。
- 外部映射入口：`http://sncon.k9.luyouxia.net:44813`。
- 同一部署的公司局域网入口：`http://192.168.102.104:18080`。
- 以上地址之间的实际代理/端口映射需要你检查，不能假设两个端口直接属于同一个容器。
- Octop 运行在另一台机器；它通过 REST API 管理开户和检索，不需要直连 WeKnora 数据库、Redis、向量库或对象存储。
- 产品目标：Octop 以规范化唯一邮箱对应一个 WeKnora 用户、个人空间和一个个人 document 知识库；保留 Octop 显式授权的企业共享库。
- WeKnora 暂不可用时，Octop 注册仍成功，知识功能待开通并重试。这是 Octop 的待完成逻辑，本次服务器配置不替代它。
- 目前优先完成 WeKnora 服务端准备和共享知识真实对接；个人自动开通端到端完成状态留待 Octop 后续代码验收。

#### 2. 执行方式与变更范围

先只读发现部署目录、部署方式、服务名、反向代理、持久卷和现有管理员情况，再给出简短执行顺序并继续实施。遵循本服务器的 AGENTS.md。

修改前备份实际配置及数据库，记录备份路径、恢复命令和原镜像标识。配置备份可能含秘密，须限制访问，不放入 Git 或交接报告。使用实际服务名和 compose 文件，不盲套默认目录。

本次允许必要的配置修改、相关服务重建/重启，以及创建带有本次验收标识的少量合成测试用户、空间、知识库、文档和限定 Key。数据库、对象存储和索引数据必须保留。不要执行 `down -v`、清空卷、重建业务数据库或删除既有用户/库/文档。不要自动升级版本或运行新增版本迁移；若当前版本不满足，先提供明确兼容差异和升级方案。

不要修改 Octop 源码，不替它实现 worker，也不要运行 Octop 全仓测试。验证采用一次有界的接口验收；只针对失败项修复后复验，避免重复全量检查。

#### 3. 核实配置确实生效

目标配置如下，写入实际后端服务环境；如果用 Compose，要确认 `env_file`、`environment` 以及覆盖文件最终值一致。修改 `.env` 后按实际部署重建相关容器，单纯 restart 未必重新加载新环境。

```env
WEKNORA_AUTH_DEFAULT_TENANT_MODE=create_personal
WEKNORA_TENANT_ENABLE_RBAC=true
WEKNORA_TENANT_ENABLE_CROSS_TENANT_ACCESS=false
WEKNORA_TENANT_AUTO_CREATE_API_KEY=false
DISABLE_REGISTRATION=true
```

只输出这些非敏感变量的有效值；不要输出完整 `docker inspect` 环境、完整 compose 展开结果或完整 `.env`，它们可能包含密码。

检查系统设置 API 中已落库的对应覆盖值，使最终行为满足：

```text
auth.default_tenant_mode = create_personal
auth.registration_mode = invite_only
tenant.auto_create_api_key = false
```

通过受支持的设置 API 调整不一致的覆盖值，并回读确认；不要凭空假设 RBAC 和跨空间开关也都有同名运行时设置 API，须按实际版本源码/注册表检查。

`invite_only` 在此版本表示禁止新密码注册，并非“允许凭邀请注册”。如果部署启用了 OIDC，检查其是否仍允许首次自动开户，报告现状；不要擅自删除既有 SSO 或更改身份绑定。不要为了 Octop 开户恢复公开注册。

不要通过开启 `CanAccessAllTenants` 或关闭 RBAC 解决权限报错；也不要把个人账号都放入同一企业 tenant。

#### 4. 准备正确的系统管理员身份

确认一个可供 Octop 开户使用的专用管理账号，必须具有 **IsSystemAdmin**；空间 Owner 不足以调用系统开户接口。使用现有合法管理员创建/授予专用账号，避免把日常员工账号升权。

若部署尚无 SystemAdmin，可使用：

```env
WEKNORA_BOOTSTRAP_SYSTEM_ADMIN_EMAIL=<已存在且由操作者控制的账号邮箱>
```

该变量只在没有任何系统管理员时提升已存在账号，不能创建账号，也不能代替已有管理员给第二个账号授权。若没有可用受控账号或管理员凭据，准确报告此阻塞，并继续完成其它独立配置；不要直接改业务表造身份、重置他人密码或对外开放注册。

用正常登录取得管理员 access JWT。`POST /api/v1/system/admin/users/create` 在 v0.8.3 不接受 platform Key，也不接受 tenant retrieve Key。不要把 `JWT_SECRET` 当作 access JWT，不要自行伪造 token。

管理员 JWT 是会过期/撤销的登录凭据。报告实际到期时间和通过正常登录更新的操作方法；不要宣称保存一次便可永久自动开户，不要擅自延长 token 寿命。专用账号密码、JWT、refresh token 和 API Key 只存服务器受限文件/已有秘密管理系统，输出存放位置与用途，不输出值。交付 Octop 时走安全渠道，不能提交到仓库或粘贴聊天。

#### 5. 保留签名与加密密钥，确认持久化

检查 `JWT_SECRET`、`SYSTEM_AES_KEY` 是否已稳定配置，只报告是否配置及是否符合实际版本要求。

- 保留已有有效值，不因配置对接重新生成；替换 AES 主密钥可能导致既有模型/API Key 凭据不可解密。
- 若缺失，区分全新部署和已有加密数据。无法确认密钥与历史数据关系时先报告，不覆盖现有秘密。
- 确认数据库、文件存储和索引有持久化，必要重启后既有资料仍可访问。

#### 6. 网络入口与 Octop 的连接形式

检查两个已知入口是否正确代理到 WeKnora `/api/v1`，认证接口返回 JSON，而不是 SPA 页面、SearXNG 页面、代理登录页或 30x 跳转。反向代理需要保留 `Authorization`、`X-API-Key`、`X-Tenant-ID` 和 JSON body。

若实际 Octop 主机能访问公司局域网，优先建议内网地址；你在服务器本机访问内网成功，不能证明 Octop 主机也可达。服务器无法验证调用方网络时，将该项写为“待 Octop 主机验证”。

当前用户允许测试部署使用 HTTP，可以先完成现有 HTTP 入口的准备，不因缺少 HTTPS 中止本轮。Octop 对非 loopback HTTP 要求在 **Octop 服务进程**配置精确测试 origin，例如选择内网入口时：

```env
OCTOP_WEKNORA_TEST_HTTP_ORIGIN=http://192.168.102.104:18080
```

若仍用映射入口，则为：

```env
OCTOP_WEKNORA_TEST_HTTP_ORIGIN=http://sncon.k9.luyouxia.net:44813
```

一次选择一个真实入口，该变量不带 `/api/v1`，不能设通配符。它属于 Octop，不要误加到 WeKnora 后就宣称已生效。两地址虽指向同一服务器，对 Octop 配置仍是不同 origin；私库映射建立后不能随意切换，需固定入口或另走迁移。

如已有可用 HTTPS 入口则记录证书名称、完整证书链及客户端需要的 CA；不要通过关闭证书校验解决问题。新增域名、公网映射或证书条件不足时，给出待办即可。本次无需暴露数据库、Redis、向量服务或 MinIO 管理端口，也无需为后端 REST 接入开启任意来源 CORS 或部署 MCP Server。

Octop 会将连接 base URL 规范化为 `<origin>/api/v1`，不跟随重定向。给出可直接返回 API JSON 的稳定入口。

#### 7. 验证用户 → 个人空间 → 个人库 → 单库 Key 的完整权限链

请按实际版本接口完成以下小规模验收，使用本次专用合成身份 A、B，记录创建的资源 ID，避免与真实员工邮箱重合。测试秘密随机生成并受限保存，不打印 HTTP 原始响应中的 token/password。

1. 管理员调用 `POST /api/v1/system/admin/users/create`，请求 `username/email/password`，显式随机密码符合当前密码策略。确认 201 新建及个人 tenant；重复相同身份应返回 200 且不改变密码。用户名与邮箱指向不同身份时应拒绝，不接管已有账号。
2. 正确读取官方响应 **`{"user": {...}, "generated_password"?: "..."}`**；没有 `success/data` 外层。显式传 password 时不应依赖 generated_password。不要假定用户字段直接位于响应顶层。
3. 以新用户调用 `POST /api/v1/auth/login`，确认用户 ID、激活 tenant、成员关系及 `owner` 角色。A/B 的 tenant 必须独立。管理员 JWT 只用于管理开户，个人 KB/Key 创建使用对应用户的 Owner JWT。
4. 在 A/B 各自空间调用 `POST /api/v1/knowledge-bases`，创建 document KB。`create_personal` 不会替你创建这个 KB。通过 `GET /api/v1/knowledge-bases?creator=mine` 等实际支持的接口检查 creator 与 tenant；准确记录响应形状。
5. 分别调用 `POST /api/v1/tenants/<tenant_id>/api-keys`，创建：

   ```json
   {
     "name": "octop-acceptance-private-retrieve",
     "full_access": false,
     "knowledge_base_ids": ["<该用户的个人KB ID>"],
     "capabilities": ["retrieve"]
   }
   ```

   回读 Key 元数据，确认 tenant scope、非 full access、精确一个 KB、仅 retrieve。不要用空 KB 白名单表示“无权限”，不要给 private Key 附加 chat/ingest/manage 或平台权限。明文只在创建时安全保存，列表中的掩码不能作为可恢复密钥。

6. 后续检索/详情/chunks/preview 验收改用对应的 `X-API-Key`；同时显式带该来源真实 `X-Tenant-ID`，不得以修改 header 跨到他人空间。检索成功不能借用 Owner JWT 冒充最小 Key 已通过。

若某次创建发生超时/断线且结果不明，先以资源记录、受支持列表及本次标识对账，不盲目再建第二个用户、KB 或 Key；不能因无法恢复 Key 明文就把 masked token 当真实凭据。测试结束保留资源清单，暂不执行删除或撤销既有资源。

#### 8. 配好真实检索依赖，准备固定验收文档

确认实际使用的数据库、检索引擎、任务队列、文件存储、文档解析服务健康。只修复当前部署所需组件，不为接入 Octop 额外部署全部可选服务。

核实每个新个人空间可用的 embedding 模型及其凭据、索引维度和检索设置；需要 rerank 时核实 rerank 模型。模型目录有名称不等于模型实例可调用，新 tenant 不应被假定自动继承别的 tenant 的自定义模型/凭据。记录当前版本支持的合法配置方法；未提供模型凭据时明确报告，而不复制其它租户秘密或虚构模型 ID。

空 document KB 的创建不要求你先虚构模型 ID；但空库创建成功不能充当非空检索验收。请在受控测试空间通过支持的界面/API 配置实际可用模型，上传少量无敏感信息的文本/PDF，等待解析和索引完成。A/B 文档分别包含不同、无个人信息的固定验收标记；共享库包含第三个标记。记录预期命中文档与查询。

Octop 当前实际检索请求形态为：

```http
POST /api/v1/knowledge-search?resource_urls=handle
X-API-Key: <对应来源的retrieve Key>
X-Tenant-ID: <对应tenant_id>
Content-Type: application/json
```

```json
{"query":"<固定验收查询>","knowledge_base_ids":["<授权KB ID>"],"match_count":8}
```

按这个正常形态验证，不能通过额外禁用向量/重排的特殊参数把失败改写成通过。WeKnora 负责文档检索，Octop 回答生成仍用自身 LLM；无需为了纯检索强制调用 WeKnora knowledge-chat 或配置其生成式聊天链路。

#### 9. 准备企业共享库，执行最小真实验收

企业共享库使用独立企业 tenant/connection/retrieve Key，与个人 Key 分开。限定可检索 KB 列表。保留现有企业库；没有可用测试共享库时创建本次专用测试库。不要为了共享开启跨空间超级访问，也不要把企业库复制到每人的个人空间。

Octop 目前用企业连接与其自身显式授权控制员工共享范围，不要求把企业库加入每个个人空间。WeKnora 内部组织共享并非该 Octop 方案的必要前置；如果企业已有组织共享，只记录现状，不扩大成员权限。

逐项记录实际状态码、脱敏响应字段和结果：

| 场景 | 必须确认 |
| --- | --- |
| 注册关闭 | 有效格式的新密码注册请求被策略拒绝；既有账号仍可登录；管理员开户仍成功 |
| LIST | retrieve Key 可列出其允许库，不出现未授权库 |
| SEARCH | 上述正常 payload 能命中固定文档，响应为 JSON，`data` 有内容 |
| 归属 | 搜索命中有 `knowledge_id`；检查其 `knowledge_base_id`。若省略，用该真实 ID 的详情验证 `data.id` 与父库，不根据请求单库猜归属 |
| READ | `GET /api/v1/knowledge/<knowledge_id>` 和 `GET /api/v1/chunks/<knowledge_id>?page=1&page_size=10` 返回一致归属；准备足够内容时验证第二页与顶层分页字段 |
| 原文 | 用只读 Key 检查 `GET /api/v1/knowledge/<knowledge_id>/preview` 是否真实返回文件及正确 MIME；不可用时记录能力限制，不提升到写权限调用 download 冒充只读预览 |
| 双身份隔离 | A Key 不能列出/搜索/读取 B 的测试资料；篡改 tenant header、跨库 ID、跨文档 ID 均不能得到 B 内容。具体拒绝码按实机记录，过滤型列表/搜索也须确认零泄漏 |
| Key 能力边界 | 检索 Key 不能执行开户/管理 Key 等管理动作；只使用专用测试对象验证，避免真实数据写入 |
| 企业共享 | 企业 Key 可读取指定测试共享库；其余库受限。Octop 的员工 grant、撤权后历史/续读再授权留待 Octop 主机联调，不把单独 WeKnora 验收写成这些已通过 |

所有请求设置明确超时、禁止盲目自动重试，不跟随携带凭据的重定向。只保存状态码、字段结构、ID、请求数和必要合成标记的匹配结果。日志、报告和终端输出不包含 Authorization/X-API-Key、密码、JWT、refresh token 或真实文档正文。

#### 10. 交付文件与完成标准

在服务器实际部署目录下建立 `octop-handoff/`（若已有惯例可沿用），输出：

1. `README.md`：实际版本/commit/digest、部署方式、配置变更、备份与回滚方法、已通过和未通过项。
2. `effective-config.md`：上述五项配置的最终值、对应运行时覆盖和来源；管理员创建接口是否可用、认证种类、JWT 有效期与更新方式。
3. `resources.json`：合成 A/B 的用户/tenant/KB/文档 ID、企业 tenant/KB/文档 ID、Key ID/能力/白名单、固定查询与预期；不含 Key、密码或 JWT。
4. `api-acceptance.md`：逐场景状态、实际状态码和脱敏响应形状，注明哪些来自服务器本机、哪些尚待 Octop 主机验证。开户响应尤其要保留 `{user:...}` 层级。
5. `octop-connection.env.example`：只含非敏感示例值和变量名，不写真实凭据。至少给出：

   ```env
   OCTOP_WEKNORA_BASE_URL=<最终选择的origin，不含查询参数>
   OCTOP_WEKNORA_VERSION=<实测版本>
   OCTOP_WEKNORA_IMAGE_DIGEST=<实测镜像digest>
   OCTOP_WEKNORA_TENANT_ID=<企业共享tenant ID>
   OCTOP_WEKNORA_TEST_KB_IDS=<企业验收KB ID，多个用逗号>
   OCTOP_WEKNORA_TEST_DOCUMENT_IDS=<对应文档ID，多个用逗号>
   OCTOP_WEKNORA_PROBE_QUERY=<无敏感信息的固定查询>
   # 仅HTTP测试入口需要，值必须精确等于选定origin：
   # OCTOP_WEKNORA_TEST_HTTP_ORIGIN=<选定origin>
   # HTTPS且需自定义CA时填写Octop主机上的PEM路径：
   # OCTOP_WEKNORA_CA_BUNDLE=<Octop主机的CA文件路径>
   # OCTOP_WEKNORA_API_KEY=<通过安全渠道配置企业retrieve Key，不在此填明文>
   ```

说明：以上环境变量是 Octop 开发/诊断输入；Octop 不会仅因文件存在就自动加载。正式插件连接需在 Octop 管理员插件设置中保存 URL、tenant、KB 白名单和 Key，由 Host 加密持久化。未来开户配置另使用管理员 JWT；不得把管理员 JWT 写进 `OCTOP_WEKNORA_API_KEY`，也不要发明尚未实现的管理员环境变量名。

秘密材料另存在受限目录/秘密管理系统，说明由谁、从哪里安全提取即可，不纳入这些交接文件。不要给测试引导密码承诺永久存储；Octop 后续须执行其既定的 24 小时到期禁用、运行/恢复时清理规则，服务器本次准备不等于该清理机制已实现。

最终汇报按三类列出：

- **WeKnora 已完成且有实测证据**：具体设置、API、资源与权限。
- **需要 Octop 主机配置/验证**：选定入口的网络、HTTP 精确 origin 或 CA、插件连接、企业授权、管理员 JWT 安全录入。
- **需要 Octop 后续代码**：自动开户 HTTP 适配、对账与重试/恢复、个人来源与共享来源联合处理、对应 UI；不能写作“服务器设置完成即自动开通可用”。

缺少的管理员登录材料、模型凭据或调用方网络条件只阻塞相应步骤。继续完成其它已具备条件的配置，最终一次列明待补输入。涉及破坏性重建、既有账号接管或版本升级时，先停在具体方案等待操作者决定。

#### 11. 固定版本资料

优先核对实际运行版本对应源码；若确认为 v0.8.3，参考：

- 环境模板：<https://github.com/Tencent/WeKnora/blob/v0.8.3/.env.example>
- 容器配置：<https://github.com/Tencent/WeKnora/blob/v0.8.3/docker-compose.yml>
- 配置解析：<https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/config/config.go>
- 运行时设置：<https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/system_setting.go>
- 首个管理员：<https://github.com/Tencent/WeKnora/blob/v0.8.3/cmd/server/bootstrap.go>
- 管理员界面与能力：<https://github.com/Tencent/WeKnora/blob/v0.8.3/website-docs/03-features/20-platform-admin.md>
- 系统 API：<https://github.com/Tencent/WeKnora/blob/v0.8.3/website-docs/04-api/02-api-system.md>
- 开户实际响应：<https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/system.go#L1583-L1715>
- 用户与空间：<https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/user.go>
- API Key 范围与创建：<https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/tenant_api_key.go>
- 知识库创建：<https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/knowledgebase.go>

文档、源码和当前部署不一致时，保留脱敏实测证据，报告差异；不要改 WeKnora 返回结构来迎合客户端假设，也不要用另一版本文档宣称当前实例已支持。

### 任务结束
