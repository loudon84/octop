# P10a selected search HTTP adapters — 实施合同

状态：**独立 Spec PASS / Builder PASS，root已完整核对修订及复审**。精确两新路径基线与READY brief后派发，本地功能尚未实现；规划记录task-P10a-search-adapters-plan-review.md与plan-fix1-review.md。用户已要求无法验证的WeKnora转人工对接、先推进其它功能；P09c2保留NOT READY，未授权运行fixture。本文件只规划P10a，不改source/test/spec、不访问远端/env/凭据/Git、不重做供应商研究。

目标：在enterprise-knowledge插件内交付SearXNG/Firecrawl各自单次HTTP请求和严格normalized结果，供后续编排调用。部署地址/版本未填是live验收门，不阻本地MockTransport实现，也不能猜部署已兼容。

## 1. 权威输入、实码与精确白名单

直接沿用同目录task-P10-selected-contract-inputs.md、task-P10-actual-seam-preparation.md，prd/evidence/enterprise-p1/provider-decision.md，以及openspec/changes/enterprise-knowledge-p1/specs/enterprise-controlled-search/spec.md。SearXNG主/Firecrawl备、exact public_query、单源10秒/操作22秒、最多两请求、仅timeout/429/5xx可切一次、无redirect/retry/scrape已固定；23秒只是操作测试容差，不延长任何deadline。

**未来source仅新增** `plugins/enterprise-knowledge/enterprise_knowledge/search_http.py`；**未来test仅新增** `tests/unit/plugins/test_enterprise_search_http.py`。本草案本身仅写S/task-P10a-search-adapters-draft.md。

复用models.StrictDto；已有EvidenceRecord支持source_kind=WEB/locator且WEB不能带knowledge connection。当前weknora.py有结果cap8、content1200的先例，但这是知识读取实现，不能将其OperationBudget90/30/10拿来放宽搜索。`models.normalize_base_url()`强制/api/v1并读WeKnora HTTP env，**不复用**；纯 `_origin(value)->tuple[str,bytes,int]` 可在自己的raw URL检查后复用。现有StrictDto/SecretStr/asyncio/httpx即可，不引入SDK/HTML解析器/依赖。

不改Core/actions/operations/approvals/service/sessions/evidence persistence、policy/worker/UI/i18n/env template；不消费审批、不造WEB_ONLY准备方法、不持久request journal、不做主备切换、22秒编排或并发全局请求保证。这些由P10b及后续settings/modes精确切片实现，不能由本adapter的单实例限制冒称完成。

## 2. 内部接口与所有权

所有DTO内部、strict/frozen/extra forbid，不加公开action schema。secret字段用SecretStr、repr=False；安全边界同时覆盖**直接SearchSourceConfig构造与model_validate等对外验证入口**，不能先泄出ValidationError再仅保护Attempt构造。验证错误转换固定SearchHttpError(INVALID_CONFIG)，在原except作用域外抛出，不保留raw input/cause/context；未知provider只能None，不回显原值。内部parser的Pydantic失败也只返回安全解析结果，不让executor future留下raw异常。

```python
SearchProvider = Literal['SEARXNG', 'FIRECRAWL']

class SearchSourceConfig(StrictDto):
    provider: SearchProvider
    base_url: StrictStr
    credential: SecretStr | None = None
    ca_bundle: StrictStr | None = None  # 可信server配置，不接受浏览器路径

class WebSearchHit(StrictDto):
    provider: SearchProvider
    source_kind: Literal['WEB']
    title: StrictStr
    locator: StrictStr                # 原样经过验证的结果URL
    content: StrictStr                # 最多1200 codepoints
    original_length: StrictInt
    truncated: StrictBool
    source_version: None = None
    source_page: None = None

class SearchHttpResult(StrictDto):
    provider: SearchProvider
    status: Literal['SUCCESS', 'EMPTY']
    hits: tuple[WebSearchHit, ...]
    results_truncated: StrictBool     # 上游合法数组超过本地cap时为True

class SearchHttpAttempt:
    def __init__(self, config: SearchSourceConfig, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    async def search(self, public_query: str, *,
                     deadline_at: float | None = None) -> SearchHttpResult: ...
    async def aclose(self) -> None: ...
```

