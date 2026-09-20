from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

import yt_dlp
import os
import shutil
import threading
import time
import uuid
from pathlib import Path


app = FastAPI(title="Wave Download")

BASE_DIR = Path(__file__).resolve().parent


# =========================================================
# FFMPEG DETECTION
# =========================================================

def find_ffmpeg_dir():
    """
    Returns the folder containing ffmpeg (and ideally ffprobe), or None.
    1. Looks on the system PATH.
    2. Otherwise searches ./ffmpeg (next to this file), including nested
       folders such as ffmpeg/ffmpeg-7.1-essentials_build/bin/.
    """
    on_path = shutil.which("ffmpeg")
    if on_path:
        return str(Path(on_path).parent)

    local = BASE_DIR / "ffmpeg"
    if local.exists():
        for name in ("ffmpeg.exe", "ffmpeg"):
            for exe in local.rglob(name):
                if exe.is_file():
                    return str(exe.parent)

    return None


FFMPEG_DIR = find_ffmpeg_dir()

if FFMPEG_DIR is None:
    print("WARNING: ffmpeg not found. Merging video/audio and MP3 conversion will fail.")
else:
    print(f"Using ffmpeg from: {FFMPEG_DIR}")
    has_probe = any((Path(FFMPEG_DIR) / n).exists() for n in ("ffprobe.exe", "ffprobe"))
    if not has_probe:
        print("WARNING: ffprobe not found next to ffmpeg. Some conversions may fail.")


# =========================================================
# FOLDERS / STATIC / TEMPLATES
# =========================================================

DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static"
)

templates = Jinja2Templates(directory=str(BASE_DIR / "template"))


# =========================================================
# HOME
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )


# =========================================================
# SIZE ESTIMATION HELPERS
# =========================================================

def format_size(f):
    """Size in bytes of a single format, or None if unknown."""
    return f.get("filesize") or f.get("filesize_approx")


def estimate_total_bytes(info):
    """
    Exact total download size for the format(s) yt-dlp selected
    (video + audio streams added together). None if any size is unknown.
    """
    parts = info.get("requested_formats") or [info]
    sizes = [format_size(p) for p in parts]

    if all(sizes):
        return int(sum(sizes))

    return None


def estimate_sizes(info):
    """
    Approximate size for each quality option in the dropdown, worked out
    from the list of available formats. These are estimates: the real
    download may pick a slightly different stream.
    """
    formats = info.get("formats") or []

    def has(codec):
        return codec not in (None, "none")

    video_only = [
        f for f in formats
        if has(f.get("vcodec")) and not has(f.get("acodec")) and format_size(f)
    ]
    audio_only = [
        f for f in formats
        if has(f.get("acodec")) and not has(f.get("vcodec")) and format_size(f)
    ]
    combined = [
        f for f in formats
        if has(f.get("vcodec")) and has(f.get("acodec")) and format_size(f)
    ]

    best_audio = max(
        audio_only,
        key=lambda f: (f.get("abr") or 0, format_size(f)),
        default=None,
    )
    audio_bytes = format_size(best_audio) if best_audio else 0

    def pick_best(pool):
        return max(
            pool,
            key=lambda f: (
                f.get("height") or 0,
                f.get("fps") or 0,
                f.get("tbr") or 0,
            ),
        )

    def video_size(max_height):
        def allowed(f):
            return max_height is None or (f.get("height") or 0) <= max_height

        pool = [f for f in video_only if allowed(f)]
        if pool:
            return format_size(pick_best(pool)) + audio_bytes

        pool = [f for f in combined if allowed(f)]
        if pool:
            return format_size(pick_best(pool))

        return None

    return {
        "best": video_size(None),
        "1080": video_size(1080),
        "720": video_size(720),
        "480": video_size(480),
        "audio": audio_bytes or None,
    }


# =========================================================
# VIDEO INFORMATION
# =========================================================

@app.post("/api/info")
def get_video_info(url: str = Form(...)):
    try:
        options = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

        try:
            sizes = estimate_sizes(info)
        except Exception:
            sizes = {}

        return {
            "success": True,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "sizes": sizes,
            "url": url,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}


