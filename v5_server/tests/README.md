# 服务端测试与通用种子库

测试种子库由 [`test_seed.py`](../test_seed.py) 从公开源码生成。新克隆的仓库可以运行完整服务端回归，无需提供私人 `account.db`、发行存档、客户端安装包、证书或在线服务器。

## 从源码运行

Python 3.10+，在仓库根目录执行：

```sh
python -m venv .venv
.venv/bin/python -m pip install -r v5_server/tests/requirements.txt
.venv/bin/python v5_server/run_tests.py --output test-results
```

Windows PowerShell 将 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。已有环境也可直接执行 `python v5_server/run_tests.py`。

入口固定使用 `Asia/Shanghai` 游戏时区，自动发现 `tests/test_*.py` 并串行运行。数据库在临时目录生成，每个现有用例仍复制种子库并调用真实服务、SQLite 和 protobuf 编解码器。网络访问仅允许传输测试使用的本机回环；SQLite 仅允许临时测试目录。退出时清理数据库。退出码为 0 表示全部通过且种子和源码校验值没有变化。

`--output` 保存完整 `tests.log`、逐项结果及运行环境 `result.json`、种子输入与表行数 `seed.manifest.json`。输出文件已存在时拒绝覆盖，可为下一次运行选择新目录。

## 专项和独立种子

```sh
python v5_server/run_tests.py --test test_hero_growth --test test_item_boxes
python v5_server/run_tests.py --test test_next_batch.NextBatchTests.test_hero_break_at_threshold_preserves_level_and_debits_one_configured_step
python v5_server/test_seed.py --output /tmp/apc-test-seed.db
```

生成命令只用 Python 标准库，会同时写出 `apc-test-seed.db.manifest.json`。已有数据库和清单均不会被覆盖。可用 `--server /path/to/v5_server` 指定同版本的完整源码目录。

现有标准 `unittest` 入口也支持自动生成：

```sh
cd v5_server
TZ=Asia/Shanghai python -m unittest discover -s tests -v
```

未设置 `GM_TEST_DB_SEED` 时，同一个 Python 进程内的模块共用自动生成的种子；如果明确设置此变量，各模块继续使用指定文件的副本。推荐使用 `run_tests.py`，它始终自行生成数据并限制测试 I/O。

## 数据来源和扩展

[`db_schema.sql`](../db_schema.sql) 来自上游公开提交 `92671ab8f9fc3a7465f54ff899c4c3d552d9c151`，只包含表结构。建库还执行当前 `AccountDB` 的正常迁移。条目、角色、经验曲线、随机箱奖池、商店物品、卡池和任务分期从仓库中的 JSON 配置构造。

缺少公开完整配置的部分由生成器显式定义测试场景：角色突破规则、任务条件和奖励、货架组合、周期前置状态、活动拓扑及登录协议序列。它们用于覆盖现有断言；这份生成库不是发行版的完整游戏存档。协议模板通过公开 protobuf schema 编码，未导入抓包字节或私人玩家状态。

三个账号使用虚构名称；数字 UID 保留现有用例的兼容值。库中没有登录密码或有效凭证。生成过程固定时钟、事务批量写入、禁止联网，并且只允许打开新建数据库。清单记录生成版本、公开输入 SHA-256、数据库 SHA-256 和各表行数，用相同输入可以再次构建相同数据。

新增回归需要新的前置状态时，在 `SeedBuilder` 对应的配置或场景方法中明确补充，再用现有用例验证。保持断言所要求的真实业务路径；不依赖开发者机器上的存档或生成库二进制文件。