这些...仅合同，实施不留stub。一个instance只允许一次search；NEW→RUNNING→DONE/CANCELLED→CLOSED，首await前同步占用；任何非NEW状态search（含CLOSED）固定INVALID_USAGE，不排队、不建client/发送。此仅局部约束，不承诺同operation跨实例/进程两请求保证。

aclose状态合同固定：**NEW→CLOSED且零client/零future/零send**；RUNNING时aclose立即安全INVALID_USAGE，不改state、不cancel/search或关闭其client。caller若要停止，先cancel并await search任务终结，再aclose。DONE/CANCELLED的close只排空该attempt持有的唯一cleanup；首次close在首await前登记同一drain task，重复/并发close均await它，底层client仅关一次、TLS/parser future均实际回收后才CLOSED。CLOSED重复close幂等；重复取消不丢失原drain引用，不新建第二份关闭任务，不提前声称已排空。search失败（含TIMEOUT/输入错误）最终DONE，caller取消最终CANCELLED；清理失败不覆盖已定搜索终态。

内部 `SearchHttpError(provider: SearchProvider|None, code, status: int|None=None)` 只保留安全字段/固定args；provider尚未通过闭集校验时只能None，不回显原值。code闭集 INVALID_CONFIG/UNSUPPORTED_CONFIG/INVALID_QUERY/INVALID_USAGE/TIMEOUT/RATE_LIMITED/UPSTREAM_5XX/AUTH_REJECTED/HTTP_REJECTED/SCHEMA_INVALID/UNAVAILABLE。constructor非法config、URL/auth组合同步抛安全error且零request；methods按下面矩阵抛安全error；取消传播普通asyncio.CancelledError，不塞DTO/query/secret。异常不携带raw __cause__/__context__；若转换异常，离开原except作用域后抛安全异常，不能只from None遮盖仍保留敏感context。

## 3. exact请求、URL与auth边界

search只接受可信caller已从审批持久记录取出的**同一个非空StrictStr** public_query；不调用strip/lower/normalize，不重写任何空格、Unicode或换行；首尾空白/纯空白但非空也原样发送，因为adapter不重新裁判已批准文本。非string/空串在send前INVALID_QUERY。不要传历史、证据、附件、客户字段；adapter无接受这些数据的参数。审批证明/权限/一次消费是P10b前置，本批不假称已实现。

provider base规则由本模块小函数 `normalize_search_base_url(provider: SearchProvider, value: str)->str` 固定：absolute HTTPS origin，可带明确reverse-proxy prefix；HTTP仅字面loopback localhost/127.0.0.0/8/::1，不读WeKnora test HTTP env、不新增remote HTTP例外knob。拒userinfo、query/fragment、空host/空port/非法port、控制字符/反斜杠/裸空白、非法UTF-8；path拒dot segments、编码斜杠/反斜杠或编码dot绕过。规范化scheme/host/defaultport并去末尾slash，保留合法escaped prefix。path若已以/search、/v2或/v2/search结尾则INVALID_CONFIG，明确base不是完整endpoint，不默默猜/剥版本。

`https://search.example/proxy/`→SEARXNG `https://search.example/proxy/search`；FIRECRAWL `https://search.example/proxy/v2/search`。无prefix则/search或/v2/search；不追加/api/v1、不使用会丢prefix的urljoin绝对path。上述prefix接收限制是本地配置合同，不是对上游额外保证。实际端点/version不兼容保持配置失败，需live人工确认。