# =========================================================
# DOWNLOAD TASKS (in-memory progress tracking)
# =========================================================
# NOTE: TASKS lives in this process's memory, so run uvicorn with a
# single worker (the default). Multiple workers would not share it.

TASKS = {}
TASKS_LOCK = threading.Lock()

QUALITY_HEIGHTS = {"1080": 1080, "720": 720, "480": 480}


def set_task(task_id, **fields):
    with TASKS_LOCK:
        task = TASKS.get(task_id)

        # Once a task is cancelled, ignore late updates from the download thread
        if task is not None and not task["cancelled"]:
            task.update(fields)


def is_cancelled(task_id):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        return task is None or task["cancelled"]


def remove_task_files(task_id):
    """
    Deletes everything a download left behind: the finished file, .part
    files, and the temporary video/audio pieces created before merging.
    Retries briefly because Windows may still hold a file for a moment
    right after yt-dlp stops.
    """
    for _ in range(3):
        leftovers = list(DOWNLOAD_DIR.glob(f"*_{task_id}*"))

        if not leftovers:
            return

        for p in leftovers:
            try:
                p.unlink()
            except OSError:
                pass

        time.sleep(0.5)


def build_options(quality: str, output_template: str) -> dict:
    options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_DIR,
    }

    if quality == "audio":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        })
    else:
        height = QUALITY_HEIGHTS.get(quality)
        fmt = (
            f"bestvideo[height<={height}]+bestaudio/best"
            if height
            else "bestvideo+bestaudio/best"
        )
        options.update({
            "format": fmt,
            "merge_output_format": "mp4",
        })

    return options


def run_download(task_id: str, url: str, quality: str):
    # Video downloads are two streams (video + audio) that yt-dlp fetches
    # one after the other. When the total size is known, progress is
    # counted in bytes across both streams. If it isn't, we fall back to
    # splitting the bar evenly between the streams.
    expected_streams = 1 if quality == "audio" else 2
    state = {
        "finished_streams": 0,
        "finished_bytes": 0,
        "total": None,
    }

    def progress_hook(d):
        if is_cancelled(task_id):
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")

        if d["status"] == "downloading":
            stream_bytes = d.get("downloaded_bytes", 0)
            stream_total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = state["finished_bytes"] + stream_bytes

            total = state["total"]

            if total:
                # The estimate can be a little off; never go past 100%.
                total = max(total, downloaded)
                fraction = downloaded / total
            else:
                stream_fraction = (stream_bytes / stream_total) if stream_total else 0
                fraction = (
                    state["finished_streams"] + min(stream_fraction, 1)
                ) / expected_streams

            speed = d.get("speed")
            eta = d.get("eta")

            set_task(
                task_id,
                status="downloading",
                percent=round(min(fraction, 1) * 100, 1),
                total=total,
                speed=f"{speed / 1048576:.1f} MB/s" if speed else "",
                eta=f"{int(eta)}s" if eta is not None else "",
            )

        elif d["status"] == "finished":
            state["finished_streams"] += 1
            state["finished_bytes"] += (
                d.get("total_bytes") or d.get("downloaded_bytes") or 0
            )

            if not state["total"]:
                set_task(
                    task_id,
                    percent=round(
                        min(state["finished_streams"] / expected_streams, 1) * 100, 1
                    ),
                )

    def postprocessor_hook(d):
        if d["status"] == "started":
            if is_cancelled(task_id):
                raise yt_dlp.utils.DownloadCancelled("Cancelled by user")

            set_task(task_id, status="processing", percent=100, speed="", eta="")

    try:
        output_template = os.path.join(
            DOWNLOAD_DIR,
            f"%(title)s_{task_id}.%(ext)s"
        )

        options = build_options(quality, output_template)
        options["progress_hooks"] = [progress_hook]
        options["postprocessor_hooks"] = [postprocessor_hook]

        with yt_dlp.YoutubeDL(options) as ydl:

            # First look up the exact size of the chosen format(s).
            # Failing to find it is fine; the bar just won't show a total.
            try:
                info = ydl.extract_info(url, download=False)
                state["total"] = estimate_total_bytes(info)
                set_task(task_id, total=state["total"])
            except Exception:
                pass

            # Cancelled while we were looking up the size? Don't start.
            if is_cancelled(task_id):
                raise yt_dlp.utils.DownloadCancelled("Cancelled by user")

            ydl.extract_info(url, download=True)

        # Cancelled during the final merge/convert step (can't be interrupted)
        if is_cancelled(task_id):
            remove_task_files(task_id)
            return

        possible_files = [
            p for p in DOWNLOAD_DIR.glob(f"*_{task_id}.*")
            if p.suffix.lower() not in (".part", ".ytdl")
        ]

        if not possible_files:
            set_task(
                task_id,
                status="error",
                error="Download completed but the file could not be found.",
            )
            return

        file_path = max(possible_files, key=lambda p: p.stat().st_mtime)

        set_task(
            task_id,
            status="finished",
            percent=100,
            speed="",
            eta="",
            file=str(file_path),
        )

    except Exception as e:
        # A cancel makes yt-dlp stop with an exception; that's not an error.
        if not is_cancelled(task_id):
            set_task(task_id, status="error", error=str(e))

        remove_task_files(task_id)

    finally:
        # A cancelled task is finished with; forget it.
        with TASKS_LOCK:
            task = TASKS.get(task_id)
            if task is not None and task["cancelled"]:
                TASKS.pop(task_id, None)


