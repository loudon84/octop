# 插件开通本地配置合同 — I03a2

状态：I03a2 本地实现经 Fix2 独立 Spec/Quality PASS，原 R1/R2/R3 与新增 F1 全部关闭；实际八路径及各轮 PRE-EDIT 基线、报告、审查保留于计划 scratch。仅验收三个本地管理动作，不完成整体3.1/1.2。用户已授权持续实施，功能/测试只由 GPT-6 Luna 完成，无提交/推送/部署。固定权威：user-provisioning-contract.md 第八/九/十节及本合同；原 task-I03-config-contract-seam.md 为只读事实/提案来源，由根控制器取舍后采用以下精确合同。

根控制器技术裁决：每次 preparation 独立且保留引用，以实际 config/anchor/自己 prep CAS 保护原子激活。无需保证消灭所有永不可 resolve 的 staged 垃圾；未知状态不凭 TTL/resolve-notfound 假清理，也不永久阻塞新 save。代价为 owner 生命周期内保留少量配置操作元数据；不增加 GC 平台。active 密钥必须同事务被 config 引用并具有 COMMITTED preparation。以下历史 JWT 回放/当前损坏凭据边界必须如实保留，不把新 ref 冒称从未使用过的历史材料。

## 1. 唯一批次范围与前置门

I03a2 仅实现真实 `provisioning.config.get`、`provisioning.config.save`、`provisioning.retained.list`。不注册 status/retry/tasks.list/task.retry/reconcile，不写空处理器，不 seed 员工 task，不注册 worker。那些动作等待 a3 的实际 task/state/UUID 索引合同。

前置：I02b/c/d/e 实际验收完成；I03a1 十一 DTO 的两路径源码落盘并验收。a1 Fix1 已实际独立验收（23项定向与静态通过），a2 复用实际 DTO，不复制绕过。a2 完成仍不勾选完整 3.1、1.2、24h bootstrap、远端开通、provider 或 worker。

选定最小结构：插件内部 `ProvisioningService`，直接使用现有 Host facade；无新通用 core API、迁移、requirements、队列、GC API、设置扩展或 HTTP router。普通审批配置保持可用；pending rotation 的 staged 凭据从不供业务 resolve 使用。

## 2. 统一类型规则与存储键

以下内部 payload 均为严格模型：`schema_id="octop.enterprise.provisioning.v1"`、严格 `version=1`；extra forbid/frozen/strict，整数拒绝 bool/float/string。UUID 是 canonical UUIDv4 字符串，时间沿 a1 UTC 字符串规则。Host envelope revision 是唯一 CAS 计数，payload 不复制记录自身 revision。下表列出的字段是完整 payload，除统一 schema/version 外无隐含字段。owner 只在普通 HostRecord envelope，客户端不能提供。

| namespace / logical key | 完整 payload（除 schema/version） | owner 与生命周期 |
|---|---|---|
| retained `provision_domains` / `primary` | `domain_id:UUIDv4, canonical_base_url:normalized URL` | 无 FK，永久；首次成功 config commit 创建一次，之后只检查，不更新 |
| ordinary `provision_config` / `primary` | `domain_id:UUIDv4, name:trimmed str[1..80], base_url:normalized URL, enabled:StrictBool, admin_credential_ref:UUIDv4, admin_credential_revision:Literal[2], updated_at:UTC` | 首次成功配置的实际 admin；更新永不改 owner；删除该 owner 时按 Host 原语义级联 |
| ordinary `provision_auth_blocks` / `<credential UUID>` | `domain_id:UUIDv4, credential_revision:Literal[2], http_status:Literal[401,403], rejected_at:UTC` | 与该配置/凭据相同 owner；key 本身就是精确 ref，存在即禁止该 lineage，无 clear/disabled 字段 |
| ordinary `provision_config_preparations` / `<operation UUID>` | `domain_id:UUIDv4, expected_config_revision:StrictInt>=0, expected_anchor_revision:StrictInt>=0, previous_credential_ref:UUIDv4|null, previous_credential_revision:Literal[2]|null, planned_credential_ref:UUIDv4, planned_active_revision:Literal[2], state:PREPARING|COMMITTED|ABORTED, staged_cleanup:NOT_REQUIRED|PENDING|RELEASED|UNRESOLVED, created_at:UTC, updated_at:UTC` | 与准备时的实际 config owner 相同；缺配置时为实际请求 admin；每次独立 UUID，引用永不擦除，不被下一次 save 覆盖 |

