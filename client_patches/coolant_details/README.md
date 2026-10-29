修复 Android 临时冷却剂在背包使用数量页中把模板道具 ID 当作恢复奖励数组读取的错误。补丁只覆盖原始 `ItemUseView.lua` 的恢复量和最大数量计算，使用原有背包入口、确认按钮及成功回调；请求保留临时物品 ID 和有效期。

构建需要 Python、UnityPy 和匹配原脚本帧格式的 LuaJIT 2.1。Android 的 `scripts64` 为 FR2，`scripts32` 为 FR1，构建器会拒绝格式不匹配的字节码：

```sh
python build.py --bundle /path/to/original/scripts64.ys --output /path/to/new/scripts64.ys --test-inputs /path/to/new/original-assets
luajit test.lua /path/to/new/original-assets /path/to/new/scripts64.lua.luac /path/to/new/client-tests.json
python prepare_manifest.py --manifest /path/to/original/AssetHash_Info.bytes --build-manifest /path/to/new/scripts64.manifest.json --bundle /path/to/new/scripts64.ys --output-directory /path/to/new/cdn/android
```

跨架构构建可先用 `--wrapper-source-output` 导出包装脚本，再使用相应 LuaJIT 编译，通过 `--compiled-wrapper` 导入；构建器校验包装中的原脚本、补丁内容与架构标记。测试沿原始 `StoreItemInfo` 点击入口进入 `ItemUseView` 并确认使用，也验证修改前的临时道具错误。

`prepare_manifest.py --script-name scripts32` 可更新另一架构的脚本项。组合资源清单时应只改变这两项哈希及大小，按哈希命名部署两个包。

在版本配置 `resource_download_urls.android` 中按分号依次设置云端 `/patches/android/resources/` 入口与原有的两个官方 CDN 地址，并在 `resource_local_bundles.android` 数组中登记两个补丁包的哈希文件名。该入口和兼容的 `/android/resources/` 路径从本地提供当前清单与已登记脚本；其他资源通过 HTTP 302 转到对应平台的官方 CDN，由客户端直接下载。未配置的平台沿用原资源策略，缺失的补丁包返回 404。这项分流由服务端实现，现有 LSPosed 模块已保留官方下载域名直连，无需更新模块。
