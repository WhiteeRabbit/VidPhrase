from flask import Flask, render_template, request, redirect, send_file, jsonify
import re
import os
import json
import glob
import tempfile
import uuid
import shutil
import warnings
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from yt_dlp import YoutubeDL
from thefuzz import fuzz
from google import genai
from google.genai import types
from faster_whisper import WhisperModel
from werkzeug.utils import secure_filename

warnings.filterwarnings("ignore")
os.environ["GRPC_VERBOSITY"] = "NONE"

app = Flask(__name__)

SUPPORTED_LANGS = ["en", "ru", "it", "tr", "az", "fr", "hi", "de", "ja"]


COOKIE_FILES = [
    "./cookies/cookies_1.txt",
    "./cookies/cookies_2.txt",
    "./cookies/cookies_3.txt",
    "./cookies/cookies_4.txt",
    "./cookies/cookies_fire.txt"
]

THRESHOLD = 68
RAW_THRESHOLD = 69

client = genai.Client(api_key="YOUR_GEMINI_API_TOKEN")

RAW_TRANSCRIPTS = {}
WHISPER_MODEL = None
WHISPER_MODEL_LOCK = threading.Lock()
RAW_TRANSCRIPTS_LOCK = threading.RLock()
BACKGROUND_TASKS = {}
BACKGROUND_TASKS_LOCK = threading.RLock()
BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="vidphrase")
RAW_TRANSCRIPT_TTL = 2 * 60 * 60
TASK_TTL = 2 * 60 * 60
CLEANUP_INTERVAL = 10 * 60


def get_whisper_device():
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        with WHISPER_MODEL_LOCK:
            if WHISPER_MODEL is None:
                device, compute_type = get_whisper_device()
                print(f"Loading Faster-Whisper model on {device} with {compute_type}...")
                try:
                    WHISPER_MODEL = WhisperModel(
                        "base",
                        device=device,
                        compute_type=compute_type
                    )
                except Exception:
                    if device != "cpu":
                        WHISPER_MODEL = WhisperModel(
                            "base",
                            device="cpu",
                            compute_type="int8"
                        )
                    else:
                        raise
    return WHISPER_MODEL


def update_task(task_id, **values):
    with BACKGROUND_TASKS_LOCK:
        task = BACKGROUND_TASKS.get(task_id)
        if not task:
            return
        task.update(values)
        task["updated_at"] = time.time()


def submit_background_task(task_type, function, *args, task_data=None, **kwargs):
    task_id = str(uuid.uuid4())
    now = time.time()

    with BACKGROUND_TASKS_LOCK:
        BACKGROUND_TASKS[task_id] = {
            "id": task_id,
            "type": task_type,
            "status": "pending",
            "progress": 0,
            "message": "Task queued",
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now
        }
        if task_data:
            BACKGROUND_TASKS[task_id].update(task_data)

    try:
        BACKGROUND_EXECUTOR.submit(
            run_background_task,
            task_id,
            function,
            args,
            kwargs
        )
    except Exception:
        with BACKGROUND_TASKS_LOCK:
            BACKGROUND_TASKS.pop(task_id, None)
        raise

    return task_id


def run_background_task(task_id, function, args, kwargs):
    update_task(
        task_id,
        status="running",
        progress=1,
        message="Task started"
    )

    try:
        result = function(task_id, *args, **kwargs)
        update_task(
            task_id,
            status="completed",
            progress=100,
            message="Task completed",
            result=result,
            error=None
        )
    except Exception as exc:
        update_task(
            task_id,
            status="failed",
            progress=100,
            message="Task failed",
            error=str(exc)
        )


def get_transcript(transcript_id):
    with RAW_TRANSCRIPTS_LOCK:
        entry = RAW_TRANSCRIPTS.get(transcript_id)
        if not entry:
            return None
        entry["last_accessed"] = time.time()
        return dict(entry)


