#!/bin/bash
# 情绪漂移周报（纯统计）：只读 news/raw，不加载模型，产物写 /home/orangepi/drift/，不入库。
# 部署：crontab 周日 09:10 一档（与工作日 monitor/daily 不冲突）。
set -u
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890
cd /home/orangepi/flashquant || exit 1

# 与其他 cron 同一策略：pull 失败中止本轮，不跑旧代码
pull_out=$(git pull --ff-only origin master 2>&1)
pull_rc=$?
echo "$pull_out" | tail -1
if [ "$pull_rc" -ne 0 ]; then
  echo "[drift] git pull 失败（rc=$pull_rc）$(date '+%F %T')，中止本轮"
  exit 1
fi

mkdir -p /home/orangepi/drift
out="/home/orangepi/drift/drift_$(date +%Y%m%d).md"
echo "[drift] 运行版本 $(git rev-parse --short HEAD) $(date '+%F %T')"
"$HOME/.venvs/flashquant/bin/python" scripts/cn_sentiment_drift.py \
  --raw-dir news/raw --weeks 6 --out "$out"
echo "[drift] 已写 $out"
