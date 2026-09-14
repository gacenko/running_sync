# running_sync — контекст проекту для Claude Code

## Мета проекту
Автоматичний pipeline: після кожного бігу дані з Garmin Connect автоматично
парсяться і зберігаються на Google Drive для подальшого аналізу з AI-тренером.

## Архітектура

```
Strava webhook
    ↓ (тригер — будь-яка нова активність)
Cloudflare Worker  (worker/worker.js)
    ↓ repository_dispatch → GitHub Actions
GitHub Actions  (.github/workflows/sync.yml)
    ↓
scripts/download_fit.py   — логін в Garmin, завантаження даних
scripts/parse_fit.py      — парсинг FIT + JSON → running-data.json
scripts/log_entry.py      — компактний запис для training_log
scripts/upload_drive.py   — завантаження на Google Drive
    ↓
Google Drive
  ├── last_run.json          — остання пробіжка (стрипована версія)
  ├── training_log.json      — компактний лог останніх 8 тижнів
  └── detailed_runs/         — повні файли по кожній пробіжці
```

## Ключові рішення і чому

### Strava — тільки тригер
Strava отримує активності з Garmin але з урізаними даними. Тому Strava
використовується лише як webhook тригер. Повні дані беруться напряму з
Garmin Connect API.

### sport_type не фільтрується в Worker
Strava не надсилає `sport_type` у webhook payload — тільки `object_id`,
`object_type`, `aspect_type`. Тому фільтрація по типу активності відбувається
в `download_fit.py` — беремо останні 5 активностей і шукаємо першу з
`typeKey == "running"`.

### Workout endpoint
Використовуємо `client.connectapi("/workout-service/workout/{id}")` замість
`client.get_workout_by_id()` — бібліотечний метод розгортає RepeatGroupDTO
і губить структуру повторень (наприклад "2 Рази" в темповому тренуванні).

### last_run vs detailed_runs
- `detailed_runs/` — повний `running-data.json` з `time_series` і температурами
- `last_run.json` — стрипована версія без `time_series` і без дублікатів
  температури (єдине джерело — блок `weather`)
- Стрипінг відбувається в `upload_drive.py → build_last_run()`

### Repeat блоки в workout
`parse_fit.py` розгортає `RepeatGroupDTO` двома способами:
- `structured_steps` — зберігає repeat блок як є (для `workout.steps` в JSON)
- `flat_steps` — розгортає `iterations` разів (для `planned_active_count`)
Це критично для правильного маппінгу інтервалів: без цього другий темповий
відрізок позначається як `post_workout`.

### best_pace для recovery/cooldown
`best_pace = null` для recovery і cooldown лапів — хвости від сусідніх
інтервалів спотворюють показник. Для active і warmup — rolling 5-секундне
вікно по record-ах FIT.

### avg_stride_length
FIT `total_strides` = подвійні кроки (обидві ноги). Ділимо на 2 щоб
отримати довжину одного кроку як у Garmin CSV.

### Sleep
Garmin прив'язує сон до дня пробудження. `download_fit.py` бере дату
початку активності — це правильно, бо активність зазвичай вранці після сну.

## Структура running-data.json

```
{
  "generated_at": "...",
  "activity_start": "...",
  "activity": {
    "id": ...,
    "name": "...",
    "summary": { повні метрики активності }
  },
  "workout": {
    "id": ...,
    "name": "...",
    "steps": [ структуровані кроки з repeat блоками ]
  },
  "weather": { temperature_c, wind_speed_kmh, wind_direction, precipitation, conditions },
  "sleep": { duration_hours, deep/light/rem, score, hrv_overnight_avg, body_battery_change, resting_hr },
  "subjective": { feeling_score, feeling, perceived_effort, scale },
  "intervals": [
    {
      "interval": 0,          // 0 для warmup
      "type": "warmup|active|recovery|cooldown|post_workout",
      "summary": { avg_pace, best_pace, nonstop_pace, avg_hr, ... },
      "splits": [ { lap, avg_pace, best_pace, nonstop_pace, avg_hr, ... } ]
    }
  ],
  "time_series": {            // тільки в detailed_runs, не в last_run
    "sample_interval_sec": 10,
    "data": [ { hr, pace_sec_per_km, cadence, respiration } ]
  }
}
```

## Поточний стан (14.09.2026)

### Що працює
- Повний pipeline: Strava → Cloudflare Worker → GitHub Actions → Garmin → Drive
- Парсинг FIT з коректними pace, HR, cadence, stride, elevation
- Workout структура з repeat блоками
- Sleep, weather, subjective дані
- training_log з вікном 8 тижнів
- Backfill скрипт для відновлення пропущених активностей

### Відомі обмеження
- `nonstop_pace` на 200м лапах може відрізнятись від Garmin на ±5с — прийнятно
- `best_pace` на lap рівні ±1-2с від Garmin CSV — прийнятно
- `avg_running_cadence` на lap рівні ±1 крок — прийнятно

### Відкриті задачі
- [ ] Google Sheets інтеграція — записувати дані в Sheets для ChatGPT Project Source
- [ ] GitHub Actions artifacts — змінити на `if: failure()` щоб зберігались тільки при помилці
- [ ] Перевірити і почистити `backfill.py` після успішного запуску (або видалити)

## GitHub Secrets (всі вже налаштовані)
- `GARMIN_EMAIL`, `GARMIN_PASSWORD`
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`
- `GOOGLE_DRIVE_FOLDER_ID`
- `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN`

## Токени і терміни
- `cloudflare-strava-trigger` (GitHub PAT) → діє до 08.09.2027
- Google OAuth refresh token → поновлений після публікації app (не протухає)
- Strava webhook subscription ID: `348714`

## Важливі файли
```
scripts/
  download_fit.py   — завантаження даних з Garmin
  parse_fit.py      — основний парсер (найскладніший файл)
  log_entry.py      — трансформація в компактний training_log entry
  upload_drive.py   — завантаження на Drive + build_last_run()
worker/
  worker.js         — Cloudflare Worker
  wrangler.jsonc    — конфіг деплою
.github/workflows/
  sync.yml          — основний pipeline workflow
```

## Як деплоїти Worker
```bash
cd worker
npx wrangler deploy
```
Worker не деплоїться автоматично з git — тільки вручну через wrangler або
Cloudflare Dashboard.

## Команди для частих задач
```bash
# Запустити pipeline вручну
# GitHub → Actions → Running Sync → Run workflow

# Задеплоїти Worker
cd worker && npx wrangler deploy

# Відновити пропущені активності
python backfill.py  # потребує env змінних
```
