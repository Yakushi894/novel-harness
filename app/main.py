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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

import db
from datetime import datetime, timedelta, timezone
import llm
import pipeline

UI_TOKEN = os.environ.get("UI_TOKEN", "")
app = FastAPI(title="novel-harness")

APP_VERSION = "1.68"
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
    "plans_ready": "2.5 各章の展開計画作成済み",
    "generating": "3. 1次出力中",
    "body_ready": "3. 本文生成済み（仕上げ待ち）",
    "wait_q4": "4. 2次修正（モデル切替待ち）",
    "wait_model": "4. 2次修正（モデル切替待ち）",
    "revising": "4. 2次修正中",
    "done": "5. 完了（PHP・TXT化できます）",
    "failed": "エラー",
    "cancelled": "停止済み（工程を再実行できます）",
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
.muted{color:#777;font-size:.88em}
.side-foot{margin-top:1.5em;padding:.6em .4em;border-top:1px solid #2a2e36;font-size:.85em;color:#9aa3af}
.main{flex:1;padding:1.2em 1.6em;max-width:1000px}
textarea{width:100%} input[type=text]{width:100%}
.card{background:#fff;border:1px solid #dde1e6;border-radius:10px;padding:1em 1.2em;margin:1em 0;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.warn{border-color:#c33;background:#fff5f5}
pre{white-space:pre-wrap;background:#f7f7f7;padding:1em;border-radius:8px}
button{padding:.45em 1.1em;border:1px solid #2e6fdb;background:#2e6fdb;color:#fff;border-radius:8px;cursor:pointer}
button:hover{background:#255cb8}
form.act{margin-top:.9em}
form.act button{padding:.32em .95em}
form.act span{display:block;margin-top:.3em;color:#444;font-size:.92em;line-height:1.55}
.switch{position:relative;display:inline-block;width:46px;height:26px;vertical-align:middle;margin-right:.5em}
.switch input{opacity:0;width:0;height:0}
.switch .sl{position:absolute;inset:0;background:#bbb;border-radius:999px;transition:.2s;cursor:pointer}
.switch .sl:before{content:'';position:absolute;width:20px;height:20px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked + .sl{background:#2e6fdb}
.switch input:checked + .sl:before{transform:translateX(20px)}
.row{margin:.5em 0}
summary{cursor:pointer;font-weight:600}
.steps{color:#555}
h1{margin-top:.2em}
@media (max-width:820px){
  .layout{flex-direction:column;min-height:auto}
  .sidebar{width:100%;flex:none;position:static;height:auto;overflow:visible;padding:.7em .8em}
  .brand{padding-bottom:.3em}
  .side-sec{margin:.6em .4em .2em}
  .ws-item{display:inline-block;margin:.15em .2em;background:#23262d}
  .ws-item.active{background:#2e6fdb}
  .ch-item{padding-left:.6em}
  .side-foot{margin-top:.8em}
  .main{padding:.8em .7em;max-width:100%}
  .card{padding:.8em .8em;margin:.7em 0}
  h1{font-size:1.45em}
  h2{font-size:1.2em}
  input,textarea,select{font-size:16px}
  button{max-width:100%}
  pre{padding:.7em;font-size:.92em}
  .steps{font-size:.9em}
}
"""


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _fmt_tps(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "―"
    return f"{f:,.0f}" if f >= 100 else f"{f:.1f}"


PHASE_STATE_LABEL = {"prefill": "読み込み中", "gen": "生成中", "wait": "応答待ち"}


def _phase_text(work_id) -> str:
    """その作品の現在の処理フェーズ表示（例：第3章 本文を生成中・読み込み中（25秒））。"""
    label = db.get_setting(f"live:phase:{work_id}", "")
    if not label:
        return ""
    state = PHASE_STATE_LABEL.get(db.get_setting(f"live:phase_state:{work_id}", ""), "")
    elapsed = ""
    at = db.get_setting(f"live:phase_at:{work_id}", "")
    if at:
        try:
            elapsed = f"（{fmt_dur(int(time.time()) - int(float(at)))}）"
        except ValueError:
            pass
    return f"処理：{label}" + (f"・{state}" if state else "") + elapsed


def _loaded_model_html() -> str:
    """ロード中モデルの表示。推論サーバーに届かない場合は「接続が切れています」と出す。"""
    m = llm.loaded_model()
    if m is None:
        return "<span style='color:#e86a6a;font-weight:600'>接続が切れています</span>"
    if m == "":
        return "（モデル未ロード）"
    return esc(m)


def _age_text(age: int) -> str:
    if age < 120:
        return f"{age}秒前"
    if age < 3600:
        return f"{age // 60}分前"
    if age < 86400:
        return f"{age // 3600}時間前"
    return f"{age // 86400}日前"


def _sidebar_status() -> str:
    """サイドバー下部：直近の呼び出しのコンテキスト Fill と処理速度、生成中の逐次状態。
    最後の呼び出しから30分以上経ち、実行中のジョブも無い場合は、Fill と速度の
    数値（前回の残り）を現在値のように見せないため出さず、最終更新だけを出す。"""
    try:
        import time as _time
        pt = int(db.get_setting("live:prompt_tokens", "") or 0)
        ct = int(db.get_setting("live:completion_tokens", "") or 0)
        ctx = pipeline.ctx_window()
        fill = min(100, round((pt + ct) / ctx * 100)) if ctx and (pt + ct) else 0
        pre = db.get_setting("live:prefill_tps", "")
        dec = db.get_setting("live:decode_tps", "")
        html = "<div style='margin-top:.6em'>"
        run = db.one("SELECT work_id FROM jobs WHERE status='running' ORDER BY id LIMIT 1")
        if run:
            ph = _phase_text(run["work_id"])
            if ph:
                html += f"<div style='color:#7fd4a8;font-weight:600;margin-bottom:.3em'>{esc(ph)}</div>"
        last = 0.0
        for key in ("live:call_at", "live:at"):
            v = db.get_setting(key, "")
            if v:
                try:
                    last = max(last, float(v))
                except ValueError:
                    pass
        age = max(0, int(_time.time() - last)) if last else None
        stale = run is None and (age is None or age >= 1800)
        slots = llm.slots_status()
        if slots is not None and slots.get("n_ctx"):
            used = int(slots.get("n_prompt_tokens") or 0)
            total = int(slots["n_ctx"])
            live_fill = min(100, round(used / total * 100)) if total else 0
            html += (
                f"<div>コンテキスト Fill：{live_fill}%（{used:,} / {total:,} tok）</div>"
                f"<div style='background:#333;height:6px;border-radius:3px;margin:.2em 0'>"
                f"<div style='background:#e8862e;height:6px;border-radius:3px;width:{live_fill}%'></div></div>")
        elif slots is not None:
            html += "<div>コンテキスト Fill：―（モデル未ロード）</div>"
        elif not stale:
            html += (
                f"<div>コンテキスト Fill：{fill}%（{(pt + ct):,} / {ctx:,} tok）</div>"
                f"<div style='background:#333;height:6px;border-radius:3px;margin:.2em 0'>"
                f"<div style='background:#e8862e;height:6px;border-radius:3px;width:{fill}%'></div></div>")
        if not stale:
            html += (
                f"<div>GPU プリフィル：{_fmt_tps(pre)} tok/s</div>"
                f"<div>GPU デコード：{_fmt_tps(dec)} tok/s</div>")
        at = db.get_setting("live:at", "")
        if at and _time.time() - float(at) < 180 and db.get_setting("live:label", ""):
            html += (
                f"<div style='margin-top:.4em;color:#e8b34b'>● 生成中：第{esc(db.get_setting('live:chapter', ''))}章"
                f"（{esc(db.get_setting('live:label', ''))}）<br>"
                f"本文 {int(db.get_setting('live:body_chars', '') or 0):,}字 ／ "
                f"Think {int(db.get_setting('live:think_chars', '') or 0):,}字</div>")
        if age is not None:
            html += f"<div class='muted'>最終更新：{_age_text(age)}</div>"
        return html + "</div>"
    except Exception:
        return ""


_PLAN_STATE_STYLE = {
    "未生成": "#888",
    "生成中": "#b97d1a",
    "生成済み": "#1a7f37",
    "再生成中": "#2471a3",
    "再生成済み": "#1a7f37",
    "再々生成済み": "#1a7f37",
}


def _plan_sections_html(sections, active_sections, vers, plan_count):
    """展開計画の節一覧を、節ごとの状態チップ付きで描く。

    状態は節ごとに判定する：実行中ジョブの対象節（active_sections）に
    入っていれば 生成中／再生成中、その節の版数（vers＝本文が変わった
    回数）が2以上で 再生成済み、1で 生成済み、内容が無ければ 未生成。
    vers が未記録（旧データ）の章は計画全体の保存回数 plan_count で代用する。
    実行したところだけ状態が変わるよう、計画単位ではなく節単位で見る。
    """
    out = ""
    for name in pipeline.PLAN_SECTIONS:
        body = (sections or {}).get(name) or ""
        if name in (active_sections or set()):
            state = "再生成中" if body else "生成中"
        elif not body:
            state = "未生成"
        else:
            ver = (vers or {}).get(name)
            if ver is None:
                ver = plan_count
            state = "再生成済み" if int(ver or 0) >= 2 else "生成済み"
        chip = (f" <span style='color:{_PLAN_STATE_STYLE[state]};font-weight:700'>"
                f"（{state}）</span>")
        inner = (f"<pre>{esc(body)}</pre>" if body
                 else "<p class='muted'>まだ生成されていません。</p>")
        out += f"<details><summary>{esc(name)}{chip}</summary>{inner}</details>"
    return out


def _plan_active_sections(jobs, ch):
    """実行中ジョブが対象にする展開計画の節の集合（チップの 生成中／再生成中 用）。

    追加指定つき再生成（regen_plan）は作り直す節が決まっているのでその節だけ、
    展開計画の全体生成と「展開計画～終了状態まで自動再生成」は全節を対象にする。
    """
    secs = set()
    for j in jobs:
        if not _job_targets_chapter(j, ch):
            continue
        if j["kind"] == "regen_plan":
            secs |= set(pipeline.PLAN_REGEN_SECTIONS)
        elif j["kind"] in ("expand_plan_one", "expand_plans", "regen_full", "plan_design"):
            secs |= set(pipeline.PLAN_SECTIONS)
    return secs


def _short_title(t, n=10):
    """サイドバー用の章タイトル短縮：n文字を超えたら先頭n文字＋「・・・・」にする。"""
    t = t or ""
    return t[:n] + "・・・・" if len(t) > n else t


# 章ステータスの日本語ラベルと色。サイドバー（暗い背景）と本文エリア
# （明るい背景）のどちらでも読める中間色に寄せている。
CH_STATUS_JA = {
    "outlined": ("概要のみ", "#9099a3"),
    "generating": ("生成中", "#e09a26"),
    "generated": ("本文生成済み", "#35a45b"),
    "revising": ("修正中", "#3b8bd4"),
    "final": ("修正済み", "#35a45b"),
}


def _ch_status_label(status) -> str:
    return CH_STATUS_JA.get(status, (status, "#9099a3"))[0]


def _ch_status_style(status) -> str:
    color = CH_STATUS_JA.get(status, (status, "#9099a3"))[1]
    bold = "font-weight:700;" if status == "final" else ""
    return f"color:{color};{bold}"


def _ch_status_html(status) -> str:
    """章ステータスを「（日本語ラベル）」の色付き表示で返す。"""
    return (f"<span style='{_ch_status_style(status)}'>"
            f"（{esc(_ch_status_label(status))}）</span>")


def sidebar(current_id=None, chapters=None) -> str:
    try:
        works = db.q("SELECT * FROM works ORDER BY id DESC")
    except Exception:
        works = []
    items = ""
    for w in works:
        active = bool(current_id) and w["id"] == current_id
        cls = "ws-item active" if active else "ws-item"
        items += f"<a class='{cls}' href='/works/{w['id']}'>{esc(w['title'])}</a>"
        if active:
            # 選択中のワークスペースのときだけ、その下に章を出す。
            chs = chapters
            if chs is None:
                try:
                    chs = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number",
                               (w["id"],))
                except Exception:
                    chs = []
            for c in chs:
                items += (
                    f"<div class='ch-item'><a href='/chapters/{c['id']}' title='{esc(c['title'])}'>第{c['number']}章 {esc(_short_title(c['title']))}"
                    f"{_ch_status_html(c['status'])}</a>"
                    f"<form method='post' action='/chapters/{c['id']}/delete' "
                    "onsubmit=\"return confirm('この章を削除しますか？（本文・概要も消えます）')\" style='margin:0'>"
                    "<button class='ch-del' title='この章を削除'>✕</button></form></div>")
    return (
        "<div class='sidebar'>"
        "<div class='brand'>📚 小説ハーネス</div>"
        "<a class='ws-item' href='/'>＋ 新規ワークスペース</a>"
        "<div class='side-sec'>ワークスペース</div>"
        + (items or "<div class='ws-item'>まだありません</div>")
        + "<div class='side-foot'>"
        f"<div>ロード中モデル：{_loaded_model_html()}</div>"
        + f"<div id='dyn-side'>{_sidebar_status()}</div>"
        + "<div style='margin-top:.4em'><a href='/glossary'>📖 用語DB</a> ｜ <a href='/settings'>⚙ 設定</a></div>"
        "</div></div>")


POLL_SCRIPT = """
<script>
(function(){
  var url = __URL__;
  function swap(id, html){
    var el = document.getElementById(id);
    if(!el || el.innerHTML === html) return;
    var opens = [];
    var ds = el.querySelectorAll('details');
    for(var i=0;i<ds.length;i++) opens.push(ds[i].open);
    el.innerHTML = html;
    var nd = el.querySelectorAll('details');
    for(var j=0;j<nd.length && j<opens.length;j++) if(opens[j]) nd[j].open = true;
  }
  function tick(){
    fetch(url).then(function(r){ return r.json(); }).then(function(j){
      if(j.stop) return;
      if(j.reload){ location.reload(); return; }
      var nearBottom = (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 240);
      var regions = j.regions || {};
      for(var id in regions) swap(id, regions[id]);
      var texts = j.texts || {};
      for(var tid in texts){
        var e = document.getElementById(tid);
        if(e && e.textContent !== texts[tid]) e.textContent = texts[tid];
      }
      var styles = j.styles || {};
      for(var sid in styles){
        var se = document.getElementById(sid);
        if(se && se.style.cssText !== styles[sid]) se.style.cssText = styles[sid];
      }
      if(nearBottom) window.scrollTo(0, document.documentElement.scrollHeight);
      setTimeout(tick, 4000);
    }).catch(function(){ setTimeout(tick, 8000); });
  }
  setTimeout(tick, 4000);
})();
</script>
"""


FOLD_SCRIPT = """
<script>
// 本文（1次出力）の折りたたみ状態を WEB セッション内で覚える：
// 一度折りたたんだら、ほかの章でも折りたたんだ状態で始まる（開き直したらそちらを覚える）。
(function(){
  var KEY = 'nh:fold:draft';
  var d = document.querySelector("details[data-fold='draft']");
  if(!d) return;
  try{
    var v = sessionStorage.getItem(KEY);
    if(v === 'closed') d.removeAttribute('open');
    else if(v === 'open') d.setAttribute('open', '');
  }catch(e){}
  d.addEventListener('toggle', function(){
    try{ sessionStorage.setItem(KEY, d.open ? 'open' : 'closed'); }catch(e){}
  });
})();
</script>
"""


SIDEBAR_SCRIPT = """
<script>
(function(){
  function tick(){
    fetch('/sidebar.json').then(function(r){ return r.json(); }).then(function(j){
      var el = document.getElementById('dyn-side');
      var html = (j.regions || {})['dyn-side'];
      if(el && html && el.innerHTML !== html) el.innerHTML = html;
      setTimeout(tick, 10000);
    }).catch(function(){ setTimeout(tick, 15000); });
  }
  setTimeout(tick, 10000);
})();
</script>
"""


def page(title: str, body: str, current_id=None, chapters=None, refresh: bool = False,
         live_url: str = None) -> HTMLResponse:
    if live_url:
        auto = POLL_SCRIPT.replace("__URL__", json.dumps(live_url))
    elif refresh:
        auto = "<script>setTimeout(function(){location.reload()},5000)</script>"
    else:
        # 逐次更新のないページでも、サイドバーの状態（処理・Fill・GPU・最終更新）だけは
        # 約10秒間隔で自動更新する。
        auto = SIDEBAR_SCRIPT
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
    <label><input type='radio' name='mode' value='thread'> スレッドモード（モデル切替なし・章ごとに別スレッドで完結）</label><br>
    <label><input type='radio' name='mode' value='single'> 単章モード（1章完結・指示を出しながら対話的に詰める）</label></div>
    <div class='row'><label><input type='checkbox' name='continuation' value='1'> 続きを書く（長編向け）</label></div>
    <div class='row' style='color:#c0392b;font-weight:700'>※「続きを書く」の場合、読み込んだ文書は「第0章」として読み込まれます。新しい本文の生成は第1章から始まります。</div>
    <button>作成</button></form></div>"""
    steps = "<p class='steps'>工程：" + " ⇒ ".join(FLOW_STEPS) + "</p>"
    return page("小説ハーネス", setup_banner() + steps + items + form + DROP_SCRIPT_INDEX)


@app.post("/works")
async def create_work(title: str = Form(...), request: str = Form(""), settings: str = Form(""),
                      num_chapters: int = Form(3), mode: str = Form("switch"),
                      min_chars: int = Form(0), continuation: int = Form(0),
                      source_file: UploadFile = File(None)):
    base = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower() or "work"
    if mode == "single":
        num_chapters = 1
    wid = db.run(
        "INSERT INTO works(title, slug, request, settings, num_chapters, mode, min_chars, continuation, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (title, f"{base}-tmp", request, settings, num_chapters,
         mode if mode in ("switch", "thread", "single") else "switch",
         min_chars if min_chars in MIN_CHARS_CHOICES else 0, 1 if continuation else 0,
         db.now(), db.now()))
    # slug は一意制約があるため、採番済みの id を含めて確定させる
    # （日本語タイトル同士を同秒に作ると base と時刻だけでは衝突する）。
    slug = f"{base}-{int(time.time()) % 1000000}-{wid}"
    db.run("UPDATE works SET slug=? WHERE id=?", (slug, wid))
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


def _job_payload(j) -> dict:
    try:
        return json.loads(j["payload"] or "{}")
    except (ValueError, TypeError):
        return {}


def _job_scope(j, by_id) -> str:
    """ジョブの対象範囲の説明（第○章／第○〜○章／第○章以降／全章）。履歴表示用。"""
    payload = _job_payload(j)
    cid = payload.get("chapter_id")
    if cid and cid in by_id:
        return f"第{by_id[cid]['number']}章"
    if j["kind"] == "range_generate":
        lo, hi = payload.get("from"), payload.get("to")
        if lo and hi:
            return f"第{lo}〜{hi}章" + ("・作り直し" if payload.get("redo") else "")
    if j["kind"] == "expand_plans" and payload.get("from_number"):
        return f"第{payload['from_number']}章以降"
    if j["kind"] in ("generate_chapters", "generate_thread", "revise_all",
                     "expand_plans", "outline"):
        return "全章"
    return ""


def _job_targets_chapter(j, ch) -> bool:
    """そのジョブがその章を処理対象に含むか（再生成表記の判定用）。"""
    payload = _job_payload(j)
    if payload.get("chapter_id") == ch["id"]:
        return True
    if j["kind"] == "range_generate":
        return int(payload.get("from") or 0) <= ch["number"] <= int(payload.get("to") or 0)
    if j["kind"] == "expand_plans":
        return ch["number"] >= max(int(payload.get("from_number") or 1), 1)
    if j["kind"] in ("generate_chapters", "generate_thread", "revise_all"):
        return ch["number"] >= 1
    return False


def _jobs_html(w, jobs) -> str:
    """作品ページのジョブ一覧フラグメント（ページ本体と live.json で共用）。"""
    now_ts = time.time()
    by_id = {c["id"]: c for c in db.q("SELECT id, number FROM chapters WHERE work_id=?", (w["id"],))}
    rows = ""
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
        scope = _job_scope(j, by_id)
        rows += (
            f"<div>#{j['id']} {esc(JOB_LABELS.get(j['kind'], j['kind']))}"
            f"{('（' + scope + '）') if scope else ''} ― {status_html(j['status'])} {dur} {esc(j['error'])}"
            + (f" <form method='post' action='/jobs/{j['id']}/cancel' style='display:inline'><button>取消</button></form>"
               if j["status"] in ("queued", "running") else "") + "</div>"
            + usage_html(j))
    return rows or "<p>なし</p>"


JOB_LABELS = {"analyze": "初期文書解析", "outline": "各章概要の生成",
              "expand_plans": "各章を展開", "expand_plan_one": "展開計画の生成（この章のみ）",
              "plan_design": "展開計画・生成指示書の再生成（この章のみ）",
              "regen_plan": "展開計画の再生成", "generate_chapters": "本文生成（1次出力）",
              "generate_thread": "章ごとスレッド生成", "revise_all": "2次修正",
              "regen_chapter": "本文の再生成", "regen_state": "終了状態の再生成",
              "regen_full": "展開計画～終了状態まで自動再生成",
              "revise_outline": "修正概要の作成",
              "revise_more": "修正概要への追記＋本文の再生成",
              "revise_append": "修正概要への追記（概要のみ）",
              "outline_chapter": "概要の再生成", "search_term": "キャラクター検索",
              "term_check": "用語チェック", "final_check": "最終チェック",
              "range_generate": "範囲指定の生成（概要＋本文）"}


def _peek_text(work, ch):
    """止まった場所のちら見せ用テキスト（生成途中→異常終了ログ→保存済み本文の順）。

    戻り値は (ラベル, 本文, 異常終了ログの成果物ID or None)。
    """
    if ch is None:
        return "", "", None
    body, _t = pipeline.read_partial(work, ch)
    if body:
        return "生成途中の本文（末尾）", body, None
    log = pipeline.latest_artifact(work["id"], ch["id"], "log")
    if log:
        return "異常終了時用の確認出力（末尾）", pipeline.read_text(log["path"]), log["id"]
    art = (pipeline.latest_artifact(work["id"], ch["id"], "final")
           or pipeline.latest_artifact(work["id"], ch["id"], "draft"))
    if art:
        return "保存済みの本文（末尾）", pipeline.read_text(art["path"]), None
    return "", "", None


def _work_progress_text(w, chapters) -> str:
    """現在の進み具合を1行で表す（概要・本文・2次修正がどこまで作成済みか）。

    章番号が1から連続していれば「第X章まで」、欠けがあれば「Y章分」と
    数え方を変え、実態と違う「まで」表記にならないようにする。第0章
    （読み込んだ文書）は生成対象ではないので数に入れない。
    """
    gen = [c for c in chapters if c["number"] >= 1]
    if not gen:
        return "現在：章がまだありません。"

    def upto(nums):
        s = set(nums)
        if not s:
            return "未作成"
        m = 0
        while m + 1 in s:
            m += 1
        if s == set(range(1, m + 1)):
            return f"第{m}章まで作成済み"
        return f"{len(s)}章分作成済み"

    outlines = [c["number"] for c in gen if (c["outline"] or "").strip()]
    drafts, finals = [], []
    for c in gen:
        if pipeline.latest_artifact(w["id"], c["id"], "final"):
            finals.append(c["number"])
            drafts.append(c["number"])
        elif pipeline.latest_artifact(w["id"], c["id"], "draft"):
            drafts.append(c["number"])
    return (f"現在：概要 {upto(outlines)}・本文 {upto(drafts)}・"
            f"2次修正 {upto(finals)}（全{len(gen)}章）")


def _current_work_html(w) -> str:
    """「現在の作業」パネル：実行中／停止した工程と、本文の途中経過をちら見せする。"""
    jobs = db.q("SELECT * FROM jobs WHERE work_id=? ORDER BY id DESC LIMIT 5", (w["id"],))
    if not jobs:
        return "<p>まだ工程は実行されていません。</p>"
    chapters = db.q("SELECT * FROM chapters WHERE work_id=?", (w["id"],))

    def job_chapter(j):
        try:
            payload = json.loads(j.get("payload") or "{}")
        except ValueError:
            payload = {}
        cid = payload.get("chapter_id")
        if cid:
            return next((c for c in chapters if c["id"] == cid), None)
        if db.get_setting("live:work_id", "") == str(w["id"]):
            num = db.get_setting("live:chapter", "")
            if num.isdigit():
                return next((c for c in chapters if c["number"] == int(num)), None)
        return None

    def peek_html(j, ch):
        label, text, log_id = _peek_text(w, ch)
        if not text:
            return ""
        tail = text[-1200:]
        note = "（末尾1,200字を表示）" if len(text) > 1200 else ""
        del_btn = ""
        if log_id:
            del_btn = (f" <form method='post' action='/artifacts/{log_id}/delete' style='display:inline'>"
                       "<button>この異常終了ログを削除</button></form>")
        return (f"<p style='margin:.6em 0 .3em'>{esc(label)}{note}{del_btn}</p>"
                f"<pre style='max-height:15em;overflow:auto'>{esc(tail)}</pre>")

    active = next((j for j in jobs if j["status"] in ("queued", "running")), None)
    j = active or jobs[0]
    ch = job_chapter(j)
    label = JOB_LABELS.get(j["kind"], j["kind"])
    if j["kind"] == "range_generate":
        scope = _job_scope(j, {c["id"]: c for c in chapters})
        if scope:
            label += f"（{scope}）"
    ch_note = f"第{ch['number']}章" if ch else ""
    if active:
        if active["status"] == "queued":
            head = f"<p>待機中：{esc(label)}{('（' + ch_note + '）') if ch_note else ''} の開始待ちです。</p>"
        else:
            dur = fmt_dur(time.time() - j["created_at"])
            live_label = db.get_setting("live:label", "") if db.get_setting("live:work_id", "") == str(w["id"]) else ""
            head = (f"<p><b style='color:#b97d1a'>● 作業中</b>：{esc(label)}"
                    f"{('（' + ch_note + ('・' + esc(live_label) if live_label else '') + '）') if ch_note else ''}"
                    f" ― 開始から {dur}</p>")
            ph = _phase_text(w["id"])
            if ph:
                head += f"<p style='color:#2e7d32'><b>いま：{esc(ph)}</b></p>"
        return head + peek_html(j, ch)
    # 実行中でないときは、直近ジョブの成否を作品の状態として出し続けず、
    # 「現在の進み具合」＋「最後に実行した結果」を出す（1回のエラーで
    # 作品がエラーのままに見える、というユーザー指摘への対応）。
    prog = _work_progress_text(w, chapters)
    when = _fmt_ts(j["updated_at"])
    if j["status"] == "done":
        return (f"<p>{prog}</p>"
                f"<p class='muted'>最後に実行した結果：「{esc(label)}」は完了しています"
                f"{('（' + when + '）') if when else ''}。</p>")
    if j["status"] == "cancelled":
        head = (f"<p>{prog}</p>"
                f"<p class='muted'>最後に実行した結果：「{esc(label)}」"
                f"{('（' + ch_note + '）') if ch_note else ''}は停止ボタンで中断されました"
                f"{('（' + when + '）') if when else ''}。止まった場所は下の本文で確認できます。</p>")
        return head + peek_html(j, ch)
    head = (f"<p>{prog}</p>"
            f"<p class='muted'>最後に実行した結果：「{esc(label)}」"
            f"{('（' + ch_note + '）') if ch_note else ''}はエラー終了しました"
            f"{('（' + when + '）') if when else ''}。"
            f"<br>{esc((j['error'] or '')[:200])}</p>")
    return head + peek_html(j, ch)


def _work_head_html(w) -> str:
    """作品ページ冒頭の状態・モデル表示フラグメント。"""
    draft_model = db.get_setting(f"work:{w['id']}:draft_model", "")
    revise_model = db.get_setting(f"work:{w['id']}:revise_model", "")
    return (f"<p>状態：{esc(STAGE_LABEL.get(w['stage'], w['stage']))}</p>"
            f"<p class='steps'>工程：{' ⇒ '.join(FLOW_STEPS)}</p>"
            f"<p>ロード中モデル：{_loaded_model_html()}"
            f" ／ 1次出力モデル：{esc(draft_model) or '―'} ／ 2次修正モデル：{esc(revise_model) or '―'}</p>")


def _single_gen_html(w, sch) -> str:
    """単章パネルの生成欄フラグメント（ページ本体と live.json で共用）。"""
    if sch is None:
        return "<p>まだ生成されていません。上の指示を送ると、概要作成から生成まで一括で進みます。</p>"
    s_final = pipeline.latest_artifact(w["id"], sch["id"], "final")
    s_draft = pipeline.latest_artifact(w["id"], sch["id"], "draft")
    s_body_art = s_final or s_draft
    s_think = pipeline.latest_artifact(w["id"], sch["id"], "think")
    sp_body, sp_think = pipeline.read_partial(w, sch)
    if sp_body is not None and sch["status"] in ("generating", "revising"):
        gen_inner = (f"<p>生成中（逐次表示）：本文 {len(sp_body):,}字 ／ Think {len(sp_think or ''):,}字</p>"
                     f"<pre>{esc(sp_body)}</pre>")
        if sp_think:
            gen_inner += (f"<details><summary>Think（生成中・逐次）</summary>"
                          f"<pre>{esc(sp_think)}</pre></details>")
        return gen_inner
    if s_body_art:
        gen_inner = f"<pre>{esc(pipeline.read_text(s_body_art['path']))}</pre>"
        if s_think:
            gen_inner += (f"<details><summary>Think（思考過程）</summary>"
                          f"<pre>{esc(pipeline.read_text(s_think['path']))}</pre></details>")
        return gen_inner
    return "<p>まだ生成されていません。上の指示を送ると生成が始まります。</p>"


def _chapter_live_html(w, ch) -> str:
    """章ページの生成中（逐次表示）カード。生成中でなければ空文字。"""
    partial_body, partial_think = pipeline.read_partial(w, ch)
    if partial_body is None or ch["status"] not in ("generating", "revising"):
        return ""
    live_think = (f"<details><summary>Think（生成中・逐次）</summary><pre>{esc(partial_think)}</pre></details>"
                  if partial_think else "")
    regen = (pipeline.latest_artifact(w["id"], ch["id"], "final")
             or pipeline.latest_artifact(w["id"], ch["id"], "draft"))
    return (f"<div class='card' style='border:2px solid #e8b34b'><h2>生成中（逐次表示）{'※再生成' if regen else ''}</h2>"
            f"<p>本文 {len(partial_body):,}字 ／ Think {len(partial_think or ''):,}字"
            "（このページは自動更新されます。ブラウザを閉じても生成は続きます）</p>"
            f"<pre>{esc(partial_body)}</pre>{live_think}</div>")


CHAPTER_JOB_LABELS = {"regen_chapter": "本文の再生成", "regen_state": "終了状態の再生成",
                      "regen_full": "展開計画～終了状態まで自動再生成",
                      "revise_outline": "修正概要の作成",
                      "revise_more": "修正概要への追記＋本文の再生成",
                      "revise_append": "修正概要への追記（概要のみ）",
                      "expand_plan_one": "展開計画の生成（この章のみ）",
                      "plan_design": "展開計画・生成指示書の再生成（この章のみ）",
                      "outline_chapter": "概要の再生成"}
CHAPTER_JOB_KINDS = tuple(CHAPTER_JOB_LABELS)


def _chapter_jobs_html(jobs, by_id=None) -> str:
    """章ページの再生成系ジョブ一覧フラグメント。対象章が分かるよう範囲も出す。"""
    by_id = by_id or {}
    out = ""
    for j in jobs:
        scope = _job_scope(j, by_id)
        out += (f"<div>#{j['id']} {CHAPTER_JOB_LABELS.get(j['kind'], j['kind'])}"
                f"{('（' + scope + '）') if scope else ''} ― "
                f"{status_html(j['status'])} {esc(j['error'])}</div>" + usage_html(j))
    return out


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
        analysis_html = (f"<details class='card'><summary>初期文書解析の結果</summary>"
                         f"<pre>{esc(pipeline.read_text(analysis['path']))}</pre>"
                         "<p>この解析結果は、以降の生成（概要・本文・修正）の文脈に自動で含まれます。</p></details>")

    state_now = pipeline.current_state(w)
    state_html = ""
    if state_now:
        state_html = (f"<details class='card'><summary>現在の状態（状態チェーン）</summary>"
                      f"<pre>{esc(state_now)}</pre>"
                      "<p>各章は開始時点の状態を入力として受け取り、終了状態を次章へ渡します。"
                      "章ごとの入出力状態は各章ページで確認できます。</p></details>")

    min_chars = int(w.get("min_chars") or 0)
    plan_pending = db.get_setting(f"plan_pending:{work_id}", "")
    max_ch_number = max((c["number"] for c in chapters), default=0)
    active_jobs_w = db.q("SELECT * FROM jobs WHERE work_id=? AND status IN ('queued','running')",
                         (work_id,))
    ch_html = ""
    for ch in chapters:
        chars = pipeline.chapter_chars(work_id, ch["id"])
        chars_note = (f"<div class='row'>本文：{chars}文字"
                      + (f"（最低{min_chars}文字）" if min_chars > 0 else "") + "</div>") if chars else ""
        plan_secs = pipeline.get_plan(w, ch)
        p_count = db.one("SELECT COUNT(*) AS c FROM artifacts WHERE work_id=? AND chapter_id=? "
                         "AND kind='plan'", (w["id"], ch["id"]))["c"]
        sec_html = _plan_sections_html(plan_secs, _plan_active_sections(active_jobs_w, ch),
                                       pipeline.plan_section_versions(ch["id"]), p_count)
        plan_block = (f"<details><summary>展開計画（詳細概要・開始/終了状態・内容詳細）</summary>"
                      f"{sec_html}</details>")
        if plan_secs:
            cascade_btn = ""
            if plan_pending == str(ch["number"]) and ch["number"] < max_ch_number:
                cascade_btn = (f"<form method='post' action='/works/{w['id']}/plan_cascade' style='margin-top:.5em'>"
                               f"<input type='hidden' name='chapter_id' value='{ch['id']}'>"
                               f"<button style='background:#2e7d32;border-color:#2e7d32'>OK：第{ch['number'] + 1}章以降を再生成（開始状態の引き継ぎを反映）</button></form>")
            plan_block = f"""
        <form method='post' action='/chapters/{ch['id']}/plan_regen'>
        <div class='row'>展開計画への追加指定（内容詳細・終了状態・次章開始状態を作り直します）<textarea name='notes' rows='2'></textarea></div>
        <button>追加指定を考慮して再生成</button></form>{cascade_btn}
        """ + plan_block
        design_art = pipeline.latest_artifact(w["id"], ch["id"], "prompt")
        design_block = ""
        if design_art:
            design_block = (f"<details><summary>生成指示書</summary>"
                            f"<pre>{esc(pipeline.read_text(design_art['path']))}</pre></details>")
        ch_html += f"""<div class='card' id='ch-{ch['number']}'><h3 title='{esc(ch['title'])}'>第{ch['number']}章 {esc(_short_title(ch['title']))}（<span id='ch-status-{ch['number']}' style='{_ch_status_style(ch['status'])}'>{esc(_ch_status_label(ch['status']))}</span>）</h3>
        {chars_note}
        <form method='post' action='/chapters/{ch['id']}'>
        <div class='row'>タイトル<input type='text' name='title' value="{esc(ch['title'])}"></div>
        <div class='row'>概要<textarea name='outline' rows='4'>{esc(ch['outline'])}</textarea></div>
        <button>概要を保存（承認）</button></form>
        <form method='post' action='/chapters/{ch['id']}/outline_regen' style='margin-top:.5em'>
        <button>この章の概要だけ再生成</button></form>
        <form method='post' action='/chapters/{ch['id']}/expand_plan' style='margin-top:.5em'>
        <button>この章の展開計画のみ生成</button></form>
        <form method='post' action='/chapters/{ch['id']}/expand_plan_design' style='margin-top:.5em'>
        <button>この章の展開計画・生成指示書再生成</button></form>{plan_block}{design_block}
        <div class='row'><a href='/chapters/{ch['id']}'>章ページを開く（本文・Think・状態・修正要綱で再生成）</a></div>
        <form method='post' action='/chapters/{ch['id']}/delete' onsubmit="return confirm('この章を削除しますか？（本文・概要も消えます）')" style='margin-top:.5em'>
        <button style='background:#b03a3a;border-color:#b03a3a'>この章を削除</button></form></div>"""

    # 工程の「✔ 終了済み」バッジ：直近ジョブの完了、または成果物の揃い具合で判定
    # 直近ジョブが失敗した工程は、成果物が揃っていても「✖ エラー終了」を優先表示する
    _seen_kind = set()
    _job_done = set()
    _failed_ids = {}
    _latest_done_id = 0
    for j in db.q("SELECT id, kind, status FROM jobs WHERE work_id=? ORDER BY id DESC", (work_id,)):
        if j["status"] == "done" and not _latest_done_id:
            _latest_done_id = j["id"]
        if j["kind"] in _seen_kind:
            continue
        _seen_kind.add(j["kind"])
        if j["status"] == "done":
            _job_done.add(j["kind"])
        elif j["status"] == "failed":
            _failed_ids[j["kind"]] = j["id"]
    # エラー終了バッジは「その失敗より後に成功したジョブが1つも無い」場合だけ。
    # 別の工程でも成功があれば復旧済みとみなし、バッジは成果物の状態に戻す
    # （1回のエラーで工程がエラーのままに見える、というユーザー指摘への対応）。
    _job_failed = {k for k, fid in _failed_ids.items() if fid > _latest_done_id}
    _all_ch = {c["id"] for c in chapters}
    _plan_ch = {a["chapter_id"] for a in arts if a["kind"] == "plan"}
    _body_ch = {a["chapter_id"] for a in arts if a["kind"] in ("draft", "final")}
    _final_ch = {a["chapter_id"] for a in arts if a["kind"] == "final"}

    def _badge(done):
        return " <span style='color:#1a7f37;font-weight:700'>✔ 終了済み</span>" if done else ""

    def _badge_f(done, kind):
        if kind in _job_failed:
            return " <span style='color:#b03a3a;font-weight:700'>✖ エラー終了</span>"
        return _badge(done)

    badge_outline = _badge_f(bool(chapters) or "outline" in _job_done, "outline")
    badge_expand = _badge_f((bool(_all_ch) and _all_ch <= _plan_ch) or "expand_plans" in _job_done, "expand_plans")
    badge_gen = _badge_f((bool(_all_ch) and _all_ch <= _body_ch) or "generate_chapters" in _job_done, "generate_chapters")
    badge_rev = _badge_f((bool(_all_ch) and _all_ch <= _final_ch) or "revise_all" in _job_done, "revise_all")
    badge_thread = _badge_f((bool(_all_ch) and _all_ch <= _final_ch) or "generate_thread" in _job_done, "generate_thread")
    badge_rev_thread = _badge_f("revise_all" in _job_done, "revise_all")
    badge_check = _badge_f("final_check" in _job_done or any(a["kind"] == "check" for a in arts), "final_check")

    switch_buttons = (
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='generate_chapters'><button>3. 1次出力（本文生成・現在ロード中のモデル）</button>{badge_gen}</form>"
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='revise_all'><button>4. 2次修正（モデル切替を検知して自動実行）</button>{badge_rev}</form>")
    thread_buttons = (
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='generate_thread'><button>3. 本文生成（推敲なし・モデル切替なし）</button>{badge_thread}</form>"
        f"<form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='revise_all'><button>4. 仕上げ修正（監修）のみ（生成済みの章を同モデルで修正）</button>{badge_rev_thread}</form>")

    single_html = ""
    if (w.get("mode") or "switch") == "single":
        sch = chapters[0] if chapters else None
        src_names = "".join(
            f"<div>📄 {esc(os.path.basename(a['path']))}</div>"
            for a in arts if a["kind"] == "source")
        gen_inner = _single_gen_html(w, sch)
        single_html = f"""
    <div class='card'><h2>単章モード</h2>
    <h3>指示</h3>
    <form method='post' action='/works/{w['id']}/single_act' enctype='multipart/form-data'>
    <div class='row'><textarea name='notes' rows='4' placeholder='書きたい内容・直したい点など'></textarea></div>
    <div class='row'>TXT添付（任意・素材として生成に反映されます）<input type='file' name='file' accept='.txt,.md,.text,text/plain'></div>
    {('<p>添付済み：</p>' + src_names) if src_names else ''}
    <button>生成する</button></form>
    <h3>生成欄</h3>
    <div id='dyn-single'>{gen_inner}</div>
    </div>"""

    range_html = ""
    if (w.get("mode") or "switch") != "single":
        _cont = pipeline.is_continuation(w)
        _def = w["num_chapters"] or 3
        _gen_nums = [c["number"] for c in chapters if c["status"] == "generated"]
        _def_from = (max(_gen_nums) + 1) if _gen_nums else 1
        if _def_from > _def:
            _def_from = _def
        _first_label = ("本文解析＋第0章（読み込んだ文書）終了時の状態解析をして、第1章" if _cont else "第1章")
        range_html = f"""
    <div class='card'><h2>長編向け：範囲指定で生成（概要＋本文・推敲なし）</h2>
    <p>概要を1章ずつ作るので、10章を超える長編でも一括生成の失敗が起きにくい作りです。本文は1次出力（推敲なし）までで止まります。推敲（2次修正／仕上げ）は上の工程ボタンを別に押してください。</p>
    <form method='post' action='/works/{w['id']}/range'>
    <div class='row'>前半：{_first_label}〜第<input type='text' name='x' value='{_def}' style='width:4em'>章までの概要・本文を作成
    <button name='range_mode' value='first'>前半を作成</button></div>
    <div class='row'>後半：第<input type='text' name='from_ch' value='{_def_from}' style='width:4em'>章 〜 第<input type='text' name='to_ch' value='{_def}' style='width:4em'>章
    <button name='range_mode' value='cont'>続きを作成（未生成の章だけ）</button>
    <button name='range_mode' value='redo'>作り直し（生成済みも含む）</button></div>
    </form>
    <p>後半は、作成したい範囲を「第○章 〜 第○章」で直接指定します。実行には、開始章の前章の終了状態（キャラ表含む状態チェーン）が必要です。ない場合はお知らせに表示して実行しません。作り直しでは対象章の概要と本文を作り直します（旧本文は前版として自動で残ります）。{'続き作品では、開始を第1章にすると第0章（読み込んだ文書）の続きとして作成します。' if _cont else ''}</p></div>"""

    notices = db.q("SELECT * FROM notices WHERE work_id=? ORDER BY id DESC LIMIT 20", (work_id,))
    notice_html = ""
    if notices:
        notice_html = ("<h3 style='margin-top:1.2em'>お知らせ（用語DB）</h3>" + "".join(
            f"<div>{esc(n['body'])}</div>" for n in notices))

    job_html = _jobs_html(w, jobs)
    art_html = "".join(
        f"<div><a href='/artifacts/{a['id']}'>"
        + ("log（異常終了ログ）" if a["kind"] == "log" else esc(a["kind"]))
        + f"</a> {esc(a['path'])} <span class='muted'>作成：{_fmt_ts(a['created_at'])}</span></div>"
        for a in arts)
    source_html = "".join(
        f"<div>📄 <a href='/artifacts/{a['id']}'>{esc(os.path.basename(a['path']))}</a>"
        f" <span class='muted'>作成：{_fmt_ts(a['created_at'])}</span></div>"
        for a in arts if a["kind"] == "source")
    if source_html:
        source_html = "<p>アップロード済みファイル：</p>" + source_html

    body = f"""
    <div id='dyn-head'>{_work_head_html(w)}</div>
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
    <div class='card'><h2>現在の作業・お知らせ</h2><div id='dyn-current'>{_current_work_html(w)}</div>{notice_html}</div>
    <div class='card'><h2>工程の実行</h2>
    <p>モード：<b>{'単章モード（1章完結・対話的に詰める）' if (w.get('mode') or 'switch') == 'single' else ('スレッドモード（モデル切替なし・章ごとスレッド）' if (w.get('mode') or 'switch') == 'thread' else '切替モード（1次出力→モデル切替→2次修正）')}</b>（作品情報で変更できます）</p>
    {ws_html}
    <form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='outline'><button>2. 次章の内容①：各章概要を生成</button>{badge_outline}</form>
    <form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='expand_plans'><button>2. 次章の内容②：各章を展開（展開計画：詳細概要・開始/終了状態・内容詳細を章ごとに連鎖生成）</button>{badge_expand}</form>
    {thread_buttons if (w.get('mode') or 'switch') in ('thread', 'single') else switch_buttons}
    {single_html}
    <p><a href='/works/{w['id']}/download'>5. まとめテキストをダウンロード（TXT化・final）</a></p>
    <form method='post' action='/works/{w['id']}/jobs'><input type='hidden' name='kind' value='final_check'><button>6. 最終チェック（全章横断：文字数・繰り返し・登場・矛盾）</button>{badge_check}</form></div>
    {range_html}
    <h2>章と概要</h2>
    <form method='post' action='/works/{w['id']}/add_chapter' class='act'>
    第<input type='text' name='number' value='{(max((c['number'] for c in chapters), default=0) + 1)}' style='width:4em'>章を追加する
    <button>章を追加</button>
    <span>空の章（章BOX）を追加します。概要は章ページで直接書くか、概要の再生成・範囲指定の生成で作れます。</span></form>
    {ch_html or '<p>まだ概要がありません。</p>'}
    <div class='card'><h2>ジョブ</h2><div id='dyn-jobs'>{job_html}</div></div>
    <div class='card'><h2>成果物</h2>{art_html or '<p>なし</p>'}</div>
    <div class='card'><h2>作品情報</h2>
    <form method='post' action='/works/{w['id']}/meta'>
    <div class='row'>欲しい内容<textarea name='request' rows='4'>{esc(w['request'])}</textarea></div>
    <div class='row'>設定資料<textarea name='settings' rows='4'>{esc(w['settings'])}</textarea></div>
    <div class='row'>章数<input type='text' name='num_chapters' value='{w['num_chapters']}' style='width:4em'></div>
    <div class='row'>章の最低文字数<select name='min_chars'>{min_chars_options(w.get('min_chars'))}</select>（届かない場合は自動で続きを書き足します）</div>
    <div class='row'>モード：
    <label><input type='radio' name='mode' value='switch' {'checked' if (w.get('mode') or 'switch') == 'switch' else ''}> 切替モード</label>
    <label><input type='radio' name='mode' value='thread' {'checked' if (w.get('mode') or 'switch') == 'thread' else ''}> スレッドモード（切替なし）</label>
    <label><input type='radio' name='mode' value='single' {'checked' if (w.get('mode') or 'switch') == 'single' else ''}> 単章モード（1章完結）</label></div>
    <div class='row'><label><input type='checkbox' name='continuation' value='1' {'checked' if pipeline.is_continuation(w) else ''}> 続きを書く（長編向け）</label></div>
    <div class='row' style='color:#c0392b;font-weight:700'>※「続きを書く」の場合、読み込んだ文書は「第0章」として読み込まれます。新しい本文の生成は第1章から始まります。</div>
    <button>保存</button></form></div>
    <div class='card'><h2>ワークスペースの削除</h2>
    <form method='post' action='/works/{w['id']}/delete' onsubmit="return confirm('このワークスペースを削除しますか？（作品データが消えます。用語DBは残ります）')">
    <button style='background:#b03a3a;border-color:#b03a3a'>このワークスペースを削除</button></form>
    <p>作品データは消えますが、<b>用語DBに覚えた単語の意味は残ります</b>（次回から再検索せず使えます）。</p></div>"""
    active = any(j["status"] in ("queued", "running") for j in jobs)
    return page(w["title"], body + DROP_SCRIPT_WORK.replace("__WID__", str(work_id)),
                current_id=work_id, chapters=chapters,
                live_url=f"/works/{work_id}/live.json" if active else None)


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


@app.post("/artifacts/{artifact_id}/restore")
def artifact_restore(artifact_id: int):
    """前版バックアップ（kind='backup'）を本文（draft/final）へ復元する。

    復元先の現在の本文も save_artifact 側で自動バックアップされるため、
    復元自体も取り消せる。
    """
    a = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not a or a["kind"] != "backup" or not a["chapter_id"]:
        return RedirectResponse("/", status_code=303)
    m = re.match(r"ch(\d+)_(draft|final)_", os.path.basename(a["path"]))
    if not m:
        return RedirectResponse(f"/artifacts/{artifact_id}", status_code=303)
    target = m.group(2)
    ch = db.one("SELECT * FROM chapters WHERE id=?", (a["chapter_id"],))
    w = db.one("SELECT * FROM works WHERE id=?", (a["work_id"],))
    if not ch or not w:
        return RedirectResponse("/", status_code=303)
    pipeline.save_artifact(w, ch, target, f"ch{ch['number']:03d}.txt",
                           pipeline.read_text(a["path"]))
    if target == "final":
        db.run("UPDATE chapters SET status='final' WHERE id=?", (ch["id"],))
    elif not pipeline.latest_artifact(w["id"], ch["id"], "final"):
        db.run("UPDATE chapters SET status='generated' WHERE id=?", (ch["id"],))
    return RedirectResponse(f"/chapters/{ch['id']}", status_code=303)


@app.post("/works/{work_id}/meta")
def work_meta(work_id: int, request: str = Form(""), settings: str = Form(""),
              num_chapters: int = Form(3), mode: str = Form("switch"), min_chars: int = Form(0),
              continuation: int = Form(0)):
    db.run("UPDATE works SET request=?, settings=?, num_chapters=?, mode=?, min_chars=?, continuation=?, updated_at=? WHERE id=?",
           (request, settings, 1 if mode == "single" else num_chapters,
            mode if mode in ("switch", "thread", "single") else "switch",
            min_chars if min_chars in MIN_CHARS_CHOICES else 0, 1 if continuation else 0,
            db.now(), work_id))
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


@app.post("/works/{work_id}/range")
def work_range(work_id: int, range_mode: str = Form(...),
               x: int = Form(0), from_ch: int = Form(0), to_ch: int = Form(0)):
    """長編向け範囲生成の入口。前提（文書・前章終了状態）が無ければ
    お知らせに「ありません」と出して実行しない。"""
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w:
        return PlainTextResponse("not found", status_code=404)
    cont = pipeline.is_continuation(w)

    def back():
        return RedirectResponse(f"/works/{work_id}", status_code=303)

    if range_mode == "first":
        lo = 1
        hi = max(lo, int(x or 0))
        if cont and not db.q("SELECT id FROM artifacts WHERE work_id=? AND kind='source'", (work_id,)):
            pipeline.add_notice(w, "解析する文書がありません（ファイルがアップロードされていません）。"
                                   "先に「1. 続き読み込み」で文書をアップロードしてください。")
            return back()
        payload = {"from": lo, "to": hi, "analyze_first": cont}
    elif range_mode in ("cont", "redo"):
        lo = int(from_ch or 0)
        if lo < 1:
            pipeline.add_notice(w, "開始の章番号（第1章以降）を指定してください。"
                                   "第0章（読み込んだ文書）は生成・作り直しの対象にできません。")
            return back()
        hi = max(lo, int(to_ch or 0))
        if not pipeline._prev_end_state_exists(w, lo):
            pipeline.add_notice(w, pipeline._missing_state_message(w, lo - 1))
            return back()
        payload = {"from": lo, "to": hi, "redo": range_mode == "redo"}
    else:
        return PlainTextResponse("bad mode", status_code=400)
    pipeline.set_stage(work_id, "outlining")
    enqueue(work_id, "range_generate", payload)
    return back()


@app.post("/works/{work_id}/add_chapter")
def work_add_chapter(work_id: int, number: int = Form(0)):
    """章追加：空の章（章BOX）を1つ追加する。概要は章ページで書くか、
    概要の再生成・範囲生成で作れる。既にある番号は追加できない。"""
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w:
        return PlainTextResponse("not found", status_code=404)
    n = int(number or 0)
    if n < 1:
        pipeline.add_notice(w, "追加する章番号を指定してください。")
        return RedirectResponse(f"/works/{work_id}", status_code=303)
    if db.one("SELECT id FROM chapters WHERE work_id=? AND number=?", (work_id, n)):
        pipeline.add_notice(w, f"第{n}章は既にあります。")
        return RedirectResponse(f"/works/{work_id}", status_code=303)
    db.run("INSERT INTO chapters(work_id, number, title, outline, status) "
           "VALUES(?,?,'','','outlined')", (work_id, n))
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
    think = pipeline.latest_artifact(w["id"], ch["id"], "think")
    loop_log = pipeline.latest_artifact(w["id"], ch["id"], "log")
    design = pipeline.latest_artifact(w["id"], ch["id"], "prompt")
    in_state = pipeline.current_state(w, ch["number"])
    out_state = pipeline.chapter_end_state(w, ch)
    min_chars = int(w.get("min_chars") or 0)
    _ph = ",".join("?" for _ in CHAPTER_JOB_KINDS)
    jobs = db.q(f"SELECT * FROM jobs WHERE work_id=? AND kind IN ({_ph}) "
                "ORDER BY id DESC LIMIT 5",
                (w["id"],) + CHAPTER_JOB_KINDS)
    active = any(j["status"] in ("queued", "running") for j in jobs)
    job_html = _chapter_jobs_html(jobs, {c["id"]: c for c in chapters})

    # 状態チップ：本文は版数（完成保存の回数）で 生成済み／再生成済み／再々生成済み、
    # 計画は節ごとに _plan_sections_html が付ける。実行中は対象に応じて 生成中／再生成中。
    active_jobs = db.q("SELECT * FROM jobs WHERE work_id=? AND status IN ('queued','running')",
                       (w["id"],))
    plan_count = db.one("SELECT COUNT(*) AS c FROM artifacts WHERE work_id=? AND chapter_id=? "
                        "AND kind='plan'", (w["id"], ch["id"]))["c"]

    def _body_card(art, label, opened, fold_key=None):
        if not art:
            return ""
        text = pipeline.read_text(art["path"])
        fold = f" data-fold='{fold_key}'" if fold_key else ""
        return (f"<details class='card'{fold}{' open' if opened else ''}>"
                f"<summary>{label}（{len(text)}文字）</summary>"
                f"<pre>{esc(text)}</pre></details>")

    def _body_chip(kind, art):
        kinds = (("regen_chapter", "regen_full", "range_generate", "generate_chapters",
                  "generate_thread") if kind == "draft" else ("revise_all",))
        act = any(j["kind"] in kinds and _job_targets_chapter(j, ch) for j in active_jobs)
        if act:
            state = "再生成中" if art else "生成中"
        elif art:
            ver = pipeline.body_version(ch["id"], kind) or 1
            state = "生成済み" if ver <= 1 else ("再生成済み" if ver == 2 else "再々生成済み")
        else:
            return ""
        return (f" <span style='color:{_PLAN_STATE_STYLE[state]};font-weight:700'>"
                f"（{state}）</span>")

    draft_html = _body_card(draft, f"本文（1次出力）{_body_chip('draft', draft)}", opened=not final,
                            fold_key="draft")
    final_html = _body_card(final, f"本文（2次修正）{_body_chip('final', final)}", opened=True)
    if not draft and not final:
        draft_html = "<div class='card'><h2>本文</h2><p>まだ生成されていません。</p></div>"
    status_line = (f"<div class='card'><p>状態：<span id='ch-status-self' style='{_ch_status_style(ch['status'])}'>{esc(_ch_status_label(ch['status']))}</span>"
                   + (f" ／ 最低{min_chars}文字" if min_chars > 0 else "") + "</p></div>")
    think_html = (f"<details class='card'><summary>Think（思考過程）</summary>"
                  f"<pre>{esc(pipeline.read_text(think['path']))}</pre></details>") if think else ""
    live_html = _chapter_live_html(w, ch)
    loop_log_html = (f"<details class='card' style='border:2px solid #b03a3a'><summary>異常終了ログ（生成ループ）</summary>"
                     f"<pre>{esc(pipeline.read_text(loop_log['path']))}</pre></details>") if loop_log else ""
    design_html = (f"<details class='card'><summary>生成指示書</summary>"
                   f"<pre>{esc(pipeline.read_text(design['path']))}</pre></details>") if design else ""
    plan_secs_ch = pipeline.get_plan(w, ch)
    plan_html = ("<div class='card'><h2>展開計画</h2>"
                 + _plan_sections_html(plan_secs_ch, _plan_active_sections(active_jobs, ch),
                                       pipeline.plan_section_versions(ch["id"]), plan_count))
    if plan_secs_ch:
        pending_ch = db.get_setting(f"plan_pending:{w['id']}", "")
        max_num_ch = db.one("SELECT MAX(number) AS m FROM chapters WHERE work_id=?", (w["id"],))["m"] or 0
        cascade_ch = ""
        if pending_ch == str(ch["number"]) and ch["number"] < max_num_ch:
            cascade_ch = (f"<form method='post' action='/works/{w['id']}/plan_cascade'>"
                          f"<input type='hidden' name='chapter_id' value='{ch['id']}'>"
                          f"<button style='background:#2e7d32;border-color:#2e7d32'>OK：第{ch['number'] + 1}章以降を再生成（開始状態の引き継ぎを反映）</button></form>")
        plan_html += (f"""<form method='post' action='/chapters/{ch['id']}/plan_regen'>
            <div class='row'>展開計画への追加指定（内容詳細・終了状態・次章開始状態を作り直します）<textarea name='notes' rows='2'></textarea></div>
            <button>追加指定を考慮して再生成</button></form>{cascade_ch}""")
    plan_html += "</div>"
    state_html = ""
    if in_state or out_state:
        state_html = "<details class='card'><summary>状態（状態チェーン）</summary>"
        if in_state:
            state_html += ("<details><summary>入力状態（この章の開始時点）</summary>"
                           f"<pre>{esc(in_state)}</pre></details>")
        if out_state:
            state_html += ("<details><summary>終了状態（この章の終了時点・次章へ渡す）</summary>"
                           f"<pre>{esc(out_state)}</pre></details>")
        state_html += "</details>"

    prev_state_html = ""
    if ch["number"] >= 1:
        prev_ch = next((c for c in chapters if c["number"] == ch["number"] - 1), None)
        pstate = pipeline.chapter_end_state(w, prev_ch) if prev_ch else ""
        if pstate:
            prev_state_html = (f"<details class='card'><summary>前章（第{ch['number'] - 1}章）の終了状態（キャラ表含む）</summary>"
                               f"<pre>{esc(pstate)}</pre></details>")

    rev_art = pipeline.latest_artifact(w["id"], ch["id"], "rev_outline")
    rev_notes = db.get_setting(f"rev_outline_notes:{ch['id']}", "")
    rev_html = ""
    if rev_art:
        rev_html = f"""
    <div class='card'><h2>修正概要（確認してから本文を再生成）</h2>
    <div class='row'>使った修正要綱（修正概要に入れた文言）<textarea readonly rows='4'>{esc(rev_notes)}</textarea></div>
    <form method='post' action='/chapters/{ch['id']}/regen_from_rev'>
    <div class='row'>修正された章概要（この内容で本文を再生成します。直接編集もできます）<textarea name='rev_outline' rows='10'>{esc(pipeline.read_text(rev_art['path']))}</textarea></div>
    <button>ステップ2：この修正概要で本文を再生成</button>
    <button formaction='/chapters/{ch['id']}/apply_rev_outline'>修正概要を章概要（1回目）へ反映</button></form>
    <form method='post' action='/chapters/{ch['id']}/regen' class='act'>
    <button>章概要（1回目）で本文を再生成</button></form>
    <form method='post' action='/chapters/{ch['id']}/revise_more'>
    <div class='row'>追加の修正指示（追記）<textarea name='notes' rows='3' placeholder='例：やっぱり手紙の伏線は削る。冒頭の天気描写をもう少し長く。'></textarea></div>
    <button>追記して修正概要を作り直し、本文を再生成</button>
    <button formaction='/chapters/{ch['id']}/revise_append'>追記して修正概要のみ作り直す（本文はまだ）</button></form>
    <p>「ステップ2」は修正概要のまま本文を再生成します（生成指示書も作り直し、別スレッド実行・Context Window の80%まで使用。正規の章概要は変わりません）。<br>
    「追記して…」は、今の修正概要を土台に追加の修正指示を反映した新しい修正概要を作ります。「…本文を再生成」はそのまま本文の再生成まで一括、「…概要のみ作り直す」は新しい修正概要を確認してからステップ2へ進む使い方です（2回目以降の修正ラウンド用）。追記した内容は上の「使った修正要綱」に【追記1】【追記2】…として残ります。<br>
    「章概要（1回目）へ反映」は修正概要を正規の章概要に昇格させます（本文は変わりません）。反映後に「章概要（1回目）で本文を再生成」を押すと、正規の章概要をもとに生成指示書を作り直して本文を再生成します。</p></div>"""

    body = f"""
    <p><a href='/works/{w['id']}'>← 作品ページ「{esc(w['title'])}」に戻る</a></p>
    <div class='card'><h2>章概要</h2>
    <form method='post' action='/chapters/{ch['id']}'>
    <div class='row'>タイトル<input type='text' name='title' value="{esc(ch['title'])}"></div>
    <div class='row'>概要<textarea name='outline' rows='5'>{esc(ch['outline'])}</textarea></div>
    <button>概要を保存（承認）</button></form>
    <form method='post' action='/chapters/{ch['id']}/outline_regen' class='act'>
    <button>この章の概要だけ再生成</button>
    <span>概要が途中で切れてしまった場合などに、この章の概要だけを作り直して上書きします（本文は変わりません）。</span></form>
    <form method='post' action='/chapters/{ch['id']}/expand_plan' class='act'>
    <button>この章の展開計画のみ生成</button>
    <span>この章のみ、細概要・開始/終了状態・内容詳細（展開計画）を作ります（全章一括の「2.5 各章を展開」と同じ内容を1章だけ）。</span></form>
    <form method='post' action='/chapters/{ch['id']}/expand_plan_design' class='act'>
    <button>この章の展開計画・生成指示書再生成</button>
    <span>この章の展開計画と生成指示書の両方を作り直します（本文は変わりません）。章概要を反映し直した後に、計画と指示書を新しい概要でそろえる用途です。</span></form>
    <form method='post' action='/chapters/{ch['id']}/regen' class='act'>
    <input type='hidden' name='with_state' value='0'>
    <button>本文のみ（再）生成</button>
    <span>本文だけを作り直します。終了状態・要約は今のまま保持します（状態チェーンは変わりません）。</span></form>
    <form method='post' action='/chapters/{ch['id']}/regen' class='act'>
    <button>本文＋状態（再）生成</button>
    <span>本文に合わせて終了状態（状態チェーン）と要約も作り直します（従来の再生成）。</span></form>
    <form method='post' action='/chapters/{ch['id']}/regen_full' class='act'>
    <button>展開計画～終了状態まで自動再生成</button>
    <span>この章の展開計画を作り直し、その計画で本文・終了状態（状態チェーン）・要約までを自動で一括再生成します。</span></form></div>
    {prev_state_html}
    {status_line}
    {draft_html}
    {final_html}
    <div id='dyn-live'>{live_html}</div>
    {loop_log_html}
    {plan_html}
    {think_html}
    {design_html}
    {state_html}
    <div class='card'><h2>修正要綱で再生成（2段階）</h2>
    <form method='post' action='/chapters/{ch['id']}/revise_outline'>
    <div class='row'>修正要綱（直したい点・盛り込む要素・避ける要素など）<textarea name='notes' rows='5' placeholder='例：灯台守の口調を資料どおり敬語に寄せる。手紙の伏線をもう一度置く。戦闘描写は短く。'></textarea></div>
    <button>ステップ1：修正要綱から修正概要を作成</button></form>
    <p>まず修正要綱に沿って章概要を作り直した「修正概要」を作成します。内容を確認・編集してよければ、ステップ2で本文を再生成してください。本文の再生成で本文・Think・終了状態・要約が作り直されます。2次修正済みの章では修正済み本文（final）が消え、1次出力の状態に戻ります。入力状態（前章までの状態）は変わりません。</p>
    <div id='dyn-jobs'>{job_html}</div></div>
    {rev_html}
    {FOLD_SCRIPT}"""
    live_on = active or bool(live_html) or ch["status"] in ("generating", "revising")
    return page(f"第{ch['number']}章 {ch['title']}", body,
                current_id=w["id"], chapters=chapters,
                live_url=f"/chapters/{chapter_id}/live.json" if live_on else None)


@app.get("/sidebar.json")
def sidebar_json():
    """サイドバーの状態フラグメントだけを返す（全ページ共通の約10秒間隔の自動更新用）。"""
    return JSONResponse({"regions": {"dyn-side": _sidebar_status()}})


@app.get("/works/{work_id}/live.json")
def work_live(work_id: int):
    """作品ページの非同期更新用。動的フラグメントだけを返し、ページ全体は再読み込みしない。
    ジョブが無くなったら reload=true を返し、クライアントが1回だけ再読み込みして確定表示にする。"""
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if not w:
        return JSONResponse({"stop": True})
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work_id,))
    jobs = db.q("SELECT * FROM jobs WHERE work_id=? ORDER BY id DESC LIMIT 10", (work_id,))
    active = any(j["status"] in ("queued", "running") for j in jobs)
    regions = {
        "dyn-head": _work_head_html(w),
        "dyn-jobs": _jobs_html(w, jobs),
        "dyn-side": _sidebar_status(),
        "dyn-current": _current_work_html(w),
    }
    if (w.get("mode") or "switch") == "single":
        regions["dyn-single"] = _single_gen_html(w, chapters[0] if chapters else None)
    texts = {f"ch-status-{c['number']}": _ch_status_label(c["status"]) for c in chapters}
    styles = {f"ch-status-{c['number']}": _ch_status_style(c["status"]) for c in chapters}
    return JSONResponse({"reload": not active, "regions": regions, "texts": texts, "styles": styles})


@app.get("/chapters/{chapter_id}/live.json")
def chapter_live(chapter_id: int):
    """章ページの非同期更新用。生成中カードとジョブ・状態だけを返す。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return JSONResponse({"stop": True})
    w = db.one("SELECT * FROM works WHERE id=?", (ch["work_id"],))
    _ph = ",".join("?" for _ in CHAPTER_JOB_KINDS)
    jobs = db.q(f"SELECT * FROM jobs WHERE work_id=? AND kind IN ({_ph}) "
                "ORDER BY id DESC LIMIT 5",
                (w["id"],) + CHAPTER_JOB_KINDS)
    active = (any(j["status"] in ("queued", "running") for j in jobs)
              or ch["status"] in ("generating", "revising"))
    by_id = {c["id"]: c for c in db.q("SELECT id, number FROM chapters WHERE work_id=?", (w["id"],))}
    regions = {
        "dyn-live": _chapter_live_html(w, ch),
        "dyn-jobs": _chapter_jobs_html(jobs, by_id),
        "dyn-side": _sidebar_status(),
    }
    return JSONResponse({"reload": not active, "regions": regions,
                         "texts": {"ch-status-self": _ch_status_label(ch["status"])},
                         "styles": {"ch-status-self": _ch_status_style(ch["status"])}})


@app.post("/chapters/{chapter_id}/plan_regen")
def chapter_plan_regen(chapter_id: int, notes: str = Form("")):
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "regen_plan", {"chapter_id": chapter_id, "notes": notes})
    return RedirectResponse(f"/works/{ch['work_id']}", status_code=303)


@app.post("/works/{work_id}/plan_cascade")
def work_plan_cascade(work_id: int, chapter_id: int = Form(...)):
    """計画再生成が OK だった章の次章以降を、引き継ぎ状態で再生成する。"""
    ch = db.one("SELECT * FROM chapters WHERE id=? AND work_id=?", (chapter_id, work_id))
    if ch:
        enqueue(work_id, "expand_plans",
                {"from_number": ch["number"] + 1, "cascade": True})
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/works/{work_id}/single_act")
async def work_single_act(work_id: int, notes: str = Form(""), file: UploadFile = File(None)):
    """単章モード：指示（TXT添付込み）⇒生成欄。状態に応じて次の工程を自動で選ぶ。

    添付 TXT は素材（source）として保存され、以降の生成すべてに反映される。
    章がない場合は概要生成と生成＋仕上げを続けてキューに入れ、1回の送信で
    生成まで進む。初回生成では指示文を作品の「欲しい内容」へ追記して効かせる。
    """
    w = db.one("SELECT * FROM works WHERE id=?", (work_id,))
    if file is not None and file.filename:
        raw = await file.read()
        if raw:
            text = raw.decode("utf-8", errors="replace")
            name = re.sub(r"[^\w.\-]+", "_", file.filename or "source.txt")
            pipeline.save_artifact(w, None, "source", name, text)
    chs = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work_id,))
    if chs and chs[0]["status"] in ("generating", "revising"):
        return RedirectResponse(f"/works/{work_id}", status_code=303)  # 生成中は何もしない
    if not chs or chs[0]["status"] in ("outlined", "pending"):
        if notes.strip() and notes.strip() not in (w["request"] or ""):
            db.run("UPDATE works SET request=?, updated_at=? WHERE id=?",
                   (((w["request"] or "") + "\n" + notes.strip()).strip(), db.now(), work_id))
        if not chs:
            enqueue(work_id, "outline")
        enqueue(work_id, "generate_thread")
    else:
        enqueue(work_id, "regen_chapter", {"chapter_id": chs[0]["id"], "notes": notes})
    return RedirectResponse(f"/works/{work_id}", status_code=303)


@app.post("/chapters/{chapter_id}/regen")
def chapter_regen(chapter_id: int, notes: str = Form(""), with_state: str = Form("1")):
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "regen_chapter",
            {"chapter_id": chapter_id, "notes": notes, "with_state": with_state != "0"})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/regen_full")
def chapter_regen_full(chapter_id: int):
    """展開計画～終了状態まで自動再生成（計画→本文→状態・要約を一括で作り直す）。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "regen_full", {"chapter_id": chapter_id})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/expand_plan")
def chapter_expand_plan(chapter_id: int):
    """この章のみの展開計画（詳細概要・開始/終了状態・内容詳細）を生成する。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "expand_plan_one", {"chapter_id": chapter_id})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/expand_plan_design")
def chapter_expand_plan_design(chapter_id: int):
    """この章の展開計画と生成指示書を両方作り直す（本文は変えない）。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "plan_design", {"chapter_id": chapter_id})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/revise_outline")
def chapter_revise_outline(chapter_id: int, notes: str = Form("")):
    """修正要綱フローのステップ1：修正概要の作成ジョブを積む。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "revise_outline", {"chapter_id": chapter_id, "notes": notes})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/regen_from_rev")
def chapter_regen_from_rev(chapter_id: int, rev_outline: str = Form("")):
    """修正要綱フローのステップ2：編集済みの修正概要を保存し、それで本文を再生成する。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    w = db.one("SELECT * FROM works WHERE id=?", (ch["work_id"],))
    if rev_outline.strip():
        pipeline.save_artifact(w, ch, "rev_outline",
                               f"ch{ch['number']:03d}_rev_outline.txt", rev_outline)
    notes = db.get_setting(f"rev_outline_notes:{ch['id']}", "")
    enqueue(ch["work_id"], "regen_chapter",
            {"chapter_id": chapter_id, "notes": notes, "use_rev_outline": True})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/revise_more")
def chapter_revise_more(chapter_id: int, notes: str = Form("")):
    """修正概要への追記＋本文の再生成ジョブを積む（2回目以降の修正ラウンド）。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "revise_more", {"chapter_id": chapter_id, "notes": notes})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/revise_append")
def chapter_revise_append(chapter_id: int, notes: str = Form("")):
    """修正概要への追記のみ（本文は再生成しない）ジョブを積む。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "revise_append", {"chapter_id": chapter_id, "notes": notes})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/apply_rev_outline")
def chapter_apply_rev_outline(chapter_id: int, rev_outline: str = Form("")):
    """修正概要を正規の章概要（1回目）へ反映する。本文は再生成しない。

    編集済みの修正概要を成果物にも保存し直してから、chapters.outline を
    書き換える。反映後に本文を作り直す場合は「章概要（1回目）で本文を
    再生成」ボタン（/chapters/{id}/regen）を使う。
    """
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    w = db.one("SELECT * FROM works WHERE id=?", (ch["work_id"],))
    if rev_outline.strip():
        pipeline.save_artifact(w, ch, "rev_outline",
                               f"ch{ch['number']:03d}_rev_outline.txt", rev_outline)
        db.run("UPDATE chapters SET outline=? WHERE id=?", (rev_outline, chapter_id))
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}/outline_regen")
def chapter_outline_regen(chapter_id: int):
    """その章の概要だけを作り直すジョブを積む。"""
    ch = db.one("SELECT * FROM chapters WHERE id=?", (chapter_id,))
    if not ch:
        return RedirectResponse("/", status_code=303)
    enqueue(ch["work_id"], "outline_chapter", {"chapter_id": chapter_id})
    return RedirectResponse(f"/chapters/{chapter_id}", status_code=303)


@app.post("/chapters/{chapter_id}")
def chapter_save(chapter_id: int, title: str = Form(""), outline: str = Form("")):
    db.run("UPDATE chapters SET title=?, outline=?, status='outlined' WHERE id=?",
           (title, outline, chapter_id))
    row = db.one("SELECT work_id FROM chapters WHERE id=?", (chapter_id,))
    if row:
        pipeline.set_stage(row["work_id"], "outline_review")
    return RedirectResponse(f"/works/{row['work_id']}" if row else "/", status_code=303)


def _fmt_ts(ts):
    """成果物の作成時刻（epoch秒）を日本時間の 'YYYY-MM-DD HH:MM' で返す。"""
    try:
        return datetime.fromtimestamp(float(ts), timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


@app.get("/artifacts/{artifact_id}")
def artifact_view(artifact_id: int):
    a = db.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not a:
        return PlainTextResponse("not found", status_code=404)
    # 削除は2段構え：一覧には削除ボタンを置かず、この表示ページでのみ削除できる
    restore_btn = ""
    if a["kind"] == "backup" and re.match(r"ch\d+_(draft|final)_", os.path.basename(a["path"])):
        target = "2次修正（final）" if "_final_" in os.path.basename(a["path"]) else "1次出力（draft）"
        restore_btn = (
            f"<form method='post' action='/artifacts/{a['id']}/restore' style='display:inline' "
            f"onsubmit=\"return confirm('この前版を{target}へ復元しますか？（現在の本文も前版として残ります）')\">"
            f"<button>この版を{target}へ復元</button></form> ")
    head = (
        f"<div class='card'><b>{esc(a['kind'])} #{a['id']}</b>　作成：{_fmt_ts(a['created_at'])}"
        f"　{esc(a['path'])} "
        f"{restore_btn}"
        f"<form method='post' action='/artifacts/{a['id']}/delete' style='display:inline' "
        f"onsubmit=\"return confirm('この成果物を削除しますか？')\">"
        f"<button>この成果物を削除</button></form></div>")
    return page(f"{a['kind']} #{a['id']}", head + f"<pre>{esc(pipeline.read_text(a['path']))}</pre>")


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
    if llm.MOCK:
        conn = "モックモード"
    elif loaded is None:
        conn = "接続が切れています（URL未設定、またはサーバーに届いていません）"
    elif loaded == "":
        conn = "接続OK（モデル未ロード）"
    else:
        conn = "接続OK（ロード中モデル：" + esc(loaded) + "）"
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
    <div class='row'>Context Window（トークン）<select name='ctx_window' onchange='HAlloc()' oninput='HAlloc()'>{ctx_opts}</select> プロンプトに載せる文脈量の目安に使います（実際の上限はサーバー側の最大コンテキストです）。<b>変更すると、その場で下の各 Max Tokens が比率に応じて再配置されます</b>（各工程は別スレッド実行のため、合計ではなく工程ごとに再配置します。手動調整した値も再配置で上書きされます）</div>
    <div class='row'>Context Window（2次修正用）<select name='ctx_window_revise' onchange='HRev()' oninput='HRev()'>{ctx_rev_opts}</select><span id='reviseAlloc' style='font-weight:bold'></span> 2次修正はモデルが変わる前提なので、大きめのモデルに合わせて最大にしておくのが既定です。モデル変更時の2次修正では、この値の<b>80%</b>を Max Tokens に自動設定します。モデルが変わらない場合（スレッドモード等）は、通常の Context Window の80%を使います</div>
    {_num_row('Max Tokens：概要生成', 'max_tokens_outline', pipeline.max_tokens('max_tokens_outline', 'MAX_TOKENS_OUTLINE', 16384), '章概要作成（全体概要・章ごとの概要・修正概要・展開計画）の枠です。別スレッド実行でウィンドウの90%まで使います')}
    {_num_row('Max Tokens：生成指示書・文書解析', 'max_tokens_design', pipeline.design_max_tokens(), '初期読み込み（文書解析）と生成指示書の枠です。文書解析は出力が長くなりがちです（実測で約17,000トークン）。表示値は実行値です')}
    <div class='row'><label class='switch'><input type='checkbox' name='design_no_truncate' value='1' {'checked' if db.get_setting('design_no_truncate', '1') != '0' else ''}><span class='sl'></span></label>生成指示書・文書解析の打ち切り防止（ONなら Context Window の90%まで使い、実際の上限はサーバー側がプロンプト分を差し引いて自動調整します。OFFなら上の入力値が上限）</div>
    <div class='row'><label class='switch'><input type='checkbox' name='loop_detect' value='1' {'checked' if db.get_setting('loop_detect', '1') != '0' else ''}><span class='sl'></span></label>ループ検出（ONなら、同じ文やフレーズが異常に繰り返される生成を自動で止めます。「○○さまならしょうがない」のような決め台詞の繰り返しで誤って止まる場合はOFFにしてください）</div>
    <div class='row'><label class='switch'><input type='checkbox' name='loop_body_only' value='1' {'checked' if db.get_setting('loop_body_only', '0') == '1' else ''}><span class='sl'></span></label>ループ検出は本文のみに適用（ONなら、状態シート・概要・計画などの構造化出力では検出しません。対象は本文生成・続き書き・2次修正だけになります）</div>
    {_num_row('Max Tokens：本文生成（1次出力）', 'max_tokens_chapter', pipeline.body_max_tokens(), '本文生成は別スレッド実行のため、実行時は Context Window の80%を自動使用します（表示値は実行値です）。思考トークンも内数です')}
    {_num_row('Max Tokens：要約', 'max_tokens_summary', pipeline.max_tokens('max_tokens_summary', 'MAX_TOKENS_SUMMARY', 4096), '章の終了後に、次章へ渡す要約と状態スナップショットを作る枠です。状態は詳細に記録するため、思考分も含めて余裕を持たせています')}
    {_num_row('Max Tokens：2次修正', 'max_tokens_revise', pipeline.revise_max_tokens(True), '2次修正も別スレッド実行のため、実行時はモデル切替時はこの値（2次用 Context Window の80%）、同モデル時は通常ウィンドウの80%（' + format(pipeline.revise_max_tokens(False), ',') + '）を自動使用します（表示値は実行値です）')}
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
          max_tokens_outline: v(0.90, 4096), max_tokens_design: v(0.45, 4096),
          max_tokens_chapter: v(0.80, 4096), max_tokens_summary: v(0.10, 4096)
        }};
      }}
      function field(n) {{ return document.querySelector('input[name="' + n + '"]'); }}
      function check() {{
        var ctx = ctxSel ? (parseInt(ctxSel.value || '0', 10) || 0) : 0;
        var rc = revSel ? (parseInt(revSel.value || '0', 10) || 0) : 0;
        var over = [];
        names.forEach(function (n) {{
          var el = field(n);
          var v = parseInt((el && el.value) || '0', 10) || 0;
          var win = (n === 'max_tokens_revise') ? rc : ctx;
          if (win && v > Math.floor(win * 0.9)) over.push(n.replace('max_tokens_', ''));
        }});
        var msg = '';
        if (over.length) {{
          msg = 'ウィンドウの90%を超えている項目があります（' + over.join('、')
            + '）。各工程は別スレッド実行のため合計は無関係で、実行時は90%で頭打ちになります';
        }}
        ['tokenOver', 'tokenOverTop'].forEach(function (id) {{
          var w = document.getElementById(id);
          if (w) w.textContent = msg;
        }});
      }}
      function applyCtx() {{
        if (!ctxSel) return;
        var c = parseInt(ctxSel.value || '0', 10) || 0;
        var a = alloc(c);
        Object.keys(a).forEach(function (n) {{ var el = field(n); if (el) el.value = a[n]; }});
        var nt = field('design_no_truncate');
        var df = field('max_tokens_design');
        if (nt && nt.checked && df && c) df.value = Math.max(4096, Math.floor(c * 0.9 / 256) * 256);
        check();
      }}
      function revNote() {{
        var el = document.getElementById('reviseAlloc');
        if (!revSel) return;
        var rc = parseInt(revSel.value || '0', 10) || 0;
        var v = Math.max(4096, Math.floor(rc * 0.8 / 256) * 256);
        if (el) el.textContent = rc ? ' → モデル変更時の2次修正 Max Tokens: ' + v.toLocaleString() + '（この値の80%）' : '';
        var f = field('max_tokens_revise');
        if (f && rc) f.value = v;
        check();
      }}
      if (ctxSel) {{
        ctxSel.addEventListener('change', applyCtx);
        ctxSel.addEventListener('input', applyCtx);
      }}
      if (revSel) {{
        revSel.addEventListener('change', revNote);
        revSel.addEventListener('input', revNote);
      }}
      var ntBox = field('design_no_truncate');
      if (ntBox) ntBox.addEventListener('change', applyCtx);
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
                  show_fanfic_button: str = Form(""),
                  design_no_truncate: str = Form(""),
                  loop_detect: str = Form(""), loop_body_only: str = Form("")):
    db.set_setting("strata_base_url", strata_base_url.strip())
    if strata_api_key.strip():
        db.set_setting("strata_api_key", strata_api_key.strip())
    db.set_setting("revise_model_hint", revise_model_hint.strip())
    db.set_setting("chat_webhook_url", chat_webhook_url.strip())
    db.set_setting("web_search_enabled", "1" if web_search_enabled == "1" else "0")
    db.set_setting("show_fanfic_button", "1" if show_fanfic_button == "1" else "0")
    db.set_setting("design_no_truncate", "1" if design_no_truncate == "1" else "0")
    db.set_setting("loop_detect", "1" if loop_detect == "1" else "0")
    db.set_setting("loop_body_only", "1" if loop_body_only == "1" else "0")
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
        # max_tokens_revise は2次用ウィンドウの80%が実行値で、表示も実行値から
        # 描画するため、通常ウィンドウの再配置では上書きしない。
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
    if db.one("SELECT id FROM jobs WHERE status IN ('queued','running') LIMIT 1"):
        return PlainTextResponse(
            "実行中・待機中のジョブがあるため復元できません。ジョブの完了後に実行してください。",
            status_code=400)
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
    pipeline.ensure_alloc_v3()
    pipeline.start_worker()
