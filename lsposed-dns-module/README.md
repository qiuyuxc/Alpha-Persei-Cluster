# 深空私服 DNS · LSPosed 模块

将深空之眼的登录与网关域名重定向到自己部署的服务器，并提供游戏内子 GM 抽屉。
默认服务器为 `127.0.0.1`，可在模块设置页修改，无需重新编译。适用于 ARM64、Android 7+ 和支持 Native Hook / API 93+ 的 LSPosed。

## 使用

1. 安装自行构建的模块 APK，在 LSPosed 中启用，作用域选择 `com.yongshi.tenojo.ys`（深空之眼）。
2. 打开模块应用，输入服务器 **IPv4 地址**，点击“保存服务器地址”。不要输入协议、端口、域名或路径；默认 `127.0.0.1` 指 Android 设备自身。
3. 完全关闭并重新打开游戏。Java 与原生 DNS 会在游戏进程启动时读取同一配置。
4. 点击游戏内可自由拖动的 GM 按钮，或模块应用中的“打开子 GM 后台”。页面地址自动生成为 `https://你保存的IP/web/sub_gm.html`，使用服务器的管理员账号登录。

子 GM 页面源码和构建文件位于本仓库的 `v5_server/web_src` 与 `v5_server/static_web`。服务端需提供 HTTPS 443 端口和该页面，证书应包含所配置的 IP。客户端需支持并信任自己服务器的 CA；模块 WebView 保留证书及主机名校验。

只重定向 `open.ys4fun.com` 与 `prod-api-activity.ys4fun.com`。资源、语音、PV 和安装包域名保持官方直连；不修改系统 hosts、VPN 或全局 DNS。不需要系统悬浮窗权限。横竖屏分别记住按钮位置，横屏网页按 80% 显示。

配置文件仅存非敏感的目标 IP，通过 [LSPosed XSharedPreferences](https://github.com/LSPosed/LSPosed/wiki/New-XSharedPreferences) 共享给游戏进程。凭证由网页独立保存，不放入共享配置，也不接受 Intent 传入的服务器或凭证。WebView 页面导航和资源请求限定在保存的服务器来源。

## 构建

准备 Python 3、JDK 17+、Android SDK Platform 35 / Build-Tools、Android NDK，以及主机 C 编译器和 `readelf`。以下示例使用 Linux SDK/NDK：

```sh
export ANDROID_HOME=/path/to/android-sdk
export ANDROID_NDK_HOME=/path/to/android-ndk
python3 build.py
```

默认生成未签名 APK。脚本会下载并校验编译用 Xposed API 82（不打包进 APK），SHA256 为 `f48c635f1c7469fdec0e00ad2ea0b7a6b2f5b55065784a35b7ca3a84615e8e25`。

安装前使用自己的 PKCS12 密钥签名。密码写入本地文件，再运行：

```sh
python3 build.py --keystore /path/to/module.p12 \
  --password-file /path/to/password-file --key-alias module
```

已配置 ARM64 Termux 的环境可用 `--termux-prefix "$PREFIX"` 代替 NDK；需安装 `clang`、`aapt2`、`d8`、`apksigner` 和 Java。构建脚本不会读取项目以外的默认签名文件。密钥、密码、下载依赖和构建产物均被 Git 排除。

输出位于 `dist/AlphaPersei-DNS-LSPosed-v8.apk`。构建会执行 Java/C 域名策略、地址校验和 GM 来源限制测试，并检查原生 ELF / APK 的 16 KiB 对齐；提供签名参数时还会验证 v1/v2/v3 签名。

`tests/native_framework_test.c` 可在 Android 环境编译运行，通过模拟 LSPosed 注册验证真实 bionic 的 IPv4/IPv6、自定义地址、端口保留及官方 CDN 委托，不修改运行中游戏的 DNS。

实现参考 [LSPosed Native Hook](https://github.com/LSPosed/LSPosed/wiki/Native-Hook)。界面参考 [Miuix](https://github.com/compose-miuix-ui/miuix)，使用 Android 原生 View。许可见仓库根目录 [LICENSE](../LICENSE)。
