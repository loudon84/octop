# WeKnora v0.8.3 官方源码合同（P05/P06 输入）

日期：2026-10-24。源码基线：Tencent/WeKnora 官方 `v0.8.3` tag。仅依据公开 tag 源码；没有访问用户测试服务器、没有读取凭据或环境变量、没有执行 live 请求。用户报告其部署为 0.8.3；此前只确认该部署当前 tenant 可见 0 个知识库，因此 SEARCH/READ 及内容字段仍无实机样本。

## 结论

足以为 P05/P06 准备**明确标注 synthetic 的 contract fixture**，字段名及端点可来自 v0.8.3 DTO/handler；这不是实机 fixture，也不构成 A01PASS。现有本地研究中的 `POST /knowledge-search?resource_urls=handle` 是相对 `/api/v1` 的合法路径写法，query 参数也合法；本报告补全为 `POST /api/v1/knowledge-search`，并列明搜索条件位于 JSON body 的完整 DTO。知识库详情返回 `knowledge_base_id`（不是 `kb_id`）；搜索结果 DTO 也提供可选的 `knowledge_base_id`，但 `omitempty` 意味着不能保证每条结果都带它。SearchResult 自身的 `metadata` DTO 没有定义一个可替代的 KB 归属字段；源码另有明确的 `GET /knowledge/:id` 详情合同，可在预算内按命中真实 `knowledge_id` 补查归属。补查必须验证响应 `data.id` 与请求知识 ID 完全一致，且 `data.knowledge_base_id` 存在并属于本次授权 KB 集合；任一条件不满足都应以 schema invalid 拒绝，不能按搜索请求中的单库范围猜归属。该补查路径是源码合同事实，能否在目标部署得到符合合同的实响应仍待实装验证。

## 路由与字段

所有路径以下均带 `/api/v1` 前缀。认证使用 `X-API-Key`；platform API key 对非 tenant-optional API 必须带 `X-Tenant-ID`，v0.8.3 auth handler 明确无 header 时返回 `TENANT_REQUIRED`。用户此前记录的 `tenants/all` 与 tenant header 实测属于部署证据，不代替下述源码字段合同。

