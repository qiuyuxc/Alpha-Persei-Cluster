首页轮播保留原生排序、活动条件与卡池跳转。历史卡池缺图时使用现有探测图，其它缺图活动过滤。可用图片路径来自 Build 307/321 的安卓资源清单，准备发布清单时会校验这些路径仍存在。

客户端的 `getSpriteWithoutAtlas` 返回图片路径字符串，由原生界面在赋值时解析。首版错误地对路径调用 `isNil`，触发不存在的 `Equals` 方法，导致主页初始化中断；2026-11-03 已先回退该版本。修正版直接检查路径可用性，仅对 Unity 对象进行空对象检查。

补丁包装原始 `ActivityTools.lua` 字节码，仅替换 `GetCurrentAdvInfo`。构建过程保留其它资源对象与已有客户端修复，并核对 LuaJIT 架构。`test.lua` 验证旧式图片对象；`test_native_paths.lua` 使用原客户端的 `global`、`UnitySupport`、`MainHomeView_RefreshUI` 和 `BannerView` 字节码，验证图片路径与轮播初始化，并复现被撤回版本的报错。两种安卓脚本架构均需验证。

专项记录位于 `server-repair/upstream-triage/continuation-20261103/homepage-recovery`；真机显示效果由用户复测。
