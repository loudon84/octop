# P09c1 Tool Host 展示上下文实时更新合同

状态：**独立 Spec PASS / Builder PASS，root已完整核对**。规划审查记录为 task-P09c1-tool-context-freshness-plan-review.md。本合同不代表实施完成；精确编辑前基线、唯一功能作者可用及READY brief后才能派发。真实浏览器验收仍由root推进。

目标：同一个有效 Tool Host 在语言/主题变化后返回最新 locale/theme，已注册 renderer 获得实际 rerender；原授权目标、one-use lease、reader/evidence/continuation 状态及撤销语义保持成立。

## 1. 已读实际路径与故障

- `dashboard/src/plugins/toolRenderers/host.ts`：`createPluginUiToolHost()` 把 context 拷贝到 toolBinding；`getToolContext()` 和 `openSidePanel()` 都读这份快照。
- `dashboard/src/pages/Chat/ChatToolDockContext.tsx`：稳定 `createToolHost` 回调捕获 `captured.locale/theme`；latestRef 虽更新，不会更新已创建 Host。theme 直接读取 document attr，主题改变本身不订阅 React 更新。
- `dashboard/src/pages/Chat/components/MessageBubble.tsx` 的 `ToolDetailsInline`：host useMemo 刻意绑定 activation；仅把语言/主题加入其依赖会造新 Host。现有 presentation useMemo 在纯语言/主题变化时也可保持同引用，memo renderer 可能跳过重渲染。
- `dashboard/src/context/ThemeContext.tsx`：现有 `useTheme().mode` 就是响应式 resolved light/dark，包含 system preference变化；无需新增主题订阅器、MutationObserver或全局store。
- 已参考现有 `ChatToolDockContext.test.tsx`、`ToolDetailsInline.pluginDock.test.tsx` 和 host测试的实际Provider/租约测试方式；不修改这些文件。本草案不研究供应商业务或其它架构。

RED oracle：在同一已挂载真实 Provider/ToolDetailsInline 中切换语言/主题，不改变agent/thread/registration/activation/toolData；旧实现的原Host仍报告旧metadata，且只读host的memo renderer可能仍显示旧值。

## 2. 最小白名单（3 source + 1新test）

1. 修改 `dashboard/src/plugins/toolRenderers/host.ts`：仅Tool Host内部presentation getter及其两个消费点。
2. 修改 `dashboard/src/pages/Chat/ChatToolDockContext.tsx`：useTheme替代DOM读取；把最新presentation getter接给Tool Host。
3. 修改 `dashboard/src/pages/Chat/components/MessageBubble.tsx`：仅ToolDetailsInline订阅当前locale/theme并发送renderer rerender信号；不动其它MessageBubble逻辑。
4. 新增 `dashboard/src/pages/Chat/components/ToolDetailsInline.presentationContext.test.tsx`：同文件覆盖实际Provider及实际ToolDetailsInline，不mock Host factory/getToolContext/dock context。

不改 `types.ts` / ThemeContext / hooks / registry / authSession / chatStore / dock reducer / plugin业务模块；不新增公开ToolRenderProps字段、Core业务ID、i18n文案或API。若实现确需扩白名单，先回root，不顺带重构。

## 3. 内部接口与行为

保持 `createPluginUiToolHost(pluginId, context, binding)` 三参数签名和旧调用兼容；仅为该函数的现有binding对象增加**可选**字段：

```ts
getPresentationContext?: () => Pick<ToolRenderContext, "locale" | "theme">;
```

不把该字段加入公开 OctopPluginUIHost 或 ToolRenderProps。未提供getter的旧调用仍用创建时locale/theme。新增内部helper统一构造tool context：先检查原有isActive，再从toolBinding创建时context取agentId/threadId，仅从getter取locale/theme；显式列四字段，不能spread任意getter对象覆盖身份字段。

`getToolContext()` 和 `openSidePanel()` 传递的context都调用同一helper，避免“显示已新语言、打开面板却带旧语言”。`patchResult`的targetSession、callId、注册所有权和异步request返回后复核完全沿用原捕获边界。getter不得使inactive Host变成active；active检查失败时不能读getter后返回或回退builtin/globalcontext。

Provider中用 `const { mode: theme } = useTheme()`；locale仍取当前 `useTranslation().i18n.language || "en"`。现有latestRef继续承载这两个值；创建Host时传getter `() => ({ locale: latestRef.current.locale, theme: latestRef.current.theme })`，而原capturedScope/authEpoch/agent/thread/registration及isMounted条件不替换、不改用最新权限目标。

同一scope纯语言/主题变化：Host对象引用严格相等，创建时auth epoch不增长，scopeIdentity及localActivation不变；Host返回新的四字段值对象，identity字段仍来自原捕获。切换账号/聊天/注册、关闭/替换reader及卸载仍按既有机制撤销；这些操作后再切语言/主题，旧Host仍不可调用。

## 4. 注册renderer必须看到一次普通更新

ToolDetailsInline使用现有useTranslation并读取i18n.language、useTheme().mode。它保持host useMemo、leaseKey、localActivation、registration、parsed data、组件type/key全部不依赖locale/theme；不以新Host或新activation促成刷新。

