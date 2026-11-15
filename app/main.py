"""小説ハーネス司令塔：FastAPI + 簡易UI（AnythingLLM 風サイドバーレイアウト）。

ワークスペース＝物語（作品）単位。別の物語は別のワークスペースとして区切る。
設定は画面から変更して DB に保存する（Container Manager の GUI から起動しただけでも使える）。
"""
import html
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response

import db
import llm
import pipeline

UI_TOKEN = os.environ.get("UI_TOKEN", "")
app = FastAPI(title="novel-harness")

APP_VERSION = "1.11"
DOCKERHUB_URL = "https://hub.docker.com/r/yuichi894/novel-harness"
DOCKERHUB_SVG = (
    "<svg width='58' height='42' viewBox='0 0 58 42' xmlns='http://www.w3.org/2000/svg' "
    "role='img' aria-label='Docker Hub'><g fill='#2496ed'>"
    "<rect x='13' y='1' width='8' height='8' rx='1.5'/>"
    "<rect x='23' y='1' width='8' height='8' rx='1.5'/>"
    "<rect x='13' y='11' width='8' height='8' rx='1.5'/>"
    "<rect x='23' y='11' width='8' height='8' rx='1.5'/>"
    "<rect x='33' y='11' width='8' height='8' rx='1.5'/>"
    "<path d='M54 23c-.8 8.4-6.8 14-15 14H19C10.8 37 4.8 31.4 4 23h50z'/>"
    "<path d='M50 23c2.5-.6 4.4-2.2 5.4-4.6-2 .3-3.9-.3-5.2-1.6.3 2.2.3 4.2 0 6.2z'/>"
    "</g></svg>")

STAGE_LABEL = {
    "draft": "1. 続き読み込み（素材）",
    "analyzing": "初期文書解析中",
    "analyzed": "初期文書解析済み",
    "outlining": "2. 次章の内容を生成中",
    "outline_review": "2. 次章の内容（確認・修正待ち）",
    "generating": "3. 1次出力中",
    "wait_q4": "4. 2次修正（モデル切替待ち）",
    "wait_model": "4. 2次修正（モデル切替待ち）",
    "revising": "4. 2次修正中",
    "done": "5. 完了（PHP・TXT化できます）",
    "failed": "エラー",
}

FLOW_STEPS = ["1. 続き読み込み", "2. 次章の内容", "3. 1次出力", "4. 2次修正", "5. PHP・TXT化"]

CSS = """
*{box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;margin:0;line-height:1.6;background:#f4f5f7;color:#222}
.layout{display:flex;min-height:100vh}
.sidebar{width:280px;flex:0 0 280px;background:#17191d;color:#dfe3e8;padding:1em .8em;position:sticky;top:0;height:100vh;overflow-y:auto}
.sidebar a{color:#dfe3e8;text-decoration:none}
.brand{font-size:1.15em;font-weight:700;padding:.2em .4em .8em}
.side-sec{font-size:.78em;color:#8b93a1;text-transform:uppercase;letter-spacing:.08em;margin:1em .4em .3em}
.ws-item{display:block;padding:.45em .6em;border-radius:8px;margin:.15em 0}
.ws-item:hover{background:#23262d}
.ws-item.active{background:#2e6fdb;color:#fff}
.ws-item .st{display:block;font-size:.75em;color:#9aa3af}
.ws-item.active .st{color:#cfe2ff}
.ch-item{display:flex;align-items:center;gap:.3em;padding:.3em .6em .3em 1.2em;border-radius:8px;font-size:.92em}
.ch-item:hover{background:#23262d}
.ch-item a{flex:1}
.ch-del{background:transparent;border:none;color:#9aa3af;cursor:pointer;padding:.1em .35em;font-size:.9em;border-radius:6px}
.ch-del:hover{background:#4a2323;color:#ff9a9a}
.meter{background:#e8ebef;border-radius:6px;height:10px;margin:.3em 0;overflow:hidden}
.meter>div{background:#2e6fdb;height:100%}
.usage{font-size:.9em;color:#444}
.side-foot{margin-top:1.5em;padding:.6em .4em;border-top:1px solid #2a2e36;font-size:.85em;color:#9aa3af}
.main{flex:1;padding:1.2em 1.6em;max-width:1000px}
textarea{width:100%} input[type=text]{width:100%}
.card{background:#fff;border:1px solid #dde1e6;border-radius:10px;padding:1em 1.2em;margin:1em 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.warn{border-color:#c33;background:#fff5f5}
pre{white-space:pre-wrap;background:#f7f7f7;padding:1em;border-radius:8px}
button{padding:.45em 1.1em;border:1px solid #2e6fdb;background:#2e6fdb;color:#fff;border-radius:8px;cursor:pointer}
button:hover{background:#255cb8}
.row{margin:.5em 0}
.steps{color:#555}
h1{margin-top:.2em}
"""


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def sidebar(current_id=None, chapters=None) -> str:
    try:
        works = db.q("SELECT * FROM works ORDER BY id DESC")
    except Exception:
        works = []
    items = ""
    for w in works:
        cls = "ws-item active" if current_id and w["id"] == current_id else "ws-item"
        items += (f"<a class='{cls}' href='/works/{w['id']}'>{esc(w['title'])}"
                  f"<span class='st'>{esc(STAGE_LABEL.get(w['stage'], w['stage']))}</span></a>")
    ch_html = ""
    if chapters:
        items = ""
        for c in chapters:
            items += (
                f"<div class='ch-item'><a href='/chapters/{c['id']}'>第{c['number']}章 {esc(c['title'])}"
                f"（{esc(c['status'])}）</a>"
                f"<form method='post' action='/chapters/{c['id']}/delete' "
                "onsubmit=\"return confirm('この章を削除しますか？（本文・概要も消えます）')\" style='margin:0'>"
                "<button class='ch-del' title='この章を削除'>✕</button></form></div>")
        ch_html = "<div class='side-sec'>章</div>" + items
    return (
        "<div class='sidebar'>"
        "<div class='brand'>📚 小説ハーネス</div>"
        "<a class='ws-item' href='/'>＋ 新規ワークスペース</a>"
        "<div class='side-sec'>ワークスペース</div>"
        + (items or "<div class='ws-item'>まだありません</div>")
        + ch_html
        + "<div class='side-foot'>"
        f"<div>ロード中モデル：{esc(llm.loaded_model())}</div>"
        "<div style='margin-top:.4em'><a href='/glossary'>📖 用語DB</a> ｜ <a href='/settings'>⚙ 設定</a></div>"
        "</div></div>")


def page(title: str, body: str, current_id=None, chapters=None, refresh: bool = False) -> HTMLResponse:
    auto = "<script>setTimeout(function(){location.reload()},5000)</script>" if refresh else ""
    return HTMLResponse(
        "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f"<div class='layout'>{sidebar(current_id, chapters)}"
        f"<div class='main'><h1>{esc(title)}</h1>{body}</div></div>{auto}</body></html>")


def authed(request: Request) -> bool:
    if not UI_TOKEN:
        return True
    return request.cookies.get("ui_token") == UI_TOKEN


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    if request.url.path in ("/login", "/health") or authed(request):
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
def health():
    return {"ok": True, "loaded_model": llm.loaded_model(), "mock": llm.MOCK,
            "base_url_set": bool(llm.base_url())}


