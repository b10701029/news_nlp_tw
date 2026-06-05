#!/bin/bash
# 本機執行 news_nlp_tw
# 使用方式：bash run_local.sh
# 或每天排程：crontab -e 加入
#   30 14 * * 1-5 cd /path/to/news_nlp_tw && bash run_local.sh

# 請填入你的 API keys
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export LIVE_PULSE_BOT_TOKEN="${LIVE_PULSE_BOT_TOKEN:-}"
export TG_CHAT_ID="${TG_CHAT_ID:-5274395356}"

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "⚠️  請設定 ANTHROPIC_API_KEY"
  echo "    export ANTHROPIC_API_KEY=sk-ant-..."
  exit 1
fi

if [ -z "$LIVE_PULSE_BOT_TOKEN" ]; then
  echo "⚠️  請設定 LIVE_PULSE_BOT_TOKEN"
  exit 1
fi

cd "$(dirname "$0")"

# 安裝相依套件（第一次執行）
if ! python3 -c "import anthropic" 2>/dev/null; then
  echo "安裝相依套件..."
  pip install -r requirements.txt -q
fi

# 執行今天的公告
DATE=$(date +%Y%m%d)
echo "📰 執行 news_nlp_tw — $DATE"
python3 main.py --date "$DATE"