准备记录中 previous_ref/revision 同为 null 或同非 null；expected_config_revision=0 时必须同为 null。planned ref 必须区别于 previous ref。PREPARING 必须 PENDING；COMMITTED 必须 NOT_REQUIRED；ABORTED 只能 RELEASED 或 UNRESOLVED。每次准备记录允许自身 CAS 转终态，但不能改变 operation key、domain、expected revisions 或任何 credential ref/revision。没有 TTL、复用 ID 或全表恢复扫描。

encrypted credential payload 精确为 `{ "schema_id":"octop.enterprise.provisioning.v1", "version":1, "system_admin_jwt": <exact nonempty string> }`。JWT 沿 a1 不接受首尾空白，不解析/验证签名，不保存 token 指纹/摘要、token 原文副本、原始响应或 reason。仅 Host encrypted credential 承载秘密；record/retained/日志/错误/公开 DTO 均不含秘密。

stage 使用服务端预分配 `planned_credential_ref`：`await facade.stage_credential(owner_user_id, payload, credential_id=planned_ref)`；实际 stage revision=1，`CredentialActivation(planned_ref, owner, 1)` 使 active revision=2。所有本子系统 ref 永不重新 stage/activate/reuse，因而持久 expected active revision 固定为 2。`resolve_credential(ref, owner)` 返回解密 payload，不是 credential row，没有 metadata 或 expected_revision 参数。禁止凭空访问 `.revision`、构造 `resolve(..., expected_revision=2)` 或直接调用 repo 绕过 facade。

## 3. 所有权、锚点与权限语义

1. 一个配置 key、一个 retained domain anchor。首次 config 保存与 anchor 创建同事务，二者 expected revision 都是 0；准备 staging 不提前建立 anchor。两个首次请求只能一个赢得 anchor/config CAS。
2. 后续 base_url normalize 后必须与 anchor.canonical_base_url 精确相同，config.domain_id 必须等于 anchor.domain_id。重建只复用既存 anchor；不生成第二 domain、按配置名或新 JWT 改 domain。URL 仅是文本身份，不识别 DNS 别名或同 URL 换服务器；换服务器/迁移留后续。
3. 更新 owner 永远取当前 `HostRecord.owner_user_id`，即使操作 admin 是另一个人，也不能以 actor 为新密钥 owner。配置原 owner 后来降权，已批准配置继续有效；新请求 actor 仍须 fresh admin。原 owner disabled 的快照可用于存在性/CAS，不能把 disabled 或降权解释为“以 owner 模拟 admin”。请求 actor 自己必须通过 fresh-admin 检查。
4. 只有实际 owner 删除使普通 config/credential/preparation/auth-block 级联消失后，另一 fresh admin 可以 expected_config_revision=0 按旧 anchor 重建并成为新 config owner。retained anchor/intent/receipt/claim 不消失。
5. 所有三个服务入口都 `await facade.reauthorize_actor(actor, "admin")`，实际 action actor 原样传入，仅返回的新 actor 用于权限决策。get/list 在完整读取后、交付 DTO 前再次复核。save 在准备提交前与最终批准提交前分别紧邻复核，不能沿用入口的 is_admin。清理自己已登记的 staging 是失败收尾，不需要降权用户再获得 admin 权限；它仍受 generation、精确准备 CAS 与既有 Host owner 限制。
6. save 准备与最终提交都读取当前实际 config owner 的 `PluginUserIdentity`；若 actor 不同，额外读取 actor identity；传完整 `UserIdentityCheck`，按 user_id 去重。含 retained checks/writes 的每个普通 write、activation、deletion owner 都有覆盖。不可仅覆盖轮换 admin 而遗漏原 config owner。缺 owner 不转移所有权，也不伪造 identity。
7. fresh admin 检查和 identity check 不持有角色行锁到整个后续事务；接受 §9 的“紧邻提交时 fresh 检查”边界，不声称绝对阻止其后瞬时角色竞态。

