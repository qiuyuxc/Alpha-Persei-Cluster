助战列表在简介战力为 0 时，按成员读取完整的助战角色、钥从与刻印数据，使用原客户端 `ForeignHeroDataTemplate` 和 `calcBattlePower` 计算战力指数。卡片与编队读取同一份结果，保留原有的助战过滤、排序与非助战角色计算。

每次刷新重新读取角色持有者的养成状态；按成员编号和角色编号区分同名角色。过期回复、换公会后的回复和错误成员回复均忽略。计算使用局部角色对象，避免改变当前借用角色的全局详情缓存。

构建包装 `GuildData.lua`、`GuildBossAssistantHeadItemView.lua`、`GuildBossSelectHeroProxy.lua` 的原始字节码。两种安卓脚本架构均需验证，并从当前在线资源包构建以保留既有修复。专项记录位于 `server-repair/upstream-triage/guild-assist-power-20261104`。
