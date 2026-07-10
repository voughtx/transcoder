# GitHub Actions Worker BotAPI Transcoder V3 - 20 Bots

## New features

- Multiple workers via `WORKER_BASE_URLS`
- Multiple bots via `BOT_TOKEN_1..20`
- Smart segment retry: default `12,8,5` seconds
- Hard file size limit: default `17MB`
- Parallel Bot API uploads
- Summary message to `LOG_CHANNEL_ID`

## Required GitHub Secrets

```text
BOT_TOKEN_1=...
BOT_TOKEN_2=... optional
...
BOT_TOKEN_20=... optional
CHAT_ID=-100storage_channel
LOG_CHANNEL_ID=-100log_channel
WORKER_BASE_URLS=https://v2.7homelander.workers.dev,https://uv.v2homelander.workers.dev,https://top.v3deep.workers.dev,https://the.v4god.workers.dev,https://night.v5night.workers.dev
MONGO_URI=...
PUBLIC_WATCH_BASE_URL=https://v1homelander-8naz.onrender.com
```

## Run workflow

Actions -> Worker BotAPI Transcoder V2 Smart -> Run workflow.

Recommended:

```text
segment_times=12,8,5
hard_limit_mb=17
video_mode=copy
audio_mode=aac
max_parallel_uploads=12
```


## 20 bots note

Cloudflare Workers me bhi same `BOT_TOKEN_1..BOT_TOKEN_20` same order me hone chahiye.

Recommended first run:

```text
max_parallel_uploads=12
```

Agar 429/FloodWait na aaye aur speed badhani ho, tab:

```text
max_parallel_uploads=16 ya 20
```