@app.post("/api/download/start")
def start_download(
    url: str = Form(...),
    quality: str = Form("best")
):
    if FFMPEG_DIR is None:
        return {
            "success": False,
            "error": (
                "ffmpeg was not found. Put ffmpeg.exe (and ffprobe.exe) in an "
                "'ffmpeg' folder next to main.py, or add ffmpeg to your PATH, "
                "then restart the server."
            ),
        }

    task_id = uuid.uuid4().hex[:12]

    with TASKS_LOCK:
        TASKS[task_id] = {
            "status": "starting",
            "percent": 0,
            "total": None,
            "speed": "",
            "eta": "",
            "error": None,
            "file": None,
            "cancelled": False,
        }

    threading.Thread(
        target=run_download,
        args=(task_id, url, quality),
        daemon=True,
    ).start()

    return {"success": True, "task_id": task_id}


@app.post("/api/cancel/{task_id}")
def cancel_download(task_id: str):
    leftover_file = None

    with TASKS_LOCK:
        task = TASKS.get(task_id)

        if task is None:
            return {"success": False, "error": "Download not found."}

        if task["status"] == "finished":
            # It finished just as the user cancelled: throw the file away.
            leftover_file = task["file"]
            TASKS.pop(task_id, None)
        else:
            # The download thread notices this flag and stops.
            task["cancelled"] = True
            task["status"] = "cancelled"
            task["speed"] = ""
            task["eta"] = ""

    if leftover_file:
        Path(leftover_file).unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/progress/{task_id}")
def get_progress(task_id: str):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return {"status": "unknown", "percent": 0}

        return {
            "status": task["status"],
            "percent": task["percent"],
            "total": task["total"],
            "speed": task["speed"],
            "eta": task["eta"],
            "error": task["error"],
        }


@app.get("/api/file/{task_id}")
def get_file(task_id: str):
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        file_str = task["file"] if task and task["status"] == "finished" else None

    if not file_str or not Path(file_str).exists():
        return {"success": False, "error": "File not ready or already downloaded."}

    file_path = Path(file_str)

    media_type = (
        "audio/mpeg" if file_path.suffix.lower() == ".mp3" else "video/mp4"
    )

    def cleanup():
        file_path.unlink(missing_ok=True)
        with TASKS_LOCK:
            TASKS.pop(task_id, None)

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
        background=BackgroundTask(cleanup),
    )


# =========================================================
# API TEST
# =========================================================

@app.get("/api/hello")
async def hello():
    return {"message": "Wave Download API is working!"}




if __name__ == "__main__":
	import uvicorn

	uvicorn.run(app, host="127.0.0.1", port=8000)