| 面 | v0.8.3 注册路由与请求 | 成功响应/DTO | 范围、排序与含义 |
|---|---|---|---|
| 知识库 LIST | `GET /knowledge-bases`；可选 `agent_id`，普通 tenant list 没有分页参数。 | `{success, data: [...]}`。每项为 KnowledgeBase JSON；稳定字段含 `id`, `name`, `type`, `description`, `tenant_id`, `creator_id`, `created_at`, `updated_at`; service 计算 `knowledge_count`, `chunk_count`, `is_processing`, `processing_count`，handler 还可补充 `share_count`、`creator_name` 等字段。 | 普通路径使用 tenant repository 查询，基础顺序 `created_at DESC`，service 再按调用用户的 pin 状态加标记/排序；共享 agent 分支过滤到该 agent 配置范围。API-key scope 过滤发生于 handler。 |
| 知识文档 LIST（补充） | `GET /knowledge-bases/:id/knowledge`；query 支持 `page`, `page_size`, `tag_ids`, `keyword`, `file_type`, `parse_status`, `source`, `start_time`, `end_time`, `folder_path`, `folder_recursive`, `sort_by`, `sort_order`。默认排序 `created_at desc`；`sort_by` 允许 `updated_at|created_at|file_name`，order 为 `asc|desc`。 | `{success, data: Knowledge[], total, page, page_size}`；条目同下方 Knowledge DTO，含 `knowledge_base_id`。 | route 先验证该 KB 的 read access，再按该 KB ID 分页。两种 LIST 不应混为同一个 DTO：一个是 KB 集合，一个是某 KB 的文档集合。 |
| 搜索 | `POST /knowledge-search`；JSON DTO：`query` 必填；`knowledge_base_id` 单值兼容字段，`knowledge_base_ids`, `knowledge_ids`, `tag_ids`, `mentioned_items`；可选覆盖 `vector_threshold`, `keyword_threshold`, `match_count`, `disable_keywords_match`, `disable_vector_match`, `rerank`。Query 参数 `resource_urls=handle|public`，默认 `handle`。 | `{success, data: SearchResult[], meta: ...}`。SearchResult 可用字段包括 `id`, `content`, `knowledge_id`, `chunk_index`, `knowledge_title`, `start_at`, `end_at`, `seq`, `score`, `match_type`, `metadata`, `chunk_type`, `parent_chunk_id`, `image_info`, `knowledge_filename`, `knowledge_source`, `knowledge_channel`, `knowledge_base_id,omitempty`, `source_locators,omitempty` 等。 | handler 将单个与多个 KB ID 合并、检查 API-key targets，直接返回检索 service 的 `Results` 顺序，不做 handler 级排序。结果数组顺序应保留（可含召回/rerank 顺序）；不要仅凭请求中的 KB ID 推断命中归属。`knowledge_base_id` 字段明确定义为命中 KB，但 JSON 可省略；源码未定义可依赖的 metadata 归属替代字段。 |
| 知识详情 | `GET /knowledge/:id`。 | `{success, data: Knowledge}` 原对象。Knowledge 字段包含 `id`, `tenant_id`, **`knowledge_base_id`**, `type`, `title`, `description`, `source`, `channel`, `parse_status`, `summary_status`, `profile`, `enable_status`, `embedding_model_id`, `file_name`, `folder_path`, `file_type`, `file_size`, `file_hash`, `file_path`, `storage_size`, `metadata`, `custom_metadata`, `created_at`, `updated_at`, `processed_at`, `error_message`, `knowledge_base_name` 等。 | handler 先以 knowledge ID 找文档、解析父 KB 并验证 Viewer read access，然后按有效 tenant 取详情。详情 DTO 的授权归属字段是 `knowledge_base_id`；不要等待名为 `kb_id` 的属性。`file_path` 是存储引用字段，不是适合作为浏览器原文 URL 的承诺。 |
| chunks 分页 | `GET /chunks/:knowledge_id?page=&page_size=&chunk_type=...`。`page` 默认 1；`page_size` 默认 10，最大 100。默认只取 `text`；可重复传 `chunk_type` 覆盖。 | `{success, data: Chunk[], total, page, page_size}`。Chunk 包含 `id`, `tenant_id`, `knowledge_id`, **`knowledge_base_id`**, `content`, `content_revision`, `index_status`, `chunk_index`, `is_enabled`, `start_at`, `end_at`, `pre_chunk_id`, `next_chunk_id`, `chunk_type`, metadata 与时间字段等。 | route 通过 knowledge ID 找父 KB 并检查 Viewer read access。文档块按 `chunk_index ASC` 分页；FAQ 块默认 `updated_at DESC`（handler 没有显式传 sortOrder，repository 对 FAQ 仅在参数为 asc 时改为升序）。这些 page/page_size/total 是响应顶层字段，不在 data 行内。`chunk_index` 是文档内位置，不是 continuation 页码或源版本。 |
| 原文件资源 | `GET /knowledge/:id/download` 下载；`GET /knowledge/:id/preview` 预览。两者按真实 knowledge ID 路由，不接收由客户端拼接的 URL。 | Binary file stream，无 `{data: url}` JSON。download 使用 Content-Disposition attachment、`application/octet-stream`、`private, no-store`；preview 返回文件流并按文件类型响应。 | download 需要比详情/preview 更高的权限：Contributor、KB write 路由 guard，且 handler 要求 `OrgRoleEditor`；P06 不得通过提升权限来宣称只读原文可用。`preview` 是源码明确支持 Viewer+ read access 的只读候选合同，可用于 P06，但目标部署需验证真实响应后才能启用。另有通用 `GET /files?file_path=...`，但那是受 tenant/path 权限限制的存储代理，不等于知识原文件合同。 |

## 原文引用与版本判断

- Knowledge 的 `source` 注释语义是来源（例如 URL 类型的知识 URL 或 `manual`）；它不是统一可下载的 `original_url`。上传文件原文可由按 ID 路由的 `/:id/download` 或 `/:id/preview` 返回；P06 只可考虑具有 Viewer+ 只读合同的 preview，不能因 download 返回文件而提升调用权限。
- Knowledge DTO 有 `file_hash`, `created_at`, `updated_at`, `processed_at`，没有名为 `version`、`source_version` 或 `original_url` 的标准 JSON 字段。`file_hash` 可作为文件 hash 候选值，但源码没有把它定义为重解析/内容更新时稳定的续读版本号；不得如此宣称。
- Chunk 有 `content_revision`（当前块的编辑 revision），而搜索结果将该字段标为仅内部使用、不序列化；它也不是整个文档的源版本。
- `resource_urls=handle`（默认）保留内部资源引用形式；`resource_urls=public` 会重写为可加载引用，但该参数用于知识搜索响应和聊天流。插件不应把 handle 当成可直接访问 URL，也不应自行由 ID 或 `file_path` 拼接 URL。