@app.get("/login")
def login_form():
    return page("ログイン", "<form method='post'><div class='row'>"
                "<input type='password' name='token' placeholder='UIトークン'></div>"
                "<button>ログイン</button></form>")


@app.post("/login")
def login(token: str = Form("")):
    resp = RedirectResponse("/", status_code=303)
    if UI_TOKEN and token == UI_TOKEN:
        resp.set_cookie("ui_token", token, max_age=60 * 60 * 24 * 30)
    return resp


_DROP_OVERLAY = ("<div id='dropOverlay' style='display:none;position:fixed;inset:0;"
                 "background:rgba(36,150,237,.13);border:3px dashed #2496ed;z-index:9999;"
                 "pointer-events:none;text-align:center;padding-top:42vh;font-size:1.25em;"
                 "font-weight:bold;color:#1b6ac9'>__MSG__</div>")

_DROP_JS_COMMON = """
  var overlay = document.getElementById('dropOverlay');
  function hasFiles(e) {
    return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0;
  }
  window.addEventListener('dragover', function (e) {
    if (hasFiles(e)) { e.preventDefault(); overlay.style.display = 'block'; }
  });
  window.addEventListener('dragleave', function (e) {
    if (!e.relatedTarget) overlay.style.display = 'none';
  });
"""

# 作品ページ：ドロップしたファイルをその作品の素材として即アップロードする。
DROP_SCRIPT_WORK = (
    _DROP_OVERLAY.replace("__MSG__", "TXTファイルをドロップすると素材に追加します")
    + "<script>(function () {" + _DROP_JS_COMMON + """
  window.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    overlay.style.display = 'none';
    var f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    var fd = new FormData();
    fd.append('file', f);
    fetch('/works/__WID__/upload', { method: 'POST', body: fd })
      .then(function () { location.reload(); });
  });
})();</script>""")

# トップページ：ドロップしたファイルをワークスペース作成フォームにセットする。
DROP_SCRIPT_INDEX = (
    _DROP_OVERLAY.replace("__MSG__", "TXTファイルをドロップすると作成フォームにセットします")
    + "<script>(function () {" + _DROP_JS_COMMON + """
  var form = document.querySelector('form[action="/works"]');
  var input = form ? form.querySelector('input[name="source_file"]') : null;
  var dropped = null;
  var s = document.getElementById('srcName');
  var clr = document.getElementById('srcClear');
  function showFile(name) {
    if (s) s.textContent = name ? '選択中: ' + name : '';
    if (clr) clr.style.display = name ? '' : 'none';
  }
  if (input) input.addEventListener('change', function () {
    dropped = null;
    showFile(input.files && input.files[0] ? input.files[0].name : '');
  });
  if (clr) clr.addEventListener('click', function () {
    dropped = null;
    if (input) input.value = '';
    showFile('');
  });
  window.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    overlay.style.display = 'none';
    var f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f || !input) return;
    dropped = f;
    try { input.files = e.dataTransfer.files; } catch (err) {}
    showFile(f.name);
  });
  if (form) form.addEventListener('submit', function (e) {
    if (dropped && input && (!input.files || !input.files.length)) {
      e.preventDefault();
      var fd = new FormData(form);
      fd.set('source_file', dropped);
      fetch('/works', { method: 'POST', body: fd })
        .then(function (r) { location.href = r.url; });
    }
  });
})();</script>""")


def setup_banner() -> str:
    if llm.MOCK or llm.base_url():
        return ""
    return ("<div class='card warn'><h2>初回セットアップ</h2>"
            "<p>推論サーバー（XeStrata）の接続先が未設定です。"
            "<a href='/settings'>設定画面</a>で接続先URLを入力してください。"
            "コンテナの環境変数を触る必要はありません。</p></div>")


@app.get("/")
def index():
    works = db.q("SELECT * FROM works ORDER BY id DESC")
    items = "".join(
        f"<div class='card'><a href='/works/{w['id']}'><b>{esc(w['title'])}</b></a>"
        f" ― 状態：{esc(STAGE_LABEL.get(w['stage'], w['stage']))}"
        f" <form method='post' action='/works/{w['id']}/delete' style='display:inline' "
        f"onsubmit=\"return confirm('このワークスペースを削除しますか？（作品データが消えます。用語DBは残ります）')\">"
        f"<button style='background:#b03a3a;border-color:#b03a3a'>削除</button></form></div>"
        for w in works) or "<p>まだワークスペースがありません。左のサイドバーか下のフォームから作成してください。</p>"
    form = """<div class='card'><h2>新規ワークスペース（物語ごとに作成）</h2>
    <form method='post' action='/works' enctype='multipart/form-data'>
    <div class='row'>タイトル<input type='text' name='title' required></div>
    <div class='row'>欲しい内容<textarea name='request' rows='5'></textarea></div>
    <div class='row'>設定資料<textarea name='settings' rows='5'></textarea></div>
    <div class='row'>素材テキスト（任意・作成と同時にアップロード）<input type='file' name='source_file' accept='.txt,.md,.text,text/plain'> <span id='srcName'></span> <button type='button' id='srcClear' style='display:none;background:#b03a3a;border-color:#b03a3a'>選択を削除</button>（画面へドラッグ＆ドロップでもセットできます）</div>
    <div class='row'>章数<input type='text' name='num_chapters' value='3' style='width:4em'></div>
    <div class='row'>章の最低文字数<select name='min_chars'>""" + min_chars_options(0) + """</select>
    （届かない場合は自動で続きを書き足します）</div>
    <div class='row'>モード：
    <label><input type='radio' name='mode' value='switch' checked> 切替モード（1次出力→モデル切替→2次修正）</label><br>
    <label><input type='radio' name='mode' value='thread'> スレッドモード（モデル切替なし・章ごとに別スレッドで完結）</label></div>
    <div class='row'><label><input type='checkbox' name='claude_ok' value='1'> Muse監修レーン可</label></div>
    <button>作成</button></form></div>"""
    steps = "<p class='steps'>工程：" + " ⇒ ".join(FLOW_STEPS) + "</p>"
    return page("小説ハーネス", setup_banner() + steps + items + form + DROP_SCRIPT_INDEX)


@app.post("/works")
async def create_work(title: str = Form(...), request: str = Form(""), settings: str = Form(""),
                      num_chapters: int = Form(3), claude_ok: int = Form(0), mode: str = Form("switch"),
                      min_chars: int = Form(0), source_file: UploadFile = File(None)):
    base = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower() or "work"
    slug = f"{base}-{int(time.time()) % 1000000}"
    wid = db.run(
        "INSERT INTO works(title, slug, request, settings, num_chapters, claude_ok, mode, min_chars, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (title, slug, request, settings, num_chapters, claude_ok,
         mode if mode in ("switch", "thread") else "switch",
         min_chars if min_chars in MIN_CHARS_CHOICES else 0, db.now(), db.now()))
    pipeline.work_dir(slug)
    if source_file is not None and source_file.filename:
        raw = await source_file.read()
        if raw:
            text = raw.decode("utf-8", errors="replace")
            name = re.sub(r"[^\w.\-]+", "_", source_file.filename or "source.txt")
            w = db.one("SELECT * FROM works WHERE id=?", (wid,))
            pipeline.save_artifact(w, None, "source", name, text)
    return RedirectResponse(f"/works/{wid}", status_code=303)