## 4. 凭据内容相同、缺失与 auth quarantine

`system_admin_jwt=null` 表示保留当前凭据。非 null 在已有正常配置时，先以实际 config owner resolve 当前 active payload，严格解析后，以内存中的 UTF-8 字节做 `secrets.compare_digest` 比较（不日志、不持久摘要）。相同字节视为保留，绝不新建 ref；不同字节才生成新 ref。这是必需行为，不能仅凭“用户显式填了字段”认定轮换。

- 仅改 name/enabled、null JWT、显式相同 JWT：沿用原 ref/revision；现存 auth block 原封不动。增长 config revision、禁用再启用、任务重试都不解除 quarantine。
- 当前 ref 的 block 存在且 domain/revision/owner 匹配时，表示本地保存过确认 401/403。a2 可以保存显示名/启停；其公开 `credential_configured` 仍只表示 active 密文存在，绝不表示远端认证通过。后续实际 worker 必须阻止此 lineage 的全部员工请求和自助重试。
- 真正不同的当前 JWT：新 ref，旧 block 保留；新 ref 的 block 不能存在（fresh unique ID + RecordCheck=0），因此可成为新的未验证 lineage。旧拒绝的迟到写必须只写旧 ref 的 block，不能覆盖或误封新 ref。
- 首次/owner 删除后的合法重建必须提供非 null JWT，且 enabled=false 也不能省略；本批不支持“无凭据的半配置”。
- 既存 config 指向的 active credential 明确缺失时，get 可返回 `credential_configured=false`；save fail closed 为 PROVISIONING_UNAVAILABLE，包括显式 JWT。这是正常原子生命周期之外的损坏/外部变更，不通过猜测旧 material 来绕过拒绝 lineage。后续如需独立 repair action 须另冻合同。DB unavailable、decrypt/结构错误不能当缺失。
- 不承诺识别历史 A→B→A token 回放：旧 active 密文在轮换同事务删除，合同禁止持久指纹，故只能比较“当前”凭据。也不承诺跨原 owner 删除后的历史内容相等识别；删除后无秘密留存。若未来产品要求阻止所有历史 token 内容回用，需单独设计，不能把 ref 唯一性描述为全历史 material 唯一性。

Auth-block 写入属于后续真实 HTTP producer，不注册本批假 HTTP/rejection handler。精确后续规则已在此约束：已确认的 401/403 携带尝试时 pin 的 domain/ref/active revision/实际 owner；key=该 ref，缺行→CAS0，已有相同 tuple 幂等保留首次 rejected_at，字段不匹配报安全 unavailable；永不写 primary singleton。a2 实现严格 block 读取/校验以及 config 保存不清除的逻辑，测试用真实 Host commits 建立拒绝 fixture，不伪造已发生远端验证。

## 5. save 精确事务与并发合同

### 5.1 无新 material（null 或显式相同）

先 fresh-admin、严格读取 anchor/config/block、校验请求 expected revisions 和 URL，验证 active credential；无准备记录、无 staging。最终 fresh-admin 与当前 owner/actor identities 后，单 commit 写 config 的 name/base_url/enabled/updated_at，保留 domain/ref/revision/owner；检查当前 config expected revision（write CAS）、anchor revision（retained check）、当前 ref block revision（RecordCheck，缺行=0）。任何冲突整笔回滚并返回冲突，不自动把 expected_revision 改成新值重放。

### 5.2 首次/实际轮换

A. 入口上述校验、当前 JWT 内容比较都成功后，分配 operation UUID 与独立 planned credential UUID。读取对应新准备 key，必须缺失；准备记录 PREPARING/PENDING。再次 fresh admin、读取 owner/actor identities，commit 仅写该准备记录 CAS0；附 `RecordCheck(provision_config,primary,request.expected_revision)`、retained anchor check（首次缺 anchor 为0）、旧 ref block check（若有）与全部 owner identity_checks。首次 domain UUID 此时仅普通准备元数据，尚未获得 retained 身份域地位。

B. 准备成功后才 stage，传实际 config owner。不得先 stage 再试图补记录。stage 返回仍只是 staged、不可 resolve，不执行探测/网络。

