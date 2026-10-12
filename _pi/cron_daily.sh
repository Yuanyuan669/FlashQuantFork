#!/bin/bash
# FlashQuant 收盘日报（run.py --push：日报/LLM综述/daily_sentiment/dashboard 刷新）+ git 同步
set -u
# 代理环境（与 runner .env 同款：国外源走 mihomo，国内源直连）
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890
export no_proxy=localhost,127.0.0.1,eastmoney.com,cls.cn,sina.com.cn,10jqka.com.cn,futunn.com,wallstcn.com,jin10.com,gov.cn
cd /home/orangepi/flashquant || exit 1
export HF_ENDPOINT=https://hf-mirror.com

git pull --ff-only origin master 2>&1 | tail -1
"$HOME/.venvs/flashquant/bin/python" news_aggregator/run.py --push

git config user.name "flashquant-cron"
git config user.email "flashquant@cron.local"
git add news/ dashboard/index.html
if ! git diff --cached --quiet; then
  git commit -m "chore: daily update $(date '+%F %H:%M')"
  git pull --rebase --autostash origin master
  git push
fi
