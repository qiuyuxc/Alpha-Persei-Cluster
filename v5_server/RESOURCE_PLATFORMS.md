# 客户端平台与资源清单

SDK `/config` 与 `/sdk-api/pass/game/updateversion/getLatest` 按当前请求识别平台。查询参数、表单或 JSON 的 `platformType` 优先，支持 `android`、`ios`、`pc` 及 Windows / iPhone 等常见名称；没有参数时使用平台请求头或明确的 User-Agent。显式平台不被 User-Agent 覆盖，也不使用上一个客户端的平台。

无法判断的平台返回 `errorCode: "400"` 和说明，不默认下发 Windows 资源。部分 Android SDK 使用 `channelId=7`，因此渠道号不能单独作为 iOS 判据。资源请求优先使用 `/android/`、`/ios/`、`/pc/` 路径选择缓存，没有平台路径时使用请求头。

| 客户端 | 内置清单 |
| --- | --- |
| Windows PC | 229、311、321 |
| iOS | 229、321；未提供 311 |
| Android | 321；229、311 需要另行导入 |

缺少平台清单或清单构建号不符时明确报错，不跨平台或跨版本兜底。资源目录按平台隔离；根目录仅可提供公共页面和已登记到对应平台的清单，不再将未注明平台的资源复制给所有客户端。旧版自动复制的错误缓存建议在备份后清理，并重新导入对应平台的资源。

本地 Windows 客户端目录探测只登记 PC 清单。其他平台需取得真实清单后，通过 `res_version_manager.register_version` 提供对应 `assethash` 映射，并把资源放入 `static_resources/<platform>/` 或配置的 CDN 平台目录。不要只把 Windows 文件复制到 Android / iOS 目录。

当前资源构建版本仍由服务端统一选择；本次解决同一构建版本下的平台匹配，并未引入多构建版本同时运行。

无需玩家数据库即可验证该逻辑：在 `v5_server` 运行 `python -m unittest discover -s tests -p test_resource_platform.py -v`。测试覆盖三端混合并发请求、参数与 User-Agent 冲突、缺失清单、桌面资源导入和缓存隔离。
