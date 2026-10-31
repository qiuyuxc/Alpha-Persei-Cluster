# 更新记录 / Changelog

## 上游 v5.0.2 独立适配 — 2026-10-31

- 按客户端配置同步账号经验曲线、等级上限与升级体力奖励，70 级起自然恢复上限为 240。
- 活动 PT 与通配任务按事件计数一次，指定商店任务按店号和嵌套限制匹配。
- 登录成就从已保存的真实天数恢复，首通成就支持客户端星级数组。
- 试用角色战斗数据保留模板编号与角色类型，使用累计经验及各自突破等级；开战检查账号和队伍，保留公会助战数据。
- UDP 会话重建清理旧心跳序列，旧会话无法推进新会话计数。

本次引入可独立适配的玩法与战斗会话改动，继续使用现有单库多账号架构。上游的多存档、资源下载队列、库内 AI 配置与多人功能策略仍需另行迁移。

验证：Python 3.10 下 166 项相关专项通过，204 条登录回包的长度与压缩格式检查通过。

## v5.0.1 — 2026-10-19

### 修复与改进

- 修复永久换装/场景与部分编队状态的持久化、恢复和同步问题。
- 修复邮件领取、部分任务和养成链路的事务边界、必要回包及重复领取保护；改善刻印与钥从实例消耗后的同步。
- 修复已有任务条件满足时的模组初次开启、免费体力分档领取保护和管理面刻印库存读取。
- DNS 增加普通域名转发、UDP/TCP、大响应及失败应答支持，支持指定上游与 LAN 出站绑定。
- 保留战斗服时钟精度并增加入场阶段日志，便于继续定位实际入场耗时。
- CDN 使用公网解析和 TLS 验证；完整文件须通过长度与 MD5 检查后才发布缓存，部分 Range 或中断下载不会冒充完整文件。
- 两个封包入口共用资源合同与逐文件清单；补齐必需协议、角色编码配置、邮件模板和 iOS229/321 元数据，并降低无价值的版本轮询输出。

### 验证与使用范围

发布包通过防漏检查、ZIP CRC/清单/逐文件哈希、独立包内 Python 的 TLS、平台/版本响应、协议编解码、邮件模板加载及轮询检查。安装器与新版 GUI/游戏的完整安装验收仍需使用者复测；不将脚本检查称为所有玩法通过。

升级前备份数据库。发行证书与开发环境隔离，移动端按新宿主机指引部署证书。完整游戏资源与外置缓存不包含在服务端包内；历史 iOS311 清单缺失，不能据版本选择项认定其资源已归档。换装等资源可能在首次使用时按需下载，退出下载会留下未完成缓存。

### English summary

- Maintenance fixes for persistent skin/scene state, selected formation, mail, task, progression and entity-consumption flows.
- Improved DNS forwarding and TCP compatibility, battle timing precision, and CDN cache integrity.
- Required protocol/config assets are checked before publication; release downloads include SHA-256 checksums.
- Full game resources are external. The original iOS311 manifest is not included. Native installer/GUI acceptance remains separate from package checks.
