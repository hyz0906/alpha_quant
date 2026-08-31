# AlphaQuant × Vibe-Trading 集成工作流

> 2026-08-30 集成版。本文档是完整工作流的权威说明：模块职责、调用顺序、
> 输入输出依赖、以及"需 API key 的组件被 vibe-trading 免费模块替代"的对照表。

## 0. 一图流

```
┌─────────────────────────────────────────────────────────────────────┐
│  python3 main.py run_all --codes 512480.SH,513100.SH,588000.SH     │
└─────────────────────────────────────────────────────────────────────┘
   │
   ├─(1) fetch_data ──► VibeMarketLoader ──► 子进程 broker (cwd=$HOME)
   │                      │                    └─ vibe fetch_market_data
   │                      │                       (tencent 链, 免token, qfq)
   │                      ├─► data/<code>.csv
   │                      └─► upsert alphaquant.db.market_data
   │
   ├─(2) calc_factors ─► RSRSCalculator(向量化, N=18, M=600)
   │                      ├─► market_data.rsrs_beta/r2/zscore 回写
   │                      └─► upsert factor_data
   │
   ├─(3) gen_signals ──► SignalGenerator.generate_rotation_signals
   │                      ├─► output/signals_YYYYMMDD.json (top_k 等权)
   │                      └─► (可选) RSRS+研报情感融合 [P3-03]
   │
   ├─(4) backtest ─────► VibeBacktestExporter
   │                      ├─► CSV 注入 vibe loader cache (tushare 槽位)
   │                      ├─► runs/<name>/config.json + code/signal_engine.py
   │                      └─► 子进程: python -m backtest.runner (cwd=$HOME)
   │                          ├─ 缓存全命中 → 不调 API、不需要 token
   │                          ├─ ChinaAEngine: T+1/整手/涨跌停/佣金
   │                          │   (ETF: stamp_tax=0)
   │                          └─► runs/<name>/ 回测结果
   │
   └─(5) monitor ──────► QDIICalculator (akshare) — 独立实时环节
```

## 1. 模块清单与职责

| 环节 | 模块 | 输入 | 输出 |
|---|---|---|---|
| 数据获取 | `src/data_engine/vibe_market_loader.py` + `scripts/vibe_fetch_broker.py` | codes, start, end | `data/<code>.csv`（date,open,high,low,close,volume）；`market_data` 表 |
| 因子计算 | `src/strategies/factors/rsrs.py` | `market_data` 行情 | `market_data.rsrs_*` 回写；`factor_data` 表 |
| 信号生成 | `src/strategies/signal_generator.py` | `factor_data`（+`report_sentiment` 可选） | `output/signals_*.json` |
| 策略回测 | `src/backtest/vibe_exporter.py`（+ vibe `backtest.runner`） | `data/*.csv`, 回测参数 | `runs/<name>/`（config、engine、结果） |
| 溢价监控 | `src/data_engine/qdii_calc.py` | 实时行情(akshare) | 日志告警 / Redis 频道 |
| 基本面分析（可选） | `src/llm_agent/analyzer.py` | 研报文本 | `report_sentiment` 表（缺省 0，不阻塞主链路） |

## 2. 调用顺序与依赖

```
fetch_data ──► calc_factors ──► gen_signals        # 生产链路（日频）
     └──────────────► backtest                      # 回测链路（复用同一份 CSV）
```

- `gen_signals` 依赖 `calc_factors` 的产出（factor_data 表）
- `backtest` 只依赖 `fetch_data` 的 CSV（信号由内联 signal_engine 重算，
  与 DB 无耦合，保证回测确定性）
- `monitor` 与链路无依赖，可常驻

## 3. 常用命令（WSL 内执行）

```bash
cd ~/workspace/alpha_quant

# 一步到位：拉数据 → 算因子 → 出信号 → 跑回测
python3 main.py run_all --codes 512480.SH,513100.SH,588000.SH \
    --start 2024-01-01 --start-fetch 2020-01-01 --top-k 2

# 分步执行
python3 main.py fetch_data   --codes 512480.SH,513100.SH --start 2020-01-01
python3 main.py calc_factors --codes 512480.SH,513100.SH -n 18 -m 600
python3 main.py gen_signals  --top-k 2 --fusion
python3 main.py backtest     --name my_run --codes 512480.SH,513100.SH,588000.SH \
                             --start 2024-01-01 --no-fetch   # 复用现有 csv
python3 main.py backtest     --name my_run2 --strategy rsrs_timing ...  # 换策略模板

# 滑动窗口回测（多策略 × 多窗口对比，覆盖不同市场周期）
python3 main.py sliding_backtest --name sliding_20260831 \
    --start 2023-01-01 --end 2026-06-30 \
    --window-months 6 --step-months 6 \
    --strategies rsrs_rotation,rsrs_timing,equal_weight
# 结果: runs/<name>/summary.md + summary.json + 每窗每策略一个子 run 目录

# QDII 溢价监控（独立）
python3 main.py monitor
```

### 3.2 滑动窗口回测（sliding_backtest）

**动机**：单段回测（如 2024-01~2026-06 全牛市区间）无法区分策略 alpha 与市场 beta。
把评估期切成若干 6 个月窗口，逐窗跑同一批策略并横向对比，才能看出策略在
震荡/下跌/上涨不同周期里的稳定性。

**实现**（`src/backtest/sliding_window.py`）：

