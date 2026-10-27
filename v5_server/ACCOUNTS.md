# 注册与独立存档

游戏内直接选择「账号密码登录」：输入未注册的数字账号和密码，首次登录会自动创建独立新号并进入游戏。也可使用 `/web/account.html` 注册和管理账号。

- 账号为 6–16 位数字，兼容当前 Android SDK 的数字输入框，不需要真实手机号。密码 8–64 个字符。
- 自动注册只用于游戏 SDK 的账号密码登录。已有账号仍校验原密码，密码错误或账号停用不会重新建号、覆盖密码或重置存档；网页、令牌与聚合登录也不会自动建号。
- 同一数字账号的并发首次登录只创建一个 UID，账号、初始存档和首次 SDK 会话在同一事务中提交。网页注册与游戏首次注册共享新号创建频率限制；已有账号的正常登录不消耗注册配额。
- 每次注册创建独立 UID。初始等级 1、经验 0、朝约 1 级、100 体力，货币、装备、钥从、主线通关和引导记录从空状态开始。
- 新号默认保留新手引导。没有复制高等级账号再降级；登录中的静态录制进度也不会下发给新号。
- 主线/新手任务从第一阶段开始，阶段总结任务领奖后开放下一阶段。关卡通关记录按 UID 保存。
- 现有存档通过一次性管理员设置链接绑定登录账号，保留等级、角色、物品与进度。普通注册不能指定 UID、管理员权限或导入旧模板。
- GM 后台需要管理员登录，可选择账号，停用/启用普通账号、重置密码。玩家在账号中心修改自己的密码。
- 密码使用独立随机盐的 scrypt 哈希；网页、SDK、聚合登录和游戏令牌按用途隔离，数据库只存令牌哈希。修改密码或停用账号会使旧令牌和在线连接失效。
- 当前仍是一组服务器进程、一份数据库中的多账号；区服标签不创建不同世界。

## 首次设置

先准备包含基础数据和目标存档的 `account.db`（数据库不随源码发布）。在服务端本机进入 `v5_server` 后运行以下代码，输入要保留的存档 UID，生成七天有效的一次性管理员设置链接：

```sh
python - <<'PYTHON'
from account_db import AccountDB
from auth_service import get_service
from urllib.parse import quote
with_db = AccountDB()
try:
    uid = int(input("要绑定的现有存档 UID: "))
    token = get_service(with_db).bootstrap_token(uid)
    print("https://127.0.0.1/web/account.html#setup=" + quote(token))
finally:
    with_db.close()
PYTHON
```

在部署地址打开该链接并设置账号密码。凭证用过即失效，数据库只保存其哈希；不要公开或转发链接。管理员设置完成前，公开注册保持关闭。

以前任意账号、任意密码生成的缓存登录令牌将失效。设置后，在游戏中退出旧登录，用绑定的数字账号和密码重新登录；新注册的数字账号对应新的开荒存档。

管理员设置不会重置存档，也不会把普通注册的第一个访问者提升为管理员。

## 接口

| 接口 | 权限与用途 |
| --- | --- |
| `GET /api/auth/status` | 公开，初始化及 `game_auto_registration` 状态 |
| `POST /sdk-api/pass/user/login` | 游戏账号密码登录；未注册的合法数字账号自动建号 |
| `POST /api/auth/setup` | 一次性设置凭证、数字账号、密码 |
| `POST /api/auth/register` | 公开，创建普通新号 |
| `POST /api/auth/login` | 数字账号、密码，返回网页令牌 |
| `GET /api/auth/me` | 网页 Bearer 令牌，当前账号 |
| `POST /api/auth/password` | 网页令牌、原密码、新密码 |
| `POST /api/auth/logout` | 撤销当前网页令牌 |
| `GET /api/gm/accounts` | 管理员，账号与存档列表 |
| `POST /api/gm/accounts/disabled` | 管理员，`uid,disabled` |
| `POST /api/gm/accounts/password` | 管理员，`uid,password` |

所有 GM 与旧 `/api/` 管理接口需要管理员网页令牌。启动器使用的只读 `GET /api/system/res_version` 保持公开。

## 验证与范围

用完整数据库的备份运行，测试内部再复制到临时目录，不能把在线库直接作为测试工作库：

```sh
GM_TEST_DB_SEED=/path/to/snapshot.db python -m unittest discover -s tests -p test_accounts.py -v
GM_TEST_DB_SEED=/path/to/snapshot.db python -m unittest discover -s tests -p test_game_registration.py -v
GM_TEST_DB_SEED=/path/to/snapshot.db python -m unittest discover -s tests -p test_gm_management.py -v
cd web_src
npm run check
npm run build
```

账号测试覆盖设置凭证、旧档保留、并发重复注册、事务回滚、密码验证、权限、令牌过期/撤销、实际 TCP 握手、完整登录数据与任务阶段推进。测试使用隔离数据库并禁止登录测试访问外部网络。

这次补的是账号体系和新号初始进度。原项目的部分活动与玩法仍使用模拟实现；自动测试不等于 Android 客户端所有新手战斗和后续活动均已逐项通关验证。

新号登录会补齐极限机装的未完成任务记录。客户端在计算常驻活动完成度时会直接读取这些任务的完成标记，缺记录会中断整个登录；补齐不会发放奖励，也不会推进主线任务阶段。
