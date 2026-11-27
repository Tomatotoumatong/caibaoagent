# qa_test — 最小新裁判（测试版）

给 earnings_radar 换一个面向研究价值的裁判：三组“正确性 × 覆盖率”成对指标 + 一个价值指标（电话会分析师问答命中率），并能冻结基线、校准匹配裁判本身。

earnings_radar 的数据库只读，不改主项目任何文件。

## 指标

| 组 | 正确性 | 覆盖率 | 数据来源 |
|---|---|---|---|
| A 提取 vs XBRL | 一致 / 已比对 | 已比对 / XBRL 有值 | R0 actuals × R2 xbrl：营收、GAAP EPS（R2 原有）+ 毛利率、营业利润率（新增） |
| B 数字与引用纪律 | 无数字违规且无引用错误的 LLM 卡片占比 | 核心阶段（static / frozen / R0 / reflection）状态为 ok 的占比 | cards.validation / status |
| C 观察项可判定 | LLM 与代码判定一致的占比（有 check 的项） | 被财报判定（非 not_disclosed）的观察项占比 | R0 answers |
| V 价值 | 冻结观察项被分析师问到（等级 2）的占比，及稀有度加权版本 | 被至少一个观察项命中的问题占比 | 电话会逐字稿 + 匹配裁判 |

成对报告的原因：只报正确性会奖励“不出”。

- **A 组的 triage**：XBRL 值是推算的（fiscal Q4 = 全年 − 前三季，股数会变）时，差异不算提取错误，单列为 triage。
- **V 组的两条基线**，与系统观察项同样数量（K），并用同一个裁判、同一个提示，分开判定，裁判不知道自己在判哪一组：
  - `prior_q`：上一场电话会里最先被问到、主题互不相同的 K 个问题；
  - `template`：`rubric/sector_templates.json` 的行业通用模板前 K 条，已冻结。
- **稀有度权重**：idf(主题) = ln((1+N)/(1+df)) + 1。df = 开发集里有多少场电话会问到该主题。

## 数据源的真实约束（2026-11-26 查 roic.ai API 文档）

- **免费档只给每家公司最近 2 场电话会**，按滑动 60 秒限 5 次请求。之前说的“免费 8 个季度”来自 roic 的营销博客，以 API 文档为准，那个说法是错的。
- 要历史需要 Professional 档；付费的 Individual 档在电话会页面上同样标为“最近 2 场”。
  - 通用的 Response format 页写的是“Free 2 years / Individual 5 years history”，和电话会页面的说法冲突。以电话会页面为准，probe 会实测列表里到底返回几场。
- 所以和期权快照一样：**当季不存，就永远拿不到**。本季（Q3）这批电话会大多要到一二月下一轮财报后才会被挤出窗口，但不要拖。
- 免费档只有 **raw 逐字稿：一整段纯文本，没有说话人标注**；带说话人的 edited 版需付费。
  - 问答切分靠代码：识别主持人交接语，如 “Our next question comes from the line of X with Y”。
  - 从每段交接里抽出分析师的问题靠 DeepSeek，但每个问题必须附一段原文逐字摘录（quote），代码核对原文里确实有，核对不上就丢弃。
  - `probe` 的首要任务就是量这一步的质量。
- `identifier` 格式是 `交易所:代码`（如 `NASDAQ:NVDA`）。share class 代码（BF.B）的拼法未在文档里写明，probe 会依次尝试。

### 探针结果（2026-11-26，10 只标的，34 次请求，0 次 429）

- 9/10 标的拿到 2 场；BRK.B 公司存在但不开电话会（`NYSE:BRK.B` 返回 200 + 空列表；拼错的代码返回 404）。现在遇到 200 + 空列表就停止尝试其他拼法。
- 列表确实只返回最近 2 场，“Free 2 years” 的说法不适用于电话会。
- ROIC 的 fiscal_year / fiscal_quarter 全部是**财年口径**；period_end_date 是名义月末（NVDA 写 10-31，实际 10-26），对齐容差 10 天或按月份匹配都没问题。
- 新近的电话会当天就能拿到（NVDA、ADI、CRWD、FICO 都在发布当天生成）。

### 窗口期（“最近 2 场”的后果）

一家公司开下一场电话会后，倒数第三场就拿不到了：
- Q3 开发集和留出事件的**上一季**电话会（7–8 月），会在 1 月中旬（银行股最早）到 2 月陆续出窗口 → `archive-dev` 和 `archive --sp500` 应在 1 月上旬前跑完。
- Q3 电话会本身一直留到 4 月。
- 之后每季跑一次 `archive --sp500`（约 2–6 小时，已存的跳过），就能保证每个事件都有“本季 + 上季”。

## 干净样本隔离（代码强制）

- 所有逐字稿默认 **sealed**（封存）。以下两类不封存：
  - 开发集 27 个 Q3 事件自己的那场电话会；
  - 2026-10-17 之前发布的电话会，属于历史，例如“上一季”基线。
- `load_text` 拒绝读取封存的逐字稿，抽问题、匹配、抽样标注都过不去。
- 解封必须调用 `unseal` 并写理由，记入 `unseal_ledger`（`status` 可看条数）。
- probe 对封存的逐字稿只算不含内容的结构统计（长度、交接段数），不写任何摘录。

## 运行