1. `build_windows` 按自然月切窗（默认 6 个月窗、6 个月步长，可重叠）。
2. **全局一次性拉数**：先按「首窗起点 − warmup_years(默认3年) ~ 末窗终点」
   确保三只标的的 csv 齐全（避免每个窗口各自调 broker 覆写同一 csv 反复重拉），
   已有 csv 首末日期覆盖需求（含 10 天容差，避开元旦等假日首日无 K 线）则跳过。
3. 逐窗口 × 逐策略实例化 `VibeBacktestExporter` 跑 vibe 回测，
   读取各自 `evaluation_metrics.json`（窗口口径指标）。
4. 聚合输出 `runs/<name>/summary.md`（三张表：各窗总收益 / 各窗超额 / 策略聚合）
   与 `summary.json`（机器可读全量），失败 run 记入 `failed_runs`。

**策略注册表**（`vibe_exporter.STRATEGY_TEMPLATES`，`--strategy` 选择）：

| 策略 | 逻辑 | 角色 |
|---|---|---|
| `rsrs_rotation` | 截面轮动：RSRS 修正分 top_k 等权，低于 min_score 空仓，日频调仓 | 主动策略 |
| `rsrs_rotation_weekly` | 同上，但每周首个交易日才调仓，权重持有整周 | 主动策略（低换手） |
| `rsrs_timing` | 单标的滞回择时：分 > +0.7 满仓、< −0.7 空仓，区间持有 | 主动策略（少换手） |
| `equal_weight` | 全部标的等权买入持有 | **被动基准的实盘化** |

`equal_weight` 有两个用途：一是作为「什么都不做」的对照组；二是校验基准口径——
`write_evaluation_metrics` 自算的 benchmark 就是窗口内等权买入持有，
所以 equal_weight 的超额应 ≈ 0（实测各窗 ±0.6% 以内，差异来自手续费），
若显著偏离说明基准或计费出了 bug。

新增策略 = 在 `STRATEGY_TEMPLATES` 里加一个信号引擎模板（vibe AST 沙箱限制：
只能用白名单语法与 pandas/numpy），无需改其他代码。

### 3.1 从 Windows 侧驱动 WSL（免切终端）

本项目的代码与依赖都在 WSL 里，但可以在 Windows 的 Git Bash 中直接驱动：

```bash
export MSYS_NO_PATHCONV=1            # 防止 Git Bash 把 /mnt/... 转成 C:/...
wsl.exe -- bash -c 'cd ~/workspace/alpha_quant && python3 main.py run_all --codes 512480.SH,513100.SH'
```

也可执行 Windows 盘上的脚本（WSL 把 C/D/I 盘挂在 `/mnt/c` 等）：

```bash
wsl.exe -- bash -c "/mnt/c/wb/task.sh"     # 退出码原样传回
```

**可靠性排序**（引号越多越容易踩坑）：

| 方式 | 适用场景 | 坑 |
|---|---|---|
| **写文件 → 执行文件**（推荐） | 多行 Python/复杂脚本 | 用 Write 工具写到 `\\wsl.localhost\...\home\hyz0906\xxx.py` 再 `python3 ~/xxx.py`，零转义问题 |
| `bash -c '...'`（单引号） | 短命令、含 `&&` | 内层双引号需写成 `\"`；**PowerShell 5.1 会吃掉引号，别用** |
| `bash -c "/mnt/c/x.sh"` | 现成脚本 | 路径用正斜杠；PowerShell 传参会吞反斜杠 |
| heredoc（`<<'EOF'`） | — | ❌ 经 wsl.exe 传递时转义会被破坏，实测报 `unexpected character after line continuation` |

其他注意点：

- 中文输出正常，退出码原样传递
- stderr 常出现 `wsl: Failed to translate '\Program Files\WorkBuddy\...'`，
  是 WorkBuddy 的 shim 目录触发的路径转换警告，**无害**，可用
  `2>&1 \| grep -v "Failed to translate"` 过滤
- 默认 cwd 不可依赖，命令里显式 `cd ~/workspace/alpha_quant`
- 单次命令建议包 `timeout 600`，避免网络卡顿挂死

## 4. 替代方案对照（去 API key 化）

| 原组件 | 依赖 | 替代为 | 依据 |
|---|---|---|---|
| `TushareLoader` | TUSHARE_TOKEN | vibe `fetch_market_data`（tencent 链首，免 token，前复权 qfq） | 回退链 tencent→mootdx→eastmoney→baostock→akshare→tushare，任一可用即出数 |
| `BacktestEngine`（Backtrader） | backtrader 包（未安装） | vibe `backtest.runner` + `ChinaAEngine` | ChinaAEngine 内置 T+1、100 股整手、涨跌停、佣金；ETF 免印花税须显式 `stamp_tax=0`（已内置） |
| 研报爬虫 `crawler.py` | DeepSeek key（保留） | 可选 `mx-finance-search` skill 或人工投喂 | 情感分数缺省 0，主链路不阻塞 |

### 4.1 免 token 回测的实现机制（loader cache 注入）

vibe runner 的引擎路由按 `source` 字段选择执行规则：只有
`source ∈ {tushare, akshare}` 才路由到 ChinaAEngine；`local` 会错误地落到
CryptoEngine。因此本工作流：

1. config 声明 `source=CACHE_SOURCE`（**值为 `akshare`，不是 tushare**，原因见
   §6.6），只为拿到 A 股交易规则引擎
