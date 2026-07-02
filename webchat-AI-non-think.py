import json
import http.server
import socketserver
import urllib.request
import urllib.error

# ═══════════════════════════════════════════════════════════
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ollama Chat</title>
<style>
  :root {
    --bg: #0f0f13; --surface: #1a1a24; --surface2: #22222f;
    --border: #2a2a3a; --primary: #6c5ce7;
    --primary-glow: rgba(108,92,231,.25);
    --accent: #a29bfe; --text: #e2e2ea; --text2: #8888a0;
    --user-bg: #6c5ce7; --bot-bg: #1e1e2e;
    --danger: #e74c3c; --success: #00b894;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    height: 100vh; overflow: hidden;
  }

  #setupScreen {
    display: flex; align-items: center; justify-content: center;
    height: 100vh;
    background:
      radial-gradient(ellipse at 20% 50%, rgba(108,92,231,.12) 0%, transparent 60%),
      radial-gradient(ellipse at 80% 50%, rgba(162,155,254,.08) 0%, transparent 60%),
      var(--bg);
  }
  .setup-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 20px; padding: 48px 40px; width: 460px; max-width: 94vw;
    box-shadow: 0 24px 80px rgba(0,0,0,.5); animation: fadeUp .5s ease;
  }
  @keyframes fadeUp { from {opacity:0;transform:translateY(24px);} to {opacity:1;transform:translateY(0);} }
  .setup-card h1 {
    font-size: 26px; font-weight: 700; margin-bottom: 6px;
    background: linear-gradient(135deg, var(--accent), var(--primary));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .setup-card p.sub { color: var(--text2); font-size: 14px; margin-bottom: 32px; }
  .field { margin-bottom: 22px; }
  .field label {
    display: block; font-size: 13px; font-weight: 600; color: var(--text2);
    margin-bottom: 8px; text-transform: uppercase; letter-spacing: .6px;
  }
  .field input {
    width: 100%; padding: 14px 16px; border-radius: 12px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text); font-size: 15px; outline: none;
    transition: border .2s, box-shadow .2s;
  }
  .field input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-glow); }
  .field input::placeholder { color: #555568; }
  .btn-start {
    width: 100%; padding: 15px; border: none; border-radius: 12px;
    background: linear-gradient(135deg, var(--primary), #8b7cf7);
    color: #fff; font-size: 16px; font-weight: 600; cursor: pointer;
    transition: transform .15s, box-shadow .2s; margin-top: 8px;
  }
  .btn-start:hover { transform: translateY(-2px); box-shadow: 0 8px 30px var(--primary-glow); }
  .btn-start:active { transform: scale(.98); }
  .error-msg { color: var(--danger); font-size: 13px; margin-top: 12px; display: none; text-align: center; }

  #chatScreen { display:none; height:100vh; flex-direction:column; }
  .chat-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 24px; background: var(--surface);
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .chat-header .left { display:flex; align-items:center; gap:12px; }
  .chat-header .dot {
    width:10px; height:10px; border-radius:50%;
    background: var(--success); box-shadow: 0 0 8px rgba(0,184,148,.5);
  }
  .chat-header .model-name { font-weight: 600; font-size: 15px; }
  .chat-header .api-label { font-size: 12px; color: var(--text2); }
  .header-btns { display:flex; gap:8px; }
  .header-btns button {
    padding: 8px 14px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--surface2); color: var(--text2); font-size: 13px;
    cursor: pointer; transition: all .2s;
  }
  .header-btns button:hover { border-color: var(--primary); color: var(--text); }

  .messages {
    flex: 1; overflow-y: auto; padding: 24px;
    display: flex; flex-direction: column; gap: 16px; scroll-behavior: smooth;
  }
  .messages::-webkit-scrollbar { width:6px; }
  .messages::-webkit-scrollbar-track { background:transparent; }
  .messages::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }

  .msg {
    display: flex; gap: 12px; max-width: 82%;
    animation: msgIn .3s ease;
  }
  @keyframes msgIn { from {opacity:0;transform:translateY(10px);} to {opacity:1;transform:translateY(0);} }
  .msg.user { align-self: flex-end; flex-direction: row-reverse; }
  .msg.bot  { align-self: flex-start; }

  .avatar {
    width: 34px; height: 34px; border-radius: 10px;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; flex-shrink: 0; margin-top: 2px;
  }
  .msg.user .avatar { background: var(--primary); }
  .msg.bot  .avatar { background: var(--surface2); border:1px solid var(--border); }

  .bubble {
    padding: 12px 16px; border-radius: 16px; font-size: 14.5px;
    line-height: 1.65; word-wrap: break-word; white-space: pre-wrap;
  }
  .msg.user .bubble {
    background: var(--user-bg); color: #fff; border-bottom-right-radius: 4px;
  }
  .msg.bot .bubble {
    background: var(--bot-bg); border: 1px solid var(--border); border-bottom-left-radius: 4px;
  }

  .typing-dots span {
    display: inline-block; width: 7px; height: 7px;
    background: var(--text2); border-radius: 50%; margin: 0 2px;
    animation: bounce .6s infinite alternate;
  }
  .typing-dots span:nth-child(2) { animation-delay:.2s; }
  .typing-dots span:nth-child(3) { animation-delay:.4s; }
  @keyframes bounce { to { opacity:.3; transform:translateY(-4px); } }

  .input-area {
    padding: 16px 24px 20px; background: var(--surface);
    border-top: 1px solid var(--border); flex-shrink: 0;
  }
  .input-row {
    display: flex; gap: 10px; align-items: flex-end;
    max-width: 900px; margin: 0 auto;
  }
  .input-row textarea {
    flex: 1; padding: 14px 16px; border-radius: 14px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text); font-size: 14.5px; font-family: inherit;
    resize: none; outline: none; max-height: 140px; min-height: 48px;
    line-height: 1.5; transition: border .2s;
  }
  .input-row textarea:focus { border-color: var(--primary); }
  .btn-send {
    width: 48px; height: 48px; border-radius: 14px; border: none;
    background: var(--primary); color: #fff; font-size: 20px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all .15s; flex-shrink: 0;
  }
  .btn-send:hover { background: #7d6ff0; transform:scale(1.05); }
  .btn-send:disabled { opacity:.4; cursor:not-allowed; transform:none; }

  .welcome {
    text-align: center; color: var(--text2); margin: auto;
    font-size: 15px; line-height: 1.8;
  }
  .welcome .icon { font-size: 48px; margin-bottom: 12px; }

  .ctx-info {
    font-size: 11px; color: var(--text2); text-align: center;
    padding: 4px; opacity: 0.7;
  }
</style>
</head>
<body>

<div id="setupScreen">
  <div class="setup-card">
    <h1>🦙 Ollama Chat</h1>
    <p class="sub">Nhập thông tin kết nối để bắt đầu trò chuyện</p>
    <div class="field">
      <label>Ollama API URL</label>
      <input type="text" id="apiUrl" placeholder="http://localhost:11434" />
    </div>
    <div class="field">
      <label>Tên Model</label>
      <input type="text" id="modelName" placeholder="gemma3:4b" />
    </div>
    <button class="btn-start" onclick="startChat()">Bắt đầu trò chuyện →</button>
    <div class="error-msg" id="setupError"></div>
  </div>
</div>

<div id="chatScreen">
  <div class="chat-header">
    <div class="left">
      <div class="dot"></div>
      <div>
        <div class="model-name" id="headerModel">model</div>
        <div class="api-label" id="headerApi">api</div>
      </div>
    </div>
    <div class="header-btns">
      <button onclick="clearChat()">🗑 Xóa chat</button>
      <button onclick="goBack()">⚙ Đổi model</button>
    </div>
  </div>

  <div class="messages" id="messages">
    <div class="welcome">
      <div class="icon">💬</div>
      Bắt đầu trò chuyện với model<br>
      <strong id="welcomeModel"></strong>
    </div>
  </div>
  <div class="ctx-info" id="ctxInfo">Lịch sử: 0 tin nhắn</div>

  <div class="input-area">
    <div class="input-row">
      <textarea id="userInput" rows="1"
        placeholder="Nhập tin nhắn..."
        onkeydown="handleKey(event)"
        oninput="autoResize(this)"></textarea>
      <button class="btn-send" id="btnSend" onclick="sendMessage()">➤</button>
    </div>
  </div>
</div>

<script>
let API_URL = '';
let MODEL   = '';
let history = [];
let streaming = false;

(function init(){
  const saved = localStorage.getItem('ollama_cfg');
  if(saved){
    try {
      const c = JSON.parse(saved);
      document.getElementById('apiUrl').value   = c.api || '';
      document.getElementById('modelName').value = c.model || '';
    } catch(e){}
  }
})();

function updateCtxInfo(){
  const n = history.length;
  document.getElementById('ctxInfo').textContent =
    `🧠 AI đang nhớ ${n} tin nhắn trong lịch sử`;
}

function startChat(){
  API_URL = document.getElementById('apiUrl').value.trim().replace(/\/+$/,'');
  MODEL   = document.getElementById('modelName').value.trim();
  const err = document.getElementById('setupError');

  if(!API_URL){ err.textContent='⚠ Vui lòng nhập API URL'; err.style.display='block'; return; }
  if(!MODEL)  { err.textContent='⚠ Vui lòng nhập tên model'; err.style.display='block'; return; }
  err.style.display='none';

  localStorage.setItem('ollama_cfg', JSON.stringify({api:API_URL, model:MODEL}));

  document.getElementById('setupScreen').style.display = 'none';
  document.getElementById('chatScreen').style.display  = 'flex';
  document.getElementById('headerModel').textContent   = MODEL;
  document.getElementById('headerApi').textContent     = API_URL;
  document.getElementById('welcomeModel').textContent   = MODEL;
  document.getElementById('userInput').focus();
  updateCtxInfo();
}

function goBack(){
  document.getElementById('chatScreen').style.display  = 'none';
  document.getElementById('setupScreen').style.display = 'flex';
}

function clearChat(){
  history = [];
  updateCtxInfo();
  document.getElementById('messages').innerHTML = `
    <div class="welcome">
      <div class="icon">💬</div>
      Bắt đầu trò chuyện với model<br>
      <strong>${MODEL}</strong>
    </div>`;
}

function autoResize(el){
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 140) + 'px';
}