C. stage 成功后最终 fresh admin、读取当前 identities。严格读取/确认自己的 preparation 仍 PREPARING 且 expected revision 是持有值；不修改其初始 expected config/anchor。一个 commit 同时包含：

- config `RecordWrite(primary, actual_owner, new_payload, request.expected_revision)`；
- 自己 preparation 的 `RecordWrite(..., state=COMMITTED, staged_cleanup=NOT_REQUIRED, expected_revision=held_prepare_revision)`；
- `CredentialActivation(planned_ref, actual_owner, expected_revision=1)`；
- 有旧 active ref 时 `CredentialDeletion(previous_ref, actual_owner, expected_revision=2)`；
- 首次无 anchor 时 `RetainedRecordWrite(provision_domains,primary,domain_payload,0)`，否则现有 anchor 的 `RetainedRecordCheck`；已有普通 config 必须引用该 anchor；
- block checks：当前旧 ref block revision（若有），planned ref block revision=0；
- 所有普通/credential owner 的当前 identity_checks，若 actor 不同亦有 actor check。`requirements=()`。

Config write 自身就是 config CAS，prep write 自身就是 prep CAS，不另造“事务后激活”。若使用同键额外 check，必须与 write expected revision 一致。Host commit 返回 ordinary HostRecord 列表；retained 不在返回中，输出 anchor_revision 在首次成功为1，其他沿已验证 anchor；需要再读取时走真实 get，不猜返回 mixed DTO。

成功后 active revision=2 的新 ref 被同事务 config 引用，旧 active 删除，自己的准备记录已 COMMITTED。任何失败则四者全部不生效，旧批准 config+旧 active 仍然存在。不能采用 activate→另存 config→再删旧 active 的三提交模式。

两个请求基于同一 config revision 都可登记各自 preparation 并 stage；最终只有一个 config CAS 可成功。落败准备仍有精确 ref、其 staged 不可 resolve；不把它提升为另一配置，也不扫描其他请求准备。旧请求不能修改自己的 expected revision 后抢新 config。

## 6. 失败、取消与崩溃：跟踪保证，不扩成 GC 平台

每次独立 preparation 是持久追踪元数据，没有 singleton 未决门闩。未知结果/遗留 staged 不阻塞下一个 admin save；新请求仍使用实际 config/anchor CAS，自己的独立 preparation CAS。所有 ref 永不清空，即使标 ABORTED/RELEASED。

| 时间窗口 | 必须保证的行为 |
|---|---|
| 准备 commit 未知/失败，尚未 stage | 不开始 stage；可能存在的 PREPARING 不含秘密且保留 planned ref；新 save 独立推进 |
| stage 明确失败或已完成后 final commit 明确失败 | 以自己准备 CAS 将 PREPARING→ABORTED/UNRESOLVED，再尽力 `release_staged_credential(planned_ref, actual_owner)`；成功 true 且该 stage 已确定 settled 时 CAS 标 RELEASED。失败/false/无法证明 settled 保持 UNRESOLVED |
| cancel 发生在 stage await | 不把取消 awaiter 当底层执行已停止；本地保留实际 task handle，shield 并等实际 operation settle，再做上述精确收尾。收尾又被取消/Host generation 不可用时持久 PREPARING/PENDING 已足以保留引用，交还取消 |
| cancel/错误发生在 final commit await | 本地能等待实际 commit settle 就等待；不能在未知 COMMIT 后盲删 new active。重读自己 preparation：COMMITTED 表明原子提交曾成功，不能改 ABORTED；PREPARING 才能以原 revision CAS 抢占 ABORTED。该 CAS 与 final commit 同一 prep CAS 竞争，只有一个胜；final 已成功则 abort 必失败 |
| abort 赢了但旧 stage 仍在其他进程执行 | 它之后至多产生 staged；final commit 的 prep CAS/状态已不允许激活。不得声称 release 已证明将来不会再插入；保持 ref 和 UNRESOLVED，无 active 泄漏 |
| process crash | 已提交 config+COMMITTED 的 active 有 config 引用；未提交的只有 durable preparation 与可能 staged。后续新 save 不恢复/重放旧请求，不扫描准备全表，不因 age/TTL 假定旧 operation 已死 |
| owner 删除 | 按原 FK 删除该 owner 普通记录/凭据；retained anchor 留存。旧 operation 的 identity/prep/config/anchor前提阻止恢复该 owner 或误激活另一 owner 下的 config |

