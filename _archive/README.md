# _archive — purani files (backup, active NAHI)

Ye folder me purane transcoder versions hain — **active NAHI**, sirf backup/reference
ke liye. GitHub Actions sirf `.github/workflows/` me se chalte hain, to yahan ki
workflows run NAHI hongi (yehi chahiye tha).

## Active files (main folders)
- `.github/workflows/transcode_worker_v6_smart.yml` → current transcoder
- `.github/workflows/prewarm_r2_sac.yml` → current prewarm (no-secret)
- `scripts/transcode_upload_worker_v6.py` → current transcoder (smart segmentation)

## Archived (purane versions)
- workflows: transcode_worker v1, v2, v3_20bots, v4_live, v5_dynamic
- scripts: tcr.py (container-aware intermediate), transcode_upload_worker v2, v3, v4_live, v5
