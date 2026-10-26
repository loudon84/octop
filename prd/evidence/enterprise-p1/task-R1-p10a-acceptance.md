# R1 P10a 独立验收（2026-10-26）

## 范围

对照 `search-http-adapters-contract.md` 复查已提交实现；本批不新增功能（`pre_send` 属 R2a）。

## 命令与结果

```text
uv run pytest tests/unit/plugins/test_enterprise_search_http.py -q
→ 125 passed in 5.55s, exit 0

uv run ruff check .../search_http.py .../test_enterprise_search_http.py
→ All checks passed

uv run ruff format --check (同路径)
→ 2 files already formatted

uv run mypy --strict .../search_http.py
→ Success: no issues found in 1 source file
```

## 合同核对（本地 Mock）

| 要求 | 结论 |
|---|---|
| 单源最多一次 send；无 adapter 内 fallback | PASS（实现与测试矩阵覆盖） |
| 10s 含 TLS/parse；deadline 只可收紧 | PASS |
| cap 8 / snippet 1200；整数组坏行 SCHEMA_INVALID | PASS |
| exact public_query；SearXNG 禁凭据；Firecrawl 可选 Bearer | PASS |
| 错误码闭集；secrecy；生命周期/cancel/drain | PASS |
| 可选 `pre_send` | **未实现**（按计划属 R2a，记 follow-up） |
| 真实供应商 live | **未跑**；人工单列，不勾满主任务 5.1 |

## 结论

**本地独立 Spec/Quality 验收 PASS。** 可进入 P10b1 合同冻结与 R2。不勾选 OpenSpec 主任务 5.1 整项（缺真实探测证据）。
