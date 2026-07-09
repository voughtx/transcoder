# GitHub Actions Worker BotAPI Transcoder V1

## Setup

1. GitHub repo banao.
2. Is folder ka content repo me push karo.
3. Repo Settings -> Secrets and variables -> Actions -> New repository secret.

Required secrets:

```text
BOT_TOKEN_1 = same token as Cloudflare Worker BOT_TOKEN_1
CHAT_ID = -100your_channel_id
WORKER_BASE_URL = https://hv.rangxdark.workers.dev
MONGO_URI = your mongodb uri
```

Optional multiple bots:

```text
BOT_TOKEN_2 ... BOT_TOKEN_10
```

## Run

GitHub repo -> Actions -> Worker BotAPI Transcoder -> Run workflow.

Inputs:

```text
video_url
_title
segment_time = 10
video_mode = copy or encode
audio_mode = aac or copy
```

Recommended first test:

```text
video_mode=copy
audio_mode=aac
segment_time=10
```

## Output

- MongoDB me movie save hoga.
- Artifacts me metadata.json and worker_playlist.m3u8 milega.
- Watch URL: your streamer /watch/{movie_id}

## Important

Har init/segment file <=20MB honi chahiye. Agar fail ho, segment_time kam karo ya bitrate/encode mode use karo.