`release_staged_credential` 不是 active 删除 API；false 也不是“确定清理成功”。`resolve_credential` not_found 不能区分尚未 stage、staged 不可 resolve 或晚到 stage；禁止据此清空元数据。若 final commit 结果未知而后续另一合法轮换已经替换它，原 COMMITTED preparation 仍提供历史追踪，新旧密钥删除已由各自 config 原子提交完成。

本批**不承诺消灭所有 staged 垃圾、不加定时清理、准备全表扫描、任意 ref 的管理端删除接口或新的平台恢复服务**。后续如需恢复，只按已知 operation/ref 定点、遵守 preparation/config CAS 与实际 lifecycle drain 证据，另冻小批；未知 staged 永远不具备授权能力。对生产源代码的本地取消实现要求是有 actual-operation settlement 证据，不能仅用 mock CancelledError 推断 DB 没提交。

旧 config 在新 PREPARING 期间继续代表已批准配置；它若已有 auth block 或 enabled=false 则仍不能外呼。不能因 pending rotate 临时把旧 config disabled，也不能在 new commit 前使用新 JWT。成功 rotation 后后续 producer 使用新精确 config revision；迟到旧请求不得授权提交（属 a3/b 的 pinned config 规则）。

## 7. 精确服务签名、公开投影与三 action 接线

```python
class ProvisioningService:
    def __init__(self, facade: Any) -> None: ...  # 与既有插件边界一致；不新造核心 Protocol

    async def get_config(self, actor: PluginActor) -> ProvisioningConfig: ...

    async def save_config(
        self, actor: PluginActor, request: ProvisioningConfigSaveInput
    ) -> ProvisioningConfig: ...

    async def list_retained(
        self, actor: PluginActor, request: ProvisioningRetainedListInput
    ) -> ProvisioningRetainedListOutput: ...


def register_provisioning_actions(facade: Any) -> tuple[ActionSpec, ...]: ...
```

上面是本地已实现的真实方法签名，不授权落盘 `...`/pass stub。内部 record 模型建议确切类名 `ProvisioningDomainRecord`、`ProvisioningConfigRecord`、`ProvisioningAuthBlockRecord`、`ProvisioningConfigPreparationRecord`，不导出为 HTTP DTO。可用私有 typed helper 组织实际逻辑，不增加 worker/provider 服务接口或未来空方法。

| action | input | output | permission |
|---|---|---|---|
| provisioning.config.get | 既有 EmptyInput | a1 ProvisioningConfig | admin |
| provisioning.config.save | a1 ProvisioningConfigSaveInput | a1 ProvisioningConfig | admin |
| provisioning.retained.list | a1 ProvisioningRetainedListInput | a1 ProvisioningRetainedListOutput | admin |

`provisioning_actions.py` 复用既有 `actions.ActionSpec`；三个 async handler 闭包委托一个真实 ProvisioningService。`main.py` 的实际 register_host_setup 回调登记 `register_user_identity_consumer()`，将新增三个 action tuple 与既有 register_actions 返回值逐个 `register_action`。不改既有 KnowledgeService v1、settings.get、connection/binding DTO、tools、共享连接流程，也不额外 register_service 假装新公共版本。不改 actions.py 本体，不加 worker。

config.get：anchor 与 config 严格校验；确实都缺失时 revision=0/anchor_revision=0/name=null/base_url=null/enabled=false/credential_configured=false/updated_at=null。只有 anchor 存在而 config 缺失时 revision=0，anchor_revision=实际值，base_url=该 canonical_base_url，其他默认不变，使新 admin 可按原域重建。config 存在时必须有匹配 anchor，返回仅 a1 字段；credential_configured 是能 resolve 且 payload 结构合法的当前 active material 是否存在，不是远端有效性。普通记录/secret/ref/domain_id/owner/block原因/preparation 均不输出。错误结构/版本安全 unavailable，不能当 revision0。

读取不创建 config、anchor、task、preparation；不存在 task 不用假状态填充。get/list 不要求原 config owner 仍为 admin，也不拿它冒充请求 actor。

