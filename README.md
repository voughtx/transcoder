# GitHub Actions Worker BotAPI Transcoder V2 Smart

## New features

- Multiple workers via `WORKER_BASE_URLS`
- Multiple bots via `BOT_TOKEN_1..10`
- Smart segment retry: default `12,8,5` seconds
- Hard file size limit: default `17MB`
- Parallel Bot API uploads
- Summary message to `LOG_CHANNEL_ID`

## Required GitHub Secrets

```text
BOT_TOKEN_1=...
BOT_TOKEN_2=... optional
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
max_parallel_uploads=8
```