选择最小兼容render信号：现有 `presentation` useMemo在locale/theme变化时生成新对象，业务字段仍只有既有location/active/state，值原样保留。将locale/theme加入该memo依赖；如lint判定为非取值依赖，用一条定点说明明确这是memo注册renderer的刷新依赖，不删lint规则、不虚构业务字段。

这样React.memo注册renderer也收到新的presentation prop并正常render，再调用同一个Host读取最新metadata；普通renderer同样可用。没有public订阅API或额外epoch；不强制remount。presentation对象引用是render信号，不能用作证据权限generation、网络重新授权、reader reset或continuation失效依据。验收用有本地选择/continuation状态的memo测试renderer证明这点。

Host自身getter更新和React rerender缺一不可；只写host单元测试、mock实时getter，或只把locale/theme放入host useMemo都不满足本合同。

## 5. 聚焦回归测试合同

新测试文件使用真实 `ChatToolDockProvider`、`createPluginUiToolHost`、registry、`ToolDetailsInline`、`ThemeProvider/useTheme` 与独立i18next实例/I18nextProvider。通过真实 `changeLanguage()` 与测试控件的 `setPreference()` 改值；按需stub window.matchMedia以驱动system模式，不靠改DOM attr+无关rerender模拟主题响应。

可mock与此任务无关的HTTP/media外设，不能mockuseChatToolDock/createToolHost/getToolContext/useTheme。fixture只用generic test renderer/plugin/tool ID；不引用企业/供应商Core业务ID。每测试清registry、卸载providers、恢复浏览器stub及本地appearance状态，不改共享test setup。

### A. 实际Provider持有同一Host

- Probe只在首次挂载创建Host；之后真实language en→zh、theme light→dark，Host严格 `toBe(originalHost)` 且同Host.getToolContext得到新locale/theme、原agent/thread。
- 同一Host.openSidePanel的实际callback收到最新metadata及原scope/agent/thread/callId/plugin；不把latest权限目标混入捕获context。
- 提供不带新可选getter的旧调用兼容性仍由既有host.test.ts覆盖；该文件不需要改动。

### B. 实际ToolDetailsInline + memo注册renderer

- 注册 `memo(Renderer)`，由实际ToolDetailsInline挂载；renderer只在render调用props.host.getToolContext显示locale/theme，并有mount/unmount计数、selection/continuation本地状态和一个受控pending request。
- 保持相同toolData对象、args/data、registration、scope及activation；只切语言/主题。DOM显示最新值，renderer render计数增加，Host严格相同、mount=1/unmount=0；selection/continuation和presentation.state不变，pending request不重复发起/不因metadata变化失效。
- 覆盖inline与已打开side-panel reader两种位置，使用现有真实dock harness模式（useChatDockPanel+Provider）取得实际one-use activation；纯主题更新不能close/reopen或生成新activation。
- theme preference为system时驱动matchMedia change，同样发生实际renderer更新；验证resolvedmode，无需手工DOM属性变动。

### C. 撤销不得被metadata刷新复活

- 保存原inline Host，打开reader导致其one-use lease被消耗；语言/主题切换后原Host的getToolContext/openSidePanel/request/patchResult仍拒绝，当前reader只刷新展示。
- 保存reader Host，真实关闭reader，再切语言/主题；旧Host仍拒绝。新reader即便同callId也不能复用旧Host；原已闭reader不被remount恢复。
- 分别覆盖auth invalidation、scope切换、registration replacement后再语言翻转；旧Host仍拒绝，旧pending request的晚到结果被现有权限复核拒绝，不写错targetthread。可用参数化复用同一harness。

RED验收必须是本次具体行为失败：至少A的旧Hostmetadata不刷新、B的memo renderer文本不刷新；不能只以测试文件不存在/模块缺失作为RED。C是保留已有授权保证的回归，不能为让RED通过删除它们。

## 6. 执行与门禁（待批准，不在规划阶段运行）

- [ ] 冻结草案/白名单及编辑前基线；先加新测试，在旧实现获得上述具体RED证据。
- [ ] 实现三个窄改动；运行新测试及现有host/Provider/pluginDock回归。示例在dashboard目录执行 `npx vitest run src/pages/Chat/components/ToolDetailsInline.presentationContext.test.tsx src/plugins/toolRenderers/host.test.ts src/pages/Chat/ChatToolDockContext.test.tsx src/pages/Chat/components/ToolDetailsInline.pluginDock.test.tsx`。
- [ ] 执行 `npx tsc -b` 与适当定点lint；按仓库门禁由root安排 `make all`、`make build-frontend`，build artifact不手改。
- [ ] 独立Spec/Quality复核稳定Host、memo刷新、无reader reset及撤销矩阵；root做真实浏览器语言/主题切换验收后才声称完成。

待审具体选择：可选getter只允许presentation字段；新presentation对象作为无公开字段的memo刷新信号；新单文件真实集成测试是否已给出足够独立RED。均为本地实际风险的窄修正，不扩大权限、来源、provider或业务UI范围。
