# P10b1 Search Settings Contract（冻结）

状态：**Spec 冻结 READY（2026-10-26）**；R0–R2 后端实现与定向 Mock 验收通过。**N01 增量（2026-10-26）** 冻结员工投影白名单与 CA `KEEP|REPLACE|CLEAR`（由 N02a-1 实现）；候选草案字段不得直接当已接受 API。本批动作仍为 `search.get` / `search.save` / `search.test` / `search.recover` 与 adapter 可选 `pre_send`；不含主备编排、三模式、员工 `query.approve`。不勾选 OpenSpec 主 5.3 / UI 2.4 整项。

## 1. Actions

| name | permission | 输入 | 输出 |
|---|---|---|---|
| `search.get` | `authenticated` | `EmptyInput` | admin → `SearchSettingsOutput`；员工 → `SearchSettingsEmployeeOutput`（见 §5.1） |
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
  ca_op: KEEP | REPLACE | CLEAR   # default KEEP；N01/N02a-1
  ca_bundle: str | None           # only with REPLACE；server-trusted string only
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

- SEARXNG：`credential` 必须始终 None；`REPLACE`/`CLEAR` 带非空 credential → `UNSUPPORTED_CONFIG`；`CLEAR` 对 SEARXNG 凭据为 no-op（无凭据）
- FIRECRAWL：`KEEP` 省略/忽略 credential 字段；`REPLACE` 需非空 Bearer 材料；`CLEAR` 删除 active ref
- 同源关系固定 `UNKNOWN`
- **CA（N01）：**
  - `ca_op=KEEP`（默认）：忽略 `ca_bundle` 字段；保留存储中的旧 CA（含「从未配置」）
  - `ca_op=REPLACE`：必须提交非空可信 `ca_bundle` 字符串；禁止路径/env 自动加载
  - `ca_op=CLEAR`：显式清除 CA；`ca_bundle` 必须为 None
  - **禁止**把「省略 `ca_bundle`」解释为清除；旧实现该行为视为缺陷，由 N02a-1 关闭
  - get 对 admin 只回显 `ca_bundle_configured: bool`，永不回显 CA 明文
- version：仅 `reported_version` 存储与 admin 回显

### 5.1 员工 `search.get` 字段白名单（N01）

员工响应 schema：`SearchSettingsEmployeeOutput`（或等价投影，不得复用完整 admin `SearchSourceView`）。

**允许字段：**

| 字段 | 语义 |
|---|---|
| `revision` | 配置 CAS 水位（只读观察；员工不用于管理编辑） |
| `recovery_blocked` | 凭据准备残留阻塞（只读；员工不可 recover） |
| `primary_ready` | 主源地址已配置且非 recovery_blocked |
| `secondary_ready` | 备源地址已配置且非 recovery_blocked |
| `web_search_ready` | `primary_ready ∧ secondary_ready`（**配置投影**，见 §5.2） |

**禁止返回（员工）：** `primary` / `secondary` 对象及其 `base_url`、`reported_version`、`ca_bundle_configured`、`credential_configured`、`origin_relation`；全部 `diagnostics`；任何 CA/凭据材料或诊断详情。

Admin 仍返回完整 `SearchSettingsOutput`（含两源视图与 diagnostics）。

真实 action 入口必须负例断言员工响应键集合 ⊆ 白名单。

### 5.2 三种状态不得混淆

| 状态 | 来源 | 不得推导 |
|---|---|---|
| 配置可用（`*_ready` / `web_search_ready`） | 配置投影：地址存在且非 `recovery_blocked` | ≠ 最近诊断成功；≠ 员工已获联网授权 |
| 最近诊断结果 | admin-only `diagnostics` / `search.test` 输出 | ≠ 自动授予员工 query.approve 或改 session mode |
| 员工实际授权 | session/operation/policy 与后续批准动作 | 前端不得用 `web_search_ready` 替代服务端授权 |

## 6. 错误码（插件动作）

复用现有 PluginActionError code 风格：`CONFIG_INVALID`、`CONFIG_CONFLICT`、`UNSUPPORTED_CONFIG`、`RECOVERY_BLOCKED`、`ROLLBACK_FAILED`、`FORBIDDEN`、`UPSTREAM_*`（test 映射 SearchHttpError）。

## 7. 验收矩阵（本地）

- Host + SQLite 真实 facade；MockTransport 注入 test
- admin 限制；非 admin save/test 拒绝
- 凭据 KEEP/REPLACE/CLEAR；SEARXNG 禁凭据
- **CA KEEP/REPLACE/CLEAR**：编辑 URL 不丢 CA；显式 CLEAR 生效；CAS 冲突不修改旧 CA
- **员工 get 字段白名单**（真实 action 入口）
- CAS 冲突；迟到 revision
- recovery_blocked 阻断 save/test；recover 路径
- pre_send 降权/冲突 → 零 send
- 诊断不 fallback；无 query/secret 泄漏
- 定向 pytest + Ruff/format/strict mypy

## 8. Composer Host 类型子集（N01 核对，实现属 N03）

插件 UI 仅声明与 Host 相同的子集，不发明运行时接口。对照 `dashboard/src/plugins/toolRenderers/types.ts`：

- `PluginHost.registerComposerPreparation(handler): () => void`
- `ComposerSelectionCapture` = `{ state: "uninitialized" } | { state: "selected"; value: PluginJsonObject }`
- `ComposerPreparationHandler`：`captureSelection` / `prepare` / `restoreSelection`
- `PluginJsonObject` = `{ readonly [key: string]: PluginJson }`

插件本地 `ui/src/host.ts` 在 N03 补齐上述声明；N01 仅冻结，不改运行时。

## 9. 非目标

主备 22s 编排、operation 去重、WEB evidence、Settings UI（N02）、员工批准词、live 供应商、勾选 OpenSpec 5.3/2.4 整项、私库来源。
