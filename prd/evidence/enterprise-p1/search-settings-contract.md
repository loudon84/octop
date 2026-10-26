# P10b1 Search Settings Contract（冻结）

状态：**Spec 冻结 READY（2026-10-26）**；同日后端实现与定向 Mock 验收通过（见 `execution-status.md` R0–R2 段）。候选草案字段不得直接当已接受 API。本批只交付 `search.get` / `search.save` / `search.test` / `search.recover` 与 adapter 可选 `pre_send`；不含主备编排、UI、三模式、员工 `query.approve`。不勾选 OpenSpec 主 5.3 / UI 2.4 整项。

## 1. Actions

| name | permission | 输入 | 输出 |
|---|---|---|---|
| `search.get` | `authenticated` | `EmptyInput` | `SearchSettingsOutput`（脱敏；员工可见 readiness 摘要；admin 另见 recovery/diagnostics） |
| `search.save` | `admin` | `SearchSaveInput` | `SearchSettingsOutput` |
| `search.test` | `admin` | `SearchTestInput` | `SearchTestOutput` |
| `search.recover` | `admin` | `SearchRecoverInput` | `SearchSettingsOutput` |

写/测/恢复均在 handler 内再次 `reauthorize_actor(..., "admin")`。

## 2. 决策 1 — 管理员诊断 ≠ 员工批准

`search.test` 绑定：

- 当前 **admin** actor（入口 + 物理 send 前复核）
- `source_role`: `PRIMARY` | `SECONDARY`
- `expected_revision`: 配置 CAS
- 显式非空 `public_query`（逐字符发送；不 trim）

禁止：thread / 专家 / 消费 `query.approve` / 写 WEB evidence / 调模型 / fallback 另一源 / 改 session mode。

一次只测所选源。诊断写入 `search_diagnostics` 命名空间（脱敏 status/code/latency_ms/hit_count/results_truncated）；不持久 query 明文到诊断 payload。

## 3. 决策 2 — 物理发送前授权（R2a）

`SearchHttpAttempt.search(..., pre_send: Callable[[], Awaitable[None]] | None = None)`：

- 在 TLS/client 就绪、request 组装完毕、**最终 sync remaining>0 门之后、`client.send` 之前**调用
- `pre_send` 抛任意异常或自身超时（计入同一 deadline）→ **零 HTTP**；映射为安全错误（权限类 → 由 settings 层转为 `PluginActionError`；adapter 层超时仍 `TIMEOUT`）
- 无 callback 时行为与已验收 P10a 完全一致
- 不延长 10s 预算；callback 耗时占用同一 deadline

Settings `search.test` 的 callback：`reauthorize_actor(admin)` + 配置 revision 仍匹配 + 非 `recovery_blocked`。

## 4. 决策 3 — 凭据准备残留恢复

命名空间：

- `search_sources` — 单逻辑键 `default`，payload 含两源配置与 revision
- `search_intents` — 同键；`state`: PREPARING | RESOLVED | ABORTED | BLOCKED
- `search_recovery` — 同键；镜像 intent 供人工恢复
- `search_diagnostics` — 按 source_role 键 `PRIMARY` / `SECONDARY` 最近一次诊断摘要

流程对齐 connection intent：

1. save 开始：intent+recovery → PREPARING（CAS 检查 sources revision）
2. 需要新 Firecrawl 密钥则 `stage_credential`（预分配 UUID）
3. 成功 `commit` sources + intent/recovery RESOLVED + activations/deletions
4. 失败：`release_staged_credential`；成功则 ABORTED；release 失败 → BLOCKED（fail-closed），抛 `ROLLBACK_FAILED`
5. `recovery_blocked`（PREPARING/BLOCKED）：get 标注；save/test 拒绝；`search.recover` 清理 staged（若可知）并将 intent→ABORTED 且不谎称凭据已删成功若 release 失败

不假定 Host 自动 GC。

## 5. 决策 4 — DTO / 可信输入

固定角色：PRIMARY=`SEARXNG`，SECONDARY=`FIRECRAWL`（不可互换）。

```text
SearchSaveInput:
  expected_revision: int
  primary: SearchSourceSavePart   # provider fixed SEARXNG
  secondary: SearchSourceSavePart # provider fixed FIRECRAWL

SearchSourceSavePart:
  base_url: str
  ca_bundle: str | None           # server-trusted string only
  credential_op: KEEP | REPLACE | CLEAR
  credential: SecretStr | None    # only with REPLACE for FIRECRAWL
  reported_version: str | None    # client-reported; stored as-is, never drives branching

SearchTestInput:
  source_role: PRIMARY | SECONDARY
  expected_revision: int
  public_query: StrictStr         # non-empty; exact

SearchRecoverInput:
  expected_revision: int          # sources revision for CAS when clearing block
```

规则：

- SEARXNG：`credential` 必须始终 None；`REPLACE`/`CLEAR` 带非空 credential → `UNSUPPORTED_CONFIG`；`CLEAR` 对 SEARXNG 为 no-op（无凭据）
- FIRECRAWL：`KEEP` 省略/忽略 credential 字段；`REPLACE` 需非空 Bearer 材料；`CLEAR` 删除 active ref
- 同源关系固定 `UNKNOWN`
- CA：仅 admin 提交字符串；禁止浏览器路径/env 自动加载
- version：仅 `reported_version` 存储与回显

## 6. 错误码（插件动作）

复用现有 PluginActionError code 风格：`CONFIG_INVALID`、`CONFIG_CONFLICT`、`UNSUPPORTED_CONFIG`、`RECOVERY_BLOCKED`、`ROLLBACK_FAILED`、`FORBIDDEN`、`UPSTREAM_*`（test 映射 SearchHttpError）。

## 7. 验收矩阵（本地）

- Host + SQLite 真实 facade；MockTransport 注入 test
- admin 限制；非 admin save/test 拒绝
- KEEP/REPLACE/CLEAR；SEARXNG 禁凭据
- CAS 冲突；迟到 revision
- recovery_blocked 阻断 save/test；recover 路径
- pre_send 降权/冲突 → 零 send
- 诊断不 fallback；无 query/secret 泄漏
- 定向 pytest + Ruff/format/strict mypy

## 8. 非目标

主备 22s 编排、operation 去重、WEB evidence、Settings UI、员工批准词、live 供应商、勾选 OpenSpec 5.3/2.4 整项。
