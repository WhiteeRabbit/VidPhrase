# VidPhrase

<p align="center">
  <img src="https://github.com/user-attachments/assets/32dd351b-e977-44b1-923e-505e3f1aa8d2" width="969" alt="vidphrs">
</p>
<p align="center">
  <strong>
    Search phrases inside YouTube videos, uploaded video and audio files using subtitles, comments, descriptions, and AI-powered semantic search.
  </strong>
</p>

<p align="center">
  <a href="#-features">✨ Features</a> •
  <a href="#-how-it-works">🧠 How It Works</a> •
  <a href="#-tech-stack">🛠️ Tech Stack</a> •
  <a href="#-setup">⚙️ Setup</a> •
  <a href="#-cookies-helper">⚠️ Cookies Helper</a> •
  <a href="#-usage">▶️ Usage</a> •
  <a href="#-project-structure">📁 Structure</a>
</p>

---

## ✨ Features

- 🔎 Search phrases inside **YouTube videos**
- 🎬 Search inside **uploaded video files**
- 🎧 Search inside **uploaded audio files**
- 📝 Search using **YouTube subtitles**
- 💬 Search inside **YouTube comments**
- 📄 Search inside **video descriptions**
- 🧠 **AI-powered semantic search**
- 🌍 Multi-language subtitle support
- 🎙️ Local transcription using **Faster-Whisper**
- 📥 Download subtitles as text files
- ⚡ Fast and lightweight **Flask** web interface

---

## 🧠 How It Works

VidPhrase supports multiple search methods.

### 🔍 Exact & Fuzzy Search

Search directly inside:

- YouTube subtitles
- YouTube comments
- Video descriptions
- Uploaded video transcriptions
- Uploaded audio transcriptions

Even if the phrase contains typos or slightly different wording, fuzzy matching can still return relevant results.

### 🤖 AI Semantic Search

VidPhrase can search by **meaning**, not only by exact keywords.

The AI understands:

- synonyms and paraphrases
- abbreviations and acronyms
- broader and narrower concepts
- technologies, products, services, and brands related to the query
- explanations and examples that imply the same idea
- surrounding context even when the exact words never appear

#### Example

**Query**

```text
artificial intelligence
```

Possible matches:

- AI
- Machine Learning
- Deep Learning
- Neural Networks
- LLMs
- ChatGPT
- GPT
- Transformers
- Generative AI

---

**Query**

```text
cloud technologies
```

Possible matches:

- AWS
- Amazon Web Services
- Google Cloud
- Azure
- Kubernetes
- Docker
- Containers
- Serverless
- Cloud Infrastructure
- Virtual Machines

This allows you to discover moments that are conceptually related to your query instead of being limited to exact keyword matching.

---

## 🛠️ Tech Stack

- 🐍 Python
- 🌐 Flask
- 📺 yt-dlp
- 🎙️ faster-whisper
- 🔍 thefuzz
- 🤖 Google GenAI
- 🍪 browser-cookie3
- 🔧 Werkzeug

---

## ⚙️ Setup

### 1️⃣ Install Python

#### Windows

Download Python from:

https://www.python.org/downloads/

During installation, make sure to enable:

- ✅ Add Python to PATH
- ✅ Install pip

#### Ubuntu / Debian

```bash
sudo apt update
sudo apt install python3 python3-pip
```

---

### 2️⃣ Verify Installation

Windows:

```bash
python --version
pip --version
```

Linux:

```bash
python3 --version
pip3 --version
```

---

### 3️⃣ Clone the Repository

```bash
git clone https://github.com/yourusername/VidPhrase.git
cd VidPhrase
```

---

### 4️⃣ Create a Virtual Environment (Recommended)

```bash
python -m venv venv
```

#### Windows

```bash
venv\Scripts\activate
```

#### Linux / macOS

```bash
source venv/bin/activate
```

---

### 5️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

or

```bash
pip3 install -r requirements.txt
```

---

### 6️⃣ Configure Gemini API

VidPhrase uses **Google Gemini** for semantic search.

Open `app.py` and replace:

```python
client = genai.Client(api_key="GEMINI_API_TOKEN")
```

with your own API key:

```python
api_key="YOUR_GEMINI_API_KEY"
```

Without a valid Gemini API key, AI Semantic Search will not work.

---

## ⚠️ Cookies Helper

YouTube sometimes rate-limits requests and `yt-dlp` may fail with:

```text
HTTP Error 429: Too Many Requests
```

If this happens, generate fresh browser cookies by running:

```bash
python3 cookie_fetch_profiles.py
```

The script automatically extracts YouTube cookies from available Chrome and Firefox profiles and saves them inside the `cookies/` directory.

After generating the cookies, restart VidPhrase and try again.

---

### 7️⃣ Run the Application

Windows:

```bash
python app.py
```

Linux:

```bash
python3 app.py
```

Open:

```text
http://127.0.0.1:9005
```

---
## ▶️ Usage

1. Open VidPhrase in your browser.
2. Paste a YouTube link or upload a video/audio file.
3. Enter the phrase you want to find.
4. Choose the search method:

- 📝 Subtitle Search
- 💬 Comment & Description Search
- 🎙️ Whisper Transcription Search
- 🧠 AI Semantic Search

5. Browse the results.
6. Jump directly to the relevant moment and continue from the surrounding context.

---

## 📁 Project Structure

```text
VidPhrase/
├── app.py
├── cookie_fetch_profiles.py
├── requirements.txt
├── cookies/
│   ├── cookies_1.txt
│   ├── cookies_2.txt
│   ├── cookies_3.txt
│   ├── cookies_4.txt
│   └── cookies_fire.txt
├── templates/
├── static/
└── README.md
```

---

## 📝 Notes

- `re`
- `os`
- `json`
- `glob`
- `tempfile`
- `uuid`
- `shutil`
- `warnings`
- `io.BytesIO`

These modules are part of Python's standard library and do **not** need to be installed separately.

- Subtitle search uses fuzzy matching and may return closely related phrases.
- AI search is designed to find contextual and conceptual matches, not only exact words.
- Local video and audio files are automatically transcribed using Faster-Whisper.
- The application runs on port **9005** by default.

---

<p align="center">
  Made with ❤️ to make searching through videos and audio effortless.
</p>
