from flask import Flask, render_template, request, redirect, send_file
import re
import os
import json
import glob
import tempfile
import uuid
import shutil
import warnings
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


# HERE YOU CAN ADD YOUR YOUTUBE COOKIE FILES
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


def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is None:
        print("Loading Faster-Whisper model...")
        WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
    return WHISPER_MODEL


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

        score = fuzz.partial_ratio(query, normalized_text)
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
    return json.loads(response.text)


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

        score = fuzz.partial_ratio(query, normalized_text)
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

        score = fuzz.partial_ratio(query, normalized_text)
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


def transcribe_raw_video(video_path):
    model = get_whisper_model()
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        language=None,
        vad_filter=True
    )

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

        if query in normalized_text:
            score = 100
        else:
            score = fuzz.partial_ratio(query, normalized_text)

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
            search_lang=search_lang
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
            search_lang=search_lang
        )

    try:
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
            return render_template(
                "ai_search.html",
                results=None,
                error="No subtitles found for this language.",
                video_url=video_url,
                phrase=phrase,
                search_lang=search_lang
            )

        ai_matches = get_ai_answer(subtitles_list, phrase)
        
        results = []
        if isinstance(ai_matches, list):
            for match in ai_matches:
                start_seconds = match.get("start_time", 0)
                matched_phrase = match.get("matched_text", "")
                score = match.get("relevance_score", "AI Match")
                
                time_formatted = seconds_to_time(start_seconds)
                youtube_link = f"https://youtube.com/watch?v={video_id}&t={int(start_seconds)}s"
                
                results.append({
                    "percentage": f"AI Match ({score}%)" if isinstance(score, int) else "AI Match",
                    "text": matched_phrase,
                    "link": youtube_link,
                    "time": time_formatted,
                    "score": score if isinstance(score, int) else 0
                })

        results.sort(key=lambda x: x["score"], reverse=True)

        if not results:
            return render_template(
                "ai_search.html",
                results=None,
                error="AI couldn't find any matching contexts.",
                video_url=video_url,
                phrase=phrase,
                search_lang=search_lang
            )

        return render_template(
            "ai_search.html",
            results=results,
            error=None,
            video_url=video_url,
            phrase=phrase,
            search_lang=search_lang
        )

    except Exception as e:
        return render_template(
            "ai_search.html",
            results=None,
            error=f"Error: {str(e)}",
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
            search_lang=search_lang
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
            search_lang=search_lang
        )

    try:
        if search_type == "comment":
            results = search_in_comments_and_description(normalized_url, phrase)
        else:
            results = search_in_subtitles(normalized_url, phrase, lang=search_lang)

        if not results:
            return render_template(
                "index.html",
                results=None,
                error="Phrase not found",
                video_url=video_url,
                phrase=phrase,
                search_type=search_type,
                search_lang=search_lang
            )

        return render_template(
            "index.html",
            results=results,
            error=None,
            video_url=video_url,
            phrase=phrase,
            search_type=search_type,
            search_lang=search_lang
        )

    except Exception as e:
        return render_template(
            "index.html",
            results=None,
            error=f"Error: {str(e)}",
            video_url=video_url,
            phrase=phrase,
            search_type=search_type,
            search_lang=search_lang
        )


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
            search_mode="basic"
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
                search_mode="basic"
            )

        filename = secure_filename(file.filename)
        tmpdir = tempfile.mkdtemp(prefix="raw_whisper_")
        video_path = os.path.join(tmpdir, filename)
        file.save(video_path)

        try:
            transcript_rows, info = transcribe_raw_video(video_path)
            transcript_id = str(uuid.uuid4())

            RAW_TRANSCRIPTS[transcript_id] = {
                "segments": transcript_rows,
                "video_path": video_path,
                "tmpdir": tmpdir,
                "filename": filename,
                "language": getattr(info, "language", None),
            }

            upload_info = f"Video uploaded successfully. Detected language: {getattr(info, 'language', 'unknown')}"
            return render_template(
                "whisper_search.html",
                transcript_ready=True,
                results=None,
                error=None,
                upload_info=upload_info,
                uploaded_filename=filename,
                transcript_id=transcript_id,
                phrase="",
                search_mode="basic"
            )

        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return render_template(
                "whisper_search.html",
                transcript_ready=False,
                results=None,
                error=f"Transcription error: {str(e)}",
                upload_info=None,
                uploaded_filename=None,
                transcript_id=None,
                phrase="",
                search_mode="basic"
            )

    if action == "search":
        transcript_id = request.form.get("transcript_id", "").strip()
        phrase = request.form.get("phrase", "").strip()
        search_mode = request.form.get("search_mode", "basic").strip().lower()

        if not transcript_id or transcript_id not in RAW_TRANSCRIPTS:
            return render_template(
                "whisper_search.html",
                transcript_ready=False,
                results=None,
                error="Upload a video first.",
                upload_info=None,
                uploaded_filename=None,
                transcript_id=None,
                phrase=phrase,
                search_mode=search_mode
            )

        if not phrase:
            entry = RAW_TRANSCRIPTS.get(transcript_id, {})
            return render_template(
                "whisper_search.html",
                transcript_ready=True,
                results=None,
                error="Please enter a search phrase.",
                upload_info=None,
                uploaded_filename=entry.get("filename"),
                transcript_id=transcript_id,
                phrase=phrase,
                search_mode=search_mode
            )

        entry = RAW_TRANSCRIPTS.get(transcript_id, {})
        segments = entry.get("segments", [])

        try:
            if search_mode == "ai":
                ai_input = [
                    {
                        "start": seg.get("start", 0),
                        "text": seg.get("text", "")
                    }
                    for seg in segments
                ]

                ai_matches = get_ai_answer(ai_input, phrase)
                results = []

                if isinstance(ai_matches, list):
                    for match in ai_matches:
                        start_seconds = match.get("start_time", 0)
                        matched_phrase = match.get("matched_text", "")
                        score = match.get("relevance_score", "AI Match")

                        results.append({
                            "percentage": f"AI Match ({score}%)" if isinstance(score, int) else "AI Match",
                            "text": matched_phrase,
                            "time": seconds_to_time(start_seconds),
                            "score": score if isinstance(score, int) else 0,
                        })

                results.sort(key=lambda x: x["score"], reverse=True)

            else:
                results = search_in_raw_segments(segments, phrase)

            if not results:
                return render_template(
                    "whisper_search.html",
                    transcript_ready=True,
                    results=None,
                    error="Phrase not found.",
                    upload_info=None,
                    uploaded_filename=entry.get("filename"),
                    transcript_id=transcript_id,
                    phrase=phrase,
                    search_mode=search_mode
                )

            return render_template(
                "whisper_search.html",
                transcript_ready=True,
                results=results,
                error=None,
                upload_info=None,
                uploaded_filename=entry.get("filename"),
                transcript_id=transcript_id,
                phrase=phrase,
                search_mode=search_mode
            )

        except Exception as e:
            return render_template(
                "whisper_search.html",
                transcript_ready=True,
                results=None,
                error=f"Error: {str(e)}",
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
        search_mode="basic"
    )
    
@app.route('/download_whisper_subtitles', methods=['POST'])
def download_whisper_subtitles():
    transcript_id = request.form.get("transcript_id", "").strip()

    entry = RAW_TRANSCRIPTS.get(transcript_id)
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
    app.run(debug=False, port=9005)
