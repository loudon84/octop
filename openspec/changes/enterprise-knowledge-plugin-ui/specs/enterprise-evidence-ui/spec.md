# Spec Delta

## Purpose

让员工能从专家答案中的真实证据卡片核对来源，在受权限约束的侧栏连续阅读检索文本并打开可用原文；历史查看与实时结果保持一致，明确呈现截断、无页码、无原文和授权失效等状态。

## ADDED Requirements

### Requirement: Server-backed evidence cards
Cards SHALL display only server-registered evidence IDs and actual source/title/time/truncation metadata. Fabricated IDs or free-text URLs MUST NOT appear as trusted citations. Knowledge and web sources MUST remain distinguishable and provider fallback must show actual source.

#### Scenario: Model fabricates an evidence reference
- **WHEN** an answer contains an ID absent from registered evidence
- **THEN** the UI does not render a trusted citation card for it

### Requirement: Retrieved-text side panel
Opening a knowledge citation SHALL use the existing tool side panel and label content as retrieved text. Reading and continuation MUST use authenticated plugin actions; missing page numbers MUST NOT become invented pages and missing originals MUST be explicitly unavailable.

#### Scenario: Long text continuation
- **WHEN** the server returns truncated content with a continuation
- **THEN** the panel offers load-more, preserves text order and shows progress/error without silently dropping text

#### Scenario: Access revoked after card shown
- **WHEN** current authorization rejects an open/read/continue action
- **THEN** new content is not loaded and a localized access-change message is shown

### Requirement: Controlled original access
Original-file actions MUST use opaque authenticated host resource references. Browser state, HTML and downloads MUST NOT expose provider keys or private signed source URLs; source revision changes MUST invalidate unreliable continuation state.

#### Scenario: Source changed
- **WHEN** continuation reports that source revision changed
- **THEN** the panel stops appending and offers an explicit restart of reading

### Requirement: History and accessible reader states
History reopen SHALL resolve retained server evidence within normal conversation permissions. Reader/card loading, empty, failure, denied and unavailable states MUST support en/zh and keyboard navigation, return focus on close, and format times using server timezone.

#### Scenario: History citation reopened
- **WHEN** an authorized user reopens a past tool result
- **THEN** source metadata comes from retained evidence and any new read is reauthorized before fetching

### Requirement: Evidence actions follow authenticated context
History and current evidence MUST load through evidence.list using version 1 and an operation_id. Text and continuation SHALL use evidence.read; original availability SHALL use evidence.original with operation_id and evidence_id. Actions MUST carry current agent/thread context, never a browser-chosen provider URL. Available originals SHALL use only opaque authenticated host resources.

#### Scenario: A retained operation is reopened
- **WHEN** a real current or historical tool result supplies an operation reference
- **THEN** the UI loads its evidence through evidence.list under current authorization before treating registered IDs as citations; a model-written ID alone does not create a trusted card

#### Scenario: Original action returns an opaque resource
- **WHEN** evidence.original confirms an authorized original is available
- **THEN** the UI requests that host resource with authentication and does not navigate to a private supplier URL
