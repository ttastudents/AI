#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AnonToken Chat — giao diện chat giống ChatGPT / Open WebUI trong 1 file duy nhất.

Cách chạy:
    python3 app.py

Cấu hình qua biến môi trường (tuỳ chọn):
    LLM_API_BASE  : URL API chuẩn OpenAI-compatible (mặc định: http://localhost:11434/v1 — Ollama)
    LLM_API_KEY   : API key nếu backend yêu cầu (mặc định: rỗng)
    LLM_MODEL     : model mặc định nếu backend không trả về danh sách model
    PORT          : cổng chạy web (mặc định: 8000)
    HOST          : địa chỉ bind (mặc định: 0.0.0.0)

Tài khoản đăng nhập nằm trong file anontoken.txt (cùng thư mục), mỗi dòng một
tài khoản theo cú pháp:  ten_dang_nhap:mat_khau
Dòng bắt đầu bằng # là ghi chú. Thêm/sửa user không cần khởi động lại server.
"""

import concurrent.futures
import hmac
import html as html_lib
import io
import ipaddress
import json
import os
import re
import secrets
import time
import urllib.parse
import zipfile
from xml.etree import ElementTree

import requests
from flask import (Flask, Response, abort, redirect, request, session,
                   stream_with_context, url_for)

# ----------------------------------------------------------------------------
# Cấu hình
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "anontoken.txt")

API_BASE = os.environ.get("LLM_API_BASE", "http://localhost:11434/v1").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
DEFAULT_MODEL = os.environ.get("LLM_MODEL", "")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # giới hạn upload 20MB

# ----------------------------------------------------------------------------
# Trích xuất văn bản từ tệp đính kèm.
# Chủ đích: mọi tệp đều được chuyển thành VĂN BẢN THUẦN rồi đưa vào ngữ cảnh,
# nhờ đó hoạt động với cả những model chỉ hỗ trợ text (không đọc được ảnh).
# ----------------------------------------------------------------------------
MAX_EXTRACT_CHARS = 40000  # cắt bớt để không tràn context của model

TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".log",
             ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".sh", ".sql",
             ".yaml", ".yml", ".ini", ".conf"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
              ".tiff", ".heic"}


def extract_txt(data):
    for enc in ("utf-8-sig", "utf-16", "cp1258", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def extract_docx(data):
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(ns + "p"):
        parts = []
        for node in p.iter():
            if node.tag == ns + "t" and node.text:
                parts.append(node.text)
            elif node.tag in (ns + "tab",):
                parts.append("\t")
            elif node.tag in (ns + "br", ns + "cr"):
                parts.append("\n")
        paras.append("".join(parts))
    return "\n".join(paras)


def extract_pdf(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages):
        pages.append(page.extract_text() or "")
        if sum(len(t) for t in pages) > MAX_EXTRACT_CHARS:
            break
    return "\n\n".join(pages)


def load_users():
    """Đọc anontoken.txt -> dict {username: password}. Đọc lại mỗi lần gọi
    nên có thể thêm user mà không cần restart."""
    users = {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                username, password = line.split(":", 1)
                username = username.strip()
                if username:
                    users[username] = password
    except FileNotFoundError:
        pass
    return users


def check_login(username, password):
    users = load_users()
    stored = users.get(username)
    if stored is None:
        # so sánh giả để thời gian phản hồi đồng đều
        hmac.compare_digest("x" * 32, password)
        return False
    return hmac.compare_digest(stored, password)


def current_user():
    return session.get("user")


def llm_headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = "Bearer " + API_KEY
    return h


# ----------------------------------------------------------------------------
# Tìm kiếm web (DuckDuckGo Lite — không cần API key).
# Chiến lược: tìm kiếm -> tải song song vài trang đầu -> trích văn bản thuần
# -> ghép vào ngữ cảnh của model kèm đánh số nguồn [1], [2]... để trích dẫn.
# Hoạt động với mọi model chỉ hỗ trợ text, không cần model biết gọi tool.
# ----------------------------------------------------------------------------
WEB_RESULTS = 6          # số kết quả lấy tiêu đề + mô tả
WEB_FETCH_PAGES = 3      # số trang đầu tải toàn văn
WEB_PAGE_CHARS = 3500    # ký tự tối đa trích từ mỗi trang
WEB_QUERY_CHARS = 300    # cắt bớt truy vấn quá dài

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _strip_tags(s):
    return html_lib.unescape(_TAG_RE.sub(" ", s))


def _clean_ws(s):
    return _WS_RE.sub(" ", s).strip()


def ddg_search(query):
    """Tìm trên DuckDuckGo Lite. Trả về [{title, url, snippet}]."""
    r = requests.post(
        "https://lite.duckduckgo.com/lite/",
        data={"q": query},
        headers={"User-Agent": BROWSER_UA},
        timeout=10,
    )
    r.raise_for_status()
    page = r.text
    link_ms = list(re.finditer(
        r'<a[^>]+href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>',
        page, re.S))
    snip_ms = list(re.finditer(
        r'class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>', page, re.S))
    results = []
    seen = set()
    for m in link_ms:
        url = html_lib.unescape(m.group(1))
        # bỏ link quảng cáo / nội bộ của DDG và link trùng
        if not url.startswith(("http://", "https://")):
            continue
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if "duckduckgo.com" in host or url in seen:
            continue
        seen.add(url)
        snippet = ""
        for sm in snip_ms:  # snippet nằm ngay sau link tương ứng
            if sm.start() > m.end():
                snippet = _strip_tags(sm.group(1))
                break
        results.append({
            "title": _clean_ws(_strip_tags(m.group(2)))[:200],
            "url": url,
            "snippet": _clean_ws(snippet)[:300],
        })
        if len(results) >= WEB_RESULTS:
            break
    return results


def _blocked_host(host):
    """Chặn tải về địa chỉ nội bộ (localhost, IP private) để tránh SSRF."""
    if not host:
        return True
    host = host.lower().strip("[]")
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return False  # là tên miền bình thường


def html_to_text(doc):
    """Chuyển HTML thành văn bản thuần, bỏ menu/script/rác."""
    doc = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|form|iframe)"
                 r"[^>]*>.*?</\1>", " ", doc)
    doc = re.sub(r"(?is)<!--.*?-->", " ", doc)
    doc = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>|</section>|"
                 r"</article>", "\n", doc)
    doc = _strip_tags(doc)
    lines = []
    for ln in doc.split("\n"):
        ln = _clean_ws(ln)
        if len(ln) >= 25:  # bỏ dòng quá ngắn (thường là menu, nút bấm)
            lines.append(ln)
    return "\n".join(lines)


def fetch_page_text(url):
    """Tải một trang kết quả và trích văn bản (giới hạn dung lượng)."""
    host = urllib.parse.urlsplit(url).hostname
    if _blocked_host(host):
        return ""
    r = requests.get(url, headers={"User-Agent": BROWSER_UA},
                     timeout=8, stream=True)
    ctype = r.headers.get("Content-Type", "")
    if "html" not in ctype and "text" not in ctype:
        r.close()
        return ""
    chunks, total = [], 0
    for chunk in r.iter_content(65536):
        chunks.append(chunk)
        total += len(chunk)
        if total > 600000:
            break
    r.close()
    body = b"".join(chunks)
    try:
        doc = body.decode("utf-8")
    except UnicodeDecodeError:
        doc = body.decode(r.encoding or "utf-8", errors="replace")
    return html_to_text(doc)


def perform_web_search(query):
    """Tìm kiếm + tải các trang đầu song song. Trả về (context, sources)."""
    results = ddg_search(query)
    if not results:
        return "", []
    pages = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WEB_FETCH_PAGES) as ex:
        futs = {ex.submit(fetch_page_text, r["url"]): r["url"]
                for r in results[:WEB_FETCH_PAGES]}
        for fut in concurrent.futures.as_completed(futs):
            try:
                pages[futs[fut]] = fut.result()
            except Exception:
                pages[futs[fut]] = ""
    parts = []
    for i, res in enumerate(results, 1):
        entry = "[%d] %s\nURL: %s\n" % (i, res["title"], res["url"])
        body = pages.get(res["url"], "")
        entry += body[:WEB_PAGE_CHARS] if body else res["snippet"]
        parts.append(entry)
    sources = [{"title": r["title"], "url": r["url"]} for r in results]
    return "\n\n".join(parts), sources


WEB_PROMPT = (
    "Dưới đây là kết quả tìm kiếm web mới nhất cho câu hỏi của người dùng. "
    "Hãy ưu tiên thông tin từ các nguồn này khi trả lời; khi dùng nguồn nào "
    "hãy ghi số trích dẫn tương ứng dạng [1], [2]. Nếu các kết quả không "
    "liên quan, hãy trả lời theo hiểu biết của bạn và nói rõ điều đó.\n\n")


# ----------------------------------------------------------------------------
# Trang đăng nhập
# ----------------------------------------------------------------------------
LOGIN_HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Đăng nhập — AnonToken Chat</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #212121; color: #ececec; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    width: 100%; max-width: 380px; padding: 40px 32px;
    background: #2b2b2b; border: 1px solid #3a3a3a; border-radius: 16px;
    box-shadow: 0 20px 60px rgba(0,0,0,.45);
  }
  .logo {
    width: 56px; height: 56px; margin: 0 auto 18px; border-radius: 50%;
    background: #ececec; color: #212121; display: flex; align-items: center;
    justify-content: center; font-size: 28px; font-weight: 700;
  }
  h1 { font-size: 22px; text-align: center; margin-bottom: 6px; font-weight: 600; }
  .sub { text-align: center; color: #9b9b9b; font-size: 14px; margin-bottom: 26px; }
  label { display: block; font-size: 13px; color: #b4b4b4; margin: 14px 0 6px; }
  input {
    width: 100%; padding: 12px 14px; font-size: 15px; color: #ececec;
    background: #212121; border: 1px solid #4a4a4a; border-radius: 10px;
    outline: none; transition: border-color .15s;
  }
  input:focus { border-color: #8f8f8f; }
  button {
    width: 100%; margin-top: 22px; padding: 12px; font-size: 15px; font-weight: 600;
    color: #212121; background: #ececec; border: none; border-radius: 999px;
    cursor: pointer; transition: background .15s;
  }
  button:hover { background: #ffffff; }
  .error {
    margin-top: 16px; padding: 10px 12px; font-size: 13px; text-align: center;
    color: #ff8583; background: rgba(239,68,68,.12);
    border: 1px solid rgba(239,68,68,.35); border-radius: 10px;
  }
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <div class="logo">A</div>
    <h1>Chào mừng trở lại</h1>
    <p class="sub">Đăng nhập để tiếp tục với AnonToken Chat</p>
    <label for="u">Tên đăng nhập</label>
    <input id="u" name="username" autocomplete="username" autofocus required>
    <label for="p">Mật khẩu</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Đăng nhập</button>
    __ERROR__
  </form>
</body>
</html>"""


# ----------------------------------------------------------------------------
# Trang chat (giao diện kiểu ChatGPT)
# ----------------------------------------------------------------------------
CHAT_HTML = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AnonToken Chat</title>
<style>
  :root {
    --bg-main: #212121; --bg-side: #171717; --bg-hover: #2a2a2a;
    --bg-input: #2f2f2f; --bg-bubble: #2f2f2f;
    --border: #3a3a3a; --text: #ececec; --text-dim: #9b9b9b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg-main); color: var(--text); overflow: hidden;
  }
  /* ---------------- Sidebar ---------------- */
  .app { display: flex; height: 100vh; }
  .sidebar {
    width: 260px; min-width: 260px; background: var(--bg-side);
    display: flex; flex-direction: column; transition: margin-left .25s ease;
  }
  .sidebar.hidden { margin-left: -260px; }
  .side-top { padding: 12px; }
  .btn-new {
    display: flex; align-items: center; gap: 10px; width: 100%;
    padding: 10px 12px; font-size: 14px; color: var(--text);
    background: transparent; border: 1px solid var(--border);
    border-radius: 10px; cursor: pointer; transition: background .15s;
  }
  .btn-new:hover { background: var(--bg-hover); }
  .chats { flex: 1; overflow-y: auto; padding: 0 8px; }
  .chats-label { font-size: 12px; color: var(--text-dim); padding: 10px 8px 6px; }
  .chat-item {
    display: flex; align-items: center; gap: 8px; padding: 9px 10px;
    font-size: 14px; border-radius: 8px; cursor: pointer;
    color: var(--text); position: relative;
  }
  .chat-item:hover { background: var(--bg-hover); }
  .chat-item.active { background: #303030; }
  .chat-item .title {
    flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .chat-item .del {
    display: none; background: none; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 14px; padding: 2px 4px; border-radius: 6px;
  }
  .chat-item:hover .del { display: block; }
  .chat-item .del:hover { color: #ff8583; }
  .side-bottom {
    padding: 10px 12px; border-top: 1px solid #262626;
    display: flex; align-items: center; gap: 10px;
  }
  .avatar {
    width: 32px; height: 32px; border-radius: 50%; background: #7c3aed;
    color: #fff; display: flex; align-items: center; justify-content: center;
    font-size: 14px; font-weight: 700; flex: none;
  }
  .side-bottom .name { flex: 1; font-size: 14px; overflow: hidden; text-overflow: ellipsis; }
  .btn-logout {
    background: none; border: none; color: var(--text-dim); cursor: pointer;
    font-size: 13px; padding: 6px 8px; border-radius: 8px;
  }
  .btn-logout:hover { background: var(--bg-hover); color: var(--text); }
  /* ---------------- Main ---------------- */
  .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 8px; padding: 10px 16px;
  }
  .btn-icon {
    background: none; border: none; color: var(--text-dim); cursor: pointer;
    padding: 8px; border-radius: 8px; font-size: 16px; line-height: 1;
  }
  .btn-icon:hover { background: var(--bg-hover); color: var(--text); }
  select.model {
    background: transparent; color: var(--text); border: none;
    font-size: 16px; font-weight: 600; cursor: pointer; outline: none;
    padding: 6px 8px; border-radius: 8px; max-width: 60vw;
  }
  select.model:hover { background: var(--bg-hover); }
  select.model option { background: var(--bg-input); }
  /* ---------------- Messages ---------------- */
  .scroll { flex: 1; overflow-y: auto; }
  .thread { max-width: 768px; margin: 0 auto; padding: 12px 20px 24px; }
  .msg { display: flex; margin-bottom: 22px; }
  .msg.user { justify-content: flex-end; }
  .msg.user .content {
    max-width: 78%; background: var(--bg-bubble); padding: 10px 18px;
    border-radius: 22px; white-space: pre-wrap; word-wrap: break-word;
    font-size: 15px; line-height: 1.65;
  }
  .msg.assistant { gap: 14px; }
  .msg.assistant .bot-avatar {
    width: 30px; height: 30px; border-radius: 50%; flex: none; margin-top: 2px;
    border: 1px solid var(--border); display: flex; align-items: center;
    justify-content: center; font-size: 15px; background: var(--bg-main);
  }
  .msg.assistant .content {
    min-width: 0; font-size: 15px; line-height: 1.7; overflow-wrap: break-word;
  }
  .content p { margin: 0 0 12px; } .content p:last-child { margin-bottom: 0; }
  .content h1, .content h2, .content h3 { margin: 18px 0 10px; line-height: 1.3; }
  .content h1 { font-size: 1.35em; } .content h2 { font-size: 1.2em; } .content h3 { font-size: 1.08em; }
  .content ul, .content ol { margin: 0 0 12px 24px; }
  .content li { margin-bottom: 4px; }
  .content blockquote {
    border-left: 3px solid #565656; margin: 0 0 12px; padding: 2px 14px; color: #c9c9c9;
  }
  .content a { color: #66b2ff; }
  .content code {
    background: #424242; padding: 2px 6px; border-radius: 6px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .875em;
  }
  .codeblock { margin: 0 0 14px; border-radius: 10px; overflow: hidden; border: 1px solid var(--border); }
  .codeblock .cb-head {
    display: flex; justify-content: space-between; align-items: center;
    background: #2f2f2f; padding: 6px 14px; font-size: 12px; color: var(--text-dim);
  }
  .codeblock .cb-head button {
    background: none; border: none; color: var(--text-dim); cursor: pointer; font-size: 12px;
  }
  .codeblock .cb-head button:hover { color: var(--text); }
  .codeblock pre {
    background: #0d0d0d; padding: 14px; overflow-x: auto; font-size: 13.5px; line-height: 1.55;
  }
  .codeblock pre code { background: none; padding: 0; font-size: inherit; }
  details.think {
    margin: 0 0 12px; border: 1px solid var(--border); border-radius: 10px;
    background: #262626; font-size: 13.5px;
  }
  details.think summary {
    cursor: pointer; padding: 8px 14px; color: var(--text-dim);
    user-select: none; list-style: none;
  }
  details.think summary::before { content: '▸ '; }
  details.think[open] summary::before { content: '▾ '; }
  details.think .think-body {
    padding: 2px 14px 10px; color: #b0b0b0; line-height: 1.6; white-space: pre-wrap;
  }
  .cursor {
    display: inline-block; width: 8px; height: 15px; background: var(--text);
    border-radius: 2px; animation: blink 1s steps(1) infinite; vertical-align: -2px;
  }
  @keyframes blink { 50% { opacity: 0; } }
  .msg-error { color: #ff8583; font-size: 14px; }
  /* ---------------- Welcome ---------------- */
  .welcome {
    height: 100%; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 14px; padding: 20px;
  }
  .welcome h2 { font-size: 28px; font-weight: 600; text-align: center; }
  .welcome p { color: var(--text-dim); font-size: 15px; text-align: center; }
  /* ---------------- Composer ---------------- */
  .composer { padding: 0 20px 8px; }
  .composer-inner { max-width: 768px; margin: 0 auto; }
  .inputbox {
    display: flex; align-items: flex-end; gap: 8px;
    background: var(--bg-input); border-radius: 28px; padding: 10px;
    border: 1px solid var(--border);
  }
  .inputbox textarea {
    flex: 1; background: none; border: none; outline: none; resize: none;
    color: var(--text); font-size: 15px; line-height: 1.5; font-family: inherit;
    max-height: 200px; padding: 6px 0;
  }
  .inputbox textarea::placeholder { color: #8a8a8a; }
  .btn-send {
    width: 36px; height: 36px; border-radius: 50%; border: none; flex: none;
    background: var(--text); color: var(--bg-main); font-size: 16px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .btn-attach {
    width: 36px; height: 36px; border-radius: 50%; border: none; flex: none;
    background: transparent; color: var(--text-dim); cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .btn-attach:hover { background: #424242; color: var(--text); }
  .btn-web {
    height: 36px; border-radius: 18px; border: 1px solid transparent; flex: none;
    background: transparent; color: var(--text-dim); cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 6px;
    padding: 0 12px; font-size: 13px; font-family: inherit;
  }
  .btn-web:hover { background: #424242; color: var(--text); }
  .btn-web.on {
    color: #66b2ff; background: rgba(102,178,255,.12);
    border-color: rgba(102,178,255,.4);
  }
  .search-note {
    display: flex; align-items: center; gap: 8px; color: var(--text-dim);
    font-size: 13.5px; margin-bottom: 10px;
  }
  .search-note .spin {
    width: 13px; height: 13px; border: 2px solid #555; flex: none;
    border-top-color: var(--text); border-radius: 50%;
    animation: rot .8s linear infinite;
  }
  @keyframes rot { to { transform: rotate(360deg); } }
  .sources { margin-top: 12px; }
  .sources-label { font-size: 12px; color: var(--text-dim); margin-bottom: 6px; }
  .sources a {
    display: inline-flex; align-items: center; gap: 5px; max-width: 300px;
    margin: 0 6px 6px 0; padding: 4px 10px; font-size: 12.5px;
    background: #2f2f2f; border: 1px solid var(--border); border-radius: 999px;
    color: var(--text-dim); text-decoration: none; overflow: hidden;
  }
  .sources a:hover { color: var(--text); border-color: #565656; }
  .sources a .st {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .sources a .sn { color: #66b2ff; flex: none; }
  .attachments { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 6px 8px; }
  .attachments:empty { display: none; }
  .file-chip {
    display: inline-flex; align-items: center; gap: 8px; max-width: 260px;
    background: #424242; border-radius: 10px; padding: 6px 10px; font-size: 13px;
  }
  .file-chip .fname {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .file-chip .fmeta { color: var(--text-dim); font-size: 11.5px; flex: none; }
  .file-chip .rm {
    background: none; border: none; color: var(--text-dim); cursor: pointer;
    font-size: 13px; padding: 0 2px; flex: none;
  }
  .file-chip .rm:hover { color: #ff8583; }
  .msg-files { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .msg.user .msg-files .file-chip { background: #454545; }
  .btn-send:disabled { background: #4a4a4a; color: #8a8a8a; cursor: default; }
  .btn-send.stop { background: var(--text); }
  .hint { text-align: center; font-size: 12px; color: #8a8a8a; padding: 8px 0 10px; }
  /* ---------------- Scrollbar & mobile ---------------- */
  ::-webkit-scrollbar { width: 8px; }
  ::-webkit-scrollbar-thumb { background: #454545; border-radius: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  @media (max-width: 768px) {
    .sidebar { position: fixed; z-index: 30; height: 100%; box-shadow: 0 0 40px rgba(0,0,0,.5); }
    .overlay {
      display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 20;
    }
    .overlay.show { display: block; }
  }
</style>
</head>
<body>
<div class="app">
  <div class="overlay" id="overlay" onclick="toggleSidebar()"></div>
  <nav class="sidebar" id="sidebar">
    <div class="side-top">
      <button class="btn-new" onclick="newChat()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
        Cuộc trò chuyện mới
      </button>
    </div>
    <div class="chats">
      <div class="chats-label">Lịch sử trò chuyện</div>
      <div id="chatList"></div>
    </div>
    <div class="side-bottom">
      <div class="avatar" id="userAvatar"></div>
      <div class="name" id="userName"></div>
      <button class="btn-logout" onclick="location.href='/logout'" title="Đăng xuất">Thoát</button>
    </div>
  </nav>

  <main class="main">
    <div class="topbar">
      <button class="btn-icon" onclick="toggleSidebar()" title="Ẩn/hiện sidebar">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/></svg>
      </button>
      <select class="model" id="modelSelect" title="Chọn model"></select>
    </div>

    <div class="scroll" id="scrollArea">
      <div class="welcome" id="welcome">
        <h2>Tôi có thể giúp gì cho bạn?</h2>
        <p>Bắt đầu cuộc trò chuyện bằng cách nhập tin nhắn bên dưới.</p>
      </div>
      <div class="thread" id="thread" style="display:none"></div>
    </div>

    <div class="composer">
      <div class="composer-inner">
        <div class="attachments" id="attachList"></div>
        <div class="inputbox">
          <input type="file" id="fileInput" multiple style="display:none"
                 accept=".pdf,.docx,.txt,.md,.csv,.json,.xml,.html,.log,.py,.js,.ts,.java,.c,.cpp,.h,.sh,.sql,.yaml,.yml,.ini,.conf">
          <button class="btn-attach" onclick="document.getElementById('fileInput').click()" title="Đính kèm tệp (PDF, Word, TXT...)">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/></svg>
          </button>
          <button class="btn-web" id="webBtn" onclick="toggleWeb()" title="Bật/tắt tìm kiếm web">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z"/></svg>
            <span>Tìm kiếm</span>
          </button>
          <textarea id="input" rows="1" placeholder="Nhắn tin cho AnonToken Chat..."></textarea>
          <button class="btn-send" id="sendBtn" onclick="onSendClick()" disabled title="Gửi">
            <svg id="sendIcon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
          </button>
        </div>
        <div class="hint">AI có thể mắc lỗi. Hãy kiểm tra lại các thông tin quan trọng.</div>
      </div>
    </div>
  </main>
</div>

<script>
const USERNAME = __USERNAME__;
const STORE_KEY = 'anontoken_chats_' + USERNAME;

let chats = loadChats();          // [{id, title, messages:[{role, content}]}]
let currentId = null;
let generating = false;
let aborter = null;

// ---------------- Lưu trữ (localStorage theo từng user) ----------------
function loadChats() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || []; }
  catch (e) { return []; }
}
function saveChats() { localStorage.setItem(STORE_KEY, JSON.stringify(chats)); }
function currentChat() { return chats.find(c => c.id === currentId) || null; }

// ---------------- Markdown renderer (nhẹ, tự viết, không phụ thuộc mạng) ----
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, (m, c) => '<code>' + c + '</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function renderMarkdown(text) {
  const out = [];
  const lines = text.split('\n');
  let i = 0, para = [], list = null;
  const flushPara = () => {
    if (para.length) { out.push('<p>' + inlineMd(esc(para.join('\n'))).replace(/\n/g,'<br>') + '</p>'); para = []; }
  };
  const flushList = () => {
    if (list) { out.push('<' + list.tag + '>' + list.items.map(x => '<li>' + inlineMd(esc(x)) + '</li>').join('') + '</' + list.tag + '>'); list = null; }
  };
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      flushPara(); flushList();
      const lang = fence[1] || 'code';
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
      i++; // bỏ dòng ``` đóng (nếu có)
      out.push('<div class="codeblock"><div class="cb-head"><span>' + esc(lang) +
        '</span><button onclick="copyCode(this)">Sao chép</button></div><pre><code>' +
        esc(code.join('\n')) + '</code></pre></div>');
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)/);
    if (h) { flushPara(); flushList(); out.push('<h' + h[1].length + '>' + inlineMd(esc(h[2])) + '</h' + h[1].length + '>'); i++; continue; }
    const ul = line.match(/^\s*[-*]\s+(.*)/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (ul || ol) {
      flushPara();
      const tag = ul ? 'ul' : 'ol';
      if (!list || list.tag !== tag) { flushList(); list = { tag: tag, items: [] }; }
      list.items.push((ul || ol)[1]); i++; continue;
    }
    if (/^>\s?/.test(line)) {
      flushPara(); flushList();
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/,'')); i++; }
      out.push('<blockquote><p>' + inlineMd(esc(q.join('\n'))).replace(/\n/g,'<br>') + '</p></blockquote>');
      continue;
    }
    if (line.trim() === '') { flushPara(); flushList(); i++; continue; }
    para.push(line); i++;
  }
  flushPara(); flushList();
  return out.join('');
}
function copyCode(btn) {
  const code = btn.closest('.codeblock').querySelector('code').innerText;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = 'Đã chép!';
    setTimeout(() => { btn.textContent = 'Sao chép'; }, 1500);
  });
}

// ---------------- Render giao diện ----------------
function renderSidebar() {
  const el = document.getElementById('chatList');
  el.innerHTML = '';
  chats.forEach(c => {
    const item = document.createElement('div');
    item.className = 'chat-item' + (c.id === currentId ? ' active' : '');
    item.onclick = () => { selectChat(c.id); };
    const t = document.createElement('span');
    t.className = 'title';
    t.textContent = c.title || 'Cuộc trò chuyện mới';
    const del = document.createElement('button');
    del.className = 'del'; del.title = 'Xoá'; del.textContent = '✕';
    del.onclick = (e) => { e.stopPropagation(); deleteChat(c.id); };
    item.appendChild(t); item.appendChild(del);
    el.appendChild(item);
  });
}
function renderThread() {
  const thread = document.getElementById('thread');
  const welcome = document.getElementById('welcome');
  const chat = currentChat();
  thread.innerHTML = '';
  if (!chat || chat.messages.length === 0) {
    thread.style.display = 'none'; welcome.style.display = 'flex'; return;
  }
  welcome.style.display = 'none'; thread.style.display = 'block';
  chat.messages.forEach(m => thread.appendChild(buildMsgEl(m.role, m.content, m.reasoning, m.files, m.sources)));
  scrollToBottom(true);
}
function thinkHtml(reasoning, open) {
  if (!reasoning) return '';
  return '<details class="think"' + (open ? ' open' : '') + '><summary>' +
    (open ? 'Đang suy nghĩ…' : 'Đã suy nghĩ') + '</summary><div class="think-body">' +
    esc(reasoning) + '</div></details>';
}
function fileChipHtml(f) {
  return '<span class="file-chip"><span>📄</span><span class="fname">' + esc(f.name) +
    '</span>' + (f.truncated ? '<span class="fmeta">đã cắt bớt</span>' : '') + '</span>';
}
function sourcesHtml(sources) {
  if (!sources || !sources.length) return '';
  return '<div class="sources"><div class="sources-label">Nguồn tham khảo</div>' +
    sources.map((s, i) =>
      '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer" title="' +
      esc(s.url) + '"><span class="sn">[' + (i + 1) + ']</span><span class="st">' +
      esc(s.title || s.url) + '</span></a>').join('') + '</div>';
}
function buildMsgEl(role, content, reasoning, files, sources) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const body = document.createElement('div');
  body.className = 'content';
  if (role === 'assistant') {
    const av = document.createElement('div');
    av.className = 'bot-avatar'; av.textContent = '✦';
    wrap.appendChild(av);
    body.innerHTML = thinkHtml(reasoning, false) + renderMarkdown(content) + sourcesHtml(sources);
  } else {
    if (files && files.length) {
      const fl = document.createElement('div');
      fl.className = 'msg-files';
      fl.innerHTML = files.map(fileChipHtml).join('');
      body.appendChild(fl);
    }
    const t = document.createElement('span');
    t.textContent = content;
    body.appendChild(t);
  }
  wrap.appendChild(body);
  return wrap;
}
function scrollToBottom(force) {
  const sc = document.getElementById('scrollArea');
  const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
  if (force || nearBottom) sc.scrollTop = sc.scrollHeight;
}

// ---------------- Thao tác hội thoại ----------------
function newChat() {
  if (generating) stopGenerating();
  currentId = null;
  renderSidebar(); renderThread();
  document.getElementById('input').focus();
  closeSidebarOnMobile();
}
function selectChat(id) {
  if (generating) stopGenerating();
  currentId = id;
  renderSidebar(); renderThread();
  closeSidebarOnMobile();
}
function deleteChat(id) {
  if (!confirm('Xoá cuộc trò chuyện này?')) return;
  chats = chats.filter(c => c.id !== id);
  if (currentId === id) currentId = null;
  saveChats(); renderSidebar(); renderThread();
}
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('hidden');
  document.getElementById('overlay').classList.toggle('show',
    !document.getElementById('sidebar').classList.contains('hidden') && window.innerWidth <= 768);
}
function closeSidebarOnMobile() {
  if (window.innerWidth <= 768) {
    document.getElementById('sidebar').classList.add('hidden');
    document.getElementById('overlay').classList.remove('show');
  }
}

// ---------------- Tìm kiếm web ----------------
let webSearchOn = localStorage.getItem('anontoken_web') === '1';
function toggleWeb() {
  webSearchOn = !webSearchOn;
  localStorage.setItem('anontoken_web', webSearchOn ? '1' : '0');
  renderWebBtn();
}
function renderWebBtn() {
  document.getElementById('webBtn').classList.toggle('on', webSearchOn);
}

// ---------------- Tệp đính kèm ----------------
// Chiến lược: server trích xuất VĂN BẢN từ tệp (PDF/Word/TXT) rồi client ghép
// vào ngữ cảnh dưới dạng text thuần -> tương thích với model chỉ hỗ trợ văn bản.
let pendingFiles = []; // [{name, text, truncated, loading}]

function renderAttachments() {
  const el = document.getElementById('attachList');
  el.innerHTML = '';
  pendingFiles.forEach((f, i) => {
    const chip = document.createElement('span');
    chip.className = 'file-chip';
    const icon = document.createElement('span');
    icon.textContent = f.loading ? '⏳' : '📄';
    const name = document.createElement('span');
    name.className = 'fname';
    name.textContent = f.name;
    chip.appendChild(icon); chip.appendChild(name);
    if (f.truncated) {
      const meta = document.createElement('span');
      meta.className = 'fmeta'; meta.textContent = 'đã cắt bớt';
      chip.appendChild(meta);
    }
    if (!f.loading) {
      const rm = document.createElement('button');
      rm.className = 'rm'; rm.title = 'Gỡ tệp'; rm.textContent = '✕';
      rm.onclick = () => { pendingFiles.splice(i, 1); renderAttachments(); updateSendState(); };
      chip.appendChild(rm);
    }
    el.appendChild(chip);
  });
}

async function onFilesPicked(ev) {
  const files = Array.from(ev.target.files || []);
  ev.target.value = '';
  for (const f of files) {
    const entry = { name: f.name, loading: true };
    pendingFiles.push(entry);
    renderAttachments();
    try {
      const fd = new FormData();
      fd.append('file', f);
      const res = await fetch('/api/upload', { method: 'POST', body: fd });
      if (res.status === 401) { location.href = '/login'; return; }
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
      entry.text = data.text;
      entry.truncated = data.truncated;
      entry.loading = false;
    } catch (err) {
      pendingFiles = pendingFiles.filter(x => x !== entry);
      alert('Không đính kèm được "' + f.name + '":\n' + (err.message || err));
    }
    renderAttachments(); updateSendState();
  }
}
document.getElementById('fileInput').addEventListener('change', onFilesPicked);

// Ghép nội dung tệp vào tin nhắn gửi cho model (dạng văn bản thuần)
function buildModelContent(m) {
  if (!m.files || !m.files.length) return m.content;
  let s = '';
  m.files.forEach(f => {
    const nl = String.fromCharCode(10);
    // Ghép chuỗi 3 dấu nháy kép từ 2 phần: nếu viết liền, nó sẽ kết thúc sớm
    // raw-string CHAT_HTML phía Python và làm vỡ JS của cả trang.
    const q3 = '""' + '"';
    s += 'Nội dung tệp đính kèm "' + f.name + '"' +
      (f.truncated ? ' (đã cắt bớt vì quá dài)' : '') + ':' + nl +
      q3 + nl + f.text + nl + q3 + nl + nl;
  });
  return s + (m.content || 'Người dùng gửi (các) tệp ở trên. Hãy đọc và tóm tắt những điểm chính.');
}

// ---------------- Gửi tin nhắn & streaming ----------------
function onSendClick() {
  if (generating) { stopGenerating(); } else { sendMessage(); }
}
function stopGenerating() {
  if (aborter) aborter.abort();
}
function setGenerating(on) {
  generating = on;
  const btn = document.getElementById('sendBtn');
  const icon = document.getElementById('sendIcon');
  if (on) {
    btn.disabled = false; btn.title = 'Dừng';
    icon.outerHTML = '<svg id="sendIcon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>';
  } else {
    btn.title = 'Gửi';
    document.getElementById('sendIcon').outerHTML =
      '<svg id="sendIcon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    updateSendState();
  }
}
function updateSendState() {
  if (generating) return;
  const hasText = document.getElementById('input').value.trim() !== '';
  const hasFiles = pendingFiles.some(f => !f.loading);
  const uploading = pendingFiles.some(f => f.loading);
  document.getElementById('sendBtn').disabled = uploading || (!hasText && !hasFiles);
}

async function sendMessage() {
  const input = document.getElementById('input');
  const text = input.value.trim();
  const files = pendingFiles.filter(f => !f.loading);
  if ((!text && files.length === 0) || pendingFiles.some(f => f.loading) || generating) return;
  input.value = ''; autoResize();
  pendingFiles = []; renderAttachments(); updateSendState();

  let chat = currentChat();
  if (!chat) {
    const titleSrc = text || ('📄 ' + files[0].name);
    chat = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
             title: titleSrc.length > 42 ? titleSrc.slice(0, 42) + '…' : titleSrc,
             messages: [] };
    chats.unshift(chat);
    currentId = chat.id;
  }
  const userMsg = { role: 'user', content: text };
  if (files.length) userMsg.files = files.map(f => ({ name: f.name, text: f.text, truncated: !!f.truncated }));
  chat.messages.push(userMsg);
  try { saveChats(); } catch (e) {
    alert('Bộ nhớ trình duyệt đầy — hãy xoá bớt hội thoại cũ. Tin nhắn vẫn được gửi nhưng có thể không lưu lại được.');
  }
  renderSidebar(); renderThread();

  // Tạo phần tử tin nhắn assistant để stream vào
  const thread = document.getElementById('thread');
  document.getElementById('welcome').style.display = 'none';
  thread.style.display = 'block';
  const el = buildMsgEl('assistant', '');
  el.querySelector('.content').innerHTML = '<span class="cursor"></span>';
  thread.appendChild(el);
  scrollToBottom(true);

  setGenerating(true);
  aborter = new AbortController();
  let full = '';
  let think = '';
  let sources = null;
  let searching = false;
  let renderQueued = false;
  let streamDone = false;  // chặn paint() trong rAF chạy đè lên render cuối
  const contentEl = el.querySelector('.content');
  const searchNote = () => searching
    ? '<div class="search-note"><span class="spin"></span>Đang tìm kiếm web…</div>' : '';
  const paint = () => {
    if (renderQueued || streamDone) return;
    renderQueued = true;
    requestAnimationFrame(() => {
      renderQueued = false;
      if (streamDone) return;
      // Khi chưa có nội dung trả lời thì mở khối "Đang suy nghĩ…" để người
      // dùng thấy model đang làm việc (với reasoning model như ornith:9b).
      contentEl.innerHTML = searchNote() + thinkHtml(think, full === '') +
        renderMarkdown(full) + '<span class="cursor"></span>';
      scrollToBottom(false);
    });
  };
  if (webSearchOn) { searching = true; paint(); }

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: document.getElementById('modelSelect').value,
        web_search: webSearchOn,
        messages: chat.messages.map(m => ({ role: m.role, content: buildModelContent(m) })),
      }),
      signal: aborter.signal,
    });
    if (res.status === 401) { location.href = '/login'; return; }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const data = line.slice(5).trim();
          if (!data || data === '[DONE]') continue;
          let obj;
          try { obj = JSON.parse(data); } catch (e) { continue; }
          if (obj.error) throw new Error(obj.error);
          if (obj.status === 'searching') { searching = true; paint(); continue; }
          if (obj.status === 'search_failed') { searching = false; paint(); continue; }
          if (obj.sources) { sources = obj.sources; searching = false; paint(); continue; }
          const d = (obj.choices && obj.choices[0] && obj.choices[0].delta) || {};
          if (searching) { searching = false; }
          const r = d.reasoning || d.reasoning_content || '';
          if (r) { think += r; paint(); }
          if (d.content) { full += d.content; paint(); }
        }
      }
    }
    if (!full) full = '(không có phản hồi)';
  } catch (err) {
    if (err.name === 'AbortError') {
      if (!full) full = '(đã dừng)';
    } else {
      streamDone = true;
      contentEl.innerHTML =
        '<div class="msg-error">⚠️ Lỗi: ' + esc(String(err.message || err)) +
        '<br>Kiểm tra backend LLM (LLM_API_BASE) đã chạy chưa.</div>';
      chat.messages.push({ role: 'assistant', content: '⚠️ Lỗi: ' + String(err.message || err) });
      saveChats(); setGenerating(false); aborter = null;
      return;
    }
  }
  streamDone = true;
  contentEl.innerHTML = thinkHtml(think, false) + renderMarkdown(full) + sourcesHtml(sources);
  const asstMsg = { role: 'assistant', content: full, reasoning: think || undefined };
  if (sources && sources.length) asstMsg.sources = sources;
  chat.messages.push(asstMsg);
  saveChats(); renderSidebar();
  setGenerating(false); aborter = null;
  scrollToBottom(false);
}

