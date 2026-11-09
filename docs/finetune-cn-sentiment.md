# 中文情绪：自建微调（FinFE → 快讯/公告迁移）

> 状态：**已在 7CH（RTX 5070 Ti）训练一轮并评测，未通过判据，未接线上**。线上仍是词典 `zh-v2.1`。
> 本文回答的是 `docs/architecture.md` 里那句「暂不换后端，除非在 FinFE train 上自己微调并证明能迁移」。

## 1. 先看要打败的基准

标尺是 450 条分层盲标新闻（`scripts/zh_news_eval_450.jsonl`，0 利空 / 1 中性 / 2 利好，
-1 存疑默认剔除，可评 410 条）。人工只看了**标题**，所以词典也按标题口径重算：

| 方法 | acc | macro-F1 | 中性泄漏 | 方向acc¹ | 中性率 |
| --- | --- | --- | --- | --- | --- |
| 词典 `zh-v2.1` / 标题 | .666 | .537 | .288 | .551 | .617 |
| 全判中性 | .712 | .277 | .000 | .000 | 1.000 |

逐类召回：利好 .589，利空 .429（利空精确率仅 .250）。分层 acc：公告 .885、其他 .740、
宏观 .568、个股 .493——个股层低于多数类基线（.537），而那正是交易信号的来源。

¹ 分母是人工有方向的 118 条，判中性算错。若只看双方都给出方向的 111 条，一致率 .890，
说明词典的错误主要是**弃权**而不是打反。

> 口径更正：此前用线上已存分算出的 acc .620 / macroF1 .509 是 `title + " " + content`
> 打出来的（`monitor.py:687`），且可能是旧版词典；人工只看标题，两个口径不能混用。
> 本表是标题口径的当前词典重算值。`cn_sentiment_eval.py` 会同时打印三种口径。

## 2. 数据

| 用途 | 来源 | 规模 | 位置 |
| --- | --- | --- | --- |
| 训练 | FinFE train（复旦 BBT-Fin/CFLEB 社媒三分类） | 15,157（负 4403 / 中 3892 / 正 6862） | `/tmp/finfe_train.jsonl`，**无 LICENSE，不入库** |
| 选点 | 上面分层切 8% | 1,212 | 运行时切分，seed 42 |
| 域外体检 | FinFE test | 1,000 | `/tmp/finfe_test.jsonl`，不入库 |
| 决策测试 | 自有 450 条盲标 | 450（可评 410） | `scripts/zh_news_eval_450.jsonl`，入库 |

自有 450 条在训练中**完全不看**，只在最后评测用一次，避免拿测试集选 epoch。

`/tmp` 不持久，重启后两个 finfe 文件就没了；重新生成的命令在
`scripts/cn_sentiment_finetune.py` 的 docstring 里（HF 镜像 `FinanceMTEB/FinFE`，parquet → jsonl）。

FinFE 文本是股吧口语（长度 p50 16 字 / p95 51 字 / max 64），与我们的快讯、公告标题不是同一文体。
这次微调要回答的就是这个域差能不能被抹平，所以别在 FinFE 上刷分自我安慰。
另外 `/tmp/finfe_train.jsonl` 是**按标签排序**的（0 全在前），任何手工取子集都要先打散，
`--smoke` 已经改成随机抽样。

## 3. 怎么跑

环境（已就绪，不在此展开）：`~/.venvs/flashquant/bin/python`，Python 3.12.15 /
torch 2.14.1 / transformers 5.19.0；初始化权重 `/home/orangepi/models/hw2942`
（`hw2942/bert-base-chinese-finetuning-financial-news-sentiment-v2`，3 类头，标签口径
0 负 / 1 中 / 2 正，与本仓库一致；复用它省一次下载，只是把它当域适配过的编码器）。

训练主机用 7CH（RTX 5070 Ti，torch 2.14.1+cu130，`compute_cap (12,0)`），
命令与香橙派相同，只多一个 `--device cuda`；数据与权重放在仓库外
`D:\fq-data\`（FinFE 由 hf-mirror 取 parquet 后本地转换，行数与标签分布须与香橙派那份一致）。

```bash
# 0) 先冒烟：400 条训练 + 200 条验证，1 个 epoch，产物写 <out>-smoke，跑完即可删
~/.venvs/flashquant/bin/python scripts/cn_sentiment_finetune.py --smoke 400