2. 但数据不走 akshare API：`VibeBacktestExporter.inject_cache()` 把本地 CSV
   按 vibe 的缓存格式（sha256 content-addressed parquet + metadata json，
   version=4）写入 `data/vibe_cache/akshare/<key>.parquet`
3. runner 子进程设置 `VIBE_TRADING_DATA_CACHE=true` +
   `VIBE_TRADING_DATA_CACHE_ROOT=data/vibe_cache`，akshare loader 的
   `cached_loader_fetch` 在触碰 API 前先查缓存——全命中，token 永远不需要
4. 缓存 key 已与 vibe 原版 `make_loader_cache_key` 做逐字节一致性验证

**关键**：缓存 key 的日期区间必须与 config 的 `[start_date, end_date]` 一致
（否则 runner 查不到），但**缓存帧的内容可以更长**——
`cached_loader_fetch` 命中后直接 `return cached`，不做日期裁剪。正是这一点
让 RSRS 能在评估首日就拿到 M=600 的 warmup 窗口（见 §6.8）。

### 4.2 src 包名冲突的子进程隔离

vibe-trading 安装的顶层包 `src`/`backtest` 与本项目 `src` 同名。所有
vibe 子进程（broker、runner）均以 **cwd=$HOME** 运行，此时 `import src`
唯一命中 site-packages 的 vibe 版本。本项目 `runs/` 通过
`VIBE_TRADING_ALLOWED_RUN_ROOTS` 环境变量加入 runner 的运行目录白名单。

## 5. 关键参数备忘

| 参数 | 默认 | 说明 |
|---|---|---|
| `-n` | 18 | RSRS 回归窗口（Design P1-03） |
| `-m` | 600 | beta 标准化窗口；min_periods=60（历史不足时渐进生效） |
| `--top-k` | 2 | 轮动持仓数（等权 1/k） |
| `--min-score` | 0.0 | 入选最低 RSRS 修正分 |
| `stamp_tax` | 0 | **ETF 免印花税**（ChinaAEngine 默认 0.0005 仅适用股票） |
| `commission_rate` | 0.00025 | 佣金万 2.5，最低 5 元 |
| 数据缓冲 | 评估起点前 3 年 | RSRS M=600 lookback 所需 warmup |

## 6. 已修复的历史 bug（本次集成中发现）

1. **RSRS 索引对齐 bug**（`factors/rsrs.py`）：`beta_series` 原以默认
   RangeIndex 创建，与 DatetimeIndex 的 DataFrame 做列赋值/乘法时索引
   无法对齐，导致 `rsrs_zscore`/`rsrs_score` **整列静默 NaN**——即原实现在
   任何真实（日期索引）数据上都不产生信号。已修复并加注释。
2. **`rsrs_zscore` 语义分裂**：`factors/rsrs.py` 存 raw z、
   `rsrs_momentum.py` 存修正分 z×R²。已统一为修正分（RSRS-Right 标准），
   `rsrs_momentum.py` 改为薄别名。
3. **测试构造签名不匹配**：`tests/test_rsrs.py` 以 `RSRSCalculator(n=, m=)`
   构造，canonical 类原不接受参数，测试从未跑通过。已对齐构造签名，
   `tests/test_rsrs.py` 2/2 通过。
4. **main.py 顶部 `import redis`**：redis 包未安装导致任何 CLI 命令直接
   ImportError。已改为 monitor 命令内惰性导入。

以下 5–9 项在 WSL 端到端首跑中发现并修复（均为 vibe 侧或集成侧的坑）：

5. **`--codes` 参数重复注册**（`main.py`）：`common_factor_args` 内含 `--codes`，
   同时 `backtest`/`run_all` 子命令又各自注册了一次，argparse 抛
   `ArgumentError: conflicting option string`。已拆成只含 `-n/-m` 的
   `rsrs_window_args`，`--codes` 由各子命令自行注册。
6. **缓存通道不能用 tushare**（`vibe_exporter.py`）：vibe 的 registry 只把
   `is_available()` 为真的 source 纳入回退链，而 `tushare.is_available()`
   要求配置 TUSHARE_TOKEN。无 token 时 tushare 被直接跳过（日志
   `tushare is unavailable, falling back to akshare`），注入的缓存永远读不到，
   引擎真的去调 akshare 拿**不复权**数据——实测 512480 同日收盘
   0.352（前复权）vs 0.703（不复权），volume 差 100 倍。改为注入
   `akshare` 通道（`is_available()` 仅检查包是否安装，恒真），
   数据本身仍是 tencent 前复权。
7. **`position_adjustment` 必须填 `hold`**（原为 `rebalance`）：rebalance 模式
   每天把持仓拉回目标权重并对整篮做严格资金预检
   （`projected_capital < -1e-9` 即抛 `insufficient capital for position
   rebalance`）。ETF 轮动遇到 T+1 时当日买入的仓位无法卖出
   （`can_execute=False` → reductions 为空），新标的开仓成本仍在，必然资金
   为负而崩溃。hold 模式只在方向变化时平仓重开，且买不起时按统一比例缩放
   篮子。
8. **`evaluation_start_date` 在 0.1.14 不存在**：该字段属于
   `~/source/Vibe-Trading` 的新版源码，已安装的 0.1.14 schema 里没有，
   因 `model_config = ConfigDict(extra="allow")` 而被**静默忽略**。后果是
   回测从 `start_date`（含 warmup）起就交易，RSRS 窗口不足时信号失真——
   实测 2022 年产生 **-42.58%** 的污染收益，把总收益从 +182.78% 拉低到
   +70.81%。解法两层：① config 的 `start_date` 直接取评估起点；
   ② signal_engine 加 `EVAL_START` 门槛，早于该日的权重一律置 0（warmup
   空仓）。修复后 2022/2023 收益归零。
