# Spec Delta

## Purpose

让系统管理员和有管理权限的专家配置者在现有 Octop 页面挂载的插件设置区完成知识连接、员工授权、专家绑定及搜索源配置，明确区分已保存配置、分项探测结果和真实业务可用状态。

## ADDED Requirements

### Requirement: Equivalent context preserves settings drafts
Settings bootstrap MUST depend on semantic scope rather than the identity of a Host snapshot object. Equivalent snapshots and theme/timezone/readiness rerenders MUST NOT refetch settings or reset edited fields; a real generation or agent change MUST still reload and reject stale results.

#### Scenario: Host rerenders during search configuration editing
- **WHEN** a user edits a search source and Host supplies an equivalent new context object
- **THEN** the draft is preserved and no duplicate search.get request is sent

### Requirement: Fixed plugin-owned settings entry
Enterprise forms SHALL render inside fixed settings surfaces on existing connector/expert pages. Business components MUST be loaded from the plugin; the host MUST NOT add arbitrary plugin pages or navigation. UI capabilities MUST come from authenticated server responses.

#### Scenario: Ordinary employee opens management surface
- **WHEN** a user without management permission reaches the settings entry
- **THEN** administrative inputs and actions are unavailable and no secrets or unauthorized metadata are fetched

### Requirement: Management roles are separated
The system MUST restrict connection, employee grants and search administration to admins. A non-admin expert configurator MUST have existing agent_plugins management permission and own the target expert to read/update its binding/defaults/web allowance. Other employees MUST receive no management bootstrap data or management inputs.

#### Scenario: Expert owner lacks global administration
- **WHEN** a permitted owner opens their expert settings
- **THEN** only their sanitized binding/default controls appear; connection credentials, employee grants and search administration are hidden and their data is not requested

### Requirement: Settings actions are explicit
The UI SHALL load authorized settings through settings.get/binding.get/search.get, save through connection.save/grant.save/binding.save/search.save with expected_revision, and probe through connection.probe/search.test. Catalog reads MUST be authorized; responses SHALL include current revision and sanitized status sufficient to update only the matching form.

#### Scenario: Save then probe
- **WHEN** connection.save succeeds and a probe is started
- **THEN** the UI records the returned configuration revision and applies probe results only when observed_revision matches it

### Requirement: Explicit save and separate probes
Connection saving MUST submit a revision and write-only credential input, show save progress/errors, and clear entered keys on success. LIST/SEARCH/READ probes MUST be distinct from saving and display observed revision/time. Stale probe responses MUST NOT overwrite newer configuration state.

#### Scenario: Concurrent configuration conflict
- **WHEN** another administrator saves before the current form submits
- **THEN** the UI reports a conflict and offers reload/discard without silently overwriting either edit

#### Scenario: Saved but unprobed
- **WHEN** a connection is saved without successful current-revision probes
- **THEN** the UI displays pending validation rather than READY

### Requirement: Empty default grants and bounded expert defaults
New connection/employee/expert allowed sets MUST default to empty and require explicit selection. Expert defaults MUST remain within allowed IDs; changing grants MUST not silently broaden defaults or employee selections. The UI MUST show blocked reasons for empty or invalid scope.

#### Scenario: Employee grant removed
- **WHEN** an administrator removes a knowledge base from an employee grant
- **THEN** subsequent selection/access responses exclude that resource and stale forms cannot restore access

### Requirement: Primary-secondary search configuration state
Search settings MUST distinguish fixed primary/secondary roles, masked existing credentials, current configuration revision and per-source test status. Web availability MUST require both valid sources and current permissions; failures MUST not silently change a session's mode.

#### Scenario: Only one configured source
- **WHEN** the administrator has configured only the primary source
- **THEN** web capability remains unavailable with an actionable missing-secondary explanation

### Requirement: Accessible localized state feedback
Applicable loading, empty, validation, success, denied, disabled, error and revision-conflict states MUST be visible and keyboard reachable. Business copy MUST support en/zh; timestamps MUST use server timezone and entered secrets MUST NOT persist in browser storage.

#### Scenario: Keyboard save error
- **WHEN** a keyboard user submits invalid fields
- **THEN** focus moves to the first invalid field and a localized field error is announced without clearing safe edits
