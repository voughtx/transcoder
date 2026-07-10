#!/usr/bin/env python3
import os, re, json, time, random, base64, subprocess, mimetypes, shutil, hashlib, signal
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests
from pymongo import MongoClient

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"

class SizeLimitError(Exception):
    pass

class UploadError(Exception):
    pass


def env(name, default=""):
    return os.getenv(name, default).strip()


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def parse_csv(value: str) -> List[str]:
    return [x.strip().rstrip('/') for x in (value or '').split(',') if x.strip()]


def parse_tokens() -> List[str]:
    tokens = []
    for i in range(1, 21):
        t = env(f"BOT_TOKEN_{i}")
        if t:
            tokens.append(t)
    return tokens


def run_capture(cmd):
    print("CMD:", " ".join(map(str, cmd)))
    return subprocess.run(cmd, text=True, capture_output=True, check=True)


def get_direct_link(url: str) -> str:
    try:
        r = run_capture(["yt-dlp", "-f", "best/bestvideo+bestaudio", "-g", "--no-check-certificate", "--user-agent", USER_AGENT, url])
        lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        if lines:
            return lines[0]
    except Exception as e:
        print("yt-dlp direct failed, using original URL:", e)
    return url


def parse_hls_current(index_path: Path) -> Tuple[Optional[str], List[Dict], str]:
    if not index_path.exists():
        return None, [], ""
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


def select_worker(workers: List[str], video_url: str, title: str, force_index: int) -> str:
    if not workers:
        raise RuntimeError("WORKER_BASE_URLS secret missing")
    if force_index and 1 <= force_index <= len(workers):
        return workers[force_index - 1]
    seed = f"{video_url}|{title}|{int(time.time())}"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return workers[h % len(workers)]


def build_ffmpeg_cmd(direct: str, out_dir: Path, seg_time: int, video_mode: str, audio_mode: str) -> List[str]:
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
    return base + v + a + hls_args


def delete_message(token: str, chat_id: str, message_id: int):
    try:
        requests.post(f"https://api.telegram.org/bot{token}/deleteMessage", data={"chat_id": chat_id, "message_id": message_id}, timeout=20)
    except Exception as e:
        print(f"Delete message failed {message_id}: {e}")


def upload_file(token: str, bot_idx: str, worker_base: str, chat_id: str, path: Path, caption: str, hard_limit_bytes: int):
    size = path.stat().st_size
    if size > hard_limit_bytes:
        raise SizeLimitError(f"{path.name} {size/1024/1024:.2f}MB > hard limit {hard_limit_bytes/1024/1024:.2f}MB")
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
            raise UploadError(f"Telegram upload HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        if not payload.get("ok"):
            raise UploadError(f"Telegram API error: {payload}")
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
    raise UploadError(f"Upload failed after retries: {path.name}")


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


def cleanup_uploaded(uploaded: List[Dict], tokens: List[str], chat_id: str):
    print(f"Cleaning uploaded messages from failed attempt: {len(uploaded)}")
    for item in uploaded:
        bot_idx = item.get("bot_idx", "bot1")
        m = re.search(r'(\d+)', bot_idx)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(tokens):
            delete_message(tokens[idx], chat_id, item.get("message_id"))


def attempt_live_transcode_upload(direct: str, title: str, video_url: str, seg_time: int, video_mode: str, audio_mode: str, chosen_worker: str, tokens: List[str], chat_id: str, hard_bytes: int, max_parallel: int):
    out_dir = Path("hls")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    index = out_dir / "index.m3u8"
    ffmpeg_log = Path("output/ffmpeg.log")
    ffmpeg_log.parent.mkdir(exist_ok=True)
    logf = ffmpeg_log.open("w")

    cmd = build_ffmpeg_cmd(direct, out_dir, seg_time, video_mode, audio_mode)
    print("CMD:", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=logf, stderr=logf)

    uploaded_all = []
    init_meta = None
    uploaded_seg_by_name: Dict[str, Dict] = {}
    futures = {}
    executor = ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(tokens) * 2)))
    fatal_error = None
    init_uploaded = False

    def submit_upload(seg_index: int, filename: str, duration: float):
        path = out_dir / Path(filename).name
        bot_i = seg_index % len(tokens)
        bot_idx = f"bot{bot_i+1}"
        print(f"Queue upload {seg_index+1}: {path.name} via {bot_idx} size={path.stat().st_size}")
        return executor.submit(upload_file, tokens[bot_i], bot_idx, chosen_worker, chat_id, path, f"{title} | part {seg_index+1}", hard_bytes)

    try:
        while True:
            init_uri, entries, _txt = parse_hls_current(index)

            # Upload init once available.
            if init_uri and not init_uploaded:
                init_path = out_dir / Path(init_uri).name
                if init_path.exists():
                    if init_path.stat().st_size > hard_bytes:
                        raise SizeLimitError(f"init {init_path.name} too large")
                    print(f"Uploading init {init_path.name}")
                    init_meta = upload_file(tokens[0], "bot1", chosen_worker, chat_id, init_path, f"{title} | init", hard_bytes)
                    uploaded_all.append(init_meta)
                    init_uploaded = True
                    try: init_path.unlink()
                    except Exception: pass

            # Queue new completed playlist segments.
            for i, ent in enumerate(entries):
                fname = Path(ent["uri"]).name
                if fname in uploaded_seg_by_name or fname in futures:
                    continue
                path = out_dir / fname
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size > hard_bytes:
                    raise SizeLimitError(f"{fname} {size/1024/1024:.2f}MB > hard limit")
                futures[fname] = (i, ent, submit_upload(i, fname, float(ent["duration"])))

            # Collect completed futures.
            for fname, (i, ent, fut) in list(futures.items()):
                if fut.done():
                    meta = fut.result()
                    meta.update({"i": i, "d": float(ent["duration"])})
                    uploaded_seg_by_name[fname] = meta
                    uploaded_all.append(meta)
                    del futures[fname]
                    try:
                        (out_dir / fname).unlink()
                    except Exception:
                        pass
                    print(f"Uploaded done {i+1}: {fname}")

            rc = proc.poll()
            if rc is not None:
                # One final parse/queue/collect after ffmpeg finishes.
                init_uri, entries, original = parse_hls_current(index)
                for i, ent in enumerate(entries):
                    fname = Path(ent["uri"]).name
                    if fname in uploaded_seg_by_name or fname in futures:
                        continue
                    path = out_dir / fname
                    if not path.exists():
                        raise UploadError(f"Final segment missing: {fname}")
                    if path.stat().st_size > hard_bytes:
                        raise SizeLimitError(f"{fname} {path.stat().st_size/1024/1024:.2f}MB > hard limit")
                    futures[fname] = (i, ent, submit_upload(i, fname, float(ent["duration"])))
                break
            time.sleep(0.35)

        if proc.returncode not in (0, None):
            raise RuntimeError(f"ffmpeg failed code {proc.returncode}; see output/ffmpeg.log")

        # Wait for all uploads.
        while futures:
            for fname, (i, ent, fut) in list(futures.items()):
                if fut.done():
                    meta = fut.result()
                    meta.update({"i": i, "d": float(ent["duration"])})
                    uploaded_seg_by_name[fname] = meta
                    uploaded_all.append(meta)
                    del futures[fname]
                    try: (out_dir / fname).unlink()
                    except Exception: pass
                    print(f"Uploaded done {i+1}: {fname}")
            time.sleep(0.2)

        init_uri, entries, original = parse_hls_current(index)
        if not init_meta:
            raise RuntimeError("init.mp4 was not uploaded")
        segments = []
        for i, ent in enumerate(entries):
            fname = Path(ent["uri"]).name
            meta = uploaded_seg_by_name.get(fname)
            if not meta:
                raise RuntimeError(f"Segment not uploaded: {fname}")
            meta["i"] = i
            meta["d"] = float(ent["duration"])
            segments.append(meta)
        playlist = build_worker_playlist(original, init_meta["worker_url"], [x["worker_url"] for x in segments])
        max_size = max([init_meta.get("s", 0)] + [x.get("s", 0) for x in segments])
        return init_meta, segments, playlist, max_size, uploaded_all
    except Exception as e:
        fatal_error = e
        try:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: proc.kill()
        except Exception:
            pass
        # Cancel futures (already-uploaded items will be cleaned below).
        for _fname, (_i, _ent, fut) in futures.items():
            fut.cancel()
        cleanup_uploaded(uploaded_all, tokens, chat_id)
        raise fatal_error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        logf.close()