9. **`PROJECT_ROOT` 层级算错**：`vibe_exporter.py` 与 `signal_generator.py`
   用了 `parents[3]`（文件实际在 `src/backtest/`、`src/strategies/`，应为
   `parents[2]`），导致数据目录解析成 `workspace/data/` 而非
   `workspace/alpha_quant/data/`。前者直接触发 FileNotFoundError，
   后者是未被引用的死代码（故一直没暴露）。
10. **vibe tencent loader 的 500 根静默截断**（vibe 侧 bug，本项目已在
    broker 层绕开）：腾讯 `fqkline` 接口单次最多 500 根，且**窗口内 K 线数
    超过 500 时返回的是「end_date 往前 500 根」**（末尾对齐），并非从
    start_date 起算。vibe 的 `_fetch_one` 分页却假设首页从 start_date 开始，
    翻页时 `next_start > end_date` 立即 break，于是任何 >500 根的请求都静默
    退化为最近 500 根。实测：请求 2024-01-01~2026-06-30 返回 500 根且
    first=2024-06-06；`count` 传 >500 则直接返回空。
    另测：1 年窗口（242 根）正常从头返回。
    **绕法**：`scripts/vibe_fetch_broker.py` 按 `--slice-days`（默认 365 天）
    把区间切成多段分别请求后拼接去重，实测 2021-2026 可拿到 1327 根。
    RSRS 的 M=600 必须有这个绕法，否则数据永远只有 500 根。
11. **`fetch_market_data` 返回 `list[dict]` 而非 DataFrame**，且日期字段名为
    `trade_date`（不是 `date`）。broker 里 `coerce_frame()` 负责归一化
    （兼容 list[dict] / DataFrame / cap_rows 截断后的 dict 三种形态）。

## 7. 验证情况

### 7.1 静态验证（Windows 侧）

- [x] 全部新/改文件 py_compile 通过
- [x] RSRS beta/r2/zscore 与 polyfit/corrcoef ground truth 误差 < 1e-9
- [x] 缓存 key 与 vibe 原版实现逐字节一致（含日期 normalize 边界）
- [x] signal_engine 模板通过 AST 沙箱规则静态检查（无顶层可执行语句、
      类体仅常量与方法、无 eval/exec/open/subprocess）
- [x] `tests/test_rsrs.py` 2/2 通过

### 7.2 WSL 端到端实跑（已通过）

`run_all` 四阶段全绿，样本 `512480.SH / 513100.SH / 588000.SH`，
数据 2021-01-04 ~ 2026-06-30（1370 根/标的）：

| 阶段 | 结果 |
|---|---|
| `fetch_data` | 6 段拼接，3 标的各 1370/1370/1371 行，全部 `via tencent`（前复权） |
| `calc_factors` | 每标的 754~755 个有效 RSRS 点（符合 1370−600−18+2） |
| `gen_signals` | 输出 `output/signals_YYYYMMDD.json`（截面排名 + top_k 持仓） |
| `backtest` | ChinaAEngine 跑完，产出 equity/fills/trades/risk_xray 等 13 个 artifact |

**评估窗口指标**（`runs/<name>/evaluation_metrics.json`，按 `eval_start` 切片重算）：

| 指标 | 值 | 说明 |
|---|---|---|
| 区间 | 2024-01-02 ~ 2026-06-30 | 601 bars ≈ 2.38 年 |
| 总收益 | **+182.78%** | 10 万 → 28.28 万 |
| 年化收益 | **+54.63%** | 全区间口径只有 21.81%（被 2 年 warmup 空仓稀释） |
| 夏普 | **1.52** | 全区间口径 1.02 |
| 最大回撤 | −27.33% | Calmar 2.00 |
| 基准收益 | +176.59% | 超额仅 **+6.20%** |

⚠️ **诚实结论**：年化 54.63% 看着漂亮，但同期买入持有三个 ETF 的基准也有
+176.59%，**RSRS 轮动的超额只有 +6.20%**。这套参数（top_k=2、min_score=0、
每日截面轮动）基本没有战胜简单持有，只是因为 2024-2026 这段本身是大牛市。
要判断策略有效性，需换更长的评估窗口、加入熊市样本、并对照等权基准做
显著性检验（vibe 自带 `monte_carlo_test` / `bootstrap_sharpe_ci` 可用）。

### 7.4 滑动窗口回测（7 窗 × 3 策略，已通过）

`sliding_backtest --name sliding_20260831`（2023-01~2026-06，6 个月窗 × 7 段，
21/21 run 成功，结果 `runs/sliding_20260831/summary.md`）：

| 窗口 | 区间 | rsrs_rotation | rsrs_timing | equal_weight |
|---|---|---|---|---|
| 01 | 2023H1 | +7.17% | +4.88% | +15.53% |
| 02 | 2023H2 | +1.26% | −5.51% | −5.72% |
| 03 | 2024H1 | +0.21% | +3.23% | −0.78% |
| 04 | 2024H2 | +17.61% | +13.70% | +31.29% |
| 05 | 2025H1 | −12.60% | +0.45% | +4.12% |
| 06 | 2025H2 | +37.42% | +22.07% | +31.13% |
| 07 | 2026H1 | +86.66% | +49.68% | +55.79% |