// ---------------- Ô nhập liệu ----------------
const inputEl = document.getElementById('input');
function autoResize() {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';
}
inputEl.addEventListener('input', () => { autoResize(); updateSendState(); });
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// ---------------- Danh sách model ----------------
async function loadModels() {
  const sel = document.getElementById('modelSelect');
  try {
    const res = await fetch('/api/models');
    if (res.status === 401) { location.href = '/login'; return; }
    const data = await res.json();
    const models = data.models || [];
    sel.innerHTML = '';
    if (models.length === 0) {
      const o = document.createElement('option');
      o.value = ''; o.textContent = 'Không tìm thấy model';
      sel.appendChild(o);
      return;
    }
    models.forEach(m => {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      sel.appendChild(o);
    });
    const savedModel = localStorage.getItem('anontoken_model');
    if (savedModel && models.includes(savedModel)) sel.value = savedModel;
  } catch (e) {
    sel.innerHTML = '<option value="">Backend chưa sẵn sàng</option>';
  }
}
document.getElementById('modelSelect').addEventListener('change', (e) => {
  localStorage.setItem('anontoken_model', e.target.value);
});

// ---------------- Khởi tạo ----------------
document.getElementById('userName').textContent = USERNAME;
document.getElementById('userAvatar').textContent = USERNAME.charAt(0).toUpperCase();
if (window.innerWidth <= 768) document.getElementById('sidebar').classList.add('hidden');
renderSidebar(); renderThread(); loadModels(); updateSendState(); renderWebBtn();
inputEl.focus();
</script>
</body>
</html>"""


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if check_login(username, password):
            session["user"] = username
            return redirect(url_for("index"))
        time.sleep(0.5)  # làm chậm brute-force
        error = '<div class="error">Sai tên đăng nhập hoặc mật khẩu.</div>'
    return LOGIN_HTML.replace("__ERROR__", error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return CHAT_HTML.replace("__USERNAME__", json.dumps(user))


@app.route("/api/models")
def api_models():
    if not current_user():
        abort(401)
    models = []
    try:
        r = requests.get(API_BASE + "/models", headers=llm_headers(), timeout=10)
        if r.ok:
            models = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
    except requests.RequestException:
        pass
    if not models and DEFAULT_MODEL:
        models = [DEFAULT_MODEL]
    return {"models": models}


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if not current_user():
        abort(401)
    f = request.files.get("file")
    if not f or not f.filename:
        return {"error": "Không nhận được tệp."}, 400
    name = os.path.basename(f.filename)
    ext = os.path.splitext(name)[1].lower()
    data = f.read()

    if ext in IMAGE_EXTS:
        return {"error": "Không hỗ trợ hình ảnh — model hiện tại chỉ đọc được "
                         "văn bản. Hãy gửi tệp PDF, Word (.docx) hoặc .txt."}, 415
    try:
        if ext == ".pdf":
            text = extract_pdf(data)
        elif ext == ".docx":
            text = extract_docx(data)
        elif ext == ".doc":
            return {"error": "Định dạng Word cũ (.doc) chưa được hỗ trợ. "
                             "Hãy lưu lại thành .docx rồi gửi lại."}, 415
        elif ext in TEXT_EXTS:
            text = extract_txt(data)
        else:
            return {"error": "Định dạng %s chưa được hỗ trợ. Hỗ trợ: PDF, "
                             ".docx, .txt và các tệp văn bản/mã nguồn." % (ext or "này")}, 415
    except Exception as e:
        return {"error": "Không đọc được nội dung tệp: %s" % e}, 422

    text = text.strip()
    if not text:
        return {"error": "Tệp không chứa văn bản trích xuất được (có thể là "
                         "PDF scan dạng ảnh — model văn bản không đọc được)."}, 422

    truncated = len(text) > MAX_EXTRACT_CHARS
    if truncated:
        text = text[:MAX_EXTRACT_CHARS]
    return {"name": name, "text": text, "chars": len(text), "truncated": truncated}


@app.route("/api/chat", methods=["POST"])
def api_chat():
    if not current_user():
        abort(401)
    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    model = data.get("model") or DEFAULT_MODEL
    web_search = bool(data.get("web_search"))
    if not messages:
        abort(400)

    def generate():
        sources = []
        if web_search:
            # lấy tin nhắn cuối của người dùng làm truy vấn tìm kiếm
            query = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    query = (m.get("content") or "").strip()[:WEB_QUERY_CHARS]
                    break
            if query:
                yield ("data: " + json.dumps({"status": "searching"})
                       + "\n\n").encode("utf-8")
                try:
                    context, sources = perform_web_search(query)
                except Exception:
                    context = ""
                if context:
                    # chèn kết quả tìm kiếm làm system message ngay trước
                    # tin nhắn cuối để không phá lịch sử hội thoại
                    messages.insert(len(messages) - 1 if len(messages) > 1 else 0,
                                    {"role": "system",
                                     "content": WEB_PROMPT + context})
                    yield ("data: " + json.dumps({"sources": sources})
                           + "\n\n").encode("utf-8")
                else:
                    yield ("data: " + json.dumps({"status": "search_failed"})
                           + "\n\n").encode("utf-8")

        payload = {"model": model, "messages": messages, "stream": True}
        try:
            r = requests.post(
                API_BASE + "/chat/completions",
                json=payload, headers=llm_headers(), stream=True, timeout=(10, 600),
            )
            if r.status_code != 200:
                detail = r.text[:300]
                yield ("data: " + json.dumps(
                    {"error": "Backend trả về HTTP %d: %s" % (r.status_code, detail)}
                ) + "\n\n").encode("utf-8")
                return
            # Chuyển tiếp bytes nguyên bản — không decode_unicode để tránh
            # requests giải mã nhầm ISO-8859-1 làm hỏng ký tự UTF-8 (tiếng Việt).
            for line in r.iter_lines():
                if line and line.startswith(b"data:"):
                    yield line + b"\n\n"
            yield b"data: [DONE]\n\n"
        except requests.RequestException as e:
            yield ("data: " + json.dumps(
                {"error": "Không kết nối được backend LLM (%s): %s" % (API_BASE, e)}
            ) + "\n\n").encode("utf-8")

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    if not os.path.exists(USERS_FILE):
        print("⚠️  Chưa có file %s — hãy tạo với cú pháp: ten_dang_nhap:mat_khau" % USERS_FILE)
    print("🚀 AnonToken Chat chạy tại http://%s:%d" % (HOST, PORT))
    print("   Backend LLM: %s (đổi bằng biến môi trường LLM_API_BASE)" % API_BASE)
    app.run(host=HOST, port=PORT, threaded=True)