| provider | 单次请求（仅这些字段） | auth |
|---|---|---|
| SEARXNG | POST `<base>/search`，form-urlencoded `q=<原query>&format=json`；不传pageno/engines/categories/其它字段 | credential必须None。官方无已约定标准auth header，传了任何credential即UNSUPPORTED_CONFIG、零send；不发X-API-Key/Bearer，不造auth header knob。反代需特定鉴权的部署留人工合同接续 |
| FIRECRAWL | POST `<base>/v2/search`，JSON `{query:<原query>,limit:8,highlights:false}`；不用sources（已知文档差异）、scrapeOptions/抓取参数 | credential非None时仅Authorization: Bearer；None时无auth头，表示本次无凭据，不宣称部署允许匿名。收到401/403固定失败，不能自动添key/换scheme/切备用 |

Firecrawl给定credential必须非空、无首尾空白/CRLF，保持SecretStr，不推断JWT结构；SearXNG任何非None credential都不悄悄忽略。没有required-auth toggle；实际部署是否要求Bearer仅由后续已验证配置/真实响应决定。

## 4. 10秒、TLS/client和取消清理

constructor只做短小同步纯配置/URL校验，无TLS/IO；每次search开始记录started=clock()，有效deadline=min(started+10,caller deadline_at或started+10)。deadline_at只可收紧，非finite/bool拒绝。query检查、TLS/client、HTTP body及完整parser共用同一deadline。send前同步最终门remaining>0，HTTPX/asyncio timeout不超过remaining；parse结束后保留最终同步clock门，过期只TIMEOUT。不重开10秒、不用90秒知识预算；P10b以后传整体22秒截止，本批不编排。

**JSON decode/完整数组校验/全量字段URL验证/normalized DTO投影全部进入本attempt拥有的纯executor future**，不能同步长时间占event loop再靠末尾clock检查冒称10秒内终态。传入仅已收齐response bytes及闭集provider，不传HTTP client/Host/持久层；future只产生SearchHttpResult或安全SCHEMA_INVALID解析标记，无HTTP/提交/日志/发布副作用。用shield保留future所有权，按同一deadline的此刻remaining await；超时立刻决定TIMEOUT，不等待解析完成、不返回late result、不再次send。其余同步步骤仅短小局部校验/组装，不能把任意CPU工作说成asyncio可抢占。

client构造 `ssl.create_default_context(cafile=ca_bundle)` 在executor，verify=context、follow_redirects=False、trust_env=False、无default auth/cookies/event hooks；不能verify=False、不读env/secret storage、不允许默认netrc/proxy凭据。测试只注入可信MockTransport。独立新httpx.Request+client.send(auth=None,follow_redirects=False)，headers显式provider局部设置，防client默认Authorization/X-API-Key/Cookie或响应Set-Cookie污染；请求extensions含精确剩余timeout。没有SDKretry、302重发、二次fetch、HEAD、scrape/crawl或API版本探测。

一个search可发**最多一次send**，即使429/5xx/timeout也只返回该安全错误；没有fallback代码。取消在client init/send/parse前后均不再发送，清secret引用并传播CancelledError；迟到response不得返回成功，不持久任何证据。取消不是TIMEOUT，不作为备用触发；底层ConnectError/TLS错误不是timeout，不擅自归可切换类别。

TLS/parser executor futures均由attempt持有且shield；超时/取消后，唯一cleanup回收迟到TLS client（准确aclose一次）及迟到纯解析结果（只丢弃，不publish），普通client同样归此cleanup。search结果/错误在10秒截止内决定，不等未完成TLS/解析延长deadline；caller按上述状态合同finally await aclose排空。资源排空可超过截止，不声称阻塞executor在10秒内可强杀；P10b把可等待时间纳入22秒且不因此新开轮次。cleanup异常须被回收/安全转换，不留task raw异常日志或覆盖搜索终态；无后台继续search，只有纯解析/资源清理。测试分别断言search终态deadline与最终drain，不把整个context teardown冒称10秒内。

## 5. strict parser与normalized结果

接受HTTP200的JSON成功形状（本地严格接收政策；其它2xx不冒称文档已保证）：SearXNG对象+results数组；Firecrawl success **is True**、data对象+data.web数组。显式投影required字段，未知额外字段忽略且不入DTO；重复JSON键、nonfinite常量、非法UTF-8/JSON、错误root/container/字段类型均SCHEMA_INVALID。success=1/'true'/false不通过；HTTP200 success:false从不变EMPTY。