| 策略 | 跨窗复利 | 最差单窗 | 平均夏普 | 最深回撤 | 平均超额 | 超额胜率 |
|---|---|---|---|---|---|---|
| rsrs_rotation | +186.74% | −12.60% | 1.04 | −27.34% | +0.92% | 57% |
| rsrs_timing | +113.49% | −5.51% | 0.99 | −12.22% | −6.12% | 29% |
| equal_weight | **+201.80%** | −5.72% | **1.23** | −18.74% | +0.01% | 43% |

⚠️ **结论（与 §7.2 互相印证，且更扎心）**：
1. **被动等权（+201.80%、夏普 1.23）跑赢两个主动策略**——三年半里「什么都不做」
   是最优解，RSRS 轮动只是在 w07（2026H1）一波大幅反超（+86.66% vs +55.79%），
   但 w04/w05 又大幅落后，择时版全程负超额（−6.12%、胜率 29%）。
2. 轮动的价值体现在**回撤与波动 trade-off** 上：最深回撤 −27.34% 确实比
   equal_weight 的 −18.74% 更差，并没有换来风控优势。
3. 当前参数下 RSRS 策略**未证明有效**。下一步见 §9。

**本次发现的第 12 个 bug**（已修）：`write_evaluation_metrics` 原本只裁
`eval_start` 不裁 `eval_end`，而 vibe 的回测轴是注入帧全长（2020→2026），
导致「窗口收益」实际算成「窗口起点→帧末」（w01 曾报 +187%）。现双边裁剪，
并自算窗口内等权买入持有基准替代引擎的全帧内置基准（equal_weight 各窗
超额 ≈0 验证了口径正确）。

### 7.5 参数扫描（4 组调参 × 7 窗，已通过）

默认参数跑输等权后，用同一框架扫了 4 组调参（结果
`runs/param_sweep_20260831.md`，按跨窗复利降序）：

| 配置 | 参数 | 跨窗复利 | 最差单窗 | 平均夏普 | 最深回撤 | 平均超额 | 超额胜率 |
|---|---|---|---|---|---|---|---|
| k1_ms0 | top_k=1, ms=0, 日频 | **+316.34%** | −10.47% | 1.45 | −24.94% | +8.48% | 57% |
| weekly_k2_ms05 | top_k=2, ms=0.5, **周频** | +298.17% | **−3.88%** | **1.51** | −20.79% | +5.16% | **71%** |
| k1_ms05 | top_k=1, ms=0.5, 日频 | +241.39% | −16.91% | 1.13 | −20.46% | +4.28% | 43% |
| 被动等权 | 对照 | +201.80% | −5.72% | 1.23 | −18.74% | +0.01% | 43% |
| baseline | top_k=2, ms=0, 日频 | +186.74% | −12.60% | 1.04 | −27.34% | +0.92% | 57% |
| k2_ms05 | top_k=2, ms=0.5, 日频 | +162.46% | −15.80% | 0.85 | −20.46% | −1.29% | 57% |
| timing | 滞回择时 ±0.7 | +113.49% | −5.51% | 0.99 | −12.22% | −6.12% | 29% |

**结论（部分翻案）**：
1. **调参后 RSRS 轮动能跑赢被动等权**——top_k=1 集中持仓（+316%）与
   周频调仓（+298%）都明显超过等权的 +202%，默认参数（k2/ms0/日频）
   恰恰是全场最差之一。
2. **周频版综合质量最高**：夏普 1.51 全场第一、最差单窗仅 −3.88%、
   超额胜率 71%（7 窗 5 正）。降换手 + min_score 空仓过滤确实有效。
3. top_k=1 收益最高但回撤 −24.94%，w07 单窗 +112% 说明其收益高度依赖
   牛市爆发期押中单一最强标的，稳健性不如周频版。
4. ⚠️ **过拟合警告**：扫参是在同一批数据上挑最优参数，存在选择偏差；
   且样本只有 7 个窗口、3 只 ETF、2023 后全牛市。最优参数样本外未必
   保持——下结论前必须做显著性检验（§9）。

**新增策略模板** `rsrs_rotation_weekly`：由日频模板派生（import 时 assert
锚点唯一，防两份 RSRS 核心漂移），每周首个交易日调仓、目标权重持有整周。

### 7.6 显著性检验（vibe validation 三件套 + 配对检验，已通过）

对扫参最优的两个配置 + 等权对照跑全区间（2023-01-03~2026-06-30）检验
（`runs/validation_20260831.md`，权益曲线按评估窗口切片）：

| 策略 | 夏普 | Bootstrap 95% CI | P(夏普>0) | MC p(夏普) | WF 一致性 |
|---|---|---|---|---|---|
| weekly_k2_ms05 | **1.70** | [0.67, 2.58] | 99.9% | 0.161 | 71% |
| k1_ms0 | 1.46 | [0.43, 2.37] | 99.7% | 0.758 | 86% |
| equal_weight | 1.48 | [0.35, 2.58] | 99.7% | 0.644 | 71% |

滑动窗口超额配对检验（n=7，基准=窗口等权买入持有）：

