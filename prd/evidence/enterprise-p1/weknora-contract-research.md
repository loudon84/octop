# WeKnora 合同调查（P05/P06 输入）

> 历史核查快照：下文描述用户提供测试地址/版本与授权HTTP例外之前的本地状态。当前实装发现以 manifest.json / weknora-discovery.json 为准；固定v0.8.3字段合同以 weknora-v083-source-contract.md 为准。两者都未证明真实非空SEARCH/READ或解除A-01阻塞。

日期：2026-10-24。范围：本地源码、计划、PRD 和合成测试静态核查；未访问真实服务、未读凭据、未运行测试。本文中的“基线”仅指源码/合成测试事实，不代表 WeKnora 实装已验证。

## 结论

当前 manifest 将 WeKnora 标记为 `BLOCKED`，版本、镜像 digest 和 live probe report 均为 null；compatibility.md 明确真实响应、CA、两库权限尚未验证。P01 要求实际 LIST、SEARCH、DETAIL、CHUNKS 样例及长文/分页/缺 kb_id/原文样例。`tests/fixtures/enterprise/weknora/` 目前不存在，现有 probe 单测通过 `MockTransport` 内联构造响应。因此不得把当前 DTO 映射称为已确认的上游合同。

## 当前能确认的本地合同

API 路径来自 P01/P05 和现有 adapter：`GET /knowledge-bases`、`POST /knowledge-search?resource_urls=handle`、`GET /knowledge/{knowledge_id}`、`GET /chunks/{knowledge_id}?page=&page_size=`。Probe 请求使用 JSON envelope `{success: true, data: ...}`，`X-API-Key`，可选 `X-Tenant-ID`；禁用重定向。其安全预算为单请求 30 秒、全操作 90 秒、最多 10 次 HTTP；无重试。

| 操作 | 目前 probe 会接受/检查的形状 | 当前合成样例及旧 adapter 映射 | 未证明的关键点 |
|---|---|---|---|
| LIST | `data` 是对象数组；每行必须有非空字符串 `id`。记录行数和字段名。 | 测试只给 `[{id}]`；旧 adapter 映射 `id/name/description/knowledge_count/chunk_count`。 | `name` 等是否必需；分页/total/权限过滤语义；完整可见集合是否一次返回。 |
| SEARCH | `data` 是对象数组；空列表记为 `EMPTY`，仍可整体 PASS。只记录结果数，不检验命中字段。 | 测试成功响应为空数组。旧 adapter映射 `knowledge_id`、`knowledge_title` 或 `knowledge_filename`、`chunk_index`、`score`、`content`，每段截断到 1,200 字符，最多 8 条。 | 非空行字段是否稳定；`knowledge_id` 是否必有；命中是否带 `kb_id`/来源链接/版本；搜索排序、时间和分页。 |
| DETAIL | `data` 必须为对象。`kb_id` 或 `knowledge_base_id` 若存在则必须匹配测试预期库；两者都缺失会被接受。记录字段名。 | 合成样例为 `{id, kb_id}`。旧 adapter 输出 `title`/`file_name`、`description`、`summary`。 | 所有权字段是否存在/可信；ID、原文件名、源版本和源 URL 的准确字段与语义。 |
| CHUNKS | `data` 必须为对象数组；只记录段数和行字段名。 | 合成样例每行只有 `chunk_index`。旧 adapter从行读取 `knowledge_id/title|filename/chunk_index/score/content`，按 chunk_index 排序；读取 envelope 顶层 page/page_size/total。 | 内容与排序字段是否必需/唯一；分页响应 envelope、total 含义、末页判定、内容/源版本变化；原文访问入口。 |

有一个 probe 可观测性缺口：`request()` 返回的只有 `payload.data`；因此 CHUNKS 检查拿不到 envelope 顶层的 `page/page_size/total`，虽然旧 adapter会转发这些字段。当前测试也没有断言该分页元数据。P05/P06 不能据此认为分页已验证。

## 禁止推断

- 不得把旧 adapter 的空范围行为（回退到全部凭据可见库）复用为企业授权语义；P05 明确空范围拒绝、显式越界整单拒绝，企业搜索必须先算有效范围。
- 不得因请求只包含一个 `knowledge_base_id` 就把返回文档归属猜成该库。DETAIL 缺少归属时必须用经验证的授权 metadata 补齐；仍无法确认则 `UPSTREAM_SCHEMA_INVALID`，禁止读取 chunks/原文。
- 不得把 `chunk_index` 当页码、源版本或稳定 cursor；不得按知识 ID 拼造原文 URL、公开链接或本地路径。
- `original_url`、`source_url`、`version`、更新时间等都不是当前 probe/旧 adapter 已验证字段。不存在或无法安全解析时原文标记不可用，并提供检索文本阅读。
- 旧 gateway `GatewayAdapter.call_tool` 是同步接口，WeKnora 旧 `_request` 使用同步 `httpx.Client`。P05 的插件业务应遵守计划明确的 `httpx.AsyncClient`，不能把旧同步 adapter 当作异步实现或企业授权边界。