**冻结本地cap=8、snippet=1200 codepoints**：8沿现有knowledge cap作本地产品边界，非上游硬max；1200沿weknora SearchHit内容界限。完整响应数组先逐行校验（包含第9项以后），任何一坏行整次invalid，不跳过/“全部过滤后EMPTY”；全部合法才取前8，保持上游顺序、不去重、不重排、不第二页。results_truncated=len(array)>8；EMPTY仅数组真实[]，hits=()/results_truncated=False。非空array成功必须有至少一hit。

每行必须object；raw title必须string、有效UTF-8、含至少一个非空白字符、无控制字符；保留原title（包括合法首尾空格），不trim/HTML转换/截断或由URL造标题。raw URL必须string、非空且不带首尾/内部裸空白或控制/反斜杠，urlsplit+httpx校验absolute http/https、有host/合法port、无userinfo；拒relative、//host、javascript/data/file、非法UTF-8/percent escape及编码CR/LF。结果URL可以带合法query/fragment、保持原文，不canonicalize/dereference/跟随；只是locator，不是server抓取许可。

snippet：SearXNG取content，Firecrawl取description（highlights:false）；字段缺失或空string→''；present null/bool/number/container→SCHEMA_INVALID。不去HTML标签或将snippet当HTML执行，按纯文本原样取前1200；original_length=len(raw snippet)，truncated=length>1200。WEB/provider/locator来源由adapter固定填，source_version/page=None，不猜provider版本/发布日期，不复制上游metadata、raw HTML正文或知识connection/kb/document IDs。

这些是待P10b转EvidenceRecord的internal DTO，不直接调用当前KB-only evidence persistence；retrieved_at/evidence_id/operation_id/owner与publication fresh-check由将来producer填，不能让provider响应提供这些本地权限字段。

## 6. 失败分类与secrecy

| 观察 | code；P10b可消费但本adapter不切换 |
|---|---|
| 总source截止或HTTPX TimeoutException | TIMEOUT；仅此类、429、5xx属于已固定备用候选 |
| HTTP429 / 500..599 | RATE_LIMITED / UPSTREAM_5XX；先按status，不解析其body假success |
| HTTP401/403 | AUTH_REJECTED；SearXNG禁用JSON的403也不可切换 |
| HTTP3xx、其它非200（含其它2xx） | HTTP_REJECTED；零redirect/重试 |
| HTTP200坏JSON/结构/行/success:false | SCHEMA_INVALID；不可切换、不可EMPTY |
| TLS/连接断开/其它HTTPX HTTPError | UNAVAILABLE；除明确TimeoutException外不扩充备用条件 |
| valid [] | 正常EMPTY；不可备用 |
| caller取消 | 普通CancelledError；不交结果、不备用 |

adapter不产出原query、credential、CA内容、headers/body、完整URL或raw exception日志，不含rawresponse/request属性/cause/context。HTTPX依赖可能INFO记录request.url，固定endpoint无query，故不在URL放批准词；不为本批改全局logging。SecretStr和config/attempt repr不泄露credential；error只provider/code/status，输入错误不回显raw值。query/结果DTO仍是敏感业务内容，未来worker不能把它们放审计/公开error；本批无用户文案，无i18n新增。

## 7. 实际MockTransport验收矩阵与实施顺序