| 策略 | 平均超额 | 正超额窗 | t 统计量 | 显著性 |
|---|---|---|---|---|
| weekly_k2_ms05 | +5.16% | 5/7 | 1.85 | **p<0.10** |
| k1_ms0 | +8.48% | 4/7 | 0.93 | 不显著 |
| baseline_k2_ms0 | +0.92% | 4/7 | 0.15 | 不显著 |
| timing | −6.12% | 2/7 | −2.21 | **显著为负 (p<0.05)** |

⚠️ **诚实判读**：
1. **周频版是唯一有边际统计信号的主动策略**：Sharpe 1.70 vs 等权 1.48，
   超额 t=1.85（p<0.10）。但其 Bootstrap CI [0.67, 2.58] 包含等权的 1.48，
   **无法在 95% 置信下宣称显著优于等权**——结论是「有积极迹象，未达显著」。
2. **k1 的高收益是波动堆出来的**：+317% 总收益但 Sharpe 与等权持平（1.46
   vs 1.48），超额 t=0.93 不显著——押注单一标的的方差太大。
3. **timing 显著为负（t=−2.21），正式淘汰。**
4. MC p 值普遍不显著：交易顺序对结果影响不大（少交易策略该检验功效弱）。
5. 根本约束仍是样本：3.5 年 / 3 只 ETF / 全牛市，n=7 的配对检验功效很低。
   要真正下结论需要更长历史 + 更大候选池（§9）。

### 7.7 扩展池验证（12 只 × 13 窗，翻案 §7.6）

按 §7.6 的约束把候选池从 3 只扩到 12 只、数据前推到 2017-09（覆盖 2018 熊市
与 2021-2022 下跌），重跑滑动窗口 + 参数扫描 + 显著性检验
（`runs/pool12_review_20260831.md`、`runs/pool12_validation_20260831.md`）。

**候选池**（D 池去 512000 券商，规避与 512880 证券 ρ=0.994 的重复计价）：
510300/510500/159915（宽基）、512010/159928/512880/512660/512400（行业）、
513100/513500/513050（QDII）、518880（黄金）。共同起点 2017-09，13 个可用半年窗。

**滑动窗口横评（13 窗 × 各配置）——结论反转**：

| 配置 | 跨窗复利 | 平均超额 | 超额胜率 | 最差单窗 |
|---|---|---|---|---|
| equal_weight（基准） | **+66.48%** | −0.15% | 38% | −10.56% |
| weekly k4/ms0.0 | −5.44% | −4.27% | 23% | −16.79% |
| weekly k2/ms0.5 | −6.04% | −4.00% | 31% | −18.22% |
| weekly k6/ms0.5 | −16.75% | −5.04% | 23% | −18.54% |
| weekly k8/ms0.5 | −19.01% | −5.31% | 23% | −17.98% |
| weekly k4/ms0.5 | −25.65% | −5.79% | 31% | −21.91% |
| 日频 rotation k2/ms0.5 | −32.64% | −6.63% | 8% | −20.49% |

**显著性检验 + 成本归因（全区间 2020-01-02~2026-06-30，权益曲线切片）**：

| 策略 | 净收益 | 毛收益 | 手续费 | 成本侵蚀 | 夏普 | Bootstrap 95% CI | 超额 t(n=13) |
|---|---|---|---|---|---|---|---|
| equal_weight | +77.63% | +77.76% | ¥125 | 0.13% | 0.599 | [−0.18, 1.41] | −1.59 |
| weekly k2/ms0.5 | −3.15% | +3.54% | ¥6689 | **6.69%** | 0.092 | [−0.70, 0.89] | −1.64 |
| 日频 rotation | — | — | — | — | — | — | **−3.00 (p<0.025 显著为负)** |

⚠️ **结论（正式翻案 §7.6）**：
1. **扩展池上，所有 RSRS 主动策略的窗口超额均为负**——§7.6 的「周频版唯一
   保留候选」是 3 只 ETF / 7 窗纯牛市的**选择偏差**，样本一扩大即失效。
2. **周频版毛收益 +3.54% 是正的，但被 6.69% 手续费吃成 −3.15%**：284 笔交易、
   265 次调仓、总换手 152.88，成本是扩展池上主动策略的头号杀手。等权只花
   ¥125（0.13%）。
3. **日频 rotation 显著为负（t=−3.00，p<0.025）**，正式淘汰。
4. 等权被动组合是唯一正收益项（+77.63% 净 / 0.599 夏普），但其超额 t=−1.59
   不显著（它就是基准本身，超额 ≈ −手续费）。
5. 成本归因揭示：**RSRS 在扩展池上不是「信号失效」单点问题，而是「信号偏弱 +
   换手过高」叠加**——信号毛收益 +3.54% 尚可，但周频调仓频率在 12 只池上
   制造了 6.7% 的年化级成本拖累。下一步应优先降频/加空仓门槛（§9）。

### 7.8 降频验证（月频调仓，推翻「降频可救」假设）

针对 §7.7 结论 5 的「降频压换手」方向，新增 `rsrs_rotation_monthly` 模板
（周频模板派生，仅 `to_period("W")→"M"`），在 12 只池上重跑 13 窗滑动窗口
（`runs/sliding_pool12_monthly/`），并与周频做逐窗成本归因
（`scripts/compare_cost.py`）。

**换手成本对比（13 窗累计，每窗 10 万本金独立）**：

| 配置 | 累计手续费 | 累计单边换手 | 平均每窗调仓 | 跨窗毛收益 | 跨窗净收益 |
|---|---|---|---|---|---|
| 周频 k2/ms0.5 | ¥50912 | 1001 倍 | 156 次 | **+56.63%** | −6.04% |
| 月频 k2/ms0.5 | ¥17520 | 345 倍 | 54 次 | +11.29% | −6.84% |

