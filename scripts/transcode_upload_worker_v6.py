import os, re, json, time, random, base64, subprocess, mimetypes, shutil, hashlib
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

VERBOSE = env("VERBOSE_LOGS", "").lower() in ("1", "true", "yes")
def mm(v):
    t = str(v)
    return (t[:4] + "…") if len(t) > 4 else t

def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

def parse_csv(value: str) -> List[str]:
    return [x.strip().rstrip('/') for x in (value or '').split(',') if x.strip()]

def parse_tokens() -> List[str]:
    raw_json = env("BOT_TOKENS_JSON")
    if raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, list):
                tokens = [str(x).strip() for x in data if str(x).strip()]
                if tokens:
                    return tokens
        except Exception as e:
            print("BOT_TOKENS_JSON parse error:", e)
    raw_csv = env("BOT_TOKENS")
    if raw_csv:
        tokens = [x.strip() for x in raw_csv.split(',') if x.strip()]
        if tokens:
            return tokens
    tokens = []
    for i in range(1, 201):
        t = env(f"BOT_TOKEN_{i}")
        if t:
            tokens.append(t)
    return tokens

def run_capture(cmd):
    if VERBOSE:
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

def probe_source(url: str) -> Dict:
    """ffprobe se bitrate + duration + streams. Smart segmentation ka base."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", "-user_agent", USER_AGENT, url],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[:300])
        data = json.loads(r.stdout)
        fmt = data.get("format", {})
        duration = float(fmt.get("duration") or 0)
        bitrate = int(fmt.get("bit_rate") or 0)
        if not bitrate:
            total = 0
            for s in data.get("streams", []):
                try:
                    total += int(s.get("bit_rate") or 0)
                except Exception:
                    pass
            bitrate = total
        vstreams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        astreams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        has_video = bool(vstreams)
        has_audio = bool(astreams)
        return {
            "duration": duration,
            "bitrate": bitrate,
            "has_video": has_video,
            "has_audio": has_audio,
        }
    except Exception as e:
        print("⚠️ probe fail (fallback fixed 12s):", e)
        return {"duration": 0, "bitrate": 0, "has_video": True, "has_audio": True}

def compute_seg_time(probe: Dict, hard_mb: float) -> int:
    """target size se hls_time nikaalo: time = (bytes*0.85*8) / bitrate. clamp [4,60]."""
    target_bytes = hard_mb * 1024 * 1024 * 0.85
    bitrate = probe.get("bitrate") or 0
    if bitrate <= 0:
        return 12
    t = (target_bytes * 8) / bitrate
    return int(max(4, min(60, t)))

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

def build_ffmpeg_cmd(direct: str, out_dir: Path, seg_time: int, video_mode: str, audio_mode: str, encode_bitrate: int = 0) -> List[str]:
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
        if encode_bitrate > 0:
            v = ["-c:v", "libx264", "-preset", "veryfast", "-b:v", str(encode_bitrate), "-maxrate", str(encode_bitrate), "-bufsize", str(encode_bitrate * 2), "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-force_key_frames", f"expr:gte(t,n_forced*{seg_time})"]
        else:
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
    except Exception:
        pass

def upload_file(token: str, bot_idx: str, worker_base: str, chat_id: str, path: Path, caption: str, hard_limit_bytes: int):
    size = path.stat().st_size
    if size > hard_limit_bytes:
        raise SizeLimitError(f"{path.name} {size/1024/1024:.2f}MB > hard limit {hard_limit_bytes/1024/1024:.2f}MB")
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(path, "rb") as f:
        files = {"document": (path.name, f, mimetypes.guess_type(path.name)[0] or "application/octet-stream")}
        data = {"chat_id": chat_id, "disable_notification": "true", "caption": caption[:1024]}
        r = requests.post(url, data=data, files=files, timeout=300)
    if r.status_code == 429:
        try:
            retry_after = int(r.json().get("parameters", {}).get("retry_after", 2))
        except Exception:
            retry_after = 2
        time.sleep(retry_after + 1)
        raise UploadError(f"Telegram 429 (retry_after={retry_after})")
    if r.status_code != 200:
        raise UploadError(f"Telegram upload HTTP {r.status_code}: {r.text[:500]}")
    j = r.json()
    if not j.get("ok"):
        raise UploadError(f"Telegram upload failed: {j}")
    doc = j["result"].get("document") or {}
    file_id = doc.get("file_id")
    ext = path.suffix.lower().lstrip(".") or "bin"
    worker_url = f"{worker_base.rstrip('/')}/tg/{bot_idx}/{b64url(file_id)}.{ext}"
    return {"bot": bot_idx, "file_id": file_id, "ext": ext, "worker_url": worker_url, "s": size}

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
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}, timeout=30)
    except Exception:
        pass

def cleanup_uploaded(uploaded: List[Dict], tokens: List[str], chat_id: str):
    for meta in uploaded:
        bot_i = int(str(meta.get("bot", "bot1")).replace("bot", "")) - 1
        if 0 <= bot_i < len(tokens):
            delete_message(tokens[bot_i], chat_id, meta.get("file_id", ""))

def attempt_live_transcode_upload(direct: str, title: str, video_url: str, seg_time: int, video_mode: str, audio_mode: str, chosen_worker: str, tokens: List[str], chat_id: str, hard_bytes: int, max_parallel: int, encode_bitrate: int = 0):
    out_dir = Path("hls")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    index = out_dir / "index.m3u8"
    ffmpeg_log = Path("output/ffmpeg.log")
    ffmpeg_log.parent.mkdir(exist_ok=True)
    logf = ffmpeg_log.open("w")

    cmd = build_ffmpeg_cmd(direct, out_dir, seg_time, video_mode, audio_mode, encode_bitrate)
    if VERBOSE:
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
    hard_mb = float(env("HARD_LIMIT_MB", "15"))
    hard_bytes = int(hard_mb * 1024 * 1024)
    max_parallel = int(env("MAX_PARALLEL_UPLOADS", "12"))
    force_worker_index = int(env("FORCE_WORKER_INDEX", "0") or "0")
    chat_id = env("CHAT_ID")
    log_channel = env("LOG_CHANNEL_ID")
    mongo_uri = env("MONGO_URI")
    watch_base = env("PUBLIC_WATCH_BASE_URL").rstrip('/')
    if not watch_base:
        raise SystemExit("PUBLIC_WATCH_BASE_URL secret missing")
    workers = parse_csv(env("WORKER_BASE_URLS"))
    tokens = parse_tokens()

    if not video_url: raise SystemExit("VIDEO_URL missing")
    if not chat_id: raise SystemExit("CHAT_ID secret missing")
    if not workers: raise SystemExit("WORKER_BASE_URLS secret missing")
    if not tokens: raise SystemExit("BOT_TOKEN_1 secret missing")

    Path("output").mkdir(exist_ok=True)
    direct = get_direct_link(video_url)
    chosen_worker = select_worker(workers, video_url, title, force_worker_index)
    print("Chosen worker:", chosen_worker if VERBOSE else "(masked)")
    print("Bots available:", len(tokens))

    probe = probe_source(direct)
    print(f"probe: duration={probe['duration']:.1f}s bitrate={probe.get('bitrate')} bps video={probe['has_video']} audio={probe['has_audio']}")
    smart_time = compute_seg_time(probe, hard_mb)
    print(f"smart hls_time={smart_time}s (target {hard_mb}MB, bitrate {probe.get('bitrate')})")

    tries = [smart_time, max(4, smart_time // 2)]
    init_meta = segments = playlist = max_size = selected_seg_time = None
    last_err = None
    selected_video_mode = video_mode
    for st in tries:
        print(f"\n=== attempt segment_time={st}s mode={video_mode} hard={hard_mb}MB ===")
        try:
            init_meta, segments, playlist, max_size, uploaded_all = attempt_live_transcode_upload(direct, title, video_url, st, video_mode, audio_mode, chosen_worker, tokens, chat_id, hard_bytes, max_parallel)
            selected_seg_time = st
            break
        except SizeLimitError as e:
            last_err = e
            print(f"Size limit failed for {st}s: {e}")
            continue
    if init_meta is None:
        enc_time = 12
        enc_bitrate = int((hard_mb * 1024 * 1024 * 8 * 0.7) / enc_time)
        print(f"\n=== ENCODE fallback (copy size fail) time={enc_time}s bitrate={enc_bitrate}bps ===")
        selected_video_mode = "encode"
        init_meta, segments, playlist, max_size, uploaded_all = attempt_live_transcode_upload(direct, title, video_url, enc_time, "encode", audio_mode, chosen_worker, tokens, chat_id, hard_bytes, max_parallel, enc_bitrate)
        selected_seg_time = enc_time
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
        "video_mode": selected_video_mode,
        "audio_mode": audio_mode,
        "source_bitrate": probe.get("bitrate") or 0,
        "init": init_meta,
        "segments": segments,
        "total_parts": len(segments)+1,
        "timestamp": time.time(),
    }
    Path("output/metadata.json").write_text(json.dumps(doc, indent=2))
    watch_url = f"{watch_base}/watch/{movie_id}"
    summary = f"Movie ID: {movie_id}\nTitle: {title}\nParts: {len(segments)+1}\nWorker: {chosen_worker}\nSegment time: {selected_seg_time}s (smart)\nMax file MB: {doc['max_file_mb']}\nWatch: {watch_url}\n"
    Path("output/run_summary.txt").write_text(summary)

    if mongo_uri:
        client = MongoClient(mongo_uri)
        client["video_database"]["segments"].insert_one(doc)
        print("Mongo inserted movie_id:", movie_id if VERBOSE else mm(movie_id))
    else:
        print("MONGO_URI missing, metadata only saved.")

    elapsed = int(time.time() - start_time)
    log_text = (
        "✅ <b>Worker BotAPI Upload Complete (V6.2 smart)</b>\n\n"
        f"🎬 <b>Title:</b> {title}\n"
        f"🆔 <b>Movie ID:</b> <code>{movie_id}</code>\n"
        f"🔗 <b>Source:</b> " + ("(masked)" if not VERBOSE else video_url) + "\n"
        f"📦 <b>Parts:</b> {len(segments)+1}\n"
        f"⏱️ <b>Time:</b> {elapsed//60}m {elapsed%60}s\n"
        f"🎞️ <b>Mode:</b> video={video_mode}, audio={audio_mode}\n"
        f"📏 <b>Segment:</b> {selected_seg_time}s (smart), max={doc['max_file_mb']}MB, hard={hard_mb}MB\n"
        f"🌐 <b>Worker:</b> {chosen_worker}\n"
        f"▶️ <b>Watch:</b> {watch_url}"
    )
    send_log_message(tokens[0], log_channel, log_text)
    print("DONE movie_id:", movie_id if VERBOSE else mm(movie_id))

if __name__ == "__main__":
    main()