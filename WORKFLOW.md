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
| `rsrs_rotation` | 截面轮动：RSRS 修正分 top_k 等权，低于 min_score 空仓 | 主动策略 |
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

链路已经跑通，滑动窗口对照也完成了（§7.4），结论：**两个主动策略都没跑赢
被动等权**（equal_weight 跨窗复利 +201.80% / 夏普 1.23，为三者最优）。
下一步建议按优先级：

1. **调参再判死刑**：当前只是默认参数（top_k=2、min_score=0、日频调仓）。
   用 `sliding_backtest` 批量扫参——`--min-score 0.5`（空仓过滤）、
   `--top-k 1`（集中度）、信号降频（周频/月频，需在模板里加重采样）。
   滑动窗口框架已支持「一次命令对比多组参数」。
2. 用 vibe 自带的 `monte_carlo_test` / `bootstrap_sharpe_ci` /
   `walk_forward_analysis` 做显著性检验，排除运气成分
3. 扩大候选池（当前只有 3 个 ETF，截面轮动选择空间太窄；
   STRATEGY_PLAN.md §4 有候选池扩展设计）
4. 数据起点前推到 2018（含 2018 熊市与 2022 下跌），
   当前窗口最早只到 2023，样本全是结构性牛市