## 8. retained.list 的有界读取与安全 projection 子合同

a2 是真实 reader，a3 producer 尚未存在，故必须冻结**供未来 producer 遵守的最小 header**，不能把任意 payload 暂时原样返回。无需在 a2 写入意图、回执或定义完整 task/resource record。

固定读取 `list_retained_records("provision_intents", "", after_key=request.after_key, limit=request.limit)` 一次；after_key 只接受 a1 canonical UUIDv4。每项 key 必须 canonical UUIDv4，等于 public intent_id。每页最多 request.limit 项，每项至多一次 `get_retained_record("provision_receipts", receipt_id)`。不扫 identities、ordinary tasks 或所有 receipts，不做 union，不补满过滤后的页，不按远端 ID 查询。page.next_logical_key 原样验证后投为 next_key；不另排序，承认分页跨提交非一致快照。

Intent header 的必需精确字段：`schema_id/version, domain_id:UUIDv4, local_user_id:StrictInt>0, resource_kind:USER|KB|KEY, phase:PREPARED|WRITE_PENDING|UNKNOWN|VERIFIED|REJECTED, receipt_id:UUIDv4|null`。没有重复 payload intent_id，使用 record.logical_key。内部意图剩余字段（如 operation/marker/task/proof refs）由 a3 冻结；a2 只选择这七类具名字段构建严格 header，既不接受它们为公共字段，也不假装验证完整未来 payload。缺必需字段或未知版本/非法值安全 unavailable，不能把旧格式当空页。

Receipt header 必需精确字段：`schema_id/version, intent_id:UUIDv4, domain_id:UUIDv4, local_user_id:StrictInt>0, resource_kind:USER|KB|KEY, proof_kind:CREATED_201|ADMIN_ATTESTED|OWNER_MATCH`。receipt logical key 等于 intent.receipt_id；其 intent/domain/local_user/resource_kind 必须逐一匹配。后续完整 receipt 可带资源 ID/非授权结果，但 a2 从不复制这些数据；header proof 只表达保存的证明类型，绝不表达当前 READY 或访问权。

输出每项仅：intent key、local_user_id、resource_kind、phase、proof_kind、has_receipt、review_required、intent envelope revision，再加 a1 schema/version。proof_kind 只从匹配且有效的 receipt header 取得。receipt_id=null 或指定 receipt 确实缺失：has_receipt=false/proof_kind=null；有指定但 malformed/绑定不匹配：整请求安全 unavailable，不假称 has_receipt=false。`review_required = phase in {PREPARED, WRITE_PENDING, UNKNOWN} or (phase == VERIFIED and not has_receipt)`；REJECTED 表示保存的明确未执行/拒绝终态，不凭它重新创建资源；VERIFIED+receipt 也不推导 READY。

删除员工后仍用 retained header 的 opaque local_user_id，绝不恢复 username/email，不读已删员工身份、不创建普通行。所有 output 通过显式字段构造，不使用 payload spread/model_dump 任意映射。任意 reason、JWT/Key、remote IDs、tenant/KB/user mapping、marker、receipt ID 本身均不输出。即使 malicious/legacy payload 在未选择字段中含这些值，也不能影响已选择 header 或泄出。

## 9. 安全错误与国际化

新增插件 errors：PROVISIONING_INVALID(400)、PROVISIONING_CONFLICT(409)、PROVISIONING_UNAVAILABLE(409)；已有 ACCESS_DENIED(403) 可复用。Host conflict→CONFLICT，denied→ACCESS_DENIED；真正 configuration validation→INVALID；持久 payload坏/DB unavailable/generation不可用/无法判断 credential状态→UNAVAILABLE。缺配置 GET 是安全空配置；缺当前 active 在 GET 仅 configured=false；其他 unexpected not_found 不把损坏伪装首次配置。失败清理不泄露 ref/owner/exception，失败/取消场景以准备记录留精确证据。

严格输入由真实 Host ingress验证；输入 exception/SecretStr.get_secret_value/序列化错误不记录。服务自己捕获内部模型错误并转换固定本地化错误，不回显 pydantic 原始 payload、upstream body 或 JWT。action summary 用插件 locales。建议新增 keys：actions.provisioning.config.get/save、actions.provisioning.retained.list、errors.PROVISIONING_INVALID/CONFLICT/UNAVAILABLE，各有 en/zh；不增 backend core ErrorCode。