## P05/P06 最小安全异步合同建议

将 REST 映射留在知识插件：一个操作上下文共享 AsyncClient、请求计数与 90 秒 deadline；每请求 30 秒、总数不超过 10、禁用重定向/自动重试，跨主机 HTTPS 校验证书。超过任一预算即返回明确失败/部分阅读状态。网络异常映射为脱敏稳定错误，不把响应 body、Key、query 或私有 URL 写日志。现有 builder 会将 origin 规范化为 `/api/v1`，但其单测明确允许 LAN HTTP；enterprise probe 只允许 loopback HTTP、远程目标 HTTPS并可接受 CA。企业 adapter 应以 probe/P01 规则为准，资源 ID 做 URL path quoting，不接受任意路径参数。

调用层每次先以可信 identity 重新解析 connection/config/grant/binding revision 与有效 KB 集合。LIST 对员工只返回授权交集中的行及 metadata；空集拒绝。SEARCH 只提交有效 KB ID，校验每个命中可关联到授权 KB；缺少归属时只允许经受预算约束的 metadata 补查。DETAIL 先校验真实文档 ID 和 KB 归属，再请求 chunks；P06 每次续读/原文打开都重新授权。严格 DTO（extra=forbid）区分上游原始响应与插件标准结果；缺必要字段应失败，不用 `str(None)`、默认 0、调用参数或标题补身份。

标准结果至少保留：

- KB：`kb_id` 为可信上游 ID；名称/描述/计数只作展示，不作为授权事实。
- 命中/文档：`knowledge_id`、确认过的 `kb_id`、标题、检索文本、检索时间、截断标志；来源时间/版本/原文引用仅在真实响应及访问规则确认后填充。
- 阅读页：`knowledge_id`＋`kb_id`、有序块内容、已验证分页位置/结束状态、可恢复 continuation；continuation 服务端签名并绑定 user/thread/connection/document/源版本或内容摘要/块内偏移。源版本缺失时可用完整内容摘要保护续读；无法检测源变化时不可宣称无缝连续一致。
- 原文资源：仅保存受信 WeKnora ID/已校验主机的资源引用，由 Host 代理逐次鉴权；不得向 UI 暴露私有签名 URL。没有已验证入口就返回“原文不可用”。

## 合成 fixture 矩阵（本地合同，不等同实机证据）

| 面 | 至少覆盖的成功形状 | 负例/边界 |
|---|---|---|
| 公共 envelope/HTTP | `success=true` 且 data 类型符合操作；JSON 与认证头正确 | 401、403、其他非 2xx、3xx、非法 JSON、success=false、data 类型漂移；Key/query/body 不出现在错误；取消、超时、超预算；0 自动重试 |
| LIST | 多 KB 的真实字段样例；必要 ID；可能分页的每页和结束条件 | 缺/空/重复 ID、malformed row、第二页、越权 metadata 不泄露、上游返回集合不完整 |
| SEARCH | 非空多命中、一个/多个有效 KB、真实必需字段、完整 query 不变 | EMPTY 与失败区分；缺/空 knowledge_id；缺归属后 metadata 补查/补查失败；跨库命中；超 8 条、内容 1,200/1,201 字符 |
| DETAIL/CHUNKS | 归属匹配；detail 字段；两页 chunks + envelope page/page_size/total；稳定顺序；源版本/更新时间字段若存在 | kb_id 与 knowledge_base_id 不同、归属缺失/越权、同名异 ID、缺失文档、无 chunks、缺 index/content、重复/乱序 index、页空但 total 未完、源内容变化 |
| P06 continuation/original | 12,000 字文档分段拼接；源摘要/版本稳定；已验证原文资源由代理访问 | 8,000/8,001 边界；伪造/跨 user/thread/document continuation；撤权后零外呼；源变更要求重读；URL 跨主机/跳转、路径穿越、无原文入口；块号不显示为页码 |

以上 fixture 仅验证插件对明确选定合同的处理。只有从目标专家执行主机对固定 WeKnora 版本/digest 取得脱敏的实响应、权限差异和证书结果，才可把相应字段/语义升级为真实合同并解除 P01/A-01 阻塞。
