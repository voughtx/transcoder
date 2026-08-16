#!/usr/bin/env python3
import os, re, json, time, random, base64, subprocess, mimetypes, shutil, hashlib
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from pymongo import MongoClient

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"


def env(name, default=""):
    return os.getenv(name, default).strip()


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def parse_csv(value: str) -> List[str]:
    return [x.strip().rstrip('/') for x in (value or '').split(',') if x.strip()]


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
    try:
        r = run(["yt-dlp", "-f", "best/bestvideo+bestaudio", "-g", "--no-check-certificate", "--user-agent", USER_AGENT, url], capture=True)
        lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        if lines:
            return lines[0]
    except Exception as e:
        print("yt-dlp direct failed, using original URL:", e)
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
                dur = 10.0
        elif line and not line.startswith("#"):
            entries.append({"uri": line, "duration": float(dur or 10.0)})
            dur = None
    return init, entries, text


def max_media_file_size(folder: Path) -> Tuple[int, str]:
    max_size = 0
    max_name = ""
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in ('.m4s', '.mp4', '.ts'):
            size = p.stat().st_size
            if size > max_size:
                max_size = size
                max_name = p.name
    return max_size, max_name


def build_hls(direct: str, out_dir: Path, seg_time: int, video_mode: str, audio_mode: str):
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    index = out_dir / "index.m3u8"
    seg_tpl = str(out_dir / "seg_%05d.m4s")
    base = [
        "ffmpeg", "-hide_banner", "-y",
        "-user_agent", USER_AGENT, "-headers", f"User-Agent: {USER_AGENT}\r\n",
        "-fflags", "+genpts", "-i", direct,
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-avoid_negative_ts", "make_zero"
    ]
    if video_mode == "encode":
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
    return index


def select_worker(workers: List[str], video_url: str, title: str) -> str:
    if not workers:
        raise RuntimeError("WORKER_BASE_URLS secret missing")
    seed = f"{video_url}|{title}|{int(time.time())}"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return workers[h % len(workers)]


def upload_file(token: str, bot_idx: str, worker_base: str, chat_id: str, path: Path, caption: str, hard_limit_bytes: int):
    size = path.stat().st_size
    if size > hard_limit_bytes:
        raise RuntimeError(f"{path.name} {size/1024/1024:.2f}MB > hard limit {hard_limit_bytes/1024/1024:.2f}MB")
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = {"chat_id": chat_id, "disable_notification": "true", "caption": caption[:1024]}
    for attempt in range(6):
        with path.open("rb") as f:
            resp = requests.post(url, data=data, files={"document": (path.name, f, mime)}, timeout=180)
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after") or 3)
            except Exception:
                wait = 3
            print(f"429 wait {wait}s for {path.name} via {bot_idx}")
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
        worker_url = f"{worker_base.rstrip('/')}/tg/{bot_idx}/{b64url(doc['file_id'])}.{ext}"
        return {
            "message_id": msg["message_id"],
            "file_id": doc["file_id"],
            "file_unique_id": doc.get("file_unique_id"),
            "s": doc.get("file_size", size),
            "bot_idx": bot_idx,
            "ext": ext,
            "worker_url": worker_url,
        }
    raise RuntimeError(f"Upload failed after retries: {path.name}")


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


def send_log_message(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}, timeout=30)
    except Exception as e:
        print("Log message failed:", e)


