# Spec Delta

## Purpose

保证企业插件能在已有 Octop 实例上以可追踪的版本进行独立安装、升级、停用和回退，保留用户资料与受管配置，并以真实环境证据决定第一阶段试点是否具备发布条件。

## ADDED Requirements

### Requirement: Paired installable plugin artifacts
Release MUST contain separate tool/hook ZIPs with one root per archive, self-contained built UI, version/digest and Host compatibility metadata. Updating business logic MUST be possible through plugin ZIP replacement without rebuilding an unchanged host contract.

#### Scenario: Pair version mismatch
- **WHEN** installed tool/hook versions are incompatible
- **THEN** bound enterprise calls are denied and the release gate fails

### Requirement: Idempotent legacy migration
Migration MUST be an explicit authorized management operation. The legacy origin marker and new connection MUST commit atomically with an origin uniqueness constraint. Existing source connections and remote documents MUST remain unchanged.

#### Scenario: Concurrent repeat migration
- **WHEN** two workers migrate the same origin concurrently
- **THEN** exactly one new connection exists and the second observes the committed mapping

### Requirement: Retained data and recoverable rollback
Disable/uninstall MUST retain plugin records and user data. Upgrade MUST save a managed T0 backup and unknown-data digests; rollback MUST restore compatible paired plugin versions and retain backups for at least seven days and until recovery validation passes.

#### Scenario: Recovery fails
- **WHEN** rollback cannot restore the verified T0 state
- **THEN** enterprise calls remain disabled and recovery manifests/backups are preserved

### Requirement: Evidence required before release
Alpha.1 MUST require A-01 through A-07 and plugin-boundary evidence, including real remote versions/permissions, both search sources, supported PostgreSQL constraints, Windows workflows and the annotated 20-question set. FAIL, SKIPPED, BLOCKED or absent required evidence MUST make the gate exit nonzero.

#### Scenario: Only mock tests available
- **WHEN** local tests pass but remote or Windows evidence is absent
- **THEN** the build may be reviewed locally but the Alpha.1 release gate fails

#### Scenario: Quality set
- **WHEN** the 20-question set is evaluated
- **THEN** at least 15 of 17 answerable questions cite matching evidence and all three unanswerable questions have zero fabricated citations
