# 企业插件开发环境输入

模板：[enterprise-dev.env.example](enterprise-dev.env.example)。模板没有真实Key，不会被Octop自动加载，也不会自动创建/修改远程服务。

## 已配置的本机用户变量

OCTOP_WEKNORA_API_KEY、BASE_URL、VERSION、TEST_HTTP_ORIGIN、TENANT_ID已配置在Windows当前用户环境，实际完整变量名均以OCTOP_WEKNORA_开头。Key没有写入模板。新终端可读取用户环境；已经运行的Codex/终端不一定自动更新进程环境，需要显式加载所需变量，勿打印Key：

```powershell
$env:OCTOP_WEKNORA_API_KEY = [Environment]::GetEnvironmentVariable('OCTOP_WEKNORA_API_KEY', 'User')
```

已只读确认工作空间10000目前0个知识库。完整P01需要两组已索引库/文档ID、非空检索词、消费者身份和镜像digest；只填版本不能完成兼容验收。开发测试HTTP来源例外由用户明确授权，P01b probe支持已实现并通过34项本地测试及独立审查；插件运行时支持随P04接入；其它origin不由该例外放行。

## 待手工填写的搜索变量

| 服务 | 非敏感输入 | 凭据输入 |
|---|---|---|
| SearXNG | OCTOP_SEARXNG_BASE_URL / VERSION / CA_BUNDLE | OCTOP_SEARXNG_API_KEY，是否需要取决于企业网关认证 |
| Firecrawl | OCTOP_FIRECRAWL_BASE_URL / VERSION / CA_BUNDLE / SEARCH_BACKEND | OCTOP_FIRECRAWL_API_KEY，是否需要取决于部署认证 |

主备角色暂按SearXNG主搜索、Firecrawl备搜索核查；Firecrawl此处是搜索API，不会自动增加scrape/crawl网页请求。如果Firecrawl搜索后端仍是同一个SearXNG，请在SEARCH_BACKEND注明共享依赖。SearXNG需启用JSON格式。

非敏感变量可在当前PowerShell或用户环境手工设置，例如：

```powershell
[Environment]::SetEnvironmentVariable('OCTOP_SEARXNG_BASE_URL', 'https://your-search-host', 'User')
[Environment]::SetEnvironmentVariable('OCTOP_FIRECRAWL_BASE_URL', 'https://your-firecrawl-host', 'User')
```

搜索变量目前是P10适配/联调的输入合同，尚没有已完成的读取器。插件正式运行的配置与凭据仍通过管理员动作写入Host事务/密文存储；不让工具直接拿环境Key绕过配置revision与授权。