def main():
    start_time = time.time()
    video_url = env("VIDEO_URL")
    title = env("TITLE", f"Movie_{random.randint(1000,9999)}")
    video_mode = env("VIDEO_MODE", "copy").lower()
    audio_mode = env("AUDIO_MODE", "aac").lower()
    seg_times = [int(x) for x in parse_csv(env("SEGMENT_TIMES", "12,8,5"))]
    hard_mb = float(env("HARD_LIMIT_MB", "17"))
    hard_bytes = int(hard_mb * 1024 * 1024)
    max_parallel = int(env("MAX_PARALLEL_UPLOADS", "12"))
    force_worker_index = int(env("FORCE_WORKER_INDEX", "0") or "0")
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

    Path("output").mkdir(exist_ok=True)
    direct = get_direct_link(video_url)
    chosen_worker = select_worker(workers, video_url, title, force_worker_index)
    print("Chosen worker:", chosen_worker)
    print("Bots available:", len(tokens))

    init_meta = segments = playlist = max_size = selected_seg_time = None
    last_err = None
    for st in seg_times:
        print(f"\n=== LIVE attempt segment_time={st}s hard={hard_mb}MB ===")
        try:
            init_meta, segments, playlist, max_size, uploaded_all = attempt_live_transcode_upload(direct, title, video_url, st, video_mode, audio_mode, chosen_worker, tokens, chat_id, hard_bytes, max_parallel)
            selected_seg_time = st
            break
        except SizeLimitError as e:
            last_err = e
            print(f"Size limit failed for {st}s: {e}")
            # try next smaller segment time
            continue
    if init_meta is None:
        raise RuntimeError(f"All segment tries failed. Last error: {last_err}")

    movie_id = str(random.randint(10000, 99999))
    Path("output/worker_playlist.m3u8").write_text(playlist)
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
        "max_file_mb": round(max_size/1024/1024, 3),
        "video_mode": video_mode,
        "audio_mode": audio_mode,
        "init": init_meta,
        "segments": segments,
        "total_parts": len(segments)+1,
        "timestamp": time.time(),
    }
    Path("output/metadata.json").write_text(json.dumps(doc, indent=2))
    watch_url = f"{watch_base}/watch/{movie_id}"
    summary = f"Movie ID: {movie_id}\nTitle: {title}\nParts: {len(segments)+1}\nWorker: {chosen_worker}\nSegment time: {selected_seg_time}\nMax file MB: {doc['max_file_mb']}\nWatch: {watch_url}\n"
    Path("output/run_summary.txt").write_text(summary)

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