def cleanup_old_data():
    now = time.time()
    active_tmpdirs = set()

    with BACKGROUND_TASKS_LOCK:
        for task in BACKGROUND_TASKS.values():
            if task.get("status") in {"pending", "running"}:
                tmpdir = task.get("tmpdir")
                if tmpdir:
                    active_tmpdirs.add(os.path.abspath(tmpdir))

        stale_tasks = [
            task_id
            for task_id, task in BACKGROUND_TASKS.items()
            if task.get("status") in {"completed", "failed"}
            and now - task.get("updated_at", now) > TASK_TTL
        ]

        for task_id in stale_tasks:
            BACKGROUND_TASKS.pop(task_id, None)

    with RAW_TRANSCRIPTS_LOCK:
        stale_transcripts = [
            transcript_id
            for transcript_id, entry in RAW_TRANSCRIPTS.items()
            if now - entry.get("last_accessed", entry.get("created_at", now)) > RAW_TRANSCRIPT_TTL
        ]

        for transcript_id in stale_transcripts:
            RAW_TRANSCRIPTS.pop(transcript_id, None)

    temp_root = Path(tempfile.gettempdir())
    try:
        for tmpdir in temp_root.glob("raw_whisper_*"):
            absolute_path = os.path.abspath(str(tmpdir))
            if absolute_path in active_tmpdirs:
                continue
            try:
                if now - tmpdir.stat().st_mtime > RAW_TRANSCRIPT_TTL:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        pass

    timer = threading.Timer(CLEANUP_INTERVAL, cleanup_old_data)
    timer.daemon = True
    timer.start()


cleanup_old_data()


def format_time(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02}:{m:02}:{s:02}"


def extract_video_id(url):
    patterns = [
        r"v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def seconds_to_time(sec):
    sec = int(float(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def normalize(text):
    text = str(text).lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_text(event):
    if "segs" not in event:
        return ""
    return "".join(seg.get("utf8", "") for seg in event["segs"]).replace("\n", " ").strip()


def download_subs(video_url, lang="en"):
    for cookie in COOKIE_FILES:
        if not os.path.exists(cookie):
            print(f"Skipping {cookie}: File not found.")
            continue

        print(f"Attempting with {cookie}...")

        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                "skip_download": True,
                "writeautomaticsub": True,
                "writesubtitles": True,
                "subtitleslangs": [lang],
                "subtitlesformat": "json3",
                "cookiefile": cookie,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                },
                "outtmpl": os.path.join(tmpdir, "sub.%(ext)s"),
                "quiet": False,
                "no_warnings": False,
            }

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(video_url, download=True)
                    print("Available subtitles:", info.get("subtitles", {}).keys())
                    print("Available auto captions:", info.get("automatic_captions", {}).keys())

                files = (
                    glob.glob(os.path.join(tmpdir, "*.json3")) +
                    glob.glob(os.path.join(tmpdir, "*.vtt")) +
                    glob.glob(os.path.join(tmpdir, "*.srv3")) +
                    glob.glob(os.path.join(tmpdir, "*.ttml")) +
                    glob.glob(os.path.join(tmpdir, "*.xml"))
                )

                if not files:
                    raise FileNotFoundError("No subtitle file was downloaded for this video/language")

                with open(files[0], "r", encoding="utf-8") as f:
                    return json.load(f)

            except Exception as e:
                print(f"An error occurred with {cookie}: {e}. Trying next...")
                continue

    raise RuntimeError("All cookie files failed or no subtitles were found.")

def fuzzy_score(query, normalized_text):
    if query in normalized_text:
        return 100

    query_words = set(query.split())
    text_words = set(normalized_text.split())

    if not query_words.intersection(text_words):
        return 0

    return fuzz.partial_ratio(query, normalized_text)


def search_in_subtitles(video_url, phrase, lang="en"):
    query = normalize(phrase)
    if len(query) < 3:
        raise ValueError("Search phrase is too short")

    data = download_subs(video_url, lang=lang)
    rows = []
    matches = []

    for event in data.get("events", []):
        text = extract_text(event)
        if len(text.strip()) < 3:
            continue
        start = event.get("tStartMs", 0) / 1000
        time_str = seconds_to_time(start)
        rows.append((time_str, text, start))

    video_id = extract_video_id(video_url)
    if not video_id:
        raise ValueError("Invalid YouTube URL")

    for time_str, original_text, start in rows:
        normalized_text = normalize(original_text)
        if len(normalized_text) < max(4, len(query)):
            continue

        score = fuzzy_score(query, normalized_text)
        if score >= THRESHOLD:
            link = f"https://youtube.com/watch?v={video_id}&t={int(start)}s"
            matches.append({
                "percentage": score,
                "text": original_text,
                "link": link,
                "time": time_str,
            })

    matches.sort(key=lambda x: (-x["percentage"], x["time"]))
    return matches


def extract_comment_text(comment):
    if not isinstance(comment, dict):
        return ""
    text = (
        comment.get("text")
        or comment.get("content")
        or comment.get("comment")
        or comment.get("body")
        or ""
    )
    if isinstance(text, list):
        text = " ".join(str(x) for x in text)
    return str(text).replace("\n", " ").strip()


def collect_comments(comments, out_list):
    for c in comments or []:
        text = extract_comment_text(c)
        if text:
            out_list.append(text)
        replies = c.get("replies")
        if isinstance(replies, dict):
            collect_comments(replies.get("comments", []), out_list)
        elif isinstance(replies, list):
            collect_comments(replies, out_list)


def download_data(video_url):
    ydl_opts = {
        "skip_download": True,
        "getcomments": True,
        "quiet": True,
        "no_warnings": True,
        "max_comments": 3000,
        "comment_sort": "top",
        "http_headers": {
            "User-Agent": "Mozilla/5.0"
        },
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)

    comments = []
    collect_comments(info.get("comments", []), comments)
    description = info.get("description", "") or ""
    return comments, description


def parse_ai_response(response_text):
    if not response_text:
        raise ValueError("Gemini returned an empty response")

    text = str(response_text).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)

    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        array_match = re.search(r"\[[\s\S]*\]", text)
        if not array_match:
            raise ValueError("Gemini returned invalid JSON")
        parsed = json.loads(array_match.group(0))

    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as exc:
            raise ValueError("Gemini returned invalid JSON string") from exc

    if not isinstance(parsed, list):
        raise ValueError("Gemini response must be a JSON array")

    return parsed


