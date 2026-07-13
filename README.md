# GitHub Actions Transcoder V5 Dynamic Tokens

Use one secret for all bot tokens:

```env
BOT_TOKENS_JSON=["bot1token","bot2token","bot3token", "...any count..."]
```

or CSV:

```env
BOT_TOKENS=token1,token2,token3
```

Workers are already dynamic:

```env
WORKER_BASE_URLS=https://w1,https://w2,https://w3
```

The script automatically uses all tokens and workers provided.
