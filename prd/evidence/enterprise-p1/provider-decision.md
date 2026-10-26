# 企业搜索与远程输入决策

2026-10-24：用户选择企业部署 **SearXNG + Firecrawl**。供应商组合已确定；主备角色暂按用户列出顺序核查（SearXNG主源、Firecrawl备源），已异步请求部署地址、版本、搜索API能力。没有部署或配置外部服务器；搜索服务未运行真实请求。用户要求先提供变量自行填写，模板见../../config/enterprise-dev.env.example。

## 搜索协议核查

- SearXNG提供GET/POST `/search`，POST为form data；`format=json`需在实例的`search.formats`启用，关闭时返回403。选用POST可避免把批准词放入URL；q仍必须逐字符保持服务端批准原文。[官方Search API](https://docs.searxng.org/dev/search_api.html)
- Firecrawl提供搜索与可选抓取，公开v2示例为POST `/v2/search`，query是查询，结果在data.web。当前公开文档的自部署支持表包含search，但不能据此认定用户部署版本/API相同。阶段一只用搜索结果，不开启scrapeOptions或第二轮网页抓取。[官方Search](https://docs.firecrawl.dev/features/search)、[自部署说明](https://docs.firecrawl.dev/contributing/self-host)
- Firecrawl源码可配置SEARXNG_ENDPOINT。若备源依赖同一SearXNG实例，则两个入口共享失败域，不能把入口不同写成后端独立；实际部署时须记录后端关系。它也可能只是用户希望的“搜索+抓取”组合，角色仍待部署信息确认。[官方源码config.ts](https://github.com/firecrawl/firecrawl/blob/main/apps/api/src/config.ts)

既有10秒单源/22秒总计/最多两次HTTP、仅timeout/429/5xx切换、无重试/重定向、原样public_query、WEB_ONLY零WeKnora等不变。自部署服务内部可能访问多个引擎，应区分Octop→API请求数与服务内部请求；当前预算承诺前者，不冒充能控制服务内部所有外呼。

## WeKnora输入

用户报告版本0.8.3和远程HTTP入口；manifest以reported字段保存非敏感信息，verified version/digest仍为null。Key按用户再次明确要求配置在Windows用户环境变量OCTOP_WEKNORA_API_KEY；没有保存到仓库文件或报告，验证仅输出已配置bool。

用户明确授权这一测试服务器使用HTTP，优先于原通用HTTPS默认。仅以OCTOP_WEKNORA_TEST_HTTP_ORIGIN精确匹配来源作为开发例外；其它远程地址仍保持HTTPS默认。P01b将落实probe合同和测试，P04沿用同一语义。

已显式live运行只读脱敏发现：共4个GET、0次search/read/写入；不带tenant的knowledge-bases返回409，tenants/all返回200且仅工作空间10000，带X-Tenant-ID=10000列库返回200、0库。已配置用户环境变量tenantID，不猜其它空间。仅证明当前目录链路可达，完整probe、索引文档、双身份权限、SEARCH/READ、digest仍缺，P01/A-01不计PASS。
