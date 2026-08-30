# AlphaQuant 策略体系规划与滑动窗口回测设计

> 2026-08-31。本文档回答两个问题：① alpha_quant 里有哪些策略、各自缺什么；
> ② 如何用统一的执行流程 + 半年滑动窗口回测把策略验证补齐。
> 状态标记：✅ 可跑 / ⚠️ 能跑但失真 / ❌ 缺失或 mock。

## 1. 策略盘点

| # | 策略 | 模块 | 状态 | 缺口 |
|---|---|---|---|---|
| 1 | RSRS 截面轮动 | `vibe_exporter.py` (rsrs_rotation) + `signal_generator.py` | ✅ | 仅 3 只 ETF 验证过；超额收益未证明 |
| 2 | RSRS 单标的择时（滞回） | `vibe_exporter.py` (rsrs_timing) | ✅ 本轮新增 | 首次纳入回测 |
| 3 | 等权持有对照 | `vibe_exporter.py` (equal_weight) | ✅ 本轮新增 | —（用作 alpha/beta 判据） |
| 4 | RSRS+情感融合 | `signal_generator.py generate_signal()` | ⚠️ | 依赖 `report_sentiment` 表，目前为空 → 静默退化为纯 RSRS（见 P3） |
| 5 | QDII 溢价监控 | `data_engine/qdii_calc.py` | ⚠️ mock | `future_pct=0` 硬编码、汇率未参与计算，算出的"溢价"实为当日涨跌幅（见 P2） |
| 6 | LLM 研报情感链 | `llm_agent/*` | ❌ | 需 LLM API key；`converter.py` 主体为空（见 P3） |
| 7 | 回测引擎（backtrader） | `backtest/engine.py` | ❌ 已弃用 | backtrader 未安装，已被 vibe ChinaAEngine 替代，仅保留存档 |
| 8 | 实盘执行 | `execution/qmt_*` | ❌ mock | `place_order` 为空，需 MiniQMT（见 P4） |
| 9 | 看板 | `dashboard/app.py` | ❌ | streamlit 未安装（见 P4） |

**结论：真正可回测的策略是 1/2/3 三条，共用同一数据层与回测引擎；
4/5/6 属于数据或能力缺失，先补数据再谈策略，纳入 P2/P3。**

## 2. 目标架构：统一策略执行流程

```
                       ┌──────────────────────────────────────┐
                       │  vibe-trading 数据层（免 token, qfq） │
                       │  fetch_data → data/*.csv + DB        │
                       └──────────────┬───────────────────────┘
                                      │ 统一数据契约（OHLCV, 前复权）
                                      ▼
        ┌────────────────────────────────────────────────────────┐
        │  策略注册表  vibe_exporter.STRATEGY_TEMPLATES          │
        │  rsrs_rotation / rsrs_timing / equal_weight / ...      │
        │  （新策略=一个 AST 沙箱合规模板 + 注册一行）            │
        └──────────────┬───────────────────────────┬─────────────┘
                       ▼                           ▼
            日常信号（gen_signals）      滑动窗口回测（sliding_backtest）
            output/signals_*.json       runs/<base>/w*_<strategy>/
                       │                           ▼
                       │                聚合评估 summary.md/json
                       │                （窗口×策略矩阵、跨窗复利、
                       │                 胜率、最差单窗、超额胜率）
                       ▼                           ▼
                 monitor / 告警            策略有效性结论
                 （P2 补真实溢价）          （P1 补显著性检验）
```

三条执行链共用同一份 CSV 与同一引擎（ChinaAEngine：T+1/整手/涨跌停/ETF 免印花税），
保证「信号、回测、评估」三层对数据的理解完全一致。

## 3. 滑动窗口回测设计（本轮落地）

### 3.1 为什么切窗

单一窗口的回测结果高度依赖起止点。此前整段回测（2024-01~2026-06）总收益
+182.78%，但恰好覆盖牛市，超额仅 +6.20%——无法区分 alpha 与 beta。按半年
切窗逐段评估，可暴露策略在熊市/震荡/牛市的分段一致性。

### 3.2 窗口语义

- **窗口**：自然半年（2023-01-01~2023-06-30 …），`--window-months` 可调；
- **步长**：默认 = 窗口长（不重叠连续分段）；`--step-months 3` 可得重叠滚动窗
  （此时跨窗复利无意义，报告自动标注 `compound_valid=False`）；
- **默认区间**：2023H1 ~ 2026H1 共 7 个窗口，覆盖 2023 震荡下行的两段、
  2024H1 微盘股崩盘+反弹、2024H2「924」牛市启动、2025~2026 结构市；
