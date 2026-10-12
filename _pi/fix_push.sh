#!/bin/bash
# 修复 FlashQuant push 认证：github-fq 别名走 flashquant_deploy（有写权限）
set -e
grep -q "Host github-fq" ~/.ssh/config || cat >> ~/.ssh/config <<'EOF'

Host github-fq
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/flashquant_deploy
EOF

cd /home/orangepi/flashquant
git remote set-url origin git@github-fq:Yuanyuan669/FlashQuantFork.git
git push origin master
echo "=== push OK ==="
git log origin/master --oneline -3
echo "=== .env 键名 ==="
sed 's/=.*/=/' .env | grep -v '^$'