MIN_CHARS_CHOICES = [0, 5000, 10000, 15000, 20000, 30000, 40000, 50000]


def min_chars_options(current) -> str:
    cur = int(current or 0)
    out = ""
    for v in MIN_CHARS_CHOICES:
        label = "指定なし" if v == 0 else f"{v}文字"
        out += f"<option value='{v}' {'selected' if v == cur else ''}>{label}</option>"
    return out


def fmt_dur(sec) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分{sec % 60}秒"
    return f"{sec // 3600}時間{sec % 3600 // 60}分"


JOB_STATUS_STYLE = {
    "done": "color:#1a9e4b",       # 完了：緑
    "failed": "color:#e5484d",     # イレギュラーな停止：赤
    "cancelled": "color:#e8862e",  # 取消・異常終了：オレンジ
    "running": "color:#2e6fdb",    # 実行中：青
    "queued": "color:#8b93a1",     # 待機中：グレー
}


def status_html(status) -> str:
    style = JOB_STATUS_STYLE.get(status or "")
    if style:
        return f"<span style='{style};font-weight:bold'>{esc(status)}</span>"
    return esc(status)


def usage_html(job) -> str:
    """ジョブの usage をメーターで出す。メーターは「直近の呼び出し」基準で、

    上限（max／budget）は1回ごとの値。ジョブ合計は別行に参考表示する。
    thinking の内訳はサーバーが usage に含めて返す場合だけ分かる。返らない場合は
    出力トークンで代替表示する（内訳は捏造しない）。
    """
    raw = job.get("usage") or ""
    if not raw:
        return ""
    try:
        u = json.loads(raw)
    except ValueError:
        return ""
    if not u.get("calls"):
        return ""
    budget = llm.reasoning_budget()
    mx = u.get("last_max_tokens")
    calls = u["calls"]
    # メーターは「直近の呼び出し」基準：上限（max／budget）は1回ごとの値なので、
    # ジョブ合計と並べると上限超過に見えてしまう。合計は別記する。
    last_rt = u.get("last_reasoning_tokens")
    last_comp = u.get("last_completion_tokens")
    if not isinstance(last_rt, int) and not isinstance(last_comp, int):
        # 旧形式（last_* なし）：合計しか分からないので上限とは並べない
        comp = u.get("completion_tokens")
        if not isinstance(comp, int):
            return ""
        return (f"<div class='usage'>output: 合計 {comp} tokens"
                f" ／ 入力 合計 {u.get('prompt_tokens', '―')} tokens"
                f"・API呼び出し {calls}回</div>")
    if isinstance(last_rt, int):
        main = f"thinking: 直近 {last_rt} of budget {budget if budget > 0 else '―'} (max {mx if mx else '―'}) tokens"
        pct = (last_rt / budget * 100) if budget > 0 else ((last_rt / mx * 100) if mx else 0)
    else:
        main = f"output: 直近 {last_comp} (max {mx if mx else '―'}) tokens（thinking 内訳はサーバー未提供）"
        pct = (last_comp / mx * 100) if mx else 0
    pct = max(0.0, min(100.0, pct))
    html_out = (f"<div class='usage'>{esc(main)} ／ 入力： 直近 {u.get('last_prompt_tokens', '―')} tokens</div>"
                f"<div class='meter'><div style='width:{pct:.0f}%'></div></div>")
    if calls > 1:
        html_out += (f"<div class='usage'>ジョブ合計： output {u.get('completion_tokens', '―')}"
                     f" ／ 入力 {u.get('prompt_tokens', '―')} tokens"
                     f"・API呼び出し {calls}回（1回最大 output {u.get('peak_completion_tokens', '―')}）</div>")
    return html_out


def enqueue(work_id: int, kind: str, payload: dict | None = None):
    db.run("INSERT INTO jobs(work_id, kind, status, payload, created_at, updated_at)"
           " VALUES(?,?,'queued',?,?,?)",
           (work_id, kind, db.dumps(payload or {}), db.now(), db.now()))