- **warmup**：每个窗口的评估起点往前 3 年拉数据（M=600 需约 2.5 年），
  经 vibe loader 缓存注入提供给 signal_engine，`EVAL_START` 门控保证窗口
  之前不交易——每个窗口首日 RSRS 分数即已充分预热，无前视偏差；
- **独立性**：每窗口独立跑回测、初始资金重置，窗口间不共享持仓状态
  （状态机在 warmup 数据上连续运行，仅仓位被门控归零）。

### 3.3 判读标准（写进 summary.md 的脚注）

主动策略（rotation/timing）需同时满足，方可谓"有效"：

1. **跨窗复利 > equal_weight**（否则超额全是 beta）；
2. **最差单窗回撤显著更浅**（择时的价值主要体现在熊市防守）；
3. **超额胜率 ≥ 60%**（7 窗中至少 4~5 窗跑赢基准，而非靠单窗暴利）。

### 3.4 命令

```bash
# 全默认：3 标的 × 7 窗 × 3 策略
python3 main.py sliding_backtest

# 自定义
python3 main.py sliding_backtest --start 2023-01-01 --end 2026-06-30 \
    --window-months 6 --step-months 6 \
    --strategies rsrs_rotation,rsrs_timing,equal_weight \
    --codes 512480.SH,513100.SH,588000.SH --name sliding_20260831

# 单段回测某个策略
python3 main.py backtest --strategy rsrs_timing --start 2025-01-01 --end 2025-06-30
```

## 4. 分阶段补齐计划

### P0 策略注册表 + 滑动窗口框架（本轮，✅ 完成）
- `STRATEGY_TEMPLATES` 注册表：新策略 = 一个沙箱合规模板 + 注册一行；
- 新增 `rsrs_timing`（ENTRY=0.7 / EXIT=-0.7 滞回，阈值可传参）与
  `equal_weight`（被动对照）；
- `sliding_window.py`：窗口切分 / 全局单次拉数（规避 broker 整段覆写 CSV
  导致的逐窗重拉）/ 逐窗逐策略回测 / `summary.{json,md}` 聚合；
- `main.py sliding_backtest` 子命令；`backtest` 增加 `--strategy`。

### P1 统计显著性检验（下一个优先级）
当前 7 个窗口只是描述性统计。接入 vibe 自带的检验能力：
- `monte_carlo_test`：随机打乱信号时序，看真实收益是否显著异于随机；
- `bootstrap_sharpe_ci`：夏普的置信区间，检验是否显著 > 0；
- `walk_forward_analysis`：参数（N/M/top_k/ENTRY/EXIT）滚动寻优 + 样本外验证，
  排除过拟合。
**验收**：给出「策略 P 值 / 夏普 CI / 样本外衰减率」三件套。

### P2 QDII 真实影子 IOPV（修好 monitor 的前提）
- `qdii_calc.py`：`premium = 二级市场价 / 影子 IOPV − 1`，其中影子 IOPV =
  纳指期货实时 × 汇率 × 基金持仓映射 + 误差项；删除 `future_pct=0` 与
  硬编码汇率 7.2，汇率改用实时 USDCNY；
- 阈值告警按**真实溢价**（当前 3% 阈值实际在报"单日涨 3%"的噪音）。
**验收**：盘中影子 IOPV 与基金公司官方 IOPV 偏差 < 0.5%。

### P3 LLM 情感链路（让 fusion 真正生效）
- `llm_agent/analyzer.py` 接入可用 LLM（沿用东方财富 EM_API_KEY 或替换为
  其他兼容 OpenAI 协议的服务）；补全 `converter.py`；
- 研报 → `report_sentiment` 表 → `generate_signal()` 融合不再退化为纯 RSRS；
- 融合策略注册进 `STRATEGY_TEMPLATES`，进入滑窗评估对比。
**验收**：`report_sentiment` 非空，融合信号与纯 RSRS 滑窗结果可对比。

### P4 执行与呈现（最后）
- `execution/qmt_gateway.py` 对接 MiniQMT，`place_order` 落实；
- 装 streamlit，`dashboard/app.py` 展示滑窗矩阵与最新信号；
- 每日定时任务：fetch → factors → signals → monitor。

## 5. 风险与已知边界（继承自 WORKFLOW.md §8）

- 全部标的为 ETF，`stamp_tax=0` 硬编码——换个股回测前必须加 ETF/个股判定；
- 7 个窗口仍属小样本，P1 的统计检验不做，任何结论都只能是"倾向性"；
- 腾讯前复权数据随除权事件重基准，跨年对比的绝对值有微小漂移。
