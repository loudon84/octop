# Next-stage baseline（N00）

日期：2026-10-26。性质：**本次运行记录**；不把历史全仓报告迁移到本 HEAD。

## 1. 接管事实

| 项 | 值 |
|---|---|
| Worktree | `C:\Users\Administrator\.codex\worktrees\enterprise-p1\octop` |
| Branch | `codex/enterprise-p1` |
| HEAD | `6dcdb8961b233e1bf91f9219882b66179dedb247` |
| 工作区（接管时） | `M prd/Octop_Cursor_Handoff_2026-10-26.md`；`?? prd/Octop_下一阶段开发计划与实施明细_v1.0.md`；功能代码干净 |
| 主仓 | `E:\git\octop` @ `main` — 禁止在此重做功能 |
| 执行者 | Cursor Composer（非 GPT-6 Luna；环境无该模型） |

## 2. 本次定向验证（本次运行）

```text
uv run pytest tests/unit/plugins/test_enterprise_search_http.py tests/unit/plugins/test_enterprise_search_settings.py -q
→ 136 passed in 10.77s；exit 0；PYTHONUTF8=1
```

## 3. 历史报告边界（不可套用到本 HEAD 为「已全仓验收」）

| 报告 | 说明 |
|---|---|
| `5674 passed / 236 skipped` make all | 属于旧基线 `74dc70d8` 时段记录；**不是**本 HEAD 的本次全仓运行 |
| R0–R2 execution-status 段 | 搜索设置后端已接受；定向 136 与本次复跑一致 |

## 4. R2 增量复核（只读）

| 能力 | 状态 |
|---|---|
| `search.get/save/test/recover` 已注册 | 可用 |
| Host CAS / 凭据 stage-commit / recovery_blocked | 按合同实现 |
| 诊断脱敏（无 query/secret 入诊断 payload） | 按合同 |
| `pre_send` 拒绝→零 HTTP | 按合同 |

### 已确认接入缺口（列入 N01 / N02a-1，本批不修）

1. **员工投影过宽**：`search.get`（authenticated）对员工仍返回 `primary/secondary.base_url`、`reported_version`、`ca_bundle_configured`、`credential_configured`、`origin_relation` 等；仅 `diagnostics` 按 admin 隐藏。未满足「员工仅 readiness 摘要」。
2. **CA 省略即清除**：`SearchSourceSavePart.ca_bundle` 默认 `None`，save 始终写入 `part.ca_bundle`；表单省略会丢失旧 CA。凭据已有 `KEEP|REPLACE|CLEAR`，CA 尚无对等操作。
3. **`web_search_ready`**：由双源 `base_url` 存在且非 `recovery_blocked` 投影，**不等于**最近诊断成功，也**不等于**员工联网授权。

## 5. 下一阶段起点文件存在性

| 路径 | 存在 |
|---|---|
| `enterprise_knowledge/search_settings.py` | 是 |
| `enterprise_knowledge/search_settings_actions.py` | 是 |
| `enterprise_knowledge/search_http.py` | 是 |
| `enterprise_knowledge/search.py` | 否 |
| `enterprise_knowledge/search_operations.py` | 否 |
| `ui/src/SearchSettings.tsx` | 否 |
| `ui/src/Composer.tsx` | 否 |
| `ui/src/index.ts` 注册 composer | 否（仅 settings + evidence renderer） |

## 6. N00 验收

- [x] 精确基线已记录（本次 HEAD + 本次 136）
- [x] R2 可用；已知缺陷已列精确修复子批（N01/N02a-1）
- [x] 未将旧全量报告迁移为新 HEAD 验收
