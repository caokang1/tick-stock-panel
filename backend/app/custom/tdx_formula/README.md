# 通达信公式扩展（community.tdx-formula）

把通达信的选股公式粘贴进来，拆成一个个可以勾选、可以调参数的条件，在全市场历史日线上统计信号：
历史上出过多少次、之后持有 1 / 3 / 5 / 10 天的胜率和平均收益（已扣交易成本），以及最新一个交易日有哪些股票满足条件。

这是一个独立的扩展：不修改 TSP 的核心代码，不使用也不影响 TSP 自己的数据源、指标流水线、策略引擎和回测。
页面入口是侧栏的“通达信公式”（`/tdx-formula`）。

## 它和 TSP 自带功能的关系

| | 这个扩展 | TSP 自带 |
| :-- | :-- | :-- |
| 策略怎么写 | 通达信公式（`MA`、`REF`、`CROSS`、`EXIST`、`:=` 赋值……） | 条件信号、因子 DSL、Python 策略 |
| 数据 | quant-lab 下载到本机的全市场日线 | TSP 的数据源和 enriched 表 |
| 输出 | 信号统计、最新一天选出的股票、信号明细 | 策略扫描、回测、监控、模拟盘 |

目前两边的数据和计算互不相通：这里算出的条件还不能直接交给 TSP 的回测引擎。

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

## 口径

- 后复权价格参与公式计算；涨跌停、成交价用不复权价格。
- 信号在收盘后确认，第二天开盘买入，持有 N 个交易日后收盘卖出。开盘涨停买不进的不计入。
- 交易成本：佣金万分之 2.5、卖出印花税万分之 5、滑点千分之 1。
- 不使用之后的数据：含未来函数（`REFX`、`ZIG`、`BACKSET` 等）的公式会被拒绝并说明原因。
- 停牌日占一行，长期停牌的股票复牌后几天的均线类指标会和通达信有出入。
- 公式里的 `V` 单位是股（通达信是手）；`CAPITAL` 是流通股本（股），`LTSZ` 是流通市值（元），`HSL` 是换手率（%）。

## 接口

都在 `/api/custom/tdx-formula/` 下面：`GET status`、`PUT config`、`GET blocks`、`POST parse`、`POST stats`、
`POST signals`、`POST picks`、`GET/POST saved`、`DELETE saved/{id}`。

## 目录

- `routes.py`、`service.py`：接口和业务拼装。
- `ql/`：从 quant-lab 带过来的计算内核（全市场日线的矩阵表示、通达信公式的解析与求值、条件拆分、信号统计），
  以及读 quant-lab 行情目录的 `loader.py`。
- 前端：`frontend/src/custom/tdx-formula/`。
- 测试：`backend/tests/test_tdx_formula_extension.py`、`frontend/src/custom/tdx-formula/logic.test.ts`。

## 只用到的核心能力

`app.extensions` 的 `setup(registrar)` 和 `include_router`；`app.state.repo.store.data_dir`（只用来确定扩展自己的数据目录）。
后者不是版本化的扩展契约，上游升级后需要重新跑一遍本扩展的测试。
