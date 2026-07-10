# GitHub Actions Worker BotAPI Transcoder V4 Live Upload

## Features

- Live upload while FFmpeg is still generating segments
- Deletes local segment files after upload
- 20 bots support: `BOT_TOKEN_1..20`
- Multiple workers via `WORKER_BASE_URLS`
- Smart retry: `12,8,5` seconds
- Hard limit default: `17MB`
- Failed size attempt deletes already-uploaded Telegram messages
- Log channel summary
- Force worker index for testing custom domain

## Required GitHub Secrets

```text
BOT_TOKEN_1=...
...
BOT_TOKEN_20=... optional
CHAT_ID=-100storage_channel
LOG_CHANNEL_ID=-100log_channel
WORKER_BASE_URLS=https://v1homelander.dpdns.org,https://uv.v2homelander.workers.dev,https://top.v3deep.workers.dev,https://the.v4god.workers.dev,https://night.v5night.workers.dev
MONGO_URI=...
PUBLIC_WATCH_BASE_URL=https://v1homelander-8naz.onrender.com
```

## Recommended Run Inputs

```text
segment_times=12,8,5
hard_limit_mb=17
video_mode=copy
audio_mode=aac
max_parallel_uploads=12
force_worker_index=0
```

## Force custom domain worker test

If `WORKER_BASE_URLS` first URL is custom domain, run with:

```text
force_worker_index=1
```