## 搜索与会话流不要混用

`POST /knowledge-search` 的 Swagger 标为 `application/json`；handler 注释说明它直接调用知识检索，不做 LLM 总结，不要求 session ID。它返回一次性 JSON 的 `data` 与 `meta`。

会话对话走另一路由：`POST /knowledge-chat/:session_id`、`POST /agent-chat/:session_id`，Swagger 标为 `text/event-stream`；恢复流另有 `GET /sessions/continue-stream/:session_id?message_id=...`。这些是生成式会话 SSE，不是 `knowledge-search` 的响应合同，也不应为了检索请求而接入。

## P05/P06 可采用的合成合同与未决验证

可以构造 synthetic fixture：KB 列表使用 `{success:true,data:[{id,name,...}]}`；详情至少给出 `id` 与 `knowledge_base_id`；搜索 body 用 `query` 加显式 `knowledge_base_ids`，搜索命中使用 `knowledge_id`、可选 `knowledge_base_id`、`chunk_index`、`content`、`score`；可另造命中缺少归属字段后按 `knowledge_id` 详情补查并验证 ID/授权 KB 匹配的合成案例；chunks 使用 Chunk JSON 字段及顶层分页 metadata。Fixture 必须标注“根据 Tencent/WeKnora v0.8.3 源码塑造”，不冒充来自实装，也不解除实机验证/A01 阻塞。P06 若加入只读原文，应以 Viewer 合同的 `preview` 设计和验证；不得把需要更高权限的 `download` 当作只读可用性的替代证据。

需要目标部署验证后才能升级为实装合同：

- tenant-scoped platform key 的五类路径实际状态码、可见范围与 header 行为（已有 tenants/all 与空 KB list 的部署记录仍只覆盖发现/列表）。
- 有文档数据时的 SEARCH 非空 result shape、`knowledge_base_id` 是否稳定存在、`meta` 内容、排序是否与预期召回相关性相符。
- DETAILS 的真实字段存在性，尤其历史或不同来源类型的 `source`, `file_path`, `file_hash`；空值/缺失是否被省略。
- chunks 多页样例及 `total/page/page_size`、默认只返回 text、FAQ / image chunk type 的行为；源文件 `/download`、`/preview` 的权限与文件名/媒体类型。
- 真实附件/资源引用在 handle 与 public 两模式下的形状与主机规则；源码本身不提供 P06 continuation token 或文档级版本一致性合同。

## 官方 tag 源码定位

以下链接均固定在 `v0.8.3`，链接行号对应公开源码：

- 路由与认证：[`internal/router/router.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/router/router.go#L215-L310)、[`internal/router/routes_knowledge.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/router/routes_knowledge.go#L67-L83) [`#L202-L251`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/router/routes_knowledge.go#L202-L251)、[`internal/middleware/auth.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/middleware/auth.go#L379-L408)。
- LIST、详情、搜索：[`internal/handler/knowledgebase.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/knowledgebase.go#L502-L521) [`#L535-L630`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/knowledgebase.go#L535-L630)、[`internal/application/service/knowledgebase.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/service/knowledgebase.go#L352-L413)、[`internal/application/repository/knowledgebase.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/repository/knowledgebase.go#L75-L91)、[`internal/handler/session/qa.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/session/qa.go#L842-L977)、[`internal/handler/session/types.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/session/types.go#L100-L116)。
- Knowledge/SearchResult/Chunk DTO：[`internal/types/knowledge.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/types/knowledge.go#L167-L249)、[`internal/types/search.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/types/search.go#L158-L279)、[`internal/types/chunk.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/types/chunk.go#L132-L200)。
- 文档 LIST 与分页：[`internal/handler/knowledge.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/knowledge.go#L1026-L1140)、[`internal/handler/chunk.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/chunk.go#L99-L152)、[`internal/application/repository/chunk.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/application/repository/chunk.go#L278-L312)。
- 原文件与聊天流：[`internal/handler/knowledge.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/knowledge.go#L1987-L2063)、[`internal/router/routes_chat.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/router/routes_chat.go#L133-L152)、[`internal/handler/session/stream.go`](https://github.com/Tencent/WeKnora/blob/v0.8.3/internal/handler/session/stream.go#L23-L35)。