@app.get("/works/{work_id}")
def work_detail(work_id: int):
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w:
        return PlainTextResponse("not found", status_code=404)
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work_id,))
    jobs = db.q("SELECT * FROM jobs WHERE work_id=? ORDER BY id DESC LIMIT 10", (work_id,))
    arts = db.q("SELECT * FROM artifacts WHERE work_id=? ORDER BY id DESC LIMIT 50", (work_id,))
    draft_model = db.get_setting(f"work:{work_id}:draft_model", "")
    revise_model = db.get_setting(f"work:{work_id}:revise_model", "")
    analysis = pipeline.latest_artifact(work_id, None, "analysis")

    fanfic = int(w.get("fanfic") or 0) == 1
    fanfic_html = ""
    if db.get_setting("show_fanfic_button", "1") != "0":
        fanfic_html = (
            "<div class='card'><h2>二次創作モード</h2>"
            f"<p>現在：<b>{'ON（二次創作として扱います）' if fanfic else 'OFF（文面からのみ判断します）'}</b></p>"
            f"<form method='post' action='/works/{w['id']}/fanfic'>"
            f"<input type='hidden' name='fanfic' value='{'0' if fanfic else '1'}'>"
            f"<button>{'二次創作をOFFにする' if fanfic else '二次創作をONにする'}</button></form>"
            "<p>ONにすると、AIに二次創作であることが伝わり、キャラクター概要の検索・確認が使えるようになります。</p></div>")

    search_html = ""
    if pipeline.web_search_on(w):
        char_form = ""
        if fanfic:
            char_form = (
                f"<form method='post' action='/works/{w['id']}/search'>"
                "<input type='hidden' name='mode' value='character'>"
                "<div class='row'>キャラクター名<input type='text' name='term' required></div>"
                "<div class='row'>原作名（任意）<input type='text' name='origin'></div>"
                "<button>キャラクター概要を検索して作成</button></form>"
                "<p>Web上の公開資料から、一人称・語尾・口癖・言い回し・立ち振る舞いをまとめた概要を作成し、生成に反映します。</p>")
        search_html = (
            "<div class='card'><h2>Web検索（用語・キャラクター）</h2>"
            f"<form method='post' action='/works/{w['id']}/search'>"
            "<input type='hidden' name='mode' value='word'>"
            "<div class='row'>単語・新語・造語<input type='text' name='term' required></div>"
            "<button>意味を検索して用語メモを作成</button></form>"
            "<p>モデルが知らない単語の意味を日本語 Wiktionary / Wikipedia / Web から調べ、用語メモとして生成に反映します。"
            "外部へ送るのは検索語だけで、原稿本文は送りません。</p>"
            f"<form method='post' action='/works/{w['id']}/jobs'>"
            "<input type='hidden' name='kind' value='term_check'>"
            "<button>用語チェックを実行（モデルが知らない単語を自動で調べる）</button></form>"
            "<p>素材や本文に出てきた未知の単語をモデルに指摘させ、用語DBにあれば再検索せず、なければ検索して用語DBに追加します。"
            "概要生成と1次出力の前にも自動で実行されます。</p>"
            + char_form + "</div>")

    if db.get_setting("web_search_enabled", "1") == "0":
        ws_html = ("<p>インターネットから単語の意味を取得：<b>OFF</b>"
                   "（全体設定でWeb検索が無効になっています）</p>")
    else:
        ws_on = pipeline.web_search_on(w)
        ws_html = (
            f"<form method='post' action='/works/{w['id']}/websearch'>"
            f"<input type='hidden' name='web_search' value='{'0' if ws_on else '1'}'>"
            f"<p>インターネットから単語の意味を取得：<b>{'ON' if ws_on else 'OFF'}</b> "
            f"<button>{'OFFにする' if ws_on else 'ONにする'}</button> "
            "<span style='color:#8b93a1'>（OFFの間は、用語の自動チェック・検索をこの作品では行いません）</span></p></form>")

    analysis_html = ""
    if analysis:
        analysis_html = (f"<div class='card'><h3>初期文書解析の結果</h3>"
                         f"<pre>{esc(pipeline.read_text(analysis['path']))}</pre>"
                         "<p>この解析結果は、以降の生成（概要・本文・修正）の文脈に自動で含まれます。</p></div>")

    state_now = pipeline.current_state(w)
    state_html = ""
    if state_now:
        state_html = (f"<div class='card'><h2>現在の状態（状態チェーン）</h2>"
                      f"<pre>{esc(state_now)}</pre>"
                      "<p>各章は開始時点の状態を入力として受け取り、終了状態を次章へ渡します。"
                      "章ごとの入出力状態は各章ページで確認できます。</p></div>")

    min_chars = int(w.get("min_chars") or 0)
    ch_html = ""
    for ch in chapters:
        chars = pipeline.chapter_chars(work_id, ch["id"])
        chars_note = (f"<div class='row'>本文：{chars}文字"
                      + (f"（最低{min_chars}文字）" if min_chars > 0 else "") + "</div>") if chars else ""
        ch_html += f"""<div class='card' id='ch-{ch['number']}'><h3>第{ch['number']}章 {esc(ch['title'])}（{esc(ch['status'])}）</h3>
        {chars_note}
        <form method='post' action='/chapters/{ch['id']}'>
        <div class='row'>タイトル<input type='text' name='title' value="{esc(ch['title'])}"></div>
        <div class='row'>概要<textarea name='outline' rows='4'>{esc(ch['outline'])}</textarea></div>
        <button>概要を保存（承認）</button></form>
        <div class='row'><a href='/chapters/{ch['id']}'>章ページを開く（本文・Think・状態・修正要綱で再生成）</a></div>
        <form method='post' action='/chapters/{ch['id']}/delete' onsubmit="return confirm('この章を削除しますか？（本文・概要も消えます）')" style='margin-top:.5em'>
        <button style='background:#b03a3a;border-color:#b03a3a'>この章を削除</button></form></div>"""

    switch_buttons = (
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='generate_chapters'><button>3. 1次出力（本文生成・現在ロード中のモデル）</button></form>"
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='revise_all'><button>4. 2次修正（モデル切替を検知して自動実行）</button></form>")
    thread_buttons = (
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='generate_thread'><button>3〜4. 章ごとスレッドで生成＋仕上げ（モデル切替なし・最初に選んだモデルのまま完結）</button></form>"
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='revise_all'><button>4. 仕上げ修正のみ（生成済みの章を同モデルで修正）</button></form>")

    notices = db.q("SELECT * FROM notices WHERE work_id=? ORDER BY id DESC LIMIT 20", (work_id,))
    notice_html = ""
    if notices:
        notice_html = ("<div class='card'><h2>お知らせ（用語DB）</h2>" + "".join(
            f"<div>{esc(n['body'])}</div>" for n in notices) + "</div>")

    now_ts = time.time()
    job_rows = ""
    for j in jobs:
        if j["status"] == "running":
            dur = f"（実行中・経過 {fmt_dur(now_ts - j['created_at'])}）"
            if j["kind"] == "revise_all" and w["stage"] == "wait_model":
                dur += " モデル切替待ちです：XeStrata 側でモデルを切り替えると自動で始まります（この間 running のままが正常です）"
        elif j["status"] == "queued":
            dur = "（待機中）"
        elif j["status"] in ("done", "failed"):
            dur = f"（所要 {fmt_dur(j['updated_at'] - j['created_at'])}）"
        else:
            dur = ""
        job_rows += (
            f"<div>#{j['id']} {esc(j['kind'])} ― {status_html(j['status'])} {dur} {esc(j['error'])}"
            + (f" <form method='post' action='/jobs/{j['id']}/cancel' style='display:inline'><button>取消</button></form>"
               if j["status"] in ("queued", "running") else "") + "</div>"
            + usage_html(j))
    job_html = job_rows
    art_html = "".join(
        f"<div><a href='/artifacts/{a['id']}'>{esc(a['kind'])}</a> {esc(a['path'])}"
        + (f" <form method='post' action='/artifacts/{a['id']}/delete' style='display:inline'><button>削除</button></form>"
           if a["kind"] in ("glossary", "character", "analysis", "source") else "") + "</div>"
        for a in arts)
    source_html = "".join(
        f"<div>📄 <a href='/artifacts/{a['id']}'>{esc(os.path.basename(a['path']))}</a>"
        f" <form method='post' action='/artifacts/{a['id']}/delete' style='display:inline' "
        f"onsubmit=\"return confirm('このファイルを削除しますか？')\"><button>削除</button></form></div>"
        for a in arts if a["kind"] == "source")
    if source_html:
        source_html = "<p>アップロード済みファイル：</p>" + source_html

    body = f"""
    <p>状態：{esc(STAGE_LABEL.get(w['stage'], w['stage']))}</p>
    <p class='steps'>工程：{' ⇒ '.join(FLOW_STEPS)}</p>
    <p>ロード中モデル：{esc(llm.loaded_model())}
    ／ 1次出力モデル：{esc(draft_model) or '―'} ／ 2次修正モデル：{esc(revise_model) or '―'}</p>
    {fanfic_html}
    <div class='card'><h2>1. 続き読み込み（素材ファイル：Txt等）</h2>
    <form method='post' action='/works/{w['id']}/upload' enctype='multipart/form-data'>
    <input type='file' name='file'> <button>アップロード</button></form>
    {source_html}
    <p>既存本文や設定資料を取り込むと、続き生成の起点になります。</p>
    <form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='analyze'><button>初期文書解析を実行（アップロード済み文書から基礎資料を作成）</button></form>
    </div>
    {analysis_html}
    {state_html}
    {search_html}
    {notice_html}
    <div class='card'><h2>工程の実行</h2>
    <p>モード：<b>{'スレッドモード（モデル切替なし・章ごとスレッド）' if (w.get('mode') or 'switch') == 'thread' else '切替モード（1次出力→モデル切替→2次修正）'}</b>（作品情報で変更できます）</p>
    {ws_html}
    <form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='outline'><button>2. 次章の内容（各章概要）を生成</button></form>
    {thread_buttons if (w.get('mode') or 'switch') == 'thread' else switch_buttons}
    <p><a href='/works/{w['id']}/download'>5. まとめテキストをダウンロード（TXT化・final）</a></p></div>
    <h2>章と概要</h2>{ch_html or '<p>まだ概要がありません。</p>'}
    <div class='card'><h2>ジョブ</h2>{job_html or '<p>なし</p>'}</div>
    <div class='card'><h2>成果物</h2>{art_html or '<p>なし</p>'}</div>
    <div class='card'><h2>作品情報</h2>
    <form method='post' action='/works/{w['id']}/meta'>
    <div class='row'>欲しい内容<textarea name='request' rows='4'>{esc(w['request'])}</textarea></div>
    <div class='row'>設定資料<textarea name='settings' rows='4'>{esc(w['settings'])}</textarea></div>
    <div class='row'>章数<input type='text' name='num_chapters' value='{w['num_chapters']}' style='width:4em'></div>
    <div class='row'>章の最低文字数<select name='min_chars'>{min_chars_options(w.get('min_chars'))}</select>（届かない場合は自動で続きを書き足します）</div>
    <div class='row'>モード：
    <label><input type='radio' name='mode' value='switch' {'checked' if (w.get('mode') or 'switch') == 'switch' else ''}> 切替モード</label>
    <label><input type='radio' name='mode' value='thread' {'checked' if (w.get('mode') or 'switch') == 'thread' else ''}> スレッドモード（切替なし）</label></div>
    <button>保存</button></form></div>
    <div class='card'><h2>ワークスペースの削除</h2>
    <form method='post' action='/works/{w['id']}/delete' onsubmit="return confirm('このワークスペースを削除しますか？（作品データが消えます。用語DBは残ります）')">
    <button style='background:#b03a3a;border-color:#b03a3a'>このワークスペースを削除</button></form>
    <p>作品データは消えますが、<b>用語DBに覚えた単語の意味は残ります</b>（次回から再検索せず使えます）。</p></div>"""
    active = any(j["status"] in ("queued", "running") for j in jobs)
    return page(w["title"], body + DROP_SCRIPT_WORK.replace("__WID__", str(work_id)),
                current_id=work_id, chapters=chapters, refresh=active)


@app.post("/works/{work_id}/delete")
def work_delete(work_id: int):
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if w:
        import shutil
        shutil.rmtree(os.path.join(pipeline.WORKS_DIR, w["slug"]), ignore_errors=True)
        for table in ("notices", "jobs", "artifacts", "chapters", "reviews"):
            db.run(f"DELETE FROM {table} WHERE work_id=?", (work_id,))
        db.run("DELETE FROM works WHERE id=?", (work_id,))
        # 用語DB（glossary）は残す：ワークスペースを消しても単語の意味は覚えている
    return RedirectResponse("/", status_code=303)


@app.get("/glossary")
def glossary_index():
    rows = db.q("SELECT * FROM glossary ORDER BY term")
    items = "".join(
        f"<div class='card'><label><input type='checkbox' name='terms' value=\"{esc(r['term'])}\"> "
        f"<a href='/glossary/view?term={esc(r['term'])}'><b>{esc(r['term'])}</b></a></label></div>"
        for r in rows)
    if rows:
        items = (
            "<form method='post' action='/glossary/delete_selected' "
            "onsubmit=\"return confirm('選択した用語を削除しますか？（各作品の用語メモも一緒に消えます）')\">"
            + items
            + "<p><button style='background:#b03a3a;border-color:#b03a3a'>選択した用語を削除</button></p></form>"
            "<form method='post' action='/glossary/delete_all' "
            "onsubmit=\"return confirm('用語DBの全ての用語を削除しますか？（各作品の用語メモも一緒に消えます）')\">"
            "<p><button style='background:#b03a3a;border-color:#b03a3a'>全て削除</button></p></form>")
    else:
        items = "<p>まだ用語が登録されていません。</p>"
    return page("用語DB", "<p>検索して覚えた単語の意味はここに残ります。"
                "ワークスペースを削除しても消えません。同じ単語は2回目から検索に行かず、このメモを使います。</p>"
                "<p>作品で使われた用語メモは、その作品の生成コンテキストに毎回入ります。"
                "関係ない用語が増えたら、ここで削除してください（作品側の用語メモも一緒に消えます。再取得防止の記録は残るため、削除した用語がすぐ検索し直されることはありません）。</p>" + items)


@app.get("/glossary/view")
def glossary_view(term: str):
    row = db.one("SELECT * FROM glossary WHERE term=?", (term,))
    if not row:
        return PlainTextResponse("not found", status_code=404)
    return page(f"用語：{term}", f"<pre>{esc(row['memo'])}</pre>")


def _delete_glossary_terms(terms):
    """用語DBから消し、各作品の生成コンテキストに入っている用語メモ（成果物）も一緒に消す。"""
    for term in terms:
        db.run("DELETE FROM glossary WHERE term=?", (term,))
        fname = "glossary_" + re.sub(r"[^\w]+", "_", term)[:40] + ".txt"
        for a in db.q("SELECT * FROM artifacts WHERE kind='glossary'"):
            if a["path"].endswith(fname):
                try:
                    os.remove(a["path"])
                except OSError:
                    pass
                db.run("DELETE FROM artifacts WHERE id=?", (a["id"],))


@app.post("/glossary/delete")
def glossary_delete(term: str = Form(...)):
    _delete_glossary_terms([term])
    return RedirectResponse("/glossary", status_code=303)


@app.post("/glossary/delete_selected")
def glossary_delete_selected(terms: list[str] = Form([])):
    _delete_glossary_terms([t for t in terms if t])
    return RedirectResponse("/glossary", status_code=303)


@app.post("/glossary/delete_all")
def glossary_delete_all():
    _delete_glossary_terms([r["term"] for r in db.q("SELECT term FROM glossary")])
    return RedirectResponse("/glossary", status_code=303)


@app.post("/works/{work_id}/fanfic")
def work_fanfic(work_id: int, fanfic: int = Form(0)):
    db.run("UPDATE works SET fanfic=?, updated_at=? WHERE id=?",
           (1 if fanfic else 0, db.now(), work_id))
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/works/{work_id}/websearch")
def work_websearch(work_id: int, web_search: int = Form(0)):
    db.run("UPDATE works SET web_search=?, updated_at=? WHERE id=?",
           (1 if web_search else 0, db.now(), work_id))
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/works/{work_id}/search")
def work_search(work_id: int, mode: str = Form("word"), term: str = Form(...), origin: str = Form("")):
    enqueue(work_id, "search_term", {"mode": mode, "term": term, "origin": origin})
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/artifacts/{artifact_id}/delete")
def artifact_delete(artifact_id: int):
    a = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if a:
        try:
            os.remove(a["path"])
        except OSError:
            pass
        db.run("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        return RedirectResponse(f"/works/{a['work_id']}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.post("/works/{work_id}/meta")
def work_meta(work_id: int, request: str = Form(""), settings: str = Form(""),
              num_chapters: int = Form(3), mode: str = Form("switch"), min_chars: int = Form(0)):
    db.run("UPDATE works SET request=?, settings=?, num_chapters=?, mode=?, min_chars=?, updated_at=? WHERE id=?",
           (request, settings, num_chapters,
            mode if mode in ("switch", "thread") else "switch",
            min_chars if min_chars in MIN_CHARS_CHOICES else 0, db.now(), work_id))
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/works/{work_id}/upload")
async def work_upload(work_id: int, file: UploadFile = File(...)):
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    raw = await file.read()
    text = raw.decode("utf-8", errors="replace")
    name = re.sub(r"[^\w.\-]+", "_", file.filename or "source.txt")
    pipeline.save_artifact(w, None, "source", name, text)
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/works/{work_id}/jobs")
def work_job(work_id: int, kind: str = Form(...)):
    if kind not in pipeline.HANDLERS:
        return PlainTextResponse("bad kind", status_code=400)
    stage = {"analyze": "analyzing", "outline": "outlining",
             "generate_chapters": "generating", "generate_thread": "generating",
             "revise_all": "wait_model"}.get(kind)
    if stage:
        pipeline.set_stage(work_id, stage)
    enqueue(work_id, kind)
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/jobs/{job_id}/cancel")
def job_cancel(job_id: int):
    row = db.one("SELECT work_id FROM jobs WHERE id=?", (job_id,))
    db.run("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (db.now(), job_id))
    return RedirectResponse(f"/works/{row['work_id']}" if row else "/", status_code=303)


@app.post("/chapters/{chapter_id}/delete")
def chapter_delete(chapter_id: int):
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    for a in db.q("SELECT * FROM artifacts WHERE chapter_id=?", (chapter_id,)):
        try:
            os.remove(a["path"])
        except OSError:
            pass
    db.run("DELETE FROM artifacts WHERE chapter_id=?", (chapter_id,))
    db.run("DELETE FROM reviews WHERE chapter_id=?", (chapter_id,))
    db.run("DELETE FROM chapters WHERE id=?", (chapter_id,))
    return RedirectResponse(f"/works/{ch['work_id']}", status_code=303)


@app.get("/chapters/{chapter_id}")
def chapter_page(chapter_id: int):
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return PlainTextResponse("not found", status_code=404)
    w = db.one("SELECT * FROM works WHERE id=?", (ch["work_id"],))
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (w["id"],))
    final = pipeline.latest_artifact(w["id"], ch["id"], "final")
    draft = pipeline.latest_artifact(w["id"], ch["id"], "draft")
    body_art = final or draft
    body_label = "本文（2次修正済み）" if final else "本文（1次出力）"
    think = pipeline.latest_artifact(w["id"], ch["id"], "think")
    design = pipeline.latest_artifact(w["id"], ch["id"], "prompt")
    in_state = pipeline.current_state(w, ch["number"])
    out_state = pipeline.chapter_end_state(w, ch)
    chars = pipeline.chapter_chars(w["id"], ch["id"])
    min_chars = int(w.get("min_chars") or 0)
    jobs = db.q("SELECT * FROM jobs WHERE work_id=? AND kind='regen_chapter' ORDER BY id DESC LIMIT 3",
                (w["id"],))
    active = any(j["status"] in ("queued", "running") for j in jobs)
    job_html = "".join(
        f"<div>#{j['id']} 再生成 ― {status_html(j['status'])} {esc(j['error'])}</div>" + usage_html(j)
        for j in jobs)

    body_html = (f"<pre>{esc(pipeline.read_text(body_art['path']))}</pre>"
                 if body_art else "<p>まだ生成されていません。</p>")
    think_html = (f"<div class='card'><h2>Think（思考過程）</h2>"
                  f"<pre>{esc(pipeline.read_text(think['path']))}</pre></div>") if think else ""
    design_html = (f"<details class='card'><summary>生成指示書</summary>"
                   f"<pre>{esc(pipeline.read_text(design['path']))}</pre></details>") if design else ""
    state_html = ""
    if in_state or out_state:
        state_html = "<div class='card'><h2>状態（状態チェーン）</h2>"
        if in_state:
            state_html += f"<h3>入力状態（この章の開始時点）</h3><pre>{esc(in_state)}</pre>"
        if out_state:
            state_html += f"<h3>終了状態（この章の終了時点・次章へ渡す）</h3><pre>{esc(out_state)}</pre>"
        state_html += "</div>"

    body = f"""
    <p><a href='/works/{w['id']}'>← 作品ページ「{esc(w['title'])}」に戻る</a></p>
    <div class='card'><h2>章概要</h2>
    <form method='post' action='/chapters/{ch['id']}'>
    <div class='row'>タイトル<input type='text' name='title' value="{esc(ch['title'])}"></div>
    <div class='row'>概要<textarea name='outline' rows='5'>{esc(ch['outline'])}</textarea></div>
    <button>概要を保存（承認）</button></form></div>
    <div class='card'><h2>{body_label}</h2>
    <p>文字数：{chars}文字{'（最低' + str(min_chars) + '文字）' if min_chars > 0 else ''} ／ 状態：{esc(ch['status'])}</p>
    {body_html}</div>
    {think_html}
    {design_html}
    {state_html}
    <div class='card'><h2>修正要綱で再生成</h2>
    <form method='post' action='/chapters/{ch['id']}/regen'>
    <div class='row'>修正要綱（直したい点・盛り込む要素・避ける要素など）<textarea name='notes' rows='5' placeholder='例：灯台守の口調を資料どおり敬語に寄せる。手紙の伏線をもう一度置く。戦闘描写は短く。'></textarea></div>
    <button>修正要綱を含んでこの章を再生成</button></form>
    <p>再生成すると本文・Think・終了状態・要約が作り直されます。2次修正済みの章では修正済み本文（final）が消え、1次出力の状態に戻ります。入力状態（前章までの状態）は変わりません。</p>
    {job_html}</div>"""
    return page(f"第{ch['number']}章 {ch['title']}", body,
                current_id=w["id"], chapters=chapters, refresh=active)


@app.post("/chapters/{chapter_id}/regen")
def chapter_regen(chapter_id: int, notes: str = Form("")):
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "regen_chapter", {"chapter_id": chapter_id, "notes": notes})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}")
def chapter_save(chapter_id: int, title: str = Form(""), outline: str = Form("")):
    db.run("UPDATE chapters SET title=?, outline=?, status='outlined' WHERE id=?",
           (title, outline, chapter_id))
    row = db.one("SELECT work_id FROM chapters WHERE id=?", (chapter_id,))
    if row:
        pipeline.set_stage(row["work_id"], "outline_review")
    return RedirectResponse(f"/works/{row['work_id']}" if row else "/", status_code=303)


@app.get("/artifacts/{artifact_id}")
def artifact_view(artifact_id: int):
    a = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not a:
        return PlainTextResponse("not found", status_code=404)
    return page(f"{a['kind']} #{a['id']}", f"<pre>{esc(pipeline.read_text(a['path']))}</pre>")


@app.get("/works/{work_id}/download")
def work_download(work_id: int):
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    parts = [w["title"], ""]
    for ch in db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work_id,)):
        a = pipeline.latest_artifact(work_id, ch["id"], "final")
        if a:
            parts.append(f"第{ch['number']}章　{ch['title']}")
            parts.append("")
            parts.append(pipeline.read_text(a["path"]))
            parts.append("")
    body = "\n".join(parts)
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{w["slug"]}.txt"'})