function handleKey(e){
  if(e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    sendMessage();
  }
}

async function sendMessage(){
  if(streaming) return;
  const input = document.getElementById('userInput');
  const text  = input.value.trim();
  if(!text) return;

  const w = document.querySelector('.welcome');
  if(w) w.remove();

  addMessage('user', text);
  history.push({role:'user', content:text});
  updateCtxInfo();
  input.value = '';
  input.style.height = 'auto';

  const botEl = addMessage('bot', '<div class="typing-dots"><span></span><span></span><span></span></div>');
  const bubble = botEl.querySelector('.bubble');

  streaming = true;
  document.getElementById('btnSend').disabled = true;

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        api_url: API_URL,
        model: MODEL,
        messages: history
      })
    });

    if(!resp.ok) throw new Error('HTTP ' + resp.status);

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let fullText = '';
    bubble.textContent = '';

    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buffer += decoder.decode(value, {stream:true});
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for(const line of lines){
        if(!line.startsWith('data: ')) continue;
        const data = line.slice(6).trim();
        if(data === '[DONE]') break;
        try {
          const json = JSON.parse(data);
          if(json.error) throw new Error(json.error);
          if(json.message && json.message.content){
            fullText += json.message.content;
            bubble.textContent = fullText;
            scrollBottom();
          }
        } catch(parseErr){
          if(parseErr.message && !parseErr.message.includes('JSON')){
            bubble.textContent = '❌ Lỗi: ' + parseErr.message;
          }
        }
      }
    }
    if(fullText) {
      history.push({role:'assistant', content:fullText});
      updateCtxInfo();
    }

  } catch(err){
    bubble.textContent = '❌ Lỗi: ' + err.message;
    console.error('Chat error:', err);
  } finally {
    streaming = false;
    document.getElementById('btnSend').disabled = false;
    document.getElementById('userInput').focus();
    scrollBottom();
  }
}

