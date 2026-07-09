#!/usr/bin/env python3
import os, re, json, time, math, random, base64, subprocess, mimetypes
from pathlib import Path
from typing import Optional, List

import requests
from pymongo import MongoClient

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
MAX_GETFILE_BYTES = 20 * 1024 * 1024


def env(name, default=""):
    return os.getenv(name, default).strip()


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def parse_tokens() -> List[str]:
    tokens = []
    for i in range(1, 11):
        t = env(f"BOT_TOKEN_{i}")
        if t:
            tokens.append(t)
    return tokens


def run(cmd, check=True, capture=False):
    print("CMD:", " ".join(map(str, cmd)))
    if capture:
        return subprocess.run(cmd, text=True, capture_output=True, check=check)
    return subprocess.run(cmd, check=check)


def get_direct_link(url: str) -> str:
    # Try yt-dlp direct URL. If fail, return original.
    try:
        r = run(["yt-dlp", "-f", "best/bestvideo+bestaudio", "-g", "--no-check-certificate", "--user-agent", USER_AGENT, url], capture=True)
        lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        if lines:
            return lines[0]
    except Exception as e:
        print("yt-dlp direct failed:", e)
    return url


def parse_hls(index_path: Path):
    text = index_path.read_text(errors="ignore")
    init = None
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text)
    if m:
        init = m.group(1)
    entries = []
    dur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line.split(":", 1)[1].split(",", 1)[0])
            except Exception:
                dur = float(env("SEGMENT_TIME", "10"))
        elif line and not line.startswith("#"):
            entries.append({"uri": line, "duration": float(dur or env("SEGMENT_TIME", "10"))})
            dur = None
    return init, entries, text


def upload_file(token: str, bot_idx: str, chat_id: str, path: Path, caption: str):
    size = path.stat().st_size
    if size > MAX_GETFILE_BYTES:
        raise RuntimeError(f"{path.name} {size/1024/1024:.2f}MB > 20MB. Use shorter segment_time or lower bitrate.")
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = {"chat_id": chat_id, "disable_notification": "true", "caption": caption[:1024]}
    for attempt in range(5):
        with path.open("rb") as f:
            resp = requests.post(url, data=data, files={"document": (path.name, f, mime)}, timeout=180)
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after") or 3)
            except Exception:
                wait = 3
            print(f"429 wait {wait}s")
            time.sleep(wait + 1)
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(min(2 ** attempt, 20))
            continue
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram upload HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        msg = payload["result"]
        doc = msg["document"]
        ext = path.suffix.lower().lstrip(".") or "bin"
        worker = f"{env('WORKER_BASE_URL').rstrip('/')}/tg/{bot_idx}/{b64url(doc['file_id'])}.{ext}"
        return {
            "message_id": msg["message_id"],
            "file_id": doc["file_id"],
            "file_unique_id": doc.get("file_unique_id"),
            "s": doc.get("file_size", size),
            "bot_idx": bot_idx,
            "ext": ext,
            "worker_url": worker,
        }
    raise RuntimeError(f"Upload failed: {path}")


def build_worker_playlist(original: str, init_url: Optional[str], seg_urls: List[str]) -> str:
    lines = []
    it = iter(seg_urls)
    for line in original.splitlines():
        st = line.strip()
        if st.startswith("#EXT-X-MAP:") and init_url:
            lines.append(f'#EXT-X-MAP:URI="{init_url}"')
        elif st and not st.startswith("#"):
            lines.append(next(it, line))
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def main():
    video_url = env("VIDEO_URL")
    title = env("TITLE", f"Movie_{random.randint(1000,9999)}")
    seg_time = int(env("SEGMENT_TIME", "10"))
    video_mode = env("VIDEO_MODE", "copy").lower()
    audio_mode = env("AUDIO_MODE", "aac").lower()
    chat_id = env("CHAT_ID")
    worker_base = env("WORKER_BASE_URL")
    mongo_uri = env("MONGO_URI")
    tokens = parse_tokens()

    if not video_url: raise SystemExit("VIDEO_URL missing")
    if not chat_id: raise SystemExit("CHAT_ID secret missing")
    if not worker_base: raise SystemExit("WORKER_BASE_URL secret missing")
    if not tokens: raise SystemExit("BOT_TOKEN_1 secret missing")

    out = Path("output")
    hls = Path("hls")
    out.mkdir(exist_ok=True)
    hls.mkdir(exist_ok=True)

    direct = get_direct_link(video_url)
    index = hls / "index.m3u8"
    seg_tpl = str(hls / "seg_%05d.m4s")

    base = ["ffmpeg", "-hide_banner", "-y", "-user_agent", USER_AGENT, "-headers", f"User-Agent: {USER_AGENT}\r\n", "-fflags", "+genpts", "-i", direct, "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-avoid_negative_ts", "make_zero"]
    if video_mode == "encode":
        # safe generic GOP for 30fps-ish content
        gop = max(seg_time * 30, 60)
        v = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-force_key_frames", f"expr:gte(t,n_forced*{seg_time})"]
    else:
        v = ["-c:v", "copy"]
    if audio_mode == "copy":
        a = ["-c:a", "copy"]
    else:
        a = ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000", "-af", "aresample=async=1:first_pts=0"]
    hls_args = ["-f", "hls", "-hls_time", str(seg_time), "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4", "-hls_flags", "independent_segments", "-hls_segment_filename", seg_tpl, str(index)]

    run(base + v + a + hls_args)

    init_uri, entries, original = parse_hls(index)
    print("HLS init:", init_uri, "segments:", len(entries))
    if not init_uri:
        raise RuntimeError("No init.mp4 found in fMP4 playlist")

    movie_id = str(random.randint(10000,99999))
    init_path = hls / Path(init_uri).name
    print("Uploading init", init_path)
    init_meta = upload_file(tokens[0], "bot1", chat_id, init_path, f"{title} | init")

    segments = []
    seg_urls = []
    for i, ent in enumerate(entries):
        p = hls / Path(ent["uri"]).name
        bot_i = i % len(tokens)
        bot_idx = f"bot{bot_i+1}"
        print(f"Uploading {i+1}/{len(entries)} {p.name} via {bot_idx} size={p.stat().st_size}")
        meta = upload_file(tokens[bot_i], bot_idx, chat_id, p, f"{title} | part {i+1}")
        meta.update({"i": i, "d": float(ent["duration"])})
        segments.append(meta)
        seg_urls.append(meta["worker_url"])
        time.sleep(0.25)

    playlist = build_worker_playlist(original, init_meta["worker_url"], seg_urls)
    (out / "worker_playlist.m3u8").write_text(playlist)

    doc = {
        "movie_id": movie_id,
        "delivery": "worker_botapi",
        "type": "single_worker_botapi",
        "title": title,
        "timeline": "continuous",
        "independent_segments": True,
        "container": "fmp4",
        "worker_base_url": worker_base,
        "segment_time": seg_time,
        "init": init_meta,
        "segments": segments,
        "total_parts": len(segments)+1,
        "timestamp": time.time(),
    }
    (out / "metadata.json").write_text(json.dumps(doc, indent=2))
    (out / "run_summary.txt").write_text(f"Movie ID: {movie_id}\nTitle: {title}\nParts: {len(segments)+1}\nWatch path: /watch/{movie_id}\n")

    if mongo_uri:
        client = MongoClient(mongo_uri)
        client["video_database"]["segments"].insert_one(doc)
        print("Mongo inserted movie_id:", movie_id)
    else:
        print("MONGO_URI missing, metadata only saved.")
    print("DONE movie_id:", movie_id)

if __name__ == "__main__":
    main()