# ---------- 設定画面（AnythingLLM 風：モデル/生成パラメータ/接続先） ----------

def _num_row(label, name, value, note=""):
    return (f"<div class='row'>{label}<input type='text' name='{name}' value='{esc(value)}' "
            f"style='width:10em' oninput='HCheck()'> {esc(note)}</div>")


@app.get("/settings")
def settings_form():
    cur_url = db.get_setting("strata_base_url", "") or llm.ENV_BASE_URL
    has_key = bool(db.get_setting("strata_api_key", "") or llm.ENV_API_KEY)
    loaded = llm.loaded_model()
    conn = ("接続OK（ロード中モデル：" + esc(loaded) + "）") if loaded else \
        ("モックモード" if llm.MOCK else "未接続（URL未設定、またはサーバーに届いていません）")
    def _ctx_opts(cur):
        presets = [65536, 131072, 262144, 524288]
        if cur not in presets:
            presets.append(cur)
        return "".join(
            f"<option value='{v}' {'selected' if v == cur else ''}>{v // 1024}k（{v}）</option>"
            for v in sorted(presets))

    ctx_opts = _ctx_opts(pipeline.ctx_window())
    ctx_rev_opts = _ctx_opts(pipeline.ctx_window_revise())
    body = f"""
    <script>window.HAlloc=window.HAlloc||function(){{}};window.HCheck=window.HCheck||function(){{}};window.HRev=window.HRev||function(){{}};</script>
    <form method='post' action='/settings'>
    <div class='card'><h2>接続先（推論サーバー）</h2>
    <p>状態：{conn}</p>
    <div class='row'>接続先URL（XeStrata の OpenAI 互換API）<input type='text' name='strata_base_url' value="{esc(cur_url)}" placeholder='http://192.168.1.10:8080'></div>
    <div class='row'>APIキー（{'設定済み' if has_key else '未設定'}／空欄なら変更しません。LAN 内でキー不要な場合は空のままでOK）<input type='password' name='strata_api_key' value=''></div>
    <div class='row'>2次修正モデルの目印（ロード中モデルIDに含まれる文字列。空欄なら「1次出力と違うモデルへの変更」で判定）<input type='text' name='revise_model_hint' value="{esc(db.get_setting('revise_model_hint', ''))}" placeholder='例：q4'></div>
    <div class='row'>Synology Chat Webhook URL（空欄なら通知はコンテナのログのみ）<input type='text' name='chat_webhook_url' value="{esc(db.get_setting('chat_webhook_url', ''))}"></div>
    </div>
    <div class='card'><h2>モデル / 生成パラメータ</h2>
    <p style='color:#e5484d;font-weight:bold;margin:.3em 0'>※ 値を変更しても、保存を押すまで変更されません（画面の数値はプレビューです）。</p>
    <div id='tokenOverTop' style='color:#e5484d;font-weight:bold'></div>
    <div class='row'>Context Window（トークン）<select name='ctx_window' onchange='HAlloc()' oninput='HAlloc()'>{ctx_opts}</select> プロンプトに載せる文脈量の目安に使います（実際の上限はサーバー側の最大コンテキストです）。<b>変更すると、その場で下の各 Max Tokens が比率に応じて再配置されます</b>（初期値は合計で約90%を使う配分です。手動調整した値も再配置で上書きされます）</div>
    <div class='row'>Context Window（2次修正用）<select name='ctx_window_revise' onchange='HRev()' oninput='HRev()'>{ctx_rev_opts}</select><span id='reviseAlloc' style='font-weight:bold'></span> 2次修正はモデルが変わる前提なので、大きめのモデルに合わせて最大にしておくのが既定です。モデル変更時の2次修正では、この値の<b>80%</b>を Max Tokens に自動設定します。モデルが変わらない場合（スレッドモード等）は、通常の Context Window の配分を流用します</div>
    {_num_row('Max Tokens：概要生成', 'max_tokens_outline', pipeline.max_tokens('max_tokens_outline', 'MAX_TOKENS_OUTLINE', 16384))}
    {_num_row('Max Tokens：生成指示書・文書解析', 'max_tokens_design', pipeline.max_tokens('max_tokens_design', 'MAX_TOKENS_DESIGN', 8192))}
    {_num_row('Max Tokens：本文生成（1次出力）', 'max_tokens_chapter', pipeline.max_tokens('max_tokens_chapter', 'MAX_TOKENS_CHAPTER', 32768), '思考トークンも内数です')}
    {_num_row('Max Tokens：要約', 'max_tokens_summary', pipeline.max_tokens('max_tokens_summary', 'MAX_TOKENS_SUMMARY', 4096))}
    {_num_row('Max Tokens：2次修正', 'max_tokens_revise', pipeline.max_tokens('max_tokens_revise', 'MAX_TOKENS_REVISE', 65536), '大きめのモデルに切り替える前提で、既定は余裕を持たせています。モデルの Context Window の範囲で調整してください')}
    {_num_row('Temperature', 'temperature', pipeline.temperature())}
    {_num_row('思考予算 reasoning_budget_tokens（0＝サーバー任せ）', 'reasoning_budget_tokens', llm.reasoning_budget(), 'thinking の上限の目安です。ジョブ表示のメーター（budget）にも使います')}
    <div class='row'><label><input type='checkbox' name='web_search_enabled' value='1' {'checked' if db.get_setting('web_search_enabled', '1') != '0' else ''}> Web検索を有効にする（用語・キャラクター検索。外部へ送るのは検索語のみ）</label></div>
    <div class='row'><label><input type='checkbox' name='show_fanfic_button' value='1' {'checked' if db.get_setting('show_fanfic_button', '1') != '0' else ''}> 二次創作ボタンを表示する</label></div>
    <button>保存</button>
    <div id='tokenOver' style='color:#e5484d;font-weight:bold;margin-top:.5em'></div>
    <p>保存すると即時反映されます（コンテナ再起動は不要）。モデル自体は相手先API準拠で、
    各リクエスト時点でサーバーがサーブ中のモデルが使われます。</p></div></form>
    <script>
    (function () {{
      var ctxSel = document.querySelector('select[name="ctx_window"]');
      var revSel = document.querySelector('select[name="ctx_window_revise"]');
      var names = ['max_tokens_outline', 'max_tokens_design', 'max_tokens_chapter',
                   'max_tokens_summary', 'max_tokens_revise'];
      function alloc(ctx) {{
        function v(pct, lo) {{ var n = Math.floor(ctx * pct / 256) * 256; return Math.max(lo, n); }}
        return {{
          max_tokens_outline: v(0.10, 1024), max_tokens_design: v(0.05, 1024),
          max_tokens_chapter: v(0.45, 4096), max_tokens_summary: v(0.05, 512),
          max_tokens_revise: v(0.25, 4096)
        }};
      }}
      function field(n) {{ return document.querySelector('input[name="' + n + '"]'); }}
      function check() {{
        var ctx = ctxSel ? (parseInt(ctxSel.value || '0', 10) || 0) : 0;
        var sum = 0;
        names.forEach(function (n) {{
          var el = field(n);
          sum += parseInt((el && el.value) || '0', 10) || 0;
        }});
        var msg = '';
        if (ctx && sum > ctx) {{
          msg = (sum - ctx).toLocaleString() + 'トークンオーバーです（合計 '
            + sum.toLocaleString() + ' / Context Window ' + ctx.toLocaleString() + '）';
        }}
        ['tokenOver', 'tokenOverTop'].forEach(function (id) {{
          var w = document.getElementById(id);
          if (w) w.textContent = msg;
        }});
      }}
      function applyCtx() {{
        if (!ctxSel) return;
        var a = alloc(parseInt(ctxSel.value || '0', 10) || 0);
        names.forEach(function (n) {{ var el = field(n); if (el) el.value = a[n]; }});
        check();
      }}
      function revNote() {{
        var el = document.getElementById('reviseAlloc');
        if (!el || !revSel) return;
        var rc = parseInt(revSel.value || '0', 10) || 0;
        var v = Math.max(4096, Math.floor(rc * 0.8 / 256) * 256);
        el.textContent = rc ? ' → モデル変更時の2次修正 Max Tokens: ' + v.toLocaleString() + '（この値の80%）' : '';
      }}
      if (ctxSel) {{
        ctxSel.addEventListener('change', applyCtx);
        ctxSel.addEventListener('input', applyCtx);
      }}
      if (revSel) {{
        revSel.addEventListener('change', revNote);
        revSel.addEventListener('input', revNote);
      }}
      window.HAlloc = applyCtx;
      window.HCheck = check;
      window.HRev = revNote;
      revNote();
      names.forEach(function (n) {{
        var el = field(n);
        if (el) el.addEventListener('input', check);
      }});
      check();
    }})();
    </script>
    <div class='card'><h2>データのバックアップ／復元</h2>
    <p>作品・設定・用語DBはすべてボリューム（/data と /works）に保存されるため、コンテナを再構築しても
    ボリュームを引き継げばデータはそのまま残ります。別の環境へ移す場合や、ボリュームごと作り直す場合に備えて、
    データ一式を zip でバックアップ／復元できます。</p>
    <div class='row'><a href='/backup'><button>バックアップをダウンロード（zip）</button></a></div>
    <form method='post' action='/restore' enctype='multipart/form-data'>
    <div class='row'>バックアップ zip<input type='file' name='file' accept='.zip' required></div>
    <button>バックアップから復元</button></form>
    <p>復元は DB を置き換え、生成ファイルを書き戻します（既存の追加ファイルは残ります）。
    実行中のジョブがないときに行い、復元後はコンテナの再起動を推奨します。</p></div>
    <div class='card' style='text-align:center'>
    <a href='{DOCKERHUB_URL}' target='_blank' rel='noopener' style='text-decoration:none;color:#9fd0ff'>
    {DOCKERHUB_SVG}
    <div style='font-weight:bold;margin-top:.2em'>Docker Hub</div>
    <div>yuichi894/novel-harness</div></a>
    <p>novel-harness v{APP_VERSION} ／ イメージの更新は Docker Hub から取得してください</p></div>"""
    return page("設定", body)