function addMessage(role, html){
  const container = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  const avatar = role === 'user' ? '👤' : '🤖';
  div.innerHTML = `<div class="avatar">${avatar}</div><div class="bubble">${html}</div>`;
  container.appendChild(div);
  scrollBottom();
  return div;
}

function scrollBottom(){
  const m = document.getElementById('messages');
  m.scrollTop = m.scrollHeight;
}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════
#  BACKEND - ĐÃ SỬA LỖI CONNECTION
# ═══════════════════════════════════════════════════════════
class ChatHandler(http.server.BaseHTTPRequestHandler):

    # ✅ FIX 1: Dùng HTTP/1.1 để hỗ trợ keep-alive và connection tốt hơn
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if args and isinstance(args[0], str) and 'POST' in args[0]:
            print(f"  ▸ {args[0]}")

    def do_GET(self):
        body = HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type",   "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection",     "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        try:
            data     = json.loads(body)
            api_url  = data["api_url"].rstrip("/")
            model    = data["model"]
            messages = data.get("messages", [])
        except Exception as e:
            self._send_json_error(400, str(e))
            return

        ollama_url = f"{api_url}/api/chat"
        payload    = json.dumps({
            "model":    model,
            "messages": messages,
            "stream":   True
        }).encode("utf-8")

        req = urllib.request.Request(
            ollama_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        # Gửi SSE headers
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "close")  # ✅ FIX 2: đóng connection sau stream
        self.end_headers()

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                for line in resp:
                    if line:
                        chunk = f"data: {line.decode('utf-8')}\n\n"
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()

        except urllib.error.URLError as e:
            err = json.dumps({"error": f"Không kết nối được Ollama: {e.reason}"})
            self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception as e:
            err = json.dumps({"error": str(e)})
            self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        # ✅ FIX 3: Đảm bảo đóng connection
        self.close_connection = True

    def _send_json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection",     "close")
        self.end_headers()
        self.wfile.write(body)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True  # ✅ Tránh lỗi "Address already in use" khi restart


if __name__ == "__main__":
    PORT = 9999
    server = ThreadedServer(("0.0.0.0", PORT), ChatHandler)
    print(f"""
╔══════════════════════════════════════════╗
║        🦙  OLLAMA WEB CHAT v2  🦙        ║
╠══════════════════════════════════════════╣
║                                          ║
║   ➜  http://localhost:{PORT}               ║
║                                          ║
║   ✅ Fixed: chat multiple times          ║
║   🧠 Context memory: until F5 refresh    ║
║                                          ║
╚══════════════════════════════════════════╝
    """)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹  Server đã dừng.")
        server.shutdown()