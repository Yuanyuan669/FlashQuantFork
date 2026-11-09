# 中文情绪：自建微调（FinFE → 快讯/公告迁移）

> 状态：**仅准备，未训练、未接线上**。线上仍是词典 `zh-v2.1`。
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

## 6. 已知风险

- **域迁移**：社媒口语 → 快讯/公告。公告层全是模板化标题，模型在 FinFE 里没见过。
- **中性先验错配**：FinFE 中性占 26%，新闻流占 71%。模型会系统性多判方向，`--neutral-cv`
  就是在 410 条上拟合一个中性偏置，报出的 OOF 行用到了标签本身，必须单独标注、不能当主指标。
- **公告不该进情绪分母**：无论模型多好，公告层「全判中性」基线就有 93%，应走公司行为
  解析（分红/回购/减持/立案）而不是情绪三分类。这条与趋势门的重标定（待办 #9）是两件事。
- **单种子**：CPU 训练 5 小时一轮，只跑 seed 42；如果评测结果贴着判据线（差 1–2 个点），
  不要据此拍板，先跑第二个种子看方差。

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