```bash
cd ~/Desktop/qa_test
PY=../earnings_radar_loop/.venv/bin/python      # 复用主项目的虚拟环境（只需要 requests）
set -a; source ../earnings_radar_loop/.env.collect; set +a   # 代理设置（127.0.0.1:7897）
# DEEPSEEK_API_KEY 需在环境里，与跑 earnings_radar 时相同
cp env.qa.example .env.qa   # 填入 ROIC_API_KEY（roic.ai 免费注册拿 key），不要提交或外发这个文件

$PY -m pytest -q tests                                   # 0. 自检，应 22 passed

# 1. 现在就能做：A/B/C 三组成对指标 + 冻结基线（不联网、不花钱）
$PY -m qajudge judge --no-value
$PY -m qajudge freeze --name v5r05_abc --no-value        # 写 baselines/v5r05_abc.json，只读，永不覆盖

# 2. 探针：10 只标的，约 35 次请求，约 8 分钟
$PY -m qajudge probe                                     # 结果：probe/probe_report.md + probe/samples/*.md
#    可选：顺带跑问题抽取，看原文摘录核对率（花少量 DeepSeek 费用）
#    $PY -m qajudge probe --llm
#    自选标的：--symbols NVDA,CRM,...

$PY -m qajudge seg-report                                # 离线重跑切分统计（不发请求）→ probe/seg_report.md

# ---- 看完 probe 报告、确认无坑，再往下 ----

# 3. 开发集 27 个事件：本季 + 上季电话会，约 80 次请求，约 17 分钟
$PY -m qajudge archive-dev
# 4. 抽分析师问题（每场电话会 1 次 DeepSeek 调用）→ 三组匹配（每个事件 3 次调用）
$PY -m qajudge extract
$PY -m qajudge match
$PY -m qajudge judge                                     # 校准通过前，V 会标 UNCALIBRATED

# 5. 匹配裁判校准（见下节）
$PY -m qajudge calib-sample --n 50 --seed 7
#    -> calib/pairs_<run>_n50_s7.csv（盲标表，不含裁判结果）+ calib/key_...csv（标完前不要打开）
$PY -m qajudge calib-kappa --key calib/key_<...>.csv --labels calib/labels_human.csv calib/labels_claude.csv

# 6. 校准通过后冻结完整基线
$PY -m qajudge freeze --name v5r05_full
$PY -m qajudge compare --base v5r05_abc --cand v5r05_full

$PY -m qajudge status                                    # 计数、封存数、限流记录、解封台账
# 全标普存档（季内事件只存不读，自动封存；上季电话会属历史、不封存）。约 1500–1700 次请求，5–6 小时：
# nohup $PY -m qajudge archive --sp500 > data/archive_sp500.log 2>&1 &
```

`ER_DB` 默认指向 `../earnings_radar_loop/data_q3v5_r05/radar.sqlite`，要换库时设环境变量。

## 匹配裁判校准

- **判定规则**：`rubric/MATCH_RUBRIC.md`。规则里写死了 5 条边界样例（B1–B5），包括“同指标但关注期或驱动因素不同算不算命中”。
  - 这份文件同时是裁判的系统提示和标注依据，它的哈希进入每条匹配记录和每个冻结基线。
  - 改规则会产生新的 run id，旧结果不能直接比较。
- **标注对象**：（观察项，分析师问题）配对，标 0 / 1 / 2。依据是原文摘录（`analyst_quote` + `exchange_excerpt`），不是 LLM 的转述。
- **分层抽样**：40% 裁判判为相关、30% 同主题但裁判判为无关、30% 随机，按事件轮流抽。原因是命中率只有一成左右时，随机样本几乎全是 0，κ 没有意义。
  - 报告里注明：κ 描述的是这个分层样本上的一致性。
- **门槛**：κ（命中 = 2）≥ 0.7 且 n ≥ 30，**参照必须是人工标注**，文件名含 `human` 或 `gold`。
  - 模型标注（如 `labels_claude.csv`）只能作为第二标注者；加 `--allow-model-reference` 时给出“临时通过”，`freeze` 仍不会把 V 纳入基线。
- **建议流程**：
  1. 你在不看 Claude 标注的前提下盲标一遍，存为 `labels_human.csv`；
  2. Claude 独立标一遍，存为 `labels_claude.csv`；
  3. 两者不一致的配对由你裁决，得到 `labels_gold.csv`；
  4. 报告同时给出：裁判 vs 人工、裁判 vs Claude、人工 vs Claude。

## 已知局限（测试版）

- V 只在开发集上算，开发集 27 个事件全是科技股，idf 只反映科技股内部的主题稀有度。
- 系统观察项是中文的“YES/NO 问句”，`prior_q` 基线是英文问题的转述。后者与分析师问题的表述更接近，这会让该基线偏强，对系统是保守的偏差。
- 交接段超过 2500 字符时只把开头传给抽取模型（分析师先说话），`share_truncated` 会报告这种情况的比例。
- 免费档每家只有最近 2 场电话会，所以“上一季”基线依赖存档的及时性。

## 规则变更记录

- v1（2026-11-26）：初版，B1–B5。

## 切分器变更记录

- 2026-11-26 探针后：
  1. 问答起点取“起点短语”和“第一个交接”中更早的那个；结尾式的“wraps up the Q&A portion”不再被当成起点（此前 MSFT 两场全部交接被丢弃）。
  2. 新增交接说法：is going to come from（BF.B）、up for the next question / we'll take your question now（CRM 视频会）、next question, please（ASR 漏掉主持人介绍，MSFT）。
  3. 新增收尾说法：this does conclude…、we have reached the end of our time、you may now disconnect。
  4. 700 字符内且中间没有问号的两个交接合并为一个（排队提示语）。
  5. 传给抽取模型的长度由 2500 改为 3500 字符；被截断时附上后文中以问号结尾的句子（追问）；原文核对改为对整段交接，而不是截断后的文本。
  6. 交接搜索从全文 2% 处开始（TYL 的问答从全文约 8% 处开始，原来的 10% 下限有漏掉第一个交接的风险）。