**滑动窗口横评（月频 vs 等权）**：

| 配置 | 跨窗复利 | 平均超额 | 超额胜率 | 最差单窗 |
|---|---|---|---|---|
| equal_weight（基准） | **+66.48%** | −0.15% | 38% | −10.56% |
| 月频 k2/ms0.5 | −6.84% | −3.77% | 31% | −25.33% |

⚠️ **结论（否定「降频即可救」）**：
1. **降频确实把成本砍了 2/3**：手续费 ¥50912→¥17520、换手 1001→345 倍、
   调仓 156→54 次/窗——月频在成本端完全奏效。
2. **但净收益没改善，反而略差**：−6.04% → −6.84%。因为**毛收益同步崩塌**
   +56.63% → +11.29%——RSRS 是动量型信号，其 alpha 高度依赖调仓频率，
   降频在省成本的同时把信号也钝化了。
3. **决定性事实**：周频毛收益 +56.63% 已是所有主动配置的天花板，仍**低于**
   等权 +66.48%（等权几乎零成本）。即「即使零手续费，RSRS 轮动也跑不赢
   被动等权」——**信号 alpha 本身不足，与成本无关**。
4. 至此 RSRS 截面轮动在扩展池上被**彻底证伪**：三档频率（日/周/月）、
   五档 top_k（1/2/4/6/8）、多档 min_score 全部跑输等权。§9 据此收口。

### 7.9 因子有效性诊断（截面 IC + 分层收益，定位失效根因）

针对 §7.8「信号 alpha 本身不足」的结论，用标准因子检验定位根因
（`scripts/factor_diagnosis.py`，扩展池 12 只、共同样本 2017-09 起、2700+ 观测）：

**截面 IC（Spearman rank IC，RSRS 修正分 vs 未来 k 日收益）**：

| 未来收益 | IC 均值 | ICIR | IC 标准差 | 正 IC 占比 |
|---|---|---|---|---|
| 5 日 | −0.0132 | −0.035 | 0.376 | 49.5% |
| 10 日 | −0.0186 | −0.049 | 0.381 | 49.0% |
| 20 日 | −0.0109 | −0.028 | 0.388 | 50.0% |

**分层收益（每交易日按修正分 5 分组，各组未来收益均值）**：

| 未来收益 | Q1(低) | Q2 | Q3 | Q4 | Q5(高) | 多空 Q5−Q1 | 单调？ |
|---|---|---|---|---|---|---|---|
| 5 日 | +0.14% | +0.20% | +0.18% | +0.19% | +0.18% | +0.04% | 否 |
| 10 日 | +0.23% | +0.42% | +0.43% | +0.38% | +0.34% | +0.11% | 否 |
| 20 日 | +0.58% | +0.90% | +0.73% | +0.77% | +0.48% | **−0.09%** | 否 |

⚠️ **结论（定位到根因：因子失效，非池子同质化）**：
1. **IC 均值全为负且近零**（−0.01 ~ −0.02），ICIR 绝对值 < 0.05——学界惯例
   |ICIR|>0.5 才视为可用因子，此处**低一个数量级**，截面无预测力。
2. **正 IC 占比 ≈ 50%**——与抛硬币无异，RSRS 修正分对未来收益方向**零区分度**。
3. **分层收益完全不单调**，多空收益在 ±0.1% 内抖动、20 日甚至为负——
   高分标的不比低分标的涨得多。排除了「池子同质化稀释信号」的假设：
   若因子有效只是被同质化稀释，至少应保留弱单调性；实际是**纯噪声**。
4. **根因链闭合**：RSRS（N=18/M=600 的 z×R2）在扩展池上无截面 alpha →
   毛收益天花板 +56.63% 纯属 beta（追涨赶上牛市）→ 叠加换手成本必然跑输等权。
5. **最终判定**：RSRS 截面轮动作为 AlphaQuant 的核心主动策略，**证伪成立**。
   被动等权是当前唯一站得住的基线；RSRS 仅可保留作为「上升趋势强弱」的
   描述性指标，不具选股/轮动决策价值。

### 7.10 新因子批量筛选（16 个价量因子全灭，证伪收口）

为回答 §9「换新因子」，新增因子库 `src/strategies/factors/factor_library.py`
（动量/波动/趋势/量价/风险调整 5 族 16 因子）与批量筛选脚本
`scripts/factor_screening.py`，在扩展池 12 只上跑与 §7.9 相同的截面
IC + ICIR + 分层收益三件套（向量化实现，16 因子 × 3 视界 × 120 相关对
约 3 秒跑完）。判定门槛：强通过 |ICIR|≥0.5、弱通过 |ICIR|≥0.3，且正 IC
占比须落在 [45%,55%] 之外。

**因子排名（按 best |ICIR| 降序，完整表见 `runs/factor_screening_20260831.md`）**：

