# R0–R2 Brief（2026-10-26）

## 基线

- Worktree: `C:\Users\Administrator\.codex\worktrees\enterprise-p1\octop`
- Branch / HEAD: `codex/enterprise-p1` / `74dc70d81753488cce48dc2e107e19f46dc5a741`
- Untracked at start: `prd/Octop_Cursor_Handoff_2026-10-26.md`（及本 brief / 后续合同）
- Do not modify main checkout `E:\git\octop` for feature work

## 已接受（本轮不重做）

- Host / CAS / credentials / execution wiring (P03*)
- Shared knowledge connections/grants/bindings/probes/reading (P04–P06)
- Tool/policy hooks (P07c2*)
- Generic requirements/surfaces/send-queue (P08a–c)
- Shared Settings UI + EvidenceRenderer + locale/theme (P08d, P09b, P09c1)
- Private-knowledge framework I01/I02 + I03a action DTOs (not full worker)

## 未验收 / 拟建未存在

| 项 | 状态 |
|---|---|
| P10a `search_http.py` | 已提交；作者测试有报告；**本轮 R1 独立验收** |
| `search.py` / `search_settings.py` / `provisioning_wire.py` | **不存在** |
| `search.get/save/test` actions | **未注册** |
| P10b1 settings contract | NOT READY → 本轮冻结 |
| R3 编排 / R4 UI / R5 三模式 / 私库 wire | **本轮不做** |

## 本轮精确范围

1. **R1** — 对照 `search-http-adapters-contract.md` 验收现码 + 定向测试/静态
2. **Freeze** — 写入 `search-settings-contract.md`（四项决策）
3. **R2a** — `SearchHttpAttempt.search` 可选 `pre_send`
4. **R2b** — `search.get` / `search.save` / `search.test` + recovery + MockTransport 证明
5. **Docs** — 更新 `execution-status.md`；OpenSpec 不越勾 5.3/2.4 整项

## 允许改动面

- `plugins/enterprise-knowledge/enterprise_knowledge/search_http.py`
- 新建 `plugins/enterprise-knowledge/enterprise_knowledge/search_settings.py`（及必要的 models/helpers）
- `plugins/enterprise-knowledge/enterprise_knowledge/actions.py` / `main.py`（注册 actions）
- `tests/unit/plugins/test_enterprise_search_http.py`
- 新建 `tests/unit/plugins/test_enterprise_search_settings.py`
- `prd/evidence/enterprise-p1/search-settings-contract.md`
- `prd/evidence/enterprise-p1/execution-status.md`
- 必要时 en/zh 文案键（插件侧）

## 禁止改动面

- Host 核心白名单外业务逻辑；Dashboard 业务页面；Settings.tsx 搜索 UI（R4）
- `search.py` 主备编排（R3）；provisioning_wire；composer/三模式
- 推送、发布、live probe、读取并打印真实密钥
- 重写 EvidenceRenderer / 已接受 ConnectionService 行为（仅可参考模式）

## 失败语义（摘要）

- `search.test`：非 admin / revision 冲突 / recovery_blocked / 降权 → 零 HTTP
- SearXNG 任何 credential → `UNSUPPORTED_CONFIG`
- `pre_send` 抛错或超时 → 零 send；不延长 10s 预算
- release/commit 未知 → 保持 `recovery_blocked`，不得标成功

## 定向验证命令

```powershell
$env:PYTHONUTF8 = '1'
uv run pytest tests/unit/plugins/test_enterprise_search_http.py -q
uv run pytest tests/unit/plugins/test_enterprise_search_settings.py -q
# 插件路径 Ruff/format/strict mypy（实施时按实际改动文件列出）
```