def main():
    start_time = time.time()
    video_url = env("VIDEO_URL")
    title = env("TITLE", f"Movie_{random.randint(1000,9999)}")
    video_mode = env("VIDEO_MODE", "copy").lower()
    audio_mode = env("AUDIO_MODE", "aac").lower()
    seg_times = [int(x) for x in parse_csv(env("SEGMENT_TIMES", "12,8,5"))]
    hard_mb = float(env("HARD_LIMIT_MB", "17"))
    hard_bytes = int(hard_mb * 1024 * 1024)
    max_parallel = int(env("MAX_PARALLEL_UPLOADS", "8"))
    chat_id = env("CHAT_ID")
    log_channel = env("LOG_CHANNEL_ID")
    mongo_uri = env("MONGO_URI")
    watch_base = env("PUBLIC_WATCH_BASE_URL", "https://v1homelander-8naz.onrender.com").rstrip('/')
    workers = parse_csv(env("WORKER_BASE_URLS"))
    tokens = parse_tokens()

    if not video_url: raise SystemExit("VIDEO_URL missing")
    if not chat_id: raise SystemExit("CHAT_ID secret missing")
    if not workers: raise SystemExit("WORKER_BASE_URLS secret missing")
    if not tokens: raise SystemExit("BOT_TOKEN_1 secret missing")

    out = Path("output"); out.mkdir(exist_ok=True)
    hls = Path("hls")
    direct = get_direct_link(video_url)
    chosen_worker = select_worker(workers, video_url, title)
    print("Chosen worker:", chosen_worker)

    selected_index = None
    selected_seg_time = None
    selected_max_size = None
    for st in seg_times:
        print(f"\n=== Trying segment_time={st}s ===")
        idx = build_hls(direct, hls, st, video_mode, audio_mode)
        max_size, max_name = max_media_file_size(hls)
        print(f"Max file: {max_name} = {max_size/1024/1024:.2f}MB, hard={hard_mb}MB")
        if max_size <= hard_bytes:
            selected_index = idx
            selected_seg_time = st
            selected_max_size = max_size
            break
        print("Too large, retrying with smaller segment time...")

    if not selected_index:
        raise RuntimeError(f"All segment tries failed. Max file > {hard_mb}MB. Try encode/lower bitrate.")

    init_uri, entries, original = parse_hls(selected_index)
    if not init_uri:
        raise RuntimeError("No init.mp4 found in fMP4 HLS")
    print("Selected segment_time:", selected_seg_time, "segments:", len(entries))

    movie_id = str(random.randint(10000, 99999))
    init_path = hls / Path(init_uri).name
    init_meta = upload_file(tokens[0], "bot1", chosen_worker, chat_id, init_path, f"{title} | init", hard_bytes)

    def upload_one(i_ent):
        i, ent = i_ent
        path = hls / Path(ent["uri"]).name
        bot_i = i % len(tokens)
        bot_idx = f"bot{bot_i+1}"
        print(f"Uploading {i+1}/{len(entries)} {path.name} via {bot_idx} size={path.stat().st_size}")
        meta = upload_file(tokens[bot_i], bot_idx, chosen_worker, chat_id, path, f"{title} | part {i+1}", hard_bytes)
        meta.update({"i": i, "d": float(ent["duration"])})
        return i, meta

    segments = [None] * len(entries)
    with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(tokens) * 2))) as ex:
        futures = [ex.submit(upload_one, (i, ent)) for i, ent in enumerate(entries)]
        for fut in as_completed(futures):
            i, meta = fut.result()
            segments[i] = meta
            print(f"Uploaded {i+1}/{len(entries)} -> {meta['worker_url'][:80]}...")

    seg_urls = [x["worker_url"] for x in segments]
    playlist = build_worker_playlist(original, init_meta["worker_url"], seg_urls)
    (out / "worker_playlist.m3u8").write_text(playlist)

    doc = {
        "movie_id": movie_id,
        "delivery": "worker_botapi",
        "type": "single_worker_botapi",
        "title": title,
        "source_url": video_url,
        "timeline": "continuous",
        "independent_segments": True,
        "container": "fmp4",
        "worker_base_url": chosen_worker,
        "segment_time": selected_seg_time,
        "hard_limit_mb": hard_mb,
        "max_file_mb": round((selected_max_size or 0)/1024/1024, 3),
        "video_mode": video_mode,
        "audio_mode": audio_mode,
        "init": init_meta,
        "segments": segments,
        "total_parts": len(segments)+1,
        "timestamp": time.time(),
    }
    (out / "metadata.json").write_text(json.dumps(doc, indent=2))
    watch_url = f"{watch_base}/watch/{movie_id}"
    summary = f"Movie ID: {movie_id}\nTitle: {title}\nParts: {len(segments)+1}\nWorker: {chosen_worker}\nSegment time: {selected_seg_time}\nMax file MB: {doc['max_file_mb']}\nWatch: {watch_url}\n"
    (out / "run_summary.txt").write_text(summary)

    if mongo_uri:
        client = MongoClient(mongo_uri)
        client["video_database"]["segments"].insert_one(doc)
        print("Mongo inserted movie_id:", movie_id)
    else:
        print("MONGO_URI missing, metadata only saved.")

    elapsed = int(time.time() - start_time)
    log_text = (
        "✅ <b>Worker BotAPI Upload Complete</b>\n\n"
        f"🎬 <b>Title:</b> {title}\n"
        f"🆔 <b>Movie ID:</b> <code>{movie_id}</code>\n"
        f"🔗 <b>Source:</b> {video_url}\n"
        f"📦 <b>Parts:</b> {len(segments)+1}\n"
        f"⏱️ <b>Time:</b> {elapsed//60}m {elapsed%60}s\n"
        f"🎞️ <b>Mode:</b> video={video_mode}, audio={audio_mode}\n"
        f"📏 <b>Segment:</b> {selected_seg_time}s, max={doc['max_file_mb']}MB, hard={hard_mb}MB\n"
        f"🌐 <b>Worker:</b> {chosen_worker}\n"
        f"▶️ <b>Watch:</b> {watch_url}"
    )
    send_log_message(tokens[0], log_channel, log_text)
    print("DONE movie_id:", movie_id)

if __name__ == "__main__":
    main()
