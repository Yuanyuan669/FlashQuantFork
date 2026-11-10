#!/bin/bash
# FlashQuant monitor 单轮 + git 同步（原 cron-job.org -> GitHub dispatch 链路的本地替代）
#
# 部署位置：香橙派 192.168.31.226（orangepi 用户），crontab -l 实测 2026-11-09：
#   45 8  * * 1-5  ~/flashquant/scripts/cron_monitor.sh >> ~/monitor_cron.log 2>&1
#   35 11 * * 1-5  ~/flashquant/scripts/cron_monitor.sh >> ~/monitor_cron.log 2>&1
#   5  15 * * 1-5  ~/flashquant/scripts/cron_monitor.sh >> ~/monitor_cron.log 2>&1
#   30 21 * * 1-5  ~/flashquant/scripts/cron_monitor.sh >> ~/monitor_cron.log 2>&1
#   30 18 * * 1-5  ~/flashquant/scripts/cron_daily.sh   >> ~/monitor_cron.log 2>&1
# 即每交易日 4 轮事件告警 + 1 轮收盘日报，周末不跑（与上游源停更节奏一致）。
# 调推送频率就改这张表：注意力预算保证每轮最多 1 条企微消息，轮数=消息数上限。
set -u
# 代理环境（与 runner .env 同款：国外源走 mihomo，国内源直连）
export http_proxy=http://127.0.0.1:7890
export https_proxy=http://127.0.0.1:7890
export no_proxy=localhost,127.0.0.1,eastmoney.com,cls.cn,sina.com.cn,10jqka.com.cn,futunn.com,wallstcn.com,jin10.com,gov.cn,bargo.ai
cd /home/orangepi/flashquant || exit 1
export HF_ENDPOINT=https://hf-mirror.com

# 同步生产代码：pull 失败必须中止本轮，绝不拿旧代码继续跑。
# 2026-11-09 实测教训：原来写成 `git pull ... | tail -1`，退出码被管道吃掉，
# 拉取失败也照样跑旧版本 —— 推送格式改了却不生效，排查时白查半天。
pull_out=$(git pull --ff-only origin master 2>&1)
pull_rc=$?
echo "$pull_out" | tail -1
if [ "$pull_rc" -ne 0 ]; then
  echo "[cron] git pull 失败（rc=$pull_rc）$(date '+%F %T')，中止本轮（不跑旧代码）"
  exit 1
fi
echo "[cron] 运行版本 $(git rev-parse --short HEAD) $(date '+%F %T')"
"$HOME/.venvs/flashquant/bin/python" news_aggregator/monitor.py --once --no-stock-impact

git config user.name "flashquant-cron"
git config user.email "flashquant@cron.local"
git add news/
if ! git diff --cached --quiet; then
  git commit -m "chore: monitor update $(date '+%F %H:%M')"
  if ! git pull --rebase --autostash origin master; then
    echo "[cron] rebase 失败，数据提交留在本地 $(git rev-parse --short HEAD)，需手动处理"
    exit 1
  fi
  if ! git push; then
    echo "[cron] push 失败，数据提交留在本地 $(git rev-parse --short HEAD)；下轮 pull 会因分叉而中止，需手动处理"
  fi
fi
