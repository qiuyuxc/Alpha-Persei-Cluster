# 在本地处理 Android 用户 CA 信任

本目录只提供 Python 脚本和测试，不提供或下载游戏 APK、客户端资源、服务器 CA 或签名密钥。使用者自行准备原始 APK，在自己的电脑或 Termux 环境中生成安装包。

脚本仅支持已验证的 `com.yongshi.tenojo.ys` / `v0.308.301` / versionCode `2026082719` 客户端结构。它会检查 Manifest、资源表和原 XML 的大小及 SHA256；版本不匹配或已经处理过时直接停止，不会尝试强行套用。

## 准备

- Python 3.9+，仅使用标准库。
- 自己准备的兼容原始 APK。
- 要直接生成可安装包，还需要 Java、Android SDK Build-Tools 的 `apksigner`，以及自己的 PKCS12 签名密钥。
- 原包以外预留约 **2.2 GiB** 空间生成未签名包；直接签名时预留约 **4.1 GiB**。脚本不会将整个 APK 加载到内存或解压到目录。

下载本目录的 `patch_apk.py` 即可单独运行，也可以在完整仓库根目录执行下列命令。

## 先检查版本

```sh
python tools/android-user-ca/patch_apk.py /path/to/original.apk --check
```

出现 `"compatible": true` 才说明输入符合当前脚本的适配条件。此命令不生成或修改 APK。

若要完整演算打包流程、计算未签名输出的大小和 SHA256，但不占用额外大文件空间：

```sh
python tools/android-user-ca/patch_apk.py /path/to/original.apk --dry-run
```

`--dry-run` 会顺序读取约 2 GB 的输入数据，丢弃生成的数据流，不调用签名工具。

## 生成未签名 APK

```sh
python tools/android-user-ca/patch_apk.py /path/to/original.apk \
  --output tools/android-user-ca/output/client-user-ca-unsigned.apk
```

未签名 APK 不能直接安装。可以使用自己的签名工具处理，也可以使用下面的方式一次完成打包和签名。

## 使用自己的密钥生成可安装 APK

首次使用时创建自己的密钥；下面的 `keytool` 命令会交互询问密码和证书信息：

```sh
keytool -genkeypair -storetype PKCS12 -keystore local-signing.p12 \
  -alias local -keyalg RSA -keysize 2048 -validity 10000
```

保管好密钥和密码，后续更新沿用同一把密钥。APK 的签名密钥与服务器的 TLS / CA 密钥是不同用途，不要混用。

然后运行：

```sh
python tools/android-user-ca/patch_apk.py /path/to/original.apk \
  --output tools/android-user-ca/output/client-user-ca.apk \
  --keystore /path/to/local-signing.p12 --key-alias local \
  --apksigner /path/to/android-sdk/build-tools/35.0.0/lib/apksigner.jar
```

脚本会在终端中隐藏输入签名密码。需要非交互运行时，可传 `--password-file /path/to/password-file`，不要把密码直接写在命令行中。

`--apksigner` 可指定 JAR 或可执行文件；不指定时，先查找 PATH 中的 `apksigner`，再从 `ANDROID_HOME` / `ANDROID_SDK_ROOT` 的 Build-Tools 查找。Termux 可使用已安装的 `apksigner` 与 Java。Windows PowerShell 可将上述命令写成一行，路径有空格时加引号。

脚本保留原始 APK，拒绝覆盖任何已有输出，并在签名后验证 v1/v2/v3 签名。旁边会生成 `client-user-ca.apk.sha256`，记录本次实际产物的哈希；不同使用者的签名不同，最终 APK 哈希也会不同。

## 安装与连接

生成 APK 后，继续阅读 [Android 安装与适配说明](../../docs/ANDROID_SETUP.md)：安装自己服务器的用户 CA，构建并启用 LSPosed 模块，保存服务器 IPv4，再完全重启游戏。

重新签名的 APK 不能直接覆盖不同签名的官方版或其他人构建的版本。遇到签名冲突时先备份客户端数据及下载资源，不要通过反复卸载、清数据来试错。

## 修改范围与验证

只替换两个现有条目：

- `AndroidManifest.xml`：加入对原有 XML 资源的 `networkSecurityConfig` 引用。
- `res/xml/sobot_provider_paths.xml`：信任系统及用户 CA，同时保留原 FileProvider 的路径节点。

网络 XML 由脚本自身生成。Manifest 从使用者的输入中解析和修改；脚本不包含原始或修改后的游戏 Manifest。资源表、DEX、原生库和其他资源的压缩数据、文件名及原有位置均保留，不新增普通文件，不内置私服地址或 CA，不关闭 HTTPS 证书链与主机名校验。

两个修改文件已与先前真机可登录的客户端逐字节核对。脚本还有真实大包的完整数据流演算、ZIP 布局回归测试和使用自有模块 APK 的签名集成验证；自行生成的最终客户端仍需在自己的设备上验收。

运行不含游戏文件的测试：

```sh
python -m unittest discover -s tools/android-user-ca/tests -v
```

脚本适配不等于取得游戏资源的权利；原作资源仍属于原开发商和发行商。
