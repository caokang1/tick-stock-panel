# 通达信公式扩展（community.tdx-formula）

把通达信的选股公式粘贴进来，拆成一个个可以勾选、可以调参数的条件，然后：

1. **信号统计**：历史上出过多少次信号、之后持有 1 / 3 / 5 / 10 天的胜率和平均收益（已扣交易成本），最新一个交易日有哪些股票满足条件。
2. **组合回测**：加上排名、买卖规则（最多持有几只、止盈、止损、最多持有几天）和大盘择时（指数在均线上方才出手），
   模拟一个账户按日运行，给出收益曲线、年化收益、最大回撤和每笔交易，并和指数对比。
3. **每日提醒**：把策略设为“运行中”，每天行情更新后自动推进一个模拟账户，告诉你下一个交易日该买什么、卖什么，
   并经 TSP 自己配置的飞书、企业微信机器人推送。
4. **实际持仓**：模拟账户假设每条提醒都照做。打开“记录实际持仓”后，你答复“已买入 / 没买 / 已卖出 / 没卖”并填上实际成交价，
   之后的卖出提醒和“还能买几只、买多少股”就按你真实的持仓和资金来算；没有答复的一律当作没有成交。

这是一个独立的扩展：不修改 TSP 的核心代码，不使用也不影响 TSP 自己的数据源、指标流水线、策略引擎和回测。
页面入口是侧栏的“通达信公式”（`/tdx-formula`）。

## 它和 TSP 自带功能的关系

| | 这个扩展 | TSP 自带 |
| :-- | :-- | :-- |
| 策略怎么写 | 通达信公式（`MA`、`REF`、`CROSS`、`EXIST`、`:=` 赋值……） | 条件信号、因子 DSL、Python 策略 |
| 数据 | quant-lab 下载到本机的全市场日线 | TSP 的数据源和 enriched 表 |
| 输出 | 信号统计、组合回测、每日买卖提醒、实际持仓 | 策略扫描、回测、监控、模拟盘 |
| 推送 | 用 TSP 设置里配置的飞书、企业微信机器人 | 同左 |

两边的数据和计算互不相通：这里的策略不进 TSP 的策略库，回测和模拟账户也是扩展自己的一套
（规则与 quant-lab 相同：次日开盘成交、T+1、开盘涨停买不进、开盘跌停或停牌卖不出顺延、退市按最后收盘价了结、
最低佣金、一手 100 股）。

## 行情数据：来自 quant-lab

扩展只读 quant-lab（本扩展作者的另一个项目，目前还没有公开）下载到本机的行情目录（`data/market`），不联网、不往那个目录里写任何东西。
在 quant-lab 公开、或者改成 TSP 的数据源插件之前，没有那份数据的人只能用“解析公式”，统计不可用。
那份数据全部来自免费的公开来源，带复权因子、历史上每天的 ST 状态、已退市的股票、流通股本和行业。

按下面的顺序找行情目录，找到第一个有数据的就用它：

1. 页面上“换目录”里设置的目录（保存在 `data/user_data/extensions/tdx_formula/config.json`）；
2. 环境变量 `TDX_FORMULA_DATA_DIR`；
3. 和本项目并排放着的 `../quant-lab/data`。

给的可以是 quant-lab 的 `data` 目录，也可以直接是其中的 `market` 目录。没有找到时页面会说明，解析公式仍然可用，统计不可用。

全市场十二年的数据读进内存后约占 2 到 3 GB，第一次读取要十几秒；之后留在内存里，quant-lab 更新了数据会自动重读。

## 每天的提醒是怎么产生的

这个扩展自己不下载行情。quant-lab 在收盘后更新它的行情目录；扩展每分钟看一眼那里的概况文件（`meta.json`），
发现行情到了新的一天、又有运行中的策略，就：先给自己的数据库留一份备份，再把每个运行中的策略推进到这一天、
生成提醒（打开了实际持仓的策略再按真实持仓生成一份），最后推送一次。同一个行情日期只推送一次。
也可以在页面上点“现在生成提醒”手动跑。所以要收到每日提醒，quant-lab 和 TSP 都得在运行。

## 数据放在哪里

扩展自己的数据在 `data/user_data/extensions/tdx_formula/`：

- `tdx.db`：策略、最近一次回测结果、模拟账户、提醒记录、实际持仓和成交、设置（SQLite）。
  其中模拟账户、提醒记录和实际持仓删了无法重建。
- `backups/tdx-日期.db`：每日任务开始之前自动备份，一天一份，保留最近 14 天。
- `config.json`：页面上设置的行情目录。

## 口径

- 后复权价格参与公式计算；涨跌停、成交价用不复权价格。
- 信号在收盘后确认，第二天开盘买入，持有 N 个交易日后收盘卖出。开盘涨停买不进的不计入。
- 交易成本：佣金万分之 2.5、卖出印花税万分之 5、滑点千分之 1。
- 不使用之后的数据：含未来函数（`REFX`、`ZIG`、`BACKSET` 等）的公式会被拒绝并说明原因。
- 停牌日占一行，长期停牌的股票复牌后几天的均线类指标会和通达信有出入。
- 公式里的 `V` 单位是股（通达信是手）；`CAPITAL` 是流通股本（股），`LTSZ` 是流通市值（元），`HSL` 是换手率（%）。

## 接口

都在 `/api/custom/tdx-formula/` 下面：

- 行情与公式：`GET status`、`PUT config`、`GET blocks`、`GET options`、`POST parse`、`POST stats`、`POST signals`、`POST picks`
- 策略：`GET/POST strategies`、`GET/PUT/DELETE strategies/{id}`、`POST strategies/{id}/start`、`POST strategies/{id}/stop`
- 回测：`POST backtest`、`GET strategies/{id}/backtest`
- 提醒与账户：`GET today`、`GET strategies/{id}/account`、`POST daily/run`
- 实际持仓：`GET/PUT strategies/{id}/real`、`POST strategies/{id}/real/skip-all`、`POST strategies/{id}/real/positions`、
  `POST real/todos/{id}`、`PUT/DELETE real/positions/{id}`、`POST real/positions/{id}/sell`
- 设置：`GET/PUT settings`（交易成本、模拟账户初始资金）

## 目录

- `routes.py`：接口，以及每分钟检查一次的后台线程。
- `service.py`：找行情目录、读全市场数据、解析公式、信号统计。
- `live_service.py`：策略、组合回测、每日任务、推送、实际持仓。
- `ql/`：从 quant-lab 带过来的计算内核（全市场日线的矩阵表示、通达信公式的解析与求值、条件拆分、信号统计、
  组合回测、模拟账户逐日推进、实际持仓），读 quant-lab 行情目录的 `loader.py`，以及扩展自己的数据库 `core/db.py`。
- 前端：`frontend/src/custom/tdx-formula/`。
- 测试：`backend/tests/test_tdx_formula_extension.py`、`frontend/src/custom/tdx-formula/logic.test.ts`。

## 只用到的核心能力

- `app.extensions` 的 `setup(registrar)` 和 `include_router`（版本化的扩展契约）。
- 路由自带的 lifespan，用来启动和停止后台线程（`paper_execution` 扩展用的是同一种做法）。
- `app.state.repo.store.data_dir`：只用来确定扩展自己的数据目录。
- `app.services.preferences` 里飞书、企业微信机器人的地址，和 `app.services.webhook_adapter` 的 `send_feishu`、`send_wecom`。

后三项不是版本化的扩展契约，上游升级后需要重新跑一遍本扩展的测试。
