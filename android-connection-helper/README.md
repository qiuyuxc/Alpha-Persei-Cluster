# 深空连接助手（免 root）

独立 Android 应用，通过系统本地 VPN 为 `com.yongshi.tenojo.ys` 提供进服连接。外部服务器模式将 `open.ys4fun.com` 和 `prod-api-activity.ys4fun.com` 指向填写的 IPv4；手机本地模式将这两个域名的 TCP 443/80 转发到同机 Termux 的 8443/8080。资源域名沿用当前网络的 DNS。

助手无需 root 或 LSPosed，不提供 GM 入口。当前为 **1.1 正式版**，发行包为 ARM64；构建声明最低 Android 7。外部模式沿用 1.0 的 DNS 连接方式，2026-10-31 已有用户确认进服和开启、停止正常。2026-11-01，手机本地模式在 vivo（Android 16 / API 36）通过实测同机登录，以及战场清扫关卡的战斗和结算；1.1 沿用已通过实测的连接逻辑。

## 使用

1. 按 [Android 安装与适配说明](../docs/ANDROID_SETUP.md) 安装自己生成并签名的用户 CA 版游戏，以及自己服务器的根 CA。当前游戏适配版本为 `v0.308.301`。只安装助手无法解决原版游戏的证书信任问题。
2. 从 [1.1 发行页](https://github.com/qiuyuxc/Alpha-Persei-Cluster/releases/tag/android-connect-v1.1) 下载并安装 `AlphaPersei-Connect-v1.1.apk`，或按下方说明自行构建签名。仅使用外部模式的非 ARM64 设备可继续使用 [1.0](https://github.com/qiuyuxc/Alpha-Persei-Cluster/releases/tag/android-connect-v1.0)。
3. 关闭其他 VPN，将系统“私人 DNS / 私有 DNS”设为“关闭”。Android 同一用户只能启用一个 VPN；本助手会占用这个位置。
4. 外部模式填写手机可访问的服务器 **IPv4**；手机本地模式按下节准备服务端后，选择“连接方式 → 手机本地”。点击“开启连接”，允许系统 VPN 请求。
5. 完全退出游戏后重新打开。后台保留助手，“连接已开启”只表示本地 DNS 服务正在运行，实际登录结果以游戏为准。

1.1 沿用原签名和应用包名，ARM64 设备可覆盖既有版本并保留服务器配置。默认仍选择外部模式。点击“停止连接”会主动关闭 VPN 接口与本地转发，界面恢复“未连接”；之后可再次开启。

停止连接后再修改服务器地址。网络断开时会等待恢复；切换 Wi-Fi/移动网络时重新选取底层 DNS。若系统停止服务或其他 VPN 收回权限，需要重新开启。不要启用“无 VPN 时阻止连接”或强制常开 VPN，否则分流直连可能被系统拦截。

服务器证书仍须覆盖游戏请求的域名，端口与宣告地址须能从手机访问。常用端口为 HTTPS 443、网关 TCP 8102、逻辑服 TCP 8105、战斗 UDP 6105。助手不会跳过 TLS 校验、创建账号或开启服务器端口。

## 手机本地模式

完整准备步骤见 [安卓同机服务端说明](../docs/ANDROID_LOCAL_SERVER.md)。在同一台手机的 Termux 中准备完整数据库、资源清单和自己的 CA，然后从项目目录运行：

```sh
python v5_server/main.py --android-local --db /path/to/account.db --res-version 321
```

服务端监听本机 `127.0.0.1:8443/8080`，网关、逻辑服和战斗服下发 `127.0.0.1` 及对应高位端口。助手仅添加虚拟 SDK 地址 `10.111.0.3/32` 的 TCP 路由，转发 443→8443、80→8080；网关 TCP 8102、游戏 TCP 8105、战斗 UDP 6105 直接访问本机。端口映射不解密 TLS，客户端继续验证原域名和证书链。

浏览器访问 `http://127.0.0.1:8080/cert` 下载本机服务端 CA，在系统中安装后，选择“手机本地”并开启连接。保持 Termux 和助手后台运行，完全退出并重开游戏。本阶段仍使用原有资源地址，不能据此认定已经完成完全断网的单机化。

按系统设置允许 Termux 和助手后台运行、自启动，并放宽电池优化。若切换到游戏后处理变慢或断连，可尝试分屏或小窗保留 Termux，比较响应是否改善；是否需要前台保留仍须按机型验证。后台限制、唤醒锁和验证范围见 [同机运行说明](../docs/ANDROID_LOCAL_SERVER.md#后台运行与厂商调度)。

## 界面

界面参考 [Compose Miuix](https://github.com/compose-miuix-ui/miuix) 的默认配色、大标题、16dp 圆角卡片、设置列表与按钮，支持跟随系统深色模式。首页展示连接状态与服务器地址，解析计数放在“连接诊断”中。

使用 Android 原生 View 实现，未引入 Compose Miuix 运行库或 MIUI 专有资源；字体使用系统字体。小屏与大字体可滚动，服务器地址弹窗保留旋转前的输入，弹窗与状态栏遵循明暗主题。视觉与交互仍需真机确认。

## 构建

需要 Python 3.9+、JDK 17+、Android SDK Platform 35 与 Build-Tools，无需 Gradle。本地 TCP 组件使用 Android NDK r27+，或 ARM64 Termux clang。

纯 Java 专项测试（不需要 SDK）：

```sh
python3 android-connection-helper/build.py --test-only
```

先构建固定版本的原生组件：

```sh
python3 android-connection-helper/native/build.py --ndk /path/to/android-ndk --abis arm64-v8a
```

ARM64 Termux 可改用 `--termux-prefix /data/data/com.termux/files/usr`。构建器按 `native/dependencies.json` 下载并校验上游源码归档，静态链接依赖；产物只依赖 Android 系统库，并检查 16 KiB ELF 页对齐。源码和编译缓存位于 `build/native/`。NDK 可以选择其他 ABI，本轮现成产物仅验收了 ARM64 原生运行。

生成未签名包（不能直接安装）：

```sh
python3 android-connection-helper/build.py --sdk /path/to/android-sdk --native-dir android-connection-helper/build/native/lib
```

首次创建自己的签名密钥：

```sh
keytool -genkeypair -storetype PKCS12 -keystore connection.p12 -alias connection -keyalg RSA -keysize 2048 -validity 10000
```

把密码保存为仅自己可读的文件后构建签名包：

```sh
python3 android-connection-helper/build.py --sdk /path/to/android-sdk --native-dir android-connection-helper/build/native/lib --keystore /private/connection.p12 --key-alias connection --password-file /private/connection.pass
```

Termux 使用已经安装的 `aapt2`、`d8`、`apksigner`，额外传入 `--termux-prefix /data/data/com.termux/files/usr`；SDK 仍需提供 `platforms/android-35/android.jar`。自行构建时保管签名密钥，后续更新沿用同一把密钥。

产物位于 `android-connection-helper/dist/`，包括 APK、`SHA256SUMS.txt`、`validation.json`；构建和测试日志位于 `build/`。这些文件和私有签名材料不纳入源码。

## 技术范围与验收

VPN 只添加游戏应用白名单和私有 DNS `10.111.0.2/32` 路由；`10.111.0.1` 用作 TUN 接口地址，本地模式额外添加 `10.111.0.3/32`。IPv6 允许底层网络直连，目标域名 AAAA/HTTPS 查询返回 NODATA，避免走向官方地址。这三个内部地址不能作为外部服务器地址。

上游查询绑定实际 Wi-Fi/移动网络的 DNS，并调用 VPN socket protection。UDP 截断或失败时支持 TCP 回退；本地 DNS TCP 处理帧拆分、重传、重复包、接收窗口、FIN 和有界会话。转发并发、排队数量、请求超时及会话空闲时间均有限制。没有硬编码公共 DNS，也没有远端 VPN 中转。

离线测试覆盖域名边界、IPv4 输入、DNS 编码与错误回复、UDP/TCP 校验和、大应答、TCP 重传/分段/关闭、上游回退与关闭后的迟到回复。签名构建还验证 v1/v2/v3 签名、ZIP 完整性、资源对齐与 SDK 声明。

TCP socket 在调用系统 VPN 保护前先创建底层文件描述符；本地转发与 DNS TCP 回退共用这条准备路径。“连接诊断”显示最近一次本机连接失败的阶段和原因。

本地模式额外验证 SOCKS5 目标限制、512 KiB 双向字节流、半关闭和服务停止；JNI 测试检查初始化、真实 SYN/ACK、重复启动关闭与回调中关闭。`tests/verify_native.py` 使用真实本机 TLS 服务，验证原域名 SNI、证书校验、分段、乱序和丢包重传。该测试仅使用 socketpair 与本机高位端口，不访问 TUN 设备或外部网络。

本地模式已有用户确认 Unity 同机登录和战场清扫战斗、结算正常，服务端日志提供了对应登录与战斗证据。VPN 权限撤销、通知、完整资源下载、Wi-Fi/移动网络切换、长期后台保活以及停用后的正常联网仍需分别验收；不同厂商的后台调度影响也需对照验证。
