#!/bin/bash
# FlashQuant monitor 单轮 + git 同步（原 cron-job.org -> GitHub dispatch 链路的本地替代）
set -u
# 代理环境（与 runner .env 同款：国外源走 mihomo，国内源直连）
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890
export no_proxy=localhost,127.0.0.1,eastmoney.com,cls.cn,sina.com.cn,10jqka.com.cn,futunn.com,wallstcn.com,jin10.com,gov.cn
cd /home/orangepi/flashquant || exit 1
export HF_ENDPOINT=https://hf-mirror.com

git pull --ff-only origin master 2>&1 | tail -1
"$HOME/.venvs/flashquant/bin/python" news_aggregator/monitor.py --once --no-stock-impact

git config user.name "flashquant-cron"
git config user.email "flashquant@cron.local"
git add news/
if ! git diff --cached --quiet; then
  git commit -m "chore: monitor update $(date '+%F %H:%M')"
  git pull --rebase --autostash origin master
  git push
fi
