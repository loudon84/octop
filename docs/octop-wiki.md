# Octop 项目语义总览（Semantic Overview）

## Purpose

Octop 本质上是**单进程、多租户的 AI 助手平台（agent 管理后台）**，面向个人、家庭和小型团队自托管。它不是 Agent 框架本身——真正的运行时能力由外部的 "Harness stack"（`octop-harness`、`octop-gateway`、`octop-memory`、`octop-browser`）提供，Octop 是这些运行时的 **composition root**：负责装配、生命周期管理、持久化和多入口接入（Web Dashboard、CLI、多平台 IM channel、cron） [1](#0-0) 。所有交互——WebSocket、cron 触发、IM webhook——都汇入同一个 in-process 消息处理管线，无外部消息队列 [2](#0-1) 。

## Core Architecture

依赖方向严格向内：Transport 层（`api/`、`cli/`、`launch.py`）依赖 `infra/` 领域层，`infra/` 依赖 persistence 与 config，领域层绝不反向依赖 transport。

- **Transport**：`src/octop/api/`（FastAPI routers、JWT 认证中间件、WebSocket/SSE 流式、ACP 端点）、`src/octop/cli/`（CLI 命令，含 service/run/plugin 等子命令）。
- **Orchestration**：`OctopServer`（`src/octop/infra/server.py`）是组合根，在 boot 阶段创建 `AppRuntime`，后者统一管理 `AgentManager`、`Gateway`、`CronManager`、`ProactiveCareScheduler`、`UserManager` 等单例 [3](#0-2) 。
- **Domain（`src/octop/infra/`）**：业务逻辑集中在 `agents/`（manager、experts、plugins）、`gateway/`（`GlobalProcessor` 消息管线）、`cron/`、`connectors/`、`skills/`、`knowledge/`、`memory` 相关模块、`auth/`、`users/`、`proactive/` 等子目录 。
- **Persistence**：`infra/db/` 提供 `DBPool` 与 `RepoBundle`（repository 模式），默认 SQLite WAL（`~/.octop/octop.db`），可选 PostgreSQL 控制面。

## Key Modules

- `infra/server.py`：`OctopServer`，唯一的启动序列与单例装配点。
- `infra/agents/manager.py`：`AgentManager`，管理 `HarnessAgent` 生命周期、组装 `HarnessAgentConfig`、provider 配置变更时热重载/重编译 graph [4](#0-3) 。
- `infra/gateway/processor.py`：`GlobalProcessor`，统一入口处理 Web/IM/cron 消息、slash commands、HITL 审批、session 管理。
- `infra/agents/plugins/manager.py` + `seed.py`：`PluginManager`/`PluginRegistry`，插件发现、`plugin.yaml` 校验、内置插件 seeding、加载进 harness registry [5](#0-4) 。
- `infra/agents/experts/`：Expert Library，启动时扫描的 agent 模板目录。
- `infra/db/`：migration 系统（`NNN_description.sql` / `.pg.sql` 双命名约定）与 `RepoBundle`。
- `dashboard/`：React 前端，含 `plugins/toolRenderers/` 动态工具渲染注册机制。
- 外围交付物：`docker/`、`scripts/`、`desktop`（Wails/Go）、`.fpk` 打包——属于易变的实现细节，不影响核心语义。

## Lifecycle

1. `launch.py` → `OctopServer.start()`
2. `PathLayout.ensure_root()`：保证 `~/.octop` 目录结构存在
3. 加载 `config.json` + 环境变量覆盖（`OctopConfig`）
4. 刷新 `ExpertCatalog` / `SubagentCatalog`
5. `PluginManager.load_installed()`：发现 `~/.octop/plugins/`、seed 内置插件、注册进 `PluginRegistry`
6. 打开 DB、运行 migrations（若 setup wizard 未完成则延迟控制面 DB 初始化并发出 wizard password）
7. 构建 `SharedServices`（封装 `RepoBundle`），依次实例化 `AgentManager` → `Gateway` → `CronManager` → `ProactiveCareScheduler` → `UserManager`，组成 `AppRuntime` [6](#0-5)
8. 运行时：入站消息 → FastAPI router → `GlobalProcessor.process_message()` → `AgentManager.get_agent()` → `HarnessAgent.run_task()` → 流式回推 SSE/WS [7](#0-6)

## Extension Points（显式）

- **Plugin**：新增能力优先走 `~/.octop/plugins/` 子目录 + `plugin.yaml`（kind ∈ tool/skill/hook），通过 `/plugins` API 或 CLI 安装，无需改核心代码 [8](#0-7) 。
- **Skill / Expert**：放入 library 目录即可被 boot 扫描收录；agent 级工具开关写在 agent 的 `config_json` 中，由 `AgentManager._build_harness_config()` 合并 [9](#0-8) 。
- **IM channel**：通过 `octop-gateway` 归一化接入 `GlobalProcessor`，新 channel 只需注册 `ChannelKey`，不触碰消息管线。
- **前端 tool renderer**：插件可带 UI，`syncPluginUis()`/`ensureBuiltinToolRenderers()` 动态注入聊天界面渲染组件 [10](#0-9) 。

## Extension Points（隐式）与归属判断

- 新 REST 端点 → `api/routers/`；新业务领域 → `infra/` 下新子目录并接入 `SharedServices`/`AppRuntime`；新 CLI 命令 → `cli/commands/`。
- 新持久化实体 → 必须走 `infra/db/` migration 文件（双后端 `.sql`/`.pg.sql` 命名）+ `RepoBundle` 新增 repository，禁止散落 SQL。

## Constraints / 不变式

- **启动顺序不可违反**：path → config → catalogs → plugins → DB/migrations → SharedServices → managers。任一 manager 不得先于 DB 可用。
- **单例与单进程假设**：所有状态经 `RepoBundle`/`SharedServices` 传递；不允许跨 transport 直接 import 领域对象以外的共享状态。
- **消息统一入口**：IM/web/cron 必须经由 `GlobalProcessor`，禁止在 router 中直接调 agent，否则绕过 slash command、HITL、session 逻辑。
- **Schema 演进只能追加 migration**，历史文件不可修改；双后端脚本须同步。
- **安全不变式**：JWT 多用户隔离、HITL tool 审批、shell guardrails、workspace 边界（`PathLayout` 界定 `~/.octop`）不可被绕过 [11](#0-10) 。
- **异步模型**：进程内 asyncio 流式管线；新增耗时操作必须非阻塞且不引入进程/线程级共享可变状态。

## 高风险修改类型

改动 `OctopServer` boot 顺序、`GlobalProcessor` 管线、DB migration 机制、`AgentManager` 的 config 组装/热重载逻辑、`PathLayout` 目录约定，都会波及所有入口和所有 agent——属于系统性风险。相对安全的是：新增 router、新 channel 适配、新 expert/skill、插件与 dashboard renderer 扩展。

**未完全验证之处**：我未逐文件核对 `infra/` 各子模块（如 `knowledge/`、`voice/`、`bridge/`、`mobile/`）的内部结构，部分判断基于 wiki 文档与目录结构推断；若需针对具体模块做修改，建议进一步阅读对应源码确认接口边界。

### Citations

**File:** README.md (L42-50)
```markdown
**Octop** is an open-source, self-hosted AI assistant. It's not just a tool — it's a digital life form that can operate in parallel. Through its multi-agent architecture, it builds an intelligent environment that is both independent and collaborative for teams, families, and individuals. Best of all, it runs entirely on your machine — the fully self-hosted design means privacy is never a compromise, while single-process startup makes the powerful web console, CLI, and IM integrations readily accessible.

Chat through the Web Dashboard, Feishu, DingTalk, QQ, WeChat, Telegram, Discord, WeCom, or programmatic HTTP/SSE/WebSocket. Extend capabilities with the **expert library**, **Connectors** (OAuth + MCP), and **ACP** integration for IDE workflows.

## ✨ Highlights

| | Feature | Description |
|---|---------|-------------|
| 👥 | **Multi-user expert team** | One admin, shared household; built-in expert library and expert market — switch specialists per scenario |
```

**File:** README.md (L54-55)
```markdown
| 🔒 | **Security built-in** | JWT multi-user isolation, tool approval, shell command guardrails, and PII redaction — data stays local |
| 🔌 | **Connector ecosystem** | Tencent suite (Docs, Meeting, News, …); OAuth and MCP gateway extend resource boundaries |
```

**File:** README.md (L83-85)
```markdown
</details>

## 🧠 Core Technology
```

**File:** src/octop/infra/server.py (L106-195)
```python
            if gz_path.exists():
                gz_path.unlink()


def _parse_log_compress() -> bool:
    raw = (os.environ.get("OCTOP_LOG_COMPRESS", "1") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def delaycompress_rotated_logs(
    log_dir: Path,
    *,
    enabled: bool,
    keep: Path | None = None,
) -> None:
    """Gzip older rotated plains; leave ``keep`` (or the newest) uncompressed.

    Matches logrotate ``compress`` + ``delaycompress``: postpone compression of the
    previous log file until the next rotation cycle
    (https://github.com/logrotate/logrotate).
    """
    if not enabled or not log_dir.is_dir():
        return
    plains = [
        entry
        for entry in log_dir.glob("octop.log.*")
        if entry.is_file() and not entry.name.endswith(".gz")
    ]
    if not plains:
        return
    keep_path = max(plains, key=lambda p: p.stat().st_mtime) if keep is None else keep
    keep_resolved = keep_path.resolve()
    for entry in plains:
        if entry.resolve() == keep_resolved:
            continue
        gzip_rotated_log(entry)


def _parse_log_retention_days() -> int:
    raw = os.environ.get("OCTOP_LOG_RETENTION_DAYS", str(DEFAULT_LOG_RETENTION_DAYS)) or str(
        DEFAULT_LOG_RETENTION_DAYS
    )
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_LOG_RETENTION_DAYS


def _parse_log_max_bytes() -> int:
    raw = os.environ.get("OCTOP_LOG_MAX_BYTES", str(DEFAULT_LOG_MAX_BYTES)) or str(
        DEFAULT_LOG_MAX_BYTES
    )
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_LOG_MAX_BYTES


def _build_log_handler(
    log_path: Path,
    retention_days: int,
    *,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    compress_rotated: bool = True,
) -> SizeTimedRotatingFileHandler:
    """Create a daily + size-capped rotating file handler with retention."""
    handler = SizeTimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        max_bytes=max_bytes,
        compress_rotated=compress_rotated,
    )
    # Rotated files get a date suffix, e.g. octop.log.2026-07-16
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s — %(message)s"))
    return handler


def _purge_stale_logs(log_dir: Path, retention_days: int) -> None:
    """Delete rotated octop log files older than ``retention_days`` (mtime based).

    ``TimedRotatingFileHandler`` only trims by count at rollover time, so a service
    that is offline for a long stretch can accumulate stale files. This purges them
    on startup as a safety net.
    """
    if retention_days <= 0:
        return
```

**File:** src/octop/infra/agents/manager.py (L1-25)
```python
"""AgentManager — process-wide singleton managing all HarnessAgent instances."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from octop_harness import HarnessAgent, HarnessAgentConfig, HarnessAgentManager
from octop_harness.registry import AgentEntry
from octop_harness.security.models import SecurityPolicy

from octop.i18n.domains.agents import (
    AGENT_START_TIMEOUT,
    NO_MODELS_CONFIGURED,
    format_agent_start_error,
```

**File:** src/octop/infra/agents/plugins/manager.py (L62-110)
```python
def _load_plugin_dir(plugin_dir: Path, *, install_deps: bool) -> LoadedPlugin:
    """Load one plugin, accepting the pre-rename ``harness_agent`` import."""
    ensure_legacy_harness_agent_alias()
    return load_plugin_dir(plugin_dir, install_deps=install_deps)


def _read_global_plugins(config_path: Path) -> dict[str, bool]:
    if not config_path.is_file():
        return {}
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    plugins = raw.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    out: dict[str, bool] = {}
    for plugin_id, entry in plugins.items():
        if isinstance(entry, dict):
            out[str(plugin_id)] = bool(entry.get("enabled", True))
        else:
            out[str(plugin_id)] = True
    return out


def _write_global_plugin_enabled(config_path: Path, plugin_id: str, enabled: bool) -> None:
    """Merge ``plugins.<id>.enabled`` into ``config.json`` without dropping other keys.

    A corrupt config.json raises instead of being treated as empty: merging into
    ``{}`` and writing back destroys every other setting, including the
    ``database`` section (issue #730).
    """
    try:
        data = read_json_object(config_path)
    except JsonFileCorruptError as exc:
        raise corrupt_config_error(exc.path, exc.detail) from exc
    if data is None:
        data = {}
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        data["plugins"] = plugins
    entry = plugins.get(plugin_id)
    if not isinstance(entry, dict):
        entry = {}
    entry["enabled"] = bool(enabled)
    plugins[plugin_id] = entry
    write_json_atomic(config_path, data)

```

**File:** src/octop/api/routers/plugins.py (L99-105)
```python
@router.get("", summary="List installed plugins")
async def list_plugins(
    server: OctopServer = Depends(get_server),
    _user: Any = Depends(current_user),
) -> list[dict[str, Any]]:
    return _plugin_manager(server).list_installed()

```

**File:** dashboard/src/plugins/toolRenderers/index.ts (L1-38)
```typescript
export { DefaultToolRenderer } from "./builtin/DefaultToolRenderer";
export {
  builtinPluginHost,
  createPluginUiHost,
  setPluginUiToolContext,
  setPluginUiDockHandlers,
} from "./host";
export {
  loadInstalledPluginUis,
  reloadPluginToolUis,
  resetPluginUiLoader,
  isPluginUiLoaded,
} from "./loader";
export {
  parseOctopToolOutput,
  mergePatchedToolOutput,
  resolvePluginUiData,
} from "./parseToolOutput";
export {
  registerToolRenderer,
  resolveToolRenderer,
  clearToolRenderers,
  unregisterPluginRenderers,
  listRegisteredRenderers,
  subscribeToolRenderers,
  getToolRendererVersion,
} from "./registry";
export { ensureBuiltinToolRenderers } from "./ensureBuiltins";
export { usePluginToolUis } from "./usePluginToolUis";
export { useToolRendererVersion } from "./useToolRendererVersion";
export type {
  OctopPluginUIHost,
  ParsedToolOutput,
  PluginUiModule,
  ToolRenderProps,
  ToolRendererRegistration,
  ToolRenderStatus,
} from "./types";
```