| 因子 | 族 | bestH | bestICIR | 正IC% | 多空20d | 判定 |
|---|---|---|---|---|---|---|
| max_dd_60 | 波动 | 20 | **+0.238** | 61.5% | +1.25% | FAIL |
| dollar_vol_20 | 量价 | 20 | −0.214 | 42.5% | −0.94% | FAIL |
| downside_vol_60 | 波动 | 20 | −0.195 | 41.3% | −0.90% | FAIL |
| vol_60 | 波动 | 20 | −0.194 | 40.1% | −1.18% | FAIL |
| mom_240_20 | 动量 | 20 | +0.172 | 57.8% | +0.72% | FAIL |
| vol_20 | 波动 | 20 | −0.135 | 43.2% | −0.90% | FAIL |
| pv_corr_20 | 量价 | 20 | −0.093 | 45.6% | −0.29% | FAIL |
| 其余 9 因子 | — | — | \|ICIR\|<0.07 | ≈50% | ≈±0.4% | FAIL |

⚠️ **结论（16/16 全灭）**：
1. **无一因子通过门槛**。最高 |ICIR| = 0.238（max_dd_60），仍低于弱通过
   0.3，更遑论 0.5。动量族（mom_*）与趋势族（ma_*）ICIR 普遍 < 0.1，
   与 RSRS（ICIR −0.03）同量级，均为纯噪声。
2. **相对最强的是「波动/风险族」与「流动性族」**：vol_60 / downside_vol_60 /
   dollar_vol_20 的负 IC（低波/小盘溢价方向）勉强可见但极弱，且正 IC 占比
   仅 40~43%、分层不单调——不足支撑轮动决策。
3. **经典 12-1 动量（mom_240_20）在 A 股 ETF 池上失效**（ICIR +0.172，
   多空 +0.72% 且不单调），与学界「A 股短周期反转、长周期动量弱」的结论
   一致。
4. **根因收敛**：结合 §7.9 与本次 16 因子全灭，可确定**问题不在某个因子
   构造，而在池子本身**——12 只 ETF 中 10 只是高度相关的权益类（PC1=57.5%），
   截面价量差异不足以产生可交易的截面 alpha。§7.9 曾「排除同质化假设」，
   本次 16 因子系统性失效后须**修正该结论**：RSRS 是纯噪声固然成立，但
   更底层的约束是**候选池同质化**——在一个近似单一风险因子的池子里，
   任何截面价量因子都注定失效。

### 7.3 数据正确性核验

引擎实际使用的数据（`runs/<name>/artifacts/ohlcv_*.csv`）与注入的 tencent
前复权 CSV **逐字节一致**（如 512480 于 2024-06-06：
`0.358,0.360,0.351,0.352,21251410.0`）。这一项必须每次首跑都核——§6.6 的坑
会让引擎悄悄改用不复权数据，且回测**不会报错**，只是结果全错。

## 8. 边界与已知限制

- 回测评估终点必须**早于今天**（vibe 缓存只对已完结日期区间生效）
- **vibe 0.1.14 无 `evaluation_start_date`**：评估窗口只能靠 signal_engine 的
  `EVAL_START` 门槛实现；vibe 自家指标仍是全区间口径，看
  `evaluation_metrics.json` 才是评估窗口的真实表现
- **回测日期轴由 data_map 决定**，不是 config 的 `start_date`——注入的缓存帧
  有多长，回测就从哪天开始（这也是 warmup 能生效的原因，见 §4.1）
- 数据源不可混用：tencent 前复权 vs akshare 不复权，同一标的同日收盘价可差
  2 倍以上。全链路固定走 tencent，akshare 仅作缓存投递通道
- vibetrading 的 `~/source/Vibe-Trading` 源码版本**新于**已安装的 0.1.14，
  查文档/源码时以 site-packages 里的实际文件为准（行号都对不上）
- QDII 影子 IOPV 中的美股期货涨幅与汇率仍为 stub（`qdii_calc.py`），
  溢价监控的绝对值不精确，但 >3% 告警链路可用
- `report_sentiment` 为空时融合信号自动退化为纯 RSRS
- Streamlit Dashboard（Design P4）未动，`streamlit` 包未安装

## 9. 策略有效性待办

链路、滑动窗口对照、参数扫描、显著性检验、扩展池验证、降频验证、因子
诊断、**新因子批量筛选**均已完成（§7.4~§7.10）。**最终结论（收口）**：
RSRS 与 16 个标准价量因子在扩展池上**全部失效**——最高 |ICIR|=0.238，
远低于 0.5 可用门槛。根因不在因子构造，而在**候选池同质化**：12 只 ETF
里 10 只是高度相关的权益类（PC1=57.5%），截面差异不足以产生可交易 alpha。
**被动等权是当前唯一站得住的基线。** 剩余方向按优先级：

1. **重构候选池（最高优先）**：截面价量因子在「近似单一风险因子」的池子里
   注定失效，突破点是把池子做「跨资产、跨市场、跨因子」的异构化——纳入
   黄金(518880 已列池内唯一独立标的)、债券 ETF、商品、跨境 QDII 的不同
   区域/风格，让截面存在真实的因子暴露差异，再复跑 §7.10 筛选
2. **转向时序/择时类信号**：截面轮动在同类池失效，但「股金轮动」「趋势
   择时」「波动率择时」等时序信号不依赖截面区分度，可在单标的或大类资产
   上独立验证（如 518880 vs 权益的负相关性 ρ≈0.02~0.09）
3. **接受被动基线**：把等权/风险平价做成正式组合（月度再平衡），作为
   AlphaQuant 的兜底输出，同时量化其最大回撤/换手成本
4. QDII 影子 IOPV 实装（`qdii_calc.py` 仍是 mock，premium=当日涨跌幅），
   为 QDII 溢价套利策略打基础——这是当前唯一有「结构套利」性质的独立 alpha
