# Android 客户端安装与适配

Android 需要同时处理证书信任和域名重定向。仅安装 DNS 模块，不能让原版客户端自动信任自己的服务器证书。

从 [Android 发行页](https://github.com/qiuyuxc/Alpha-Persei-Cluster/releases/tag/android-user-ca-v5) 下载以下文件：

| 文件 | 用途 |
| --- | --- |
| `AlphaPersei-v0.308.301-user-ca-v5.apk` | 已处理用户 CA 信任的游戏客户端，2,025,065,576 字节，约 1.886 GiB |
| `AlphaPersei-DNS-LSPosed-v8.apk` | LSPosed 模块 1.7，可保存服务器 IPv4 地址，并提供游戏内子 GM 入口 |
| `SHA256SUMS.txt` | 两个 APK 的 SHA256 校验值 |

游戏 APK 版本 `v0.308.301`、服务端资源构建号 `321`、模块版本 `1.7` 是三个不同的版本号，不需要相同。发行页里的源码压缩包不包含游戏 APK。

## 1. 安装用户 CA 版游戏客户端

安装 `AlphaPersei-v0.308.301-user-ca-v5.apk`。该客户端使用本项目的签名，与官方签名不同：

- 已安装相同签名的用户 CA 版时，可以覆盖更新。
- 如果系统提示签名冲突，不能直接覆盖官方版。先备份现有客户端数据与已下载资源，再安排迁移；卸载或清除数据可能导致大量资源需要重新下载。

这个 APK 已验证能够启动并登录；验证环境是 ARM64 Android 设备，并不代表所有机型都已验证。APK 只包含客户端自带资源，后续仍可能下载较大的语音和游戏资源。

## 2. 安装自己服务器的根 CA

从自己运行的服务端取得 `v5_server/sdk_ca.crt`，复制到手机，然后在系统设置中找到类似入口：

**安全 / 更多安全设置 → 加密与凭据 → 安装证书 → CA 证书**

选择该证书，完成系统确认。各品牌菜单名称可能不同，应安装到“用户 CA / 受信任凭据”，不是 VPN 客户端证书，也不是 iOS 的 `.mobileconfig` 描述文件。安装用户 CA 本身不需要 Root。

证书应来自自己信任的服务器。可以在服务端核对指纹：

```sh
openssl x509 -in v5_server/sdk_ca.crt -noout -fingerprint -sha256
```

客户端 APK **没有内置某台服务器的 CA 或连接 IP**，发行附件也不提供维护者的服务器 CA。换服务器时，应安装新服务器的 CA，并更新模块地址。服务端更换根 CA 后，手机也需要重新安装对应证书；仅更新由同一根 CA 签发的服务证书时通常无需重装根 CA。

## 3. 安装和设置 LSPosed 模块

1. 手机需具有 Root 和可用的 LSPosed 环境；当前模块面向 ARM64、Android 7+，要求 LSPosed Native Hook / API 93+。
2. 安装 `AlphaPersei-DNS-LSPosed-v8.apk`，在 LSPosed 中启用模块，作用域只选择“深空之眼” `com.yongshi.tenojo.ys`。
3. 打开“深空私服 DNS”模块应用，输入自己的服务器 **IPv4 地址**，点击“保存服务器地址”。不带 `https://`、端口或路径。
4. 完全关闭并重新打开游戏。保存提示 LSPosed 不可用时，先启用模块，再完全关闭并重新打开模块设置页。

默认 `127.0.0.1` 指手机自身。服务器运行在电脑或远程主机上时，必须填写手机可以访问的服务器地址；服务端宣告给客户端的地址也应可达。

模块只重定向 `open.ys4fun.com` 与 `prod-api-activity.ys4fun.com`，同时处理 Java 和 Unity 原生 DNS。官方资源、语音、PV 与安装包下载域名保持直连，包括 `download.ys4fun.com`、`download-eo.ys4fun.com`、`packaging.ys4fun.com`。不要额外把全部官方域名都指向本地服务器。

模块 1.7 的地址校验、Java/C 策略、原生 DNS 和 APK 构建已验证；新增设置页与 LSPosed 跨进程配置仍需在使用者设备上确认。更早版本的模块已有真机登录验证。

## 4. 进入游戏与子 GM

服务端需要提供正确的基础数据、账号设置和资源清单。当前 Android 已提供资源构建 `321`；Android 229/311 清单需另行导入，不能借用 PC 或 iOS 资源。详见 [资源平台说明](../v5_server/RESOURCE_PLATFORMS.md) 和 [账号设置](../v5_server/ACCOUNTS.md)。

默认服务端连接使用 HTTPS 443、网关 TCP 8102、逻辑服 TCP 8105 和战斗 UDP 6105。模块的 HTTPS 与子 GM 入口使用 443 端口。

游戏内点击可拖动的 GM 按钮，或在模块应用中点击“打开子 GM 后台”。入口根据保存的 IP 自动生成，例如默认地址是 `https://127.0.0.1/web/sub_gm.html`。使用服务端管理员账号登录，普通游戏账号没有 GM 权限。

服务证书需覆盖游戏请求的官方域名；模块中的子 GM 通过 IP 访问，因此证书还应包含配置的服务器 IP。出现证书错误时应核对服务器证书、CA、IP 和设备时间。模块保留证书链与主机名校验。

## 这个客户端做了什么处理

原版客户端的默认信任配置不足以直接接受用户安装的私服 CA。当前构建只调整以下两个现有文件，然后重新签名：

1. `AndroidManifest.xml`：设置 `android:networkSecurityConfig`，引用现有 XML 资源。
2. `res/xml/sobot_provider_paths.xml`：加入 `network-security-config`，信任 `system` 与 `user` 两类 CA，同时保留原 FileProvider 使用的 `sobot_root_path` 节点。

`resources.arsc`、DEX、原生库和游戏资源均保持原内容，非签名文件集合不变；打包时保留原有资源布局。没有把证书或服务器地址写进游戏代码，也没有关闭 HTTPS 证书和主机名校验。用户 CA 配置与 LSPosed DNS 模块分别解决信任和连接目标问题，两者都需要。

这套 XML 复用和打包方式只在当前客户端构建中验证过，不应直接套到其他版本。官方 APK 升级可能覆盖信任修改；游戏内资源更新与 APK 替换是两件事。

## 文件校验

下载两个 APK 和校验文件到同一目录后，可运行：

```sh
sha256sum -c SHA256SUMS.txt
```

游戏 APK 的 SHA256：

```text
13f70faa9d909077ed9e9528fe33eb9aadb6549213227c2b3d041983f0c1a491
```

模块 1.7 / v8 的 SHA256：

```text
3c786dafc07f432e35932f799ceca62058b0b85b44ed0a2c6b66b7fbb7b06682
```

两个 APK 均通过 v1/v2/v3 签名校验。游戏客户端内的原作资源权利仍归原开发商及发行商；本仓库的源码许可证不授予这些资源的权利。
