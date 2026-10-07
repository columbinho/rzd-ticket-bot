#!/bin/bash
# Разовый перенос бота на GitHub Actions: создаёт репозиторий, заливает код,
# кладёт настройки в переменные/секреты и запускает первую проверку.
#   ~/rzd-ticket-bot/deploy.sh public    — публичный репозиторий, проверка каждые 5 минут (минуты Actions бесплатны)
#   ~/rzd-ticket-bot/deploy.sh private   — приватный, каждые 30 минут (укладывается в 2000 бесплатных минут/мес)
set -e
cd "$(dirname "$0")"
VIS="${1:-}"
[ "$VIS" = public ] || [ "$VIS" = private ] || { echo "Укажи: ./deploy.sh public  или  ./deploy.sh private"; exit 1; }
GH=./bin/gh
CFG=config.json
J() { python3 -c "import json;v=json.load(open('$CFG'))$1;print(','.join(map(str,v)) if isinstance(v,list) else ('true' if v is True else 'false' if v is False else v))"; }

$GH auth status >/dev/null 2>&1 || { echo "Сначала вход: $GH auth login --web"; exit 1; }
$GH auth status 2>&1 | grep -q "'workflow'" || { echo "У gh нет права workflow. Выполни: $GH auth refresh -h github.com -s workflow"; exit 1; }

if [ "$VIS" = private ]; then
  sed -i '' 's#- cron: "\*/5 \* \* \* \*"    \# каждые 5 минут#- cron: "*/30 * * * *"   \# каждые 30 минут#' .github/workflows/check.yml
fi
git add -A
git -c user.name=levitacia -c user.email=claudeslava@icloud.com commit -qm "Расписание под $VIS-репозиторий" 2>/dev/null || true

echo "=== 1/4 Репозиторий rzd-ticket-bot ($VIS) и код"
if git remote get-url origin >/dev/null 2>&1; then
  git push -u origin main
else
  $GH repo create rzd-ticket-bot --$VIS --source . --remote origin --push
fi

echo "=== 2/4 Переменные и секреты (в код не попадают)"
for k in origin_code origin_node origin_name destination_code destination_node destination_name train dates car_types include_side_lower min_lower; do
  $GH variable set "$(echo $k | tr a-z A-Z)" --body "$(J "['$k']")"
done
J "['telegram']['bot_token']" | $GH secret set TG_BOT_TOKEN
J "['telegram']['chat_ids']"  | $GH secret set TG_CHAT_IDS

echo "=== 3/4 Первая проверка в GitHub Actions"
$GH workflow run check.yml
sleep 25
RUN=$($GH run list --workflow=check.yml --limit 1 --json databaseId -q '.[0].databaseId')
echo "run id: $RUN"
$GH run watch "$RUN" --exit-status && STATUS=OK || STATUS=FAIL

echo "=== 4/4 Итог: $STATUS"
$GH run view "$RUN" --log 2>/dev/null | grep -E "нижних|не найден|Без изменений|УВЕДОМЛЕНИЕ|отправлено|ОШИБКА|Traceback|Error" | sed 's/.*\t//' | head -20
echo
echo "Репозиторий: $($GH repo view --json url -q .url)"
echo "Готово. Дальше проверка идёт сама на серверах GitHub, Мак можно выключать."