## 10. 最小未来实现白名单与证据矩阵

源白名单 6 路径：

1. 新 `plugins/enterprise-knowledge/enterprise_knowledge/provisioning_records.py`：上面四种严格内部 records 与只读 retained headers。
2. 新 `plugins/enterprise-knowledge/enterprise_knowledge/provisioning.py`：三个真实 service 方法及其所需局部 helpers。
3. 新 `plugins/enterprise-knowledge/enterprise_knowledge/provisioning_actions.py`：三 ActionSpec/handlers。
4. 改 `plugins/enterprise-knowledge/main.py`：真实 setup身份consumer与新增三 actions接线。
5. 改 `plugins/enterprise-knowledge/enterprise_knowledge/locales/en.json`。
6. 改 `plugins/enterprise-knowledge/enterprise_knowledge/locales/zh.json`。

测试白名单 2 路径：新 `tests/unit/plugins/test_enterprise_provisioning_config.py`、`tests/unit/plugins/test_enterprise_provisioning_actions.py`。复用实际 Host/SQLite fixtures，不另加 fake repo/临时核心能力；a1公开模型源码与其已有测试只读依赖。若真实测试 helper 不足，root 先收敛需要的具体改动，不默许越过白名单。

必测且真正可证的行为：

- 实际 setup 只多三个正确权限/typed DTO actions，identity consumer实际登记；不存在 status/retry/tasks/reconcile/worker占位；旧 actions/KnowledgeService输出兼容。
- 真实 ingress 缓存 admin已降权/disabled拒绝，入口后与最终提交前降权拒绝；get/list返回前降权拒绝；非admin不能通过直接服务调用绕过；密文和invalid参数不回显。
- 首次 anchor/config原子创建；canonical文本等价接受、另一 URL拒绝；并发首次唯一成功；owner删除后 retained anchor继续，新owner只能原URL重建；更新另一个freshadmin不转移原owner。
- 真实Host mixed提交涵盖实际configowner及actor；另一admin轮换stage/resolve/activate/delete始终用configowner；owner降权不使既有config失效，owner删除/identity变化使不满足前提的commit整笔回滚。
- stage1→active2实际行为；resolve只得payload；current ref neverreuse；null、sameJWT、name-only、disable/enable均保持block lineage；不同JWT新ref，旧active原子删除；旧block迟到不盖掉新ref；缺失/坏密文严格处理；公开configured不假称auth通过。
- 实际 SQLite 并发两个 save 的 prep 均可存在，最终只一 config winner；loser没有新active；准备记录有明确ref；旧expectedrevision绝不重试成新版本。
- 在prepare、stage、最终commit每个边界注入明确失败、actual-operation取消与commit后结果丢失；检查DB最终状态，而非仅mock调用顺序。任何新active必须有同事务config引用与COMMITTED prep，旧active只在成功替换时删。abort CAS与finalcommit竞争只能一方胜。
- crash形状用持久fixture/rebind模拟（明确不是OS断电证明）：PREPARING+staged残留可追踪、不可resolve、不阻塞新保存；COMMITTED+config完整；release false不标RELEASED；已知late-stage后abort不激活。真实生命周期取消/drain证据复用已验收Host接口，不冒称全部跨进程staged已经清理。
- retained固定单页、最多N receipts、恶意cursor拒绝、未知schema/坏header/错绑定安全失败；员工/configowner删除后线索仍可读；精确输出无email/remoteIDs/secrets/reason；缺receipt为false；未选字段不扩散；返回前fresh admin。
- 无网络调用，故不证明401/403分类正确、JWT有效、实际远端账号/24h清理/worker恢复；quarantine测试是持久fixture与保存语义证据。

未来执行验证：定向 `uv run pytest tests/unit/plugins/test_enterprise_provisioning_config.py tests/unit/plugins/test_enterprise_provisioning_actions.py -q`，相关静态检查及root既定完整 `make all`。本报告没有运行或宣称任何测试通过；I02d真实PG并发验收是前置，不能用a2 SQLite测试替代。源白名单若未变，不额外增加新的PG平台测试。