def get_ai_answer(subtitles, user_query):
    prompt = f"""
You are an advanced semantic video search engine with deep contextual understanding.

Analyze the provided subtitles and find ALL moments that are related to the user's query, even if the exact words are never mentioned.

SEARCH STRATEGY:

1. Match direct mentions.
2. Match synonyms.
3. Match abbreviations and acronyms.
4. Match broader concepts.
5. Match narrower concepts.
6. Match related technologies.
7. Match products, platforms, frameworks, vendors, brands, and services commonly associated with the query.
8. Match descriptions, explanations, examples, use cases, analogies, and discussions that imply the same idea.

Examples:

- Query: "cloud technologies"
  Match:
  AWS, Amazon Web Services, EC2, S3,
  Google Cloud, GCP,
  Azure, Microsoft Azure,
  Kubernetes, Docker,
  cloud infrastructure,
  cloud computing,
  distributed systems,
  serverless,
  SaaS, PaaS, IaaS,
  hosting platforms,
  virtual machines,
  containers.

- Query: "artificial intelligence"
  Match:
  AI, machine learning, ML,
  neural networks,
  LLM,
  ChatGPT,
  GPT,
  transformers,
  deep learning,
  computer vision,
  generative AI.

- Query: "car"
  Match:
  vehicle,
  automobile,
  sedan,
  SUV,
  truck,
  BMW,
  Mercedes,
  Tesla,
  driving,
  transportation.

IMPORTANT:

- Do not require keyword overlap.
- Use conceptual understanding.
- Find every semantically relevant segment.
- Multiple results are preferred over missing relevant content.
- Return all relevant matches.
- If uncertain, include the result rather than excluding it.

For each result provide:

{{
  "start_time": "...",
  "text": "...",
  "relevance_score": 50-100
}}

User Query:"{user_query}"
    
    Subtitles (JSON format):
    {json.dumps(subtitles, ensure_ascii=False)}
    """
    
    response = client.models.generate_content(
        model='gemini-3.1-flash-lite',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "start_time": {"type": "NUMBER", "description": "The exact start time in seconds (float or int)"},
                        "matched_text": {"type": "STRING", "description": "The exact text phrase from subtitles that matched"},
                        "relevance_score": {"type": "INTEGER", "description": "Semantic matching confidence score from 50 to 100"}
                    },
                    "required": ["start_time", "matched_text", "relevance_score"],
                },
            },
            temperature=0.2
        ),
    )
    return parse_ai_response(response.text)


