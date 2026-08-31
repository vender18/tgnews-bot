# Файлы расписания

Здесь лежат `pipeline.yml` и `health.yml` — это расписание GitHub Actions.
Они должны находиться в папке `.github/workflows/`, но токен, которым
загружался код, не имел права создавать файлы расписания.

Как исправить (один раз, минута):

1. GitHub → аватар справа сверху → **Settings** → внизу слева **Developer settings**
   → **Personal access tokens** → **Tokens (classic)** → открыть свой токен → **Edit**.
2. Поставить галочку **workflow** → **Update token**.
3. В терминале:

```bash
cd ~/Desktop/claude-projects/tgnews-bot
git mv setup/workflows .github/workflows
git commit -m "Расписание GitHub Actions"
git push
```

После этого во вкладке Actions появится workflow `pipeline`, и бот заработает
по расписанию.