@app.post("/settings")
def settings_save(strata_base_url: str = Form(""), strata_api_key: str = Form(""),
                  revise_model_hint: str = Form(""), chat_webhook_url: str = Form(""),
                  ctx_window: str = Form(""), ctx_window_revise: str = Form(""),
                  max_tokens_outline: str = Form(""),
                  max_tokens_design: str = Form(""), max_tokens_chapter: str = Form(""),
                  max_tokens_summary: str = Form(""), max_tokens_revise: str = Form(""),
                  temperature: str = Form(""), reasoning_budget_tokens: str = Form(""),
                  web_search_enabled: str = Form(""),
                  show_fanfic_button: str = Form("")):
    db.set_setting("strata_base_url", strata_base_url.strip())
    if strata_api_key.strip():
        db.set_setting("strata_api_key", strata_api_key.strip())
    db.set_setting("revise_model_hint", revise_model_hint.strip())
    db.set_setting("chat_webhook_url", chat_webhook_url.strip())
    db.set_setting("web_search_enabled", "1" if web_search_enabled == "1" else "0")
    db.set_setting("show_fanfic_button", "1" if show_fanfic_button == "1" else "0")
    # Context Window が変わったら、各 Max Tokens を比率で自動再配置する。
    try:
        new_ctx = int(ctx_window) if ctx_window.strip() else None
    except ValueError:
        new_ctx = None
    if new_ctx and new_ctx != pipeline.ctx_window():
        alloc = pipeline.allocate_tokens(new_ctx)
        max_tokens_outline = str(alloc["max_tokens_outline"])
        max_tokens_design = str(alloc["max_tokens_design"])
        max_tokens_chapter = str(alloc["max_tokens_chapter"])
        max_tokens_summary = str(alloc["max_tokens_summary"])
        max_tokens_revise = str(alloc["max_tokens_revise"])
    for key, val in (("ctx_window", ctx_window), ("ctx_window_revise", ctx_window_revise),
                     ("max_tokens_outline", max_tokens_outline),
                     ("max_tokens_design", max_tokens_design), ("max_tokens_chapter", max_tokens_chapter),
                     ("max_tokens_summary", max_tokens_summary), ("max_tokens_revise", max_tokens_revise),
                     ("temperature", temperature), ("reasoning_budget_tokens", reasoning_budget_tokens)):
        val = val.strip()
        if val:
            try:
                float(val)
                db.set_setting(key, val)
            except ValueError:
                pass
    return RedirectResponse("/settings", status_code=303)