# 1) 正式训练（3 epoch，产物 /home/orangepi/models/cn-news-sent-v1/{best,last}）
~/.venvs/flashquant/bin/python scripts/cn_sentiment_finetune.py \
    --epochs 3 --out /home/orangepi/models/cn-news-sent-v1

# 2) 评测：词典（标题 / 标题+正文 / 已存分）+ 模型 + 中性阈值 5 折校准
~/.venvs/flashquant/bin/python scripts/cn_sentiment_eval.py \
    --model /home/orangepi/models/cn-news-sent-v1/best --neutral-cv 5 \
    --json-out /tmp/eval_cn_news_sent_v1.json
```

打分（默认**不接线上**，只产出 `sentiment_v2` 供影子对比）：

```bash
~/.venvs/flashquant/bin/python scripts/cn_sentiment_score.py \
    --model /home/orangepi/models/cn-news-sent-v1/best \
    --in-file news/raw/20261109.jsonl --out /tmp/scored_20261109.jsonl
```

三个脚本都只读 `news/`；打分写新文件，不改原始行里的 `sentiment`。

三个脚本都有 `--device auto|cpu|cuda`（默认 auto）：有 CUDA 就上卡，`--threads` 只在 CPU 路径生效。

## 4. 耗时（主机均为香橙派 CPU，8 核 RK3588 级）

| 阶段 | 数值 | 性质 |
| --- | --- | --- |
| 训练吞吐（bs32 / max_len 64 / 固定 padding） | 1.3 样本/秒 | **实测**（2026-11-09 bench_train.py） |
| 上表折算 15,157 条 / epoch | 3.2 h | 实测基准推算 |
| 本脚本 600 条短跑 | 3.2–3.3 样本/秒（含预热） | **实测**（2026-11-09，`--smoke 600` 共 209 s 含 200 条验证） |
| 折算 13,957 条 / epoch | 约 1.2 h | 由短跑推算，非整轮实测 |
| 3 epoch | 约 3.6 h | 推算 |
| 评测（410×3 变体 + FinFE 1,000） | 3–5 分钟 | 估计（推理实测 110–150 ms/条） |
| 影子打分（`--input full`，bs32 / max_len 128） | 270 ms/条 | **实测**（2026-11-09，20 条 sample） |
| 峰值内存 | 2.3 GB | 实测（bench_mem2.py）；Pi 15 GB 够 |

7CH（RTX 5070 Ti）实测：

| 阶段 | 数值 | 性质 |
| --- | --- | --- |
| 训练 3 epoch（13,945 条/epoch，bs32 / max_len 64，含每轮验证） | 14.1–14.4 s/epoch，端到端 **48.3 s**（约 989 样本/秒） | **实测**（2026-11-10，`--device cuda`） |
| 评测（410 条标题 + FinFE test 1,000，`--skip-raw`） | 4.7 s | **实测**（同上） |
| 冒烟 400 条 / 1 epoch | 1.1 s | 实测（含首步 CUDA 预热） |

同一份工作从 Pi 的 3.6 h 缩到 50 s，量级差约 260 倍；GPU 上做多轮消融/交叉验证的成本已经可以忽略。

影子打分按 270 ms/条折算：单日 1,000 条约 4.5 分钟，全量历史 18 万条约 13.5 小时。
全文本口径在 128 token（约 120 汉字）处截断，公告 content 为空不受影响；
若以后要看正文深处的证据，先把它调大并重测速度。

`--resume` 只恢复权重与最佳分，**不恢复优化器状态**，LR 进度按 epoch 比例近似推进；
中断后重跑同一命令即可，别把续训结果和一口气跑完的当同一份实验。

## 5. 接线上前的判据（先定后测，五条全过才换）

在 410 条域内测试集上，模型（标题口径）必须同时满足：

1. `acc > .666` 且 `macro-F1 > .537`
2. 利空召回 `> .429`（否决类信号最怕漏）
3. 中性泄漏 `< .288`
4. 与词典的配对 McNemar `p < 0.05`
5. 个股层 `acc > .493`

任何一条不过就不换后端，把原因记进 `docs/architecture.md`。判据先写死，是为了防止
「FinFE 上 80% 很漂亮」这种域外数字把决定带偏。

### 首轮结果（2026-11-10，7CH RTX 5070 Ti，seed 42，3 epoch）

初始化 `hw2942`，13,945 训练 / 1,212 验证，best 为 epoch 3（valid macro-F1 .7865）。
训练日志：loss .970→.504→.345，train acc .679→.805→.874，valid acc .757→.804→.801。

| 方法 | acc | macro-F1 | 中性泄漏 | 方向acc | 方向acc条件 | 中性率 |
| --- | --- | --- | --- | --- | --- | --- |
| 词典 `zh-v2.1` / 标题 | .666 | .537 | .288 | .551 | .890 | .617 |
| 全判中性 | .712 | .277 | .000 | .000 | .000 | 1.000 |
| **微调模型 / 标题** | **.632** | **.548** | **.360** | **.610** | **.911** | **.551** |
| 微调模型 / 中性 5 折校准（OOF） | .678 | .560 | .236 | .466 | .948 | .690 |
| 微调模型 / FinFE test | .793 | .774 | .351 | .841 | .935 | .238 |

逐类（模型 / 标题）：利空 P .348 R .571 F1 .432；中性 .827/.640/.722；利好 .406/.622/.491。
校准后：利空 .448/.464/.456；中性 .788/.764/.776；利好 .429/.467/.447。
分层 acc / 方向acc：公告 .924/.000（全判中性）、个股 .410/.661、宏观 .505/.641、其他 .700/.750；
校准后个股 .455/.484、宏观 .600/.513、其他 .780/.625。McNemar vs 词典：b=50 c=64，p=.2232。

**裁决：不通过，不换后端。** 六条里 raw 只过 2 条（macro-F1、利空召回）；中性泄漏 .360 比词典还差，
个股 acc .410 低于多数类基线 .537，McNemar 不显著。校准行虽然过 3 条，但那是用 410 条标签本身
拟合的中性偏置（OOF 也仍属乐观估计），个股 .455 依然不达标。

有价值的信号是**方向**：双方都给方向时一致率 .911（词典 .890），个股层方向 acc .661 对 .581——
模型敢下结论时更准，坏在不肯弃权（78 条中性被判利好）。公告层全判中性，与「公告该走公司行为解析
而不是情绪分类」的既有判断一致。下一步若继续，方向是压中性先验（类别加权 / 用 410 条做 5 折域适配），
而不是继续在 FinFE 上刷分。

## 6. 已知风险

- **域迁移**：社媒口语 → 快讯/公告。公告层全是模板化标题，模型在 FinFE 里没见过。
- **中性先验错配**：FinFE 中性占 26%，新闻流占 71%。模型会系统性多判方向，`--neutral-cv`
  就是在 410 条上拟合一个中性偏置，报出的 OOF 行用到了标签本身，必须单独标注、不能当主指标。
- **公告不该进情绪分母**：无论模型多好，公告层「全判中性」基线就有 93%，应走公司行为
  解析（分红/回购/减持/立案）而不是情绪三分类。这条与趋势门的重标定（待办 #9）是两件事。
- **单种子**：7CH 上 3 epoch 只要 48 s，多跑几个种子的成本已经可以忽略；判据贴着线时
  应先跑 3 个种子看方差，再谈过不过。

## 7. 通过之后怎么接（现在不做）

1. `news_aggregator/sentiment.py` 增加 `cnbert` 后端（`SentimentBackend` 子类，惰性加载
   `AutoModelForSequenceClassification`，任何异常回退词典——照 `FinBertBackend` 的写法）。
2. `config.yaml` 的 `sentiment.backend` 改 `cnbert`，同时写 `cnbert_model` 路径；
   `LEXICON_VERSION` 旁边补一个模型版本号，写进产物便于区分。
3. 先跑一轮影子模式：`cn_sentiment_score.py` 批量产出 `sentiment_v2`，与 `sentiment`
   并行落盘跑 1–2 周，看告警排序和推送量是否变差，再切。

## 8. 回滚

模型目录整个删掉即可，训练和评测脚本不写 `news/`、不改线上后端。若已经切到 `cnbert`，
把 `config.yaml` 的 `backend` 改回 `lexicon` 就回退，不需要重训。