- [ ] 先写唯一新test文件：以实际AsyncClient→MockTransport核exact route/query/body/最终headers，看到具体RED后实现唯一新模块；不以mock解析函数或声明请求字典替代实际发送。
- [ ] 核验两provider有效一条/空数组、未知extra字段不投影；Firecrawl query/limit8/highlightsFalse，SearXNG form精确解码，query含首尾空格、中文、换行、&/+/%时字符完全一致且URL无q；只一次send。
- [ ] 8/9合法行cap与results_truncated；坏第1项/第9项/all-invalid整次SCHEMA_INVALID；snippet absent/empty/1200/1201/补充平面Unicode，null/错type拒绝；title空白/控制/非str、URL schemes/userinfo/relative/裸空格/port/encoding反例；不去重/重排。
- [ ] HTTP200 malformed/duplicate-key/nonfinite/wrongroot/success false/1/null和错container；429/5xx/401/403/3xx/其它2xx/ConnectError/TimeoutException各精确code，不自动第二请求，Location指向外站也不访问。
- [ ] SearXNG credential非None→UNSUPPORTED_CONFIG/零send；Firecrawl None/有key对应无auth/Bearer；给实际client注入poison defaults/auth/cookies及响应Set-Cookie，MockTransport收到的headers仍无越界凭据；repr/error args/cause/context/caplog不含secret/query sentinel。
- [ ] URL root/prefix/trailing slash正确拼接；完整endpoint/版本重复、远程HTTP、userinfo/query/fragment/dot/encoded slash拒绝；loopbackHTTP可测；privateCA交ssl builder、verify不关闭。无需真实网络/环境CA路径。
- [ ] 用受控clock+Event证明TLS/pre-send/parse消耗同10秒、caller更早deadline有效且不能放宽、最终同步门过期零send；实际request timeout<=remaining<=10。小的更早deadline验证真实async timeout，无真实10秒sleep。
- [ ] 纯parser executor用threading.Event卡住：实际MockTransport已一次返回完整body，parser开始后deadline先到，search在解析未放行时TIMEOUT；随后aclose仍pending，放行parser后才排空。迟到合法/非法解析均不交结果/不改既定TIMEOUT，零第二send；另以受控clock核刚完成解析后的最终同步截止门，不能只推进clock等同步解析自然返回充当上述证据。
- [ ] NEW先aclose→CLOSED，零client/future/send，再search INVALID_USAGE。用Event分别卡TLS/HTTP/parser使state RUNNING，aclose固定INVALID_USAGE且不改原search/不关闭client；原任务可正常完成，或caller先cancel+await后close。DONE/CANCELLED重复并发close共享同一drain，底层client准确关闭一次。
- [ ] init/request/parser分别cancel：普通CancelledError，无后续send/迟到结果；TLS晚完成与parser晚完成均被唯一cleanup回收，重复取消aclose仍排空。直接SearchSourceConfig构造/验证非法输入的repr/args/cause/context/caplog不得带secret/query/raw provider sentinel；单实例占用不算operation级并发保证。
- [ ] 运行 `uv run pytest tests/unit/plugins/test_enterprise_search_http.py -q`、两路径Ruff/适当mypy；root最终集成make all。纯规划本次不执行；无commit/push授权。

## 8. root与独立审查冻结点

确认cap8/1200及whole-array all-invalid；确认HTTPS/loopback+prefix、HTTP200-only；确认executor完整解析沿原deadline等待及close明确状态合同。部署origin/version、SearXNG额外auth/Firecrawl实际auth、CA及same/different/unknown后端关系留人工live门，不阻本地切片。P10b另冻22秒、durable dispatch claim/journal、exact approval消费、跨并发两请求/late publication，本批不提前宣称落地。

## 9. 独立审查修订记录

按task-P10a-search-adapters-plan-review.md关闭B1：JSON decode+FULL数组校验+DTO投影进入attempt-owned纯executor future，按同一remaining deadline等待、TIMEOUT后不late publish、aclose回收；保留最终同步clock门，不加body/result上限、不跳行/增网络/重开budget。关闭B2：NEW close零资源、RUNNING close拒绝且不干预search、caller先cancel+await、DONE/CANCELLED唯一drain及并发幂等。同步明确直接SearchSourceConfig构造/验证也安全转换Pydantic异常。新增对应Event证据；两文件白名单不扩、仍NOT READY，仅待B1/B2有界复核。未改source/test/spec、未读env/凭据或访问网络/Git；P09c2持续停止。