@app.get("/backup")
def backup_download():
    """データ一式（DB＋生成ファイル）を zip でダウンロードする。

    コンテナ再構築時のデータ引き継ぎ用。DB は WAL をチェックポイントしてから取り込む。
    """
    try:
        conn = sqlite3.connect(db.DB_PATH, timeout=30)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception:
        pass
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir="/tmp")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(db.DB_PATH):
            z.write(db.DB_PATH, "harness.db")
        base = pipeline.WORKS_DIR
        for root, _dirs, files in os.walk(base):
            for fn in files:
                p = os.path.join(root, fn)
                z.write(p, os.path.join("works", os.path.relpath(p, base)))
    return FileResponse(tmp, filename="novel-harness-backup.zip",
                        media_type="application/zip")


@app.post("/restore")
async def restore_upload(file: UploadFile = File(...)):
    """バックアップ zip からデータを復元する（DB 置き換え＋ファイル書き戻し）。"""
    raw = await file.read()
    buf = io.BytesIO(raw)
    if not zipfile.is_zipfile(buf):
        return PlainTextResponse("zip ファイルではありません。", status_code=400)
    buf.seek(0)
    restored_db = False
    restored_files = 0
    with zipfile.ZipFile(buf) as z:
        for info in z.infolist():
            name = info.filename
            if info.is_dir():
                continue
            if name == "harness.db":
                tmp = db.DB_PATH + ".restore"
                with open(tmp, "wb") as f:
                    f.write(z.read(name))
                os.replace(tmp, db.DB_PATH)
                for suf in ("-wal", "-shm"):
                    p = db.DB_PATH + suf
                    if os.path.exists(p):
                        os.remove(p)
                restored_db = True
            elif name.startswith("works/"):
                rel = name[len("works/"):]
                base = os.path.abspath(pipeline.WORKS_DIR)
                target = os.path.abspath(os.path.join(base, rel))
                if not target.startswith(base + os.sep):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(z.read(name))
                restored_files += 1
    if restored_db:
        db.init_db()
    body = f"""
    <div class='card'><h2>復元しました</h2>
    <p>DB：{'置き換えました' if restored_db else 'バックアップに含まれていませんでした'} ／
    生成ファイル：{restored_files} 件を書き戻しました。</p>
    <p>確実のため、コンテナの再起動を推奨します。</p>
    <p><a href='/settings'>設定に戻る</a> ／ <a href='/'>トップへ</a></p></div>"""
    return page("復元完了", body)


@app.on_event("startup")
def _startup():
    db.init_db()
    pipeline.start_worker()
