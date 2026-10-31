# 深空连接助手（免 root）

独立 Android 应用，通过系统本地 VPN 为 `com.yongshi.tenojo.ys` 提供进服 DNS。填写服务器 IPv4 后，仅将 `open.ys4fun.com` 和 `prod-api-activity.ys4fun.com` 指向该地址；资源域名沿用当前网络的 DNS，游戏 TCP/UDP 流量直连。

助手无需 root、LSPosed 或原生库，支持 Android 7+，不提供 GM 入口。当前为 **1.0 正式版**。2026-10-31 用户已确认 VPN 进服正常，并在停止按钮修复后确认开启、停止正常。1.0 沿用通过验收的连接逻辑，更新版本号和界面版本标识。

## 使用

1. 按 [Android 安装与适配说明](../docs/ANDROID_SETUP.md) 安装自己生成并签名的用户 CA 版游戏，以及自己服务器的根 CA。当前游戏适配版本为 `v0.308.301`。只安装助手无法解决原版游戏的证书信任问题。
2. 从 [1.0 发行页](https://github.com/qiuyuxc/Alpha-Persei-Cluster/releases/tag/android-connect-v1.0) 下载并安装 `AlphaPersei-Connect-v1.0.apk`，也可以按下方说明自行构建签名。
3. 关闭其他 VPN，将系统“私人 DNS / 私有 DNS”设为“关闭”。Android 同一用户只能启用一个 VPN；本助手会占用这个位置。
4. 填写手机可访问的服务器 **IPv4**，点击“开启连接”，允许系统 VPN 请求。服务器在电脑或远程主机上时，不要填写指向手机自身的 `127.0.0.1`。
5. 完全退出游戏后重新打开。后台保留助手，“连接已开启”只表示本地 DNS 服务正在运行，实际登录结果以游戏为准。

1.0 沿用原签名，可直接覆盖安装 0.1 / 0.1.1，保留服务器配置。点击“停止连接”会主动关闭 VPN 接口，界面恢复“未连接”；之后可再次开启。

停止连接后再修改服务器地址。网络断开时会等待恢复；切换 Wi-Fi/移动网络时重新选取底层 DNS。若系统停止服务或其他 VPN 收回权限，需要重新开启。不要启用“无 VPN 时阻止连接”或强制常开 VPN，否则分流直连可能被系统拦截。

服务器证书仍须覆盖游戏请求的域名，端口与宣告地址须能从手机访问。常用端口为 HTTPS 443、网关 TCP 8102、逻辑服 TCP 8105、战斗 UDP 6105。助手不会跳过 TLS 校验、创建账号或开启服务器端口。

## 界面

界面参考 [Compose Miuix](https://github.com/compose-miuix-ui/miuix) 的默认配色、大标题、16dp 圆角卡片、设置列表与按钮，支持跟随系统深色模式。首页展示连接状态与服务器地址，解析计数放在“连接诊断”中。

使用 Android 原生 View 实现，未引入 Compose Miuix 运行库或 MIUI 专有资源；字体使用系统字体。小屏与大字体可滚动，服务器地址弹窗保留旋转前的输入，弹窗与状态栏遵循明暗主题。视觉与交互仍需真机确认。

## 构建

需要 Python 3.9+、JDK 17+、Android SDK Platform 35 与 Build-Tools；无需 Gradle 或 NDK。

纯 Java 专项测试（不需要 SDK）：

```sh
python3 android-connection-helper/build.py --test-only
```

生成未签名包（不能直接安装）：

```sh
python3 android-connection-helper/build.py --sdk /path/to/android-sdk
```

首次创建自己的签名密钥：

```sh
keytool -genkeypair -storetype PKCS12 -keystore connection.p12 -alias connection -keyalg RSA -keysize 2048 -validity 10000
```

把密码保存为仅自己可读的文件后构建签名包：

```sh
python3 android-connection-helper/build.py --sdk /path/to/android-sdk --keystore /private/connection.p12 --key-alias connection --password-file /private/connection.pass
```

Termux 使用已经安装的 `aapt2`、`d8`、`apksigner`，额外传入 `--termux-prefix /data/data/com.termux/files/usr`；SDK 仍需提供 `platforms/android-35/android.jar`。自行构建时保管签名密钥，后续更新沿用同一把密钥。

产物位于 `android-connection-helper/dist/`，包括 APK、`SHA256SUMS.txt`、`validation.json`；构建和测试日志位于 `build/`。这些文件和私有签名材料不纳入源码。

## 技术范围与验收

VPN 只添加游戏应用白名单和私有 DNS `10.111.0.2/32` 路由；`10.111.0.1` 用作 TUN 接口地址。IPv6 允许底层网络直连，目标域名 AAAA/HTTPS 查询返回 NODATA，避免走向官方地址。两个内部地址不能作为服务器地址。

上游查询绑定实际 Wi-Fi/移动网络的 DNS，并调用 VPN socket protection。UDP 截断或失败时支持 TCP 回退；本地 DNS TCP 处理帧拆分、重传、重复包、接收窗口、FIN 和有界会话。转发并发、排队数量、请求超时及会话空闲时间均有限制。没有硬编码公共 DNS，也没有远端 VPN 中转。

离线测试覆盖域名边界、IPv4 输入、DNS 编码与错误回复、UDP/TCP 校验和、大应答、TCP 重传/分段/关闭、上游回退与关闭后的迟到回复。签名构建还验证 v1/v2/v3 签名、ZIP 完整性、资源对齐与 SDK 声明。

已有用户反馈确认 VPN 进服以及开启、停止正常；未取得设备型号、Android 版本或设备日志。VPN 权限撤销、通知、资源下载、Wi-Fi/移动网络切换、后台恢复、私人 DNS 设置以及停用后的正常联网仍需分别验收。离线测试不能替代 Unity 客户端和 Android 厂商系统的实测。