def search_in_comments_and_description(video_url, phrase):
    query = normalize(phrase)
    if len(query) < 3:
        raise ValueError("Search phrase is too short")

    comments, description = download_data(video_url)
    normalized_url = extract_video_url(video_url)
    if not normalized_url:
        raise ValueError("Invalid YouTube URL")

    matches = []

    for idx, line in enumerate(description.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue

        normalized_text = normalize(text)
        if len(normalized_text) < max(4, len(query)):
            continue

        score = fuzzy_score(query, normalized_text)
        if score >= THRESHOLD:
            matches.append({
                "percentage": score,
                "text": text,
                "link": normalized_url,
                "location": f"line {idx}",
                "source": "description",
            })

    for idx, text in enumerate(comments, start=1):
        normalized_text = normalize(text)
        if len(normalized_text) < max(4, len(query)):
            continue

        score = fuzzy_score(query, normalized_text)
        if score >= THRESHOLD:
            matches.append({
                "percentage": score,
                "text": text,
                "link": normalized_url,
                "location": f"comment {idx}",
                "source": "comment",
            })

    matches.sort(key=lambda x: (-x["percentage"], x["source"], x["location"]))
    return matches


def extract_video_url(url):
    patterns = [
        r"v=([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            return f"https://www.youtube.com/watch?v={video_id}"
    return None


def transcribe_raw_video(video_path, progress_callback=None):
    model = get_whisper_model()

    with WHISPER_MODEL_LOCK:
        segments, info = model.transcribe(
            video_path,
            beam_size=5,
            language=None,
            vad_filter=True
        )

        duration = float(getattr(info, "duration", 0) or 0)
        rows = []

        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue

            rows.append({
                "start": float(segment.start),
                "time": format_time(segment.start),
                "text": text
            })

            if progress_callback and duration > 0:
                progress = min(99, max(1, int((float(segment.end) / duration) * 100)))
                progress_callback(progress, "Transcribing video")

    return rows, info


def search_in_raw_segments(segments, phrase):
    query = normalize(phrase)
    if len(query) < 3:
        raise ValueError("Search phrase is too short")

    matches = []

    for seg in segments:
        text = str(seg.get("text", "")).strip()
        if len(text) < 3:
            continue

        normalized_text = normalize(text)
        if len(normalized_text) < max(4, len(query)):
            continue

        score = fuzzy_score(query, normalized_text)

        if score >= RAW_THRESHOLD:
            matches.append({
                "percentage": score,
                "text": text,
                "time": seg.get("time", seconds_to_time(seg.get("start", 0))),
                "start": float(seg.get("start", 0)),
            })

    matches.sort(key=lambda x: (-x["percentage"], x["start"]))
    return matches


@app.route('/')
def index():
    return render_template('index.html')


def run_ai_search_task(task_id, normalized_url, phrase, search_lang):
    update_task(task_id, progress=10, message="Downloading subtitles")
    raw_data = download_subs(normalized_url, lang=search_lang)
    subtitles_list = []

    for event in raw_data.get("events", []):
        text = extract_text(event)
        if len(text.strip()) < 3:
            continue
        start = event.get("tStartMs", 0) / 1000
        subtitles_list.append({
            "start": start,
            "text": text
        })

    if not subtitles_list:
        raise ValueError("No subtitles found for this language.")

    update_task(task_id, progress=35, message="Analyzing subtitles with Gemini")
    ai_matches = get_ai_answer(subtitles_list, phrase)
    results = []
    video_id = extract_video_id(normalized_url)

    if isinstance(ai_matches, list):
        for match in ai_matches:
            start_seconds = match.get("start_time", 0)
            matched_phrase = match.get("matched_text", "")
            score = match.get("relevance_score", 0)

            try:
                numeric_score = int(float(score))
            except (TypeError, ValueError):
                numeric_score = 0

            try:
                start_value = float(start_seconds)
            except (TypeError, ValueError):
                start_value = 0

            results.append({
                "percentage": f"AI Match ({numeric_score}%)" if numeric_score else "AI Match",
                "text": matched_phrase,
                "link": f"https://youtube.com/watch?v={video_id}&t={int(start_value)}s",
                "time": seconds_to_time(start_value),
                "score": numeric_score
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    update_task(task_id, progress=95, message="Finalizing results")

    return {
        "results": results,
        "video_url": normalized_url,
        "phrase": phrase,
        "search_lang": search_lang
    }


@app.route('/task_status/<task_id>', methods=['GET'])
def task_status(task_id):
    with BACKGROUND_TASKS_LOCK:
        task = BACKGROUND_TASKS.get(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        result = {
            "id": task["id"],
            "type": task["type"],
            "status": task["status"],
            "progress": task["progress"],
            "message": task["message"],
            "error": task["error"]
        }

        if task["status"] == "completed":
            result["result"] = task["result"]

    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


def render_async_page(template, task_id, **context):
    context["task_id"] = task_id
    context["task_status_url"] = f"/task_status/{task_id}"
    return render_template(template, **context)


@app.route('/ai_search', methods=['GET', 'POST'])
def handle_ai_search():
    if request.method == 'GET':
        return render_template('ai_search.html', search_lang="en")

    video_url = request.form.get("video_url", "").strip()
    phrase = request.form.get("phrase", "").strip()
    search_lang = request.form.get("search_lang", "en").strip().lower()

    if search_lang not in SUPPORTED_LANGS:
        search_lang = "en"

    if not video_url or not phrase:
        return render_template(
            "ai_search.html",
            results=None,
            error="Please fill all fields!",
            video_url=video_url,
            phrase=phrase,
            search_lang=search_lang,
            task_id=None,
            task_status_url=None
        )

    normalized_url = extract_video_url(video_url)
    video_id = extract_video_id(video_url)
    if not normalized_url or not video_id:
        return render_template(
            "ai_search.html",
            results=None,
            error="Not valid youtube link",
            video_url=video_url,
            phrase=phrase,
            search_lang=search_lang,
            task_id=None,
            task_status_url=None
        )

    task_id = submit_background_task(
        "ai_search",
        run_ai_search_task,
        normalized_url,
        phrase,
        search_lang
    )

    return render_async_page(
        "ai_search.html",
        task_id,
        results=None,
        error="Search started. Check task status for results.",
        video_url=video_url,
        phrase=phrase,
        search_lang=search_lang
    )


@app.route('/download_subtitles', methods=['POST'])
def download_subtitles():
    video_url = request.form.get("video_url", "").strip()
    search_lang = request.form.get("search_lang", "en").strip().lower()

    if search_lang not in SUPPORTED_LANGS:
        search_lang = "en"

    normalized_url = extract_video_url(video_url)
    video_id = extract_video_id(video_url)

    if not normalized_url or not video_id:
        return redirect("/")

    try:
        data = download_subs(normalized_url, lang=search_lang)

        lines = []
        for event in data.get("events", []):
            text = extract_text(event)
            if not text:
                continue
            start = seconds_to_time(event.get("tStartMs", 0) / 1000)
            lines.append(f"[{start}] {text}")

        content = "\n".join(lines) if lines else "No subtitles found."
        buffer = BytesIO(content.encode("utf-8"))
        buffer.seek(0)

        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{video_id}_{search_lang}_subtitles.txt",
            mimetype="text/plain; charset=utf-8"
        )

    except Exception as e:
        return f"Error: {str(e)}", 500


def run_search_task(task_id, normalized_url, phrase, search_type, search_lang):
    update_task(task_id, progress=10, message="Starting search")

    if search_type == "comment":
        results = search_in_comments_and_description(normalized_url, phrase)
    else:
        results = search_in_subtitles(normalized_url, phrase, lang=search_lang)

    update_task(task_id, progress=95, message="Finalizing results")

    return {
        "results": results,
        "video_url": normalized_url,
        "phrase": phrase,
        "search_type": search_type,
        "search_lang": search_lang
    }


@app.route('/search', methods=['GET', 'POST'])
def handle_search():
    if request.method == 'GET':
        return redirect('/')

    video_url = request.form.get("video_url", "").strip()
    phrase = request.form.get("phrase", "").strip()
    search_type = request.form.get("search_type", "video").strip().lower()
    search_lang = request.form.get("search_lang", "en").strip().lower()

    if search_lang not in SUPPORTED_LANGS:
        search_lang = "en"

    if not video_url or not phrase:
        return render_template(
            "index.html",
            results=None,
            error="Please fill all fields!",
            video_url=video_url,
            phrase=phrase,
            search_type=search_type,
            search_lang=search_lang,
            task_id=None,
            task_status_url=None
        )

    normalized_url = extract_video_url(video_url)

    if not normalized_url:
        return render_template(
            "index.html",
            results=None,
            error="Not valid youtube link",
            video_url=video_url,
            phrase=phrase,
            search_type=search_type,
            search_lang=search_lang,
            task_id=None,
            task_status_url=None
        )

    task_id = submit_background_task(
        "search",
        run_search_task,
        normalized_url,
        phrase,
        search_type,
        search_lang
    )

    return render_async_page(
        "index.html",
        task_id,
        results=None,
        error="Search started. Check task status for results.",
        video_url=video_url,
        phrase=phrase,
        search_type=search_type,
        search_lang=search_lang
    )


def run_whisper_upload_task(task_id, video_path, tmpdir, filename):
    try:
        transcript_rows, info = transcribe_raw_video(
            video_path,
            progress_callback=lambda progress, message: update_task(
                task_id,
                progress=progress,
                message=message
            )
        )

        transcript_id = str(uuid.uuid4())
        now = time.time()

        with RAW_TRANSCRIPTS_LOCK:
            RAW_TRANSCRIPTS[transcript_id] = {
                "segments": transcript_rows,
                "filename": filename,
                "language": getattr(info, "language", None),
                "created_at": now,
                "last_accessed": now
            }

        return {
            "transcript_id": transcript_id,
            "filename": filename,
            "language": getattr(info, "language", None)
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        with BACKGROUND_TASKS_LOCK:
            task = BACKGROUND_TASKS.get(task_id)
            if task is not None:
                task.pop("tmpdir", None)


def run_whisper_search_task(task_id, transcript_id, phrase, search_mode):
    entry = get_transcript(transcript_id)
    if not entry:
        raise ValueError("Transcript not found or expired")

    segments = entry.get("segments", [])

    if search_mode == "ai":
        update_task(task_id, progress=20, message="Preparing AI search")
        ai_input = [
            {
                "start": seg.get("start", 0),
                "text": seg.get("text", "")
            }
            for seg in segments
        ]

        update_task(task_id, progress=40, message="Searching with Gemini")
        ai_matches = get_ai_answer(ai_input, phrase)
        results = []

        if isinstance(ai_matches, list):
            for match in ai_matches:
                start_seconds = match.get("start_time", 0)
                matched_phrase = match.get("matched_text", "")
                score = match.get("relevance_score", "AI Match")

                try:
                    numeric_score = int(float(score))
                except (TypeError, ValueError):
                    numeric_score = 0

                try:
                    start_value = float(start_seconds)
                except (TypeError, ValueError):
                    start_value = 0

                results.append({
                    "percentage": f"AI Match ({numeric_score}%)" if numeric_score else "AI Match",
                    "text": matched_phrase,
                    "time": seconds_to_time(start_value),
                    "score": numeric_score
                })

        results.sort(key=lambda x: x["score"], reverse=True)
    else:
        update_task(task_id, progress=20, message="Searching transcript")
        results = search_in_raw_segments(segments, phrase)
        update_task(task_id, progress=90, message="Finalizing results")

    return {
        "transcript_id": transcript_id,
        "filename": entry.get("filename"),
        "results": results,
        "phrase": phrase,
        "search_mode": search_mode
    }


@app.route('/whisper_search', methods=['GET', 'POST'])
def raw_search():
    if request.method == 'GET':
        return render_template(
            "whisper_search.html",
            transcript_ready=False,
            results=None,
            error=None,
            upload_info=None,
            uploaded_filename=None,
            transcript_id=None,
            phrase="",
            search_mode="basic",
            task_id=None,
            task_status_url=None
        )

    action = request.form.get("action", "").strip().lower()

    if action == "upload":
        file = request.files.get("video_file")
        if not file or not file.filename:
            return render_template(
                "whisper_search.html",
                transcript_ready=False,
                results=None,
                error="Please choose a video file.",
                upload_info=None,
                uploaded_filename=None,
                transcript_id=None,
                phrase="",
                search_mode="basic",
                task_id=None,
                task_status_url=None
            )

        filename = secure_filename(file.filename) or "video"
        tmpdir = tempfile.mkdtemp(prefix="raw_whisper_")
        video_path = os.path.join(tmpdir, filename)

        try:
            file.save(video_path)
        except Exception as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return render_template(
                "whisper_search.html",
                transcript_ready=False,
                results=None,
                error=f"Upload error: {str(exc)}",
                upload_info=None,
                uploaded_filename=None,
                transcript_id=None,
                phrase="",
                search_mode="basic",
                task_id=None,
                task_status_url=None
            )

        try:
            task_id = submit_background_task(
                "whisper_transcription",
                run_whisper_upload_task,
                video_path,
                tmpdir,
                filename,
                task_data={"tmpdir": tmpdir}
            )
        except Exception as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return render_template(
                "whisper_search.html",
                transcript_ready=False,
                results=None,
                error=f"Task error: {str(exc)}",
                upload_info=None,
                uploaded_filename=None,
                transcript_id=None,
                phrase="",
                search_mode="basic",
                task_id=None,
                task_status_url=None
            )

        return render_async_page(
            "whisper_search.html",
            task_id,
            transcript_ready=False,
            results=None,
            error="Transcription started. Check task status for progress.",
            upload_info=None,
            uploaded_filename=filename,
            transcript_id=None,
            phrase="",
            search_mode="basic"
        )

    if action == "search":
        transcript_id = request.form.get("transcript_id", "").strip()
        phrase = request.form.get("phrase", "").strip()
        search_mode = request.form.get("search_mode", "basic").strip().lower()
        entry = get_transcript(transcript_id) if transcript_id else None

        if not transcript_id or not entry:
            return render_template(
                "whisper_search.html",
                transcript_ready=False,
                results=None,
                error="Upload a video first or the transcript has expired.",
                upload_info=None,
                uploaded_filename=None,
                transcript_id=None,
                phrase=phrase,
                search_mode=search_mode,
                task_id=None,
                task_status_url=None
            )

        if not phrase:
            return render_template(
                "whisper_search.html",
                transcript_ready=True,
                results=None,
                error="Please enter a search phrase.",
                upload_info=None,
                uploaded_filename=entry.get("filename"),
                transcript_id=transcript_id,
                phrase=phrase,
                search_mode=search_mode,
                task_id=None,
                task_status_url=None
            )

        try:
            task_id = submit_background_task(
                "whisper_search",
                run_whisper_search_task,
                transcript_id,
                phrase,
                search_mode
            )
        except Exception as exc:
            return render_template(
                "whisper_search.html",
                transcript_ready=True,
                results=None,
                error=f"Task error: {str(exc)}",
                upload_info=None,
                uploaded_filename=entry.get("filename"),
                transcript_id=transcript_id,
                phrase=phrase,
                search_mode=search_mode,
                task_id=None,
                task_status_url=None
            )

        return render_async_page(
            "whisper_search.html",
            task_id,
            transcript_ready=True,
            results=None,
            error="Search started. Check task status for results.",
            upload_info=None,
            uploaded_filename=entry.get("filename"),
            transcript_id=transcript_id,
            phrase=phrase,
            search_mode=search_mode
        )

    return render_template(
        "whisper_search.html",
        transcript_ready=False,
        results=None,
        error="Invalid action.",
        upload_info=None,
        uploaded_filename=None,
        transcript_id=None,
        phrase="",
        search_mode="basic",
        task_id=None,
        task_status_url=None
    )


@app.route('/download_whisper_subtitles', methods=['POST'])
def download_whisper_subtitles():
    transcript_id = request.form.get("transcript_id", "").strip()

    entry = get_transcript(transcript_id)
    if not entry:
        return "Transcript not found", 404

    segments = entry.get("segments", [])
    original_filename = entry.get("filename", "transcript")

    lines = []
    for seg in segments:
        lines.append(f"[{seg.get('time')}] {seg.get('text')}")

    content = "\n".join(lines)

    buffer = BytesIO(content.encode("utf-8"))
    buffer.seek(0)

    base_name = os.path.splitext(original_filename)[0]

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{base_name}_subtitles.txt",
        mimetype="text/plain"
    )

if __name__ == '__main__':
    app.run(debug=False, port=9005, threaded=True)
