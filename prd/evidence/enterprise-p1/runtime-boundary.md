# 运行边界

- 本机仅开发 Octop；WeKnora 为外部已部署服务。默认测试全部合成数据/MockTransport；真实网络必须显式 live。
- Dashboard actions：JWT/current_user → 通用 Host → 插件业务权限。payload 的 user_id/role 不构成身份。
- Chat：服务端实际员工、Agent、thread、operation/revision → Host 逐轮上下文 → plugin tool/hook。共享 Agent 不共享员工身份或凭据缓存。
- 无法证明真实主体或强制 required-extension 的 IM、Bridge、team/子代理及恢复路径，企业绑定拒绝执行；普通未绑定会话保持原行为。
- Host required-extension 状态不由 plugin_context 或可写业务记录决定，卸载不移除。只允许经显式权限操作解除绑定。
- 生命周期关闭先使 facade 世代失效，取消进行中的工作，再注销 callback；完成提交前再次校验。重启未终态操作为 INTERRUPTED；旧批准不可恢复。
- 插件是受信任进程内 Python代码，Host facade 不构成恶意代码沙箱。不得宣称目录和命名空间隔离替代插件信任审查。
- P07a/P07b已通过本地独立审查：进程Registry变更先发布epoch/stale，所有相关Host完成真实COMMIT有序失效及跨loop任务drain后，才改SDK/插件文件；请求取消不代表executor worker完成。独立CLI仅在无登记runtime Host时保留同步入口，host=None不能绕过其它app。required准入证明实际Agent graph挂载的hook/context/config/epoch，成功重建前持续拒绝。该结果不代表后续业务hook、远程联调或恢复路径已验收。
- 生产凭据经 Host Fernet/opaque reference；probe 只读环境密钥。日志/fixture/浏览器存储无 Key、完整query、内部正文。
