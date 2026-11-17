"""工程の実行役。ジョブキューを1本ずつ処理する（推論サーバーが単一なので直列でよい）。

ワークスペース＝物語（作品）単位。工程は
続き読み込み → 次章の内容（概要） → 1次出力 → 2次修正 → PHP・TXT化。
モデルは相手先API準拠：各リクエスト時点でサーバーがサーブ中のモデルを使う。
2次修正は「1次出力に使ったモデルからロード中モデルが変わった」ことを検知して始める。
生成パラメータ（Context Window / Max Tokens 等）は設定画面（DB）から読む。
"""
import json
import os
import re
import threading
import time
import traceback

import db
import llm
import prompts
import search
from notify import notify

WORKS_DIR = os.environ.get("WORKS_DIR", "/works")
ENV_REVISE_HINT = os.environ.get("MODEL_Q4_HINT", os.environ.get("MODEL_Q4", ""))


# ---------- 設定（AnythingLLM 風：画面から変更でき、即時反映） ----------

def ctx_window():
    return db.get_int_setting("ctx_window", int(os.environ.get("CTX_WINDOW", "262144")))


def ctx_window_revise():
    """2次修正用 Context Window（モデルが変わる前提で、既定は最大の 524288）。"""
    return db.get_int_setting("ctx_window_revise", 524288)


def allocate_tokens(ctx):
    """Context Window から各工程の Max Tokens を自動再配置する配分（初期値）。

    各工程は独立した呼び出し（別スレッド）で実行され、文脈を持ち越さないため、
    合計で配り合う必要はなく、作業のまとまりごとに枠を決める：
    章概要作成（全体概要・章ごとの概要・修正概要・展開計画）90%／
    初期読み込み・生成指示書（文書解析＋指示書。解析は出力が長くなりがちで
    実測約17,000トークン）45%／本文生成80%／要約・状態更新10%（章の終了後に
    次章へ渡す要約と状態スナップショットを作る工程。状態は登場人物・関係・
    伏線を詳細に記録するため、思考分も含めて余裕を持たせる）／
    2次修正80%（この ctx 基準の値。実行時は対象ウィンドウの80%を使う）。
    256 単位に丸め、工程ごとの下限を保証する。
    """
    def _v(pct, lo):
        return max(lo, (int(int(ctx) * pct) // 256) * 256)
    return {
        "max_tokens_outline": _v(0.90, 4096),
        "max_tokens_design": _v(0.45, 4096),
        "max_tokens_chapter": _v(0.80, 4096),
        "max_tokens_summary": _v(0.10, 4096),
        "max_tokens_revise": _v(0.80, 4096),
    }


def ensure_alloc_v3():
    """配分見直しへの一回限りの移行（v1.25で導入、v1.26で要約枠を10%に拡大）。

    v1.25 未導入の環境では全項目を新配分で上書きし、v1.25 導入済みの環境では
    要約・状態更新の枠だけを新しい値に更新する（他の手動調整には干渉しない）。
    """
    if db.get_setting("alloc_v3", ""):
        return
    alloc = allocate_tokens(ctx_window())
    if db.get_setting("alloc_v2", ""):
        db.set_setting("max_tokens_summary", str(alloc["max_tokens_summary"]))
    else:
        for key, val in alloc.items():
            db.set_setting(key, str(val))
    db.set_setting("alloc_v2", "1")
    db.set_setting("alloc_v3", "1")


def body_max_tokens():
    """本文生成（1次出力・再生成）の Max Tokens。

    本文生成は独立した呼び出し（別スレッド）で実行され、前の生成の文脈を
    引き継がないため、1次出力の設定値で頭打ちにせず、Context Window の
    80%を出力上限としてたっぷり使う。
    """
    return max(4096, (int(ctx_window() * 0.8) // 256) * 256)


def revise_max_tokens(model_changed):
    """2次修正の Max Tokens。

    2次修正も独立した呼び出し（別スレッド）で実行されるため、1次出力の
    トークン数から差し引くような配分はせず、常に対象ウィンドウの80%を使う。
    モデルが変わった場合は2次用 Context Window、変わらない場合は通常の
    Context Window が対象になる。
    """
    ctx = ctx_window_revise() if model_changed else ctx_window()
    return max(4096, (int(ctx * 0.8) // 256) * 256)


def max_tokens(key, env_key, default):
    """設定値 → 環境変数 → Context Window からの自動配分（初期値）の順で解決する。"""
    v = db.get_setting(key, "")
    if v:
        try:
            return int(float(v))
        except ValueError:
            pass
    env = os.environ.get(env_key, "")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return allocate_tokens(ctx_window()).get(key, default)


def temperature():
    return db.get_float_setting("temperature", float(os.environ.get("TEMPERATURE", "0.8")))


def revise_hint():
    v = db.get_setting("revise_model_hint", "")
    return v if v else ENV_REVISE_HINT


def design_max_tokens():
    """生成指示書・文書解析の実行時 max_tokens。

    打ち切り防止（設定 design_no_truncate、既定ON）が有効なら、別スレッドの
    上限いっぱい（Context Window の90%）を使う。サーバー（Strata）側が
    プロンプト分を差し引いて自動調整するため大きく渡しても安全で、
    解析・指示書が途中で切れなくなる。無効なら保存済みの設定値を使う。
    """
    if db.get_setting("design_no_truncate", "1") != "0":
        return max(4096, (int(ctx_window() * 0.9) // 256) * 256)
    return max_tokens("max_tokens_design", "MAX_TOKENS_DESIGN", 8192)


def ctx_budget_chars(ctx=None):
    """プロンプトに載せる文脈（設定資料＋素材）の上限。1トークン≒1字で概算。"""
    return max(6000, min(240000, (ctx or ctx_window()) // 2))


def tail_chars():
    return max(800, min(6000, ctx_window() // 24))


# ---------- 成果物まわり ----------

def work_dir(slug):
    d = os.path.join(WORKS_DIR, slug)
    for sub in ("source", "prompts", "drafts", "summaries", "revised", "final", "states", "thinking", "logs", "live", "plans"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


# ---------- 生成中の逐次表示（ライブ部分保存） ----------
# 生成はコンテナ内のワーカーで進むため、ブラウザを閉じても・セッションが切れても
# 推論とループ検出は継続する。途中経過はここに保存され、画面側はそれを読むだけ。

def _partial_files(work, ch):
    d = os.path.join(work_dir(work["slug"]), "live")
    return (os.path.join(d, f"ch{ch['number']:03d}_body.txt"),
            os.path.join(d, f"ch{ch['number']:03d}_think.txt"))


def save_partial(work, ch, label, body, think):
    bp, tp = _partial_files(work, ch)
    for path, text in ((bp, body), (tp, think)):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text or "")
        os.replace(tmp, path)
    db.set_setting("live:work_id", str(work["id"]))
    db.set_setting("live:chapter", str(ch["number"]))
    db.set_setting("live:label", label)
    db.set_setting("live:body_chars", str(len(body or "")))
    db.set_setting("live:think_chars", str(len(think or "")))
    db.set_setting("live:at", str(time.time()))


def read_partial(work, ch):
    bp, tp = _partial_files(work, ch)
    if not os.path.isfile(bp):
        return None, None
    return read_text(bp), (read_text(tp) if os.path.isfile(tp) else "")


def clear_partial(work, ch):
    for path in _partial_files(work, ch):
        try:
            os.remove(path)
        except OSError:
            pass
    if db.get_setting("live:work_id", "") == str(work["id"]) \
            and db.get_setting("live:chapter", "") == str(ch["number"]):
        for k in ("live:work_id", "live:chapter", "live:label",
                  "live:body_chars", "live:think_chars", "live:at"):
            db.set_setting(k, "")


def progress_writer(work, ch, label, base_body=""):
    """llm.chat の progress コールバック。間引きながら途中経過を保存する。"""
    state = {"chars": 0, "at": 0.0}

    def write(content, think):
        total = len(base_body) + len(content or "") + len(think or "")
        now = time.time()
        if total - state["chars"] < 1000 and now - state["at"] < 2.0:
            return
        state["chars"] = total
        state["at"] = now
        try:
            save_partial(work, ch, label, base_body + (content or ""), think or "")
        except Exception:
            pass

    return write


def save_artifact(work, chapter, kind, filename, text):
    d = work_dir(work["slug"])
    sub = {"source": "source", "analysis": "source", "glossary": "source", "character": "source",
           "prompt": "prompts", "draft": "drafts", "state": "states", "think": "thinking",
           "summary": "summaries", "revised": "revised", "final": "final", "log": "logs",
           "plan": "plans", "rev_outline": "plans"}[kind]
    path = os.path.join(d, sub, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    db.run(
        "INSERT INTO artifacts(work_id, chapter_id, kind, path, created_at) VALUES(?,?,?,?,?)",
        (work["id"], chapter["id"] if chapter else None, kind, path, db.now()),
    )
    return path


def latest_artifact(work_id, chapter_id, kind):
    return db.one(
        "SELECT * FROM artifacts WHERE work_id=? AND chapter_id IS ? AND kind=? ORDER BY id DESC LIMIT 1",
        (work_id, chapter_id, kind),
    )


def read_text(path, limit=None):
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read()
        return t[:limit] if limit else t
    except OSError:
        return ""


def is_fanfic(work):
    return int(work.get("fanfic") or 0) == 1


def web_search_on(work):
    """全体設定とワークスペース別トグルの両方がONのときだけWeb検索する。"""
    if db.get_setting("web_search_enabled", "1") == "0":
        return False
    v = work.get("web_search")
    return (1 if v is None else int(v)) == 1


def work_mode(work):
    """switch＝モデル切替あり（1次→切替→2次）／ thread＝切替なし・章ごとスレッドで完結。"""
    return work.get("mode") or "switch"


def sys_note(work):
    """二次創作ONのとき、システムプロンプトに足す注意書き。"""
    if is_fanfic(work):
        return ("\nなお、本作は既存作品の二次創作です。登場するキャラクターは原作のキャラクターであり、"
                "言い回し・立ち振る舞いは原作とキャラクター資料に沿わせてください。")
    return ""


def settings_text(work, ctx=None):
    parts = [work["settings"] or ""]
    if is_fanfic(work):
        parts.append("【二次創作】本作は既存作品の二次創作です。キャラクターの言動は原作準拠を意識してください。")
    for a in db.q("SELECT * FROM artifacts WHERE work_id=? AND kind='analysis' ORDER BY id", (work["id"],)):
        parts.append("【初期文書解析】\n" + read_text(a["path"]))
    for a in db.q("SELECT * FROM artifacts WHERE work_id=? AND kind='glossary' ORDER BY id", (work["id"],)):
        parts.append("【用語メモ】\n" + read_text(a["path"]))
    for a in db.q("SELECT * FROM artifacts WHERE work_id=? AND kind='character' ORDER BY id", (work["id"],)):
        parts.append("【キャラクター資料】\n" + read_text(a["path"]))
    for a in db.q("SELECT * FROM artifacts WHERE work_id=? AND kind='source' ORDER BY id", (work["id"],)):
        parts.append(read_text(a["path"]))
    return "\n\n".join(p for p in parts if p)[:ctx_budget_chars(ctx)]


def outlines_text(work):
    out = []
    for ch in db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work["id"],)):
        out.append(f"第{ch['number']}章 {ch['title']}\n{ch['outline']}")
    return "\n\n".join(out)


def summaries_text(work, upto_number):
    out = []
    for ch in db.q("SELECT * FROM chapters WHERE work_id=? AND number<? ORDER BY number",
                   (work["id"], upto_number)):
        a = latest_artifact(work["id"], ch["id"], "summary")
        if a:
            out.append(f"第{ch['number']}章：{read_text(a['path'])}")
    return "\n".join(out)


def prev_tail(work, number):
    n = tail_chars()
    if number <= 1:
        srcs = db.q("SELECT * FROM artifacts WHERE work_id=? AND kind='source' ORDER BY id", (work["id"],))
        if srcs:
            return read_text(srcs[-1]["path"])[-n:]
        return ""
    prev = db.one("SELECT * FROM chapters WHERE work_id=? AND number=?", (work["id"], number - 1))
    if not prev:
        return ""
    a = latest_artifact(work["id"], prev["id"], "draft")
    return read_text(a["path"])[-n:] if a else ""


def current_state(work, before_number=None):
    """状態チェーン：指定章の開始時点の状態を返す。

    その章より前の章のうち、最も大きい番号の章の「終了状態」。なければ初期状態
    （初期文書解析から作った文書終了時点の状態）。before_number=None なら最新章の終了状態。
    状態は章ごとに受け渡され、章の途中で勝手に変わらないよう生成の文脈に必ず入れる。
    """
    rows = db.q(
        "SELECT a.path AS path, c.number AS number FROM artifacts a "
        "JOIN chapters c ON a.chapter_id = c.id "
        "WHERE a.work_id=? AND a.kind='state' ORDER BY c.number DESC, a.id DESC",
        (work["id"],))
    for r in rows:
        if before_number is None or r["number"] < before_number:
            return read_text(r["path"])
    init = latest_artifact(work["id"], None, "state")
    return read_text(init["path"]) if init else ""


def chapter_end_state(work, ch):
    a = latest_artifact(work["id"], ch["id"], "state")
    return read_text(a["path"]) if a else ""


def update_state(work, ch, body, prev_state):
    """章本文から、その章の終了状態（次の章へ渡す状態）を作る。"""
    new_state = llm.chat(
        "auto", prompts.STATE_UPDATE_SYSTEM,
        prompts.STATE_UPDATE_USER.format(
            prev_state=prev_state or "（物語の開始前。状態はまだありません）",
            number=ch["number"], title=ch["title"], text=body),
        max_tokens=max_tokens("max_tokens_summary", "MAX_TOKENS_SUMMARY", 4096),
        temperature=temperature())
    save_artifact(work, ch, "state", f"ch{ch['number']:03d}_state.txt", new_state)
    _sync_plan_from_body(work, ch, new_state)
    return new_state


def _sync_plan_from_body(work, ch, actual_state):
    """生成した本文から作った実績の終了状態を、展開計画側にも同期する。

    当該章の計画の「章終了時の状態」「次章開始状態」を実績で置き換え、
    次章の計画の「章開始時の状態」も実績に合わせる。本文で計画外に
    追加された内容の帰結も、状態として計画に反映される。
    """
    sections = get_plan(work, ch)
    if sections:
        sections["章終了時の状態"] = actual_state
        sections["次章開始状態"] = actual_state
        save_artifact(work, ch, "plan", f"ch{ch['number']:03d}_plan.txt", plan_to_text(sections))
    nxt = db.one("SELECT * FROM chapters WHERE work_id=? AND number=?",
                 (work["id"], ch["number"] + 1))
    if nxt:
        nsec = get_plan(work, nxt)
        if nsec:
            nsec["章開始時の状態"] = actual_state
            save_artifact(work, nxt, "plan", f"ch{nxt['number']:03d}_plan.txt", plan_to_text(nsec))


def save_think(work, ch):
    """直前の本文生成の thinking を成果物として残す（章ページで表示する）。"""
    think = (llm.LAST_THINKING or "").strip()
    if think:
        save_artifact(work, ch, "think", f"ch{ch['number']:03d}_think.txt", think)


def save_loop_log(work, ch, stage_label, err):
    """生成ループによる異常終了のログを成果物（kind=log）として保存する。

    成果物欄と章ページから開いて状況確認できるようにするためのもの。
    ループした本文そのものは成果物（draft/final）にせず、このログ末尾に
    抜粋としてだけ残す。
    """
    import datetime
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ch_label = f"第{ch['number']}章 {ch['title']}" if ch else "（章なし）"
    lines = [
        "# 異常終了ログ（生成ループ検出）",
        f"日時：{now}",
        f"作品：{work['title']}",
        f"章：{ch_label}",
        f"工程：{stage_label}",
        f"使用モデル：{llm.loaded_model() or '不明'}",
        f"検出対象：{err.where}",
        f"繰り返し単位（抜粋）：{err.unit[:200]}",
        f"推定繰り返し回数：約{err.repeats}回",
        f"検出時点の累積文字数：{err.chars}文字（本文{len(err.content_text)}文字／thinking {len(err.think_text)}文字）",
        "",
        "この工程は異常終了しました。ループしたテキストは本文の成果物として保存していません。",
        "状況確認のため、ループしたテキストの末尾抜粋を以下に残します。工程を再実行してください。",
        "",
        "## ループした本文（末尾2000字）",
        err.content_text[-2000:] or "（本文はまだ出力されていませんでした）",
        "",
        "## ループした thinking（末尾2000字）",
        err.think_text[-2000:] or "（thinking はありませんでした）",
    ]
    fname = f"ch{ch['number']:03d}_loop.txt" if ch else "loop.txt"
    path = save_artifact(work, ch, "log", fname, "\n".join(lines))
    err.saved_log = True
    return path


def set_stage(work_id, stage):
    db.run("UPDATE works SET stage=?, updated_at=? WHERE id=?", (stage, db.now(), work_id))


def set_chapter_status(chapter_id, status):
    db.run("UPDATE chapters SET status=? WHERE id=?", (status, chapter_id))


def work_min_chars(work):
    """章の最低文字数（作品ごとの指定）。0 は指定なし。"""
    try:
        return int(work.get("min_chars") or 0)
    except (TypeError, ValueError):
        return 0


def min_chars_note(work):
    m = work_min_chars(work)
    return f"長さの指定：この章は最低{m}文字以上で書いてください。" if m > 0 else ""


def chapter_chars(work_id, chapter_id):
    """章の現在文字数（final があれば final、なければ draft）。UI 表示用。"""
    for kind in ("final", "draft"):
        a = latest_artifact(work_id, chapter_id, kind)
        if a:
            return len(read_text(a["path"]))
    return 0


def ensure_min_chars(job, work, ch, body, progress=None):
    """本文が最低文字数に届くまで、続きを自動で書き足す。

    v1.12 で仕様変更：従来は最大5回で打ち切り、届かなくてもそのまま
    「生成済み／完成」にしていたため、最低2万字でも1万字程度で完了して
    しまった（1回の書き足しで得られるのは実測1,000〜2,000字程度）。
    - 回数上限は目標から見積もる（1,000字あたり1回＋余裕、下限10回・上限30回）
    - 各回に「今回書き足す最低文字数（不足分と3,000字の小さい方）」を明示し、
      届くまでは章を終わらせないよう指示する
    - 書き足しが進まなくなった（300字未満が2回連続）ら打ち切る
    - それでも届かなければ、下書きを保存し、章を再実行可能な状態に戻して
      エラーにする（完成扱いにしない）。停止時は JobCancelledError で中断する
      （生成中の呼び出し内でも llm 側の CANCEL_CHECK により接続ごと切れる）。
    """
    target = work_min_chars(work)
    if target <= 0:
        return body
    max_rounds = min(30, max(10, -(-target // 1000)))
    rounds = 0
    stalls = 0
    while len(body) < target and rounds < max_rounds:
        if _cancelled(job):
            raise llm.JobCancelledError("ユーザーにより停止されました")
        rounds += 1
        need = target - len(body)
        base_snapshot = body
        extra = llm.chat(
            "auto", prompts.CONTINUE_SYSTEM + sys_note(work),
            prompts.CONTINUE_USER.format(
                settings=settings_text(work),
                outlines=outlines_text(work),
                summaries=summaries_text(work, ch["number"]),
                tail=body[-2500:],
                current=len(body), target=target,
                need=need, round_min=min(need, 3000)),
            max_tokens=body_max_tokens(),
            temperature=temperature(),
            progress=(lambda c, t, _b=base_snapshot: progress(_b + (c or ""), t))
            if progress else None)
        extra = (extra or "").strip()
        if extra:
            body = body.rstrip() + "\n" + extra
            save_artifact(work, ch, "draft", f"ch{ch['number']:03d}.txt", body)
        if len(extra) < 300:
            stalls += 1
            if stalls >= 2:
                break
        else:
            stalls = 0
    if len(body) < target:
        save_artifact(work, ch, "draft", f"ch{ch['number']:03d}.txt", body)
        msg = (f"第{ch['number']}章が最低文字数に届きませんでした"
               f"（{len(body)} / {target}文字、書き足し{rounds}回で打ち切り）。"
               "完成扱いにせず下書きを保存しました。工程を再実行してください。")
        add_notice(work, msg)
        set_chapter_status(ch["id"], "outlined")
        raise RuntimeError(msg)
    return body


# ---------- 工程 ----------

def do_analyze(job, work):
    """初期文書解析：アップロード済み文書を読み、続き生成の基礎資料を作る。"""
    srcs = db.q("SELECT * FROM artifacts WHERE work_id=? AND kind='source' ORDER BY id", (work["id"],))
    if not srcs:
        raise RuntimeError("解析する文書がありません。先に Txt 等のファイルをアップロードしてください。")
    text = "\n\n".join(read_text(a["path"]) for a in srcs)
    text = text[:max(ctx_budget_chars() * 2, 12000)]
    result = llm.chat(
        "auto", prompts.ANALYSIS_SYSTEM + sys_note(work),
        prompts.ANALYSIS_USER.format(text=text),
        max_tokens=design_max_tokens(),
        temperature=temperature())
    save_artifact(work, None, "analysis", "analysis.txt", result)
    state = llm.chat(
        "auto", prompts.STATE_EXTRACT_SYSTEM + sys_note(work),
        prompts.STATE_EXTRACT_USER.format(analysis=result, text=text),
        max_tokens=design_max_tokens(),
        temperature=temperature())
    save_artifact(work, None, "state", "state_initial.txt", state)
    set_stage(work["id"], "analyzed")
    notify(f"【小説ハーネス】「{work['title']}」の初期文書解析が完了しました。解析結果と現在の状態が以降の生成に反映されます。")


_KANJI_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _kanji_to_int(s):
    if s in _KANJI_NUM:
        return _KANJI_NUM[s]
    if "十" in s:
        a, _, b = s.partition("十")
        return (_KANJI_NUM.get(a, 1) if a else 1) * 10 + (_KANJI_NUM.get(b, 0) if b else 0)
    return None


def parse_outline_text(text):
    """概要テキストから (章番号, タイトル, 本文) を抽出する。形式の揺れに強くする。

    本命は「第N章 タイトル」見出し。漢数字（第一章）や markdown 見出し等でも拾う。
    """
    text = text or ""
    found = re.findall(r"第(\d+)章[ 　]*(.*?)\n(.*?)(?=第\d+章|\Z)", text, re.S)
    if found:
        return found

    def _sub(m):
        n = _kanji_to_int(m.group(1))
        return f"第{n}章" if n else m.group(0)

    t2 = re.sub(r"第([一二三四五六七八九十]+)章", _sub, text)
    found = re.findall(r"第(\d+)章[ 　]*(.*?)\n(.*?)(?=第\d+章|\Z)", t2, re.S)
    if found:
        return found
    parts = re.split(r"^.*第(\d+)章[ 　]*(.*?)$", text, flags=re.M)
    out = []
    it = iter(parts[1:])
    for num, title, body in zip(it, it, it):
        if body.strip():
            out.append((num, title, body))
    return out


def do_outline(job, work):
    """次章の内容：各章概要を一括生成する。"""
    try:
        run_term_check(work)
    except Exception:
        pass
    user = prompts.OUTLINE_USER.format(
        request=work["request"], settings=settings_text(work),
        source="", num_chapters=work["num_chapters"])
    text = llm.chat(
        "auto", prompts.OUTLINE_SYSTEM + sys_note(work), user,
        max_tokens=max_tokens("max_tokens_outline", "MAX_TOKENS_OUTLINE", 16384),
        temperature=temperature())
    found = parse_outline_text(text)
    if not found:
        # モデルが短く打ち切る・形式を崩すことがある。生出力を残し、形式を
        # 明示して1回だけ自動リトライする。
        save_artifact(work, None, "log", "outline_failed.txt", text)
        retry_user = (user + "\n\n【重要】前回の出力は形式が崩れて読み取れませんでした。"
                      "前置きや説明は書かず、必ず「第1章 タイトル」の見出し行で始め、"
                      "全章を同じ形式で列挙してください。")
        text = llm.chat(
            "auto", prompts.OUTLINE_SYSTEM + sys_note(work), retry_user,
            max_tokens=max_tokens("max_tokens_outline", "MAX_TOKENS_OUTLINE", 16384),
            temperature=temperature())
        found = parse_outline_text(text)
        if not found:
            save_artifact(work, None, "log", "outline_failed.txt", text)
            raise RuntimeError("概要のパースに失敗しました（自動リトライ1回でも読み取れず。モデルの生出力を成果物のログに保存しました）")
    if (work.get("mode") or "switch") == "single":
        found = [(1, found[0][1], found[0][2])]  # 単章モードは最初の1章だけに絞る
    db.run("DELETE FROM chapters WHERE work_id=?", (work["id"],))
    for num, title, body in found:
        db.run("INSERT INTO chapters(work_id, number, title, outline, status) VALUES(?,?,?,?,'outlined')",
               (work["id"], int(num), title.strip(), body.strip()))
    set_stage(work["id"], "outline_review")
    notify(f"【小説ハーネス】「{work['title']}」の各章概要（次章の内容）ができました。確認と修正をお願いします。")


# ---------- 章の展開計画（全体章概要の後に章ごとに展開する） ----------
PLAN_SECTIONS = ["詳細概要", "章開始時の状態", "章終了時の目標状態", "章内容詳細", "章終了時の状態", "次章開始状態", "終了状態"]


def parse_plan(text):
    sections = {}
    for p in re.split(r"^##\s*", text or "", flags=re.M):
        if not p.strip():
            continue
        head, _, body = p.partition("\n")
        head = head.strip()
        if head in PLAN_SECTIONS:
            sections[head] = body.strip()
    return sections


def plan_to_text(sections):
    return "\n\n".join(f"## {name}\n{sections[name]}"
                       for name in PLAN_SECTIONS if sections.get(name))


def get_plan(work, ch):
    a = latest_artifact(work["id"], ch["id"], "plan")
    return parse_plan(read_text(a["path"])) if a else None


def plan_start_state(work, ch, chapters):
    """展開時の章開始状態：前章の計画の「次章開始状態」を最優先で引き継ぐ。"""
    if ch["number"] > 1:
        prev = next((c for c in chapters if c["number"] == ch["number"] - 1), None)
        if prev:
            pp = get_plan(work, prev)
            if pp and pp.get("次章開始状態"):
                return pp["次章開始状態"]
    return current_state(work, ch["number"]) or "物語の開始時点。キャラクタは初期状態にある。"


def _save_plan(work, ch, sections):
    save_artifact(work, ch, "plan", f"ch{ch['number']:03d}_plan.txt", plan_to_text(sections))
    if sections.get("章終了時の状態"):
        # 状態チェーンにも流し、本文生成の開始状態として使えるようにする
        save_artifact(work, ch, "state", f"ch{ch['number']:03d}_state.txt",
                      sections["章終了時の状態"])


def _final_note(work, ch, chapters):
    return ("この章は物語の最終章です。「終了状態」の見出しも付けてください。"
            if ch["number"] == max(c["number"] for c in chapters) else
            "この章は最終章ではありません。「終了状態」の見出しは付けないでください。")


def do_expand_plans(job, work):
    """各章の展開：詳細概要・開始状態・内容詳細・終了状態・次章開始状態（最終章は終了状態）を章ごとに連鎖生成する。

    payload.from_number があれば、その章以降だけを再生成する（カスケード用）。
    開始状態は前章の「次章開始状態」を引き継ぐため、必ず章順に処理する。
    """
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    from_number = int(payload.get("from_number") or 1)
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work["id"],))
    if not chapters:
        raise RuntimeError("先に各章概要を生成してください")
    for ch in chapters:
        if ch["number"] < from_number:
            continue
        if _cancelled(job):
            return
        text = llm.chat(
            "auto", prompts.PLAN_EXPAND_SYSTEM + sys_note(work),
            prompts.PLAN_EXPAND_USER.format(
                settings=settings_text(work), outlines=outlines_text(work),
                number=ch["number"], title=ch["title"], outline=ch["outline"],
                start_state=plan_start_state(work, ch, chapters),
                final_note=_final_note(work, ch, chapters), notes_block=""),
            max_tokens=max_tokens("max_tokens_outline", "MAX_TOKENS_OUTLINE", 16384),
            temperature=temperature())
        sections = parse_plan(text)
        if not sections.get("章内容詳細") or not sections.get("章終了時の状態"):
            save_artifact(work, ch, "log", f"ch{ch['number']:03d}_plan_failed.txt", text)
            raise RuntimeError(f"第{ch['number']}章の展開計画のパースに失敗しました（モデルの生出力を成果物のログに保存しました）")
        _save_plan(work, ch, sections)
    if payload.get("cascade"):
        db.set_setting(f"plan_pending:{work['id']}", "")
    set_stage(work["id"], "plans_ready")
    add_notice(work, "各章の展開計画を作成しました。章ごとに確認し、追加指定があれば当該章を再生成してください。")


def do_regen_plan(job, work):
    """追加指定による当該章の計画再生成：詳細概要と開始状態は固定し、内容詳細以降を作り直す。

    完了後は plan_pending に章番号を残し、作品ページで「OKなら次章以降を再生成」
    （カスケード）を押せるようにする。
    """
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    ch = db.one("SELECT * FROM chapters WHERE id=?", (payload["chapter_id"],))
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work["id"],))
    sections = get_plan(work, ch)
    if not sections:
        raise RuntimeError("先に「各章を展開」を実行してください")
    notes = (payload.get("notes") or "").strip()
    text = llm.chat(
        "auto", prompts.PLAN_REGEN_SYSTEM + sys_note(work),
        prompts.PLAN_REGEN_USER.format(
            settings=settings_text(work),
            number=ch["number"], title=ch["title"],
            detail=sections.get("詳細概要", ""),
            start_state=sections.get("章開始時の状態", ""),
            notes=notes or "（追加指定なし：内容詳細以降を練り直す）",
            final_note=_final_note(work, ch, chapters)),
        max_tokens=max_tokens("max_tokens_outline", "MAX_TOKENS_OUTLINE", 16384),
        temperature=temperature())
    new = parse_plan(text)
    if not new.get("章内容詳細") or not new.get("章終了時の状態"):
        save_artifact(work, ch, "log", f"ch{ch['number']:03d}_plan_failed.txt", text)
        raise RuntimeError("再生成した計画のパースに失敗しました（モデルの生出力を成果物のログに保存しました）")
    for k in ("章内容詳細", "章終了時の状態", "次章開始状態", "終了状態"):
        if new.get(k):
            sections[k] = new[k]
    _save_plan(work, ch, sections)
    db.set_setting(f"plan_pending:{work['id']}", str(ch["number"]))
    add_notice(work, f"第{ch['number']}章の展開計画を再生成しました。内容がOKなら「第{ch['number'] + 1}章以降を再生成」を押してください（開始状態が変わるため次章以降の計画が古くなっています）。")


def plan_instruction(work, ch):
    """本文生成の指示書へ差し込む展開計画テキスト（計画があれば）。"""
    sections = get_plan(work, ch)
    return ("\n\n【章の展開計画】\n" + plan_to_text(sections)) if sections else ""


def design_prompt(work, chapter, intent, use_tail=True):
    text = llm.chat(
        "auto", prompts.PROMPT_DESIGN_SYSTEM + sys_note(work),
        prompts.PROMPT_DESIGN_USER.format(
            settings=settings_text(work),
            state=current_state(work, chapter["number"]),
            summaries=summaries_text(work, chapter["number"]),
            prev_tail=prev_tail(work, chapter["number"]) if use_tail else "",
            source="", intent=intent),
        max_tokens=design_max_tokens(),
        temperature=temperature())
    save_artifact(work, chapter, "prompt", f"ch{chapter['number']:03d}.txt", text)
    return text


def do_generate_chapters(job, work):
    """1次出力：現在ロード中のモデルで各章本文を生成する（API準拠）。"""
    try:
        run_term_check(work)
    except Exception:
        pass
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? AND status IN ('outlined','generated') ORDER BY number",
                    (work["id"],))
    for ch in chapters:
        if _cancelled(job):
            return
        if ch["status"] == "generated":
            continue
        set_chapter_status(ch["id"], "generating")
        pw = progress_writer(work, ch, "1次出力")
        try:
            instruction = design_prompt(work, ch, work["request"]) + plan_instruction(work, ch)
            used_model = llm.current_model()
            prev_state = current_state(work, ch["number"])
            body = llm.chat(
                "auto", prompts.CHAPTER_SYSTEM + sys_note(work),
                prompts.CHAPTER_USER.format(
                    instruction=instruction, settings=settings_text(work),
                    state=prev_state,
                    outlines=outlines_text(work),
                    summaries=summaries_text(work, ch["number"]),
                    prev_tail=prev_tail(work, ch["number"]),
                    number=ch["number"], title=ch["title"], outline=ch["outline"],
                    min_chars_note=min_chars_note(work)),
                max_tokens=body_max_tokens(),
                temperature=temperature(), progress=pw)
            save_think(work, ch)
            body = ensure_min_chars(job, work, ch, body, progress=pw)
            save_artifact(work, ch, "draft", f"ch{ch['number']:03d}.txt", body)
            clear_partial(work, ch)
            db.set_setting(f"work:{work['id']}:draft_model", used_model)
            update_state(work, ch, body, prev_state)
            summary = llm.chat(
                "auto", prompts.SUMMARY_SYSTEM,
                prompts.SUMMARY_USER.format(number=ch["number"], title=ch["title"], text=body),
                max_tokens=max_tokens("max_tokens_summary", "MAX_TOKENS_SUMMARY", 4096),
                temperature=temperature())
            save_artifact(work, ch, "summary", f"ch{ch['number']:03d}.txt", summary)
            set_chapter_status(ch["id"], "generated")
        except llm.LoopDetectedError as e:
            save_loop_log(work, ch, "1次出力", e)
            clear_partial(work, ch)
            set_chapter_status(ch["id"], "outlined")
            raise
        except llm.JobCancelledError:
            clear_partial(work, ch)
            set_chapter_status(ch["id"], "outlined")
            raise
        except Exception:
            clear_partial(work, ch)
            raise
    set_stage(work["id"], "wait_model")
    notify(f"【小説ハーネス】「{work['title']}」の1次出力が終了しました（使用モデル：{llm.loaded_model()}）。"
           "2次修正に使うモデルへ切り替えて下さい。")


def revise_chapters(job, work, chapters, model_changed=False):
    """与えられた章を現在ロード中のモデルで改修し、final にする。

    model_changed=True（切替モードでモデルが変わった後の2次修正）のときは、
    2次用 Context Window を使って文脈量と Max Tokens を再配置する。
    モデルが変わらない場合（スレッドモード等）は通常の Context Window の配分を使う。
    """
    eff_ctx = ctx_window_revise() if model_changed else None
    for ch in chapters:
        if _cancelled(job):
            return False
        set_chapter_status(ch["id"], "revising")
        draft = latest_artifact(work["id"], ch["id"], "draft")
        body = read_text(draft["path"]) if draft else ""
        pw = progress_writer(work, ch, "2次修正")
        try:
            revised = llm.chat(
                "auto", prompts.REVISE_SYSTEM + sys_note(work),
                prompts.REVISE_USER.format(
                    settings=settings_text(work, ctx=eff_ctx), outlines=outlines_text(work),
                    number=ch["number"], title=ch["title"], text=body),
                max_tokens=revise_max_tokens(model_changed),
                temperature=temperature(), progress=pw,
                window=eff_ctx if model_changed else ctx_window())
            clear_partial(work, ch)
        except llm.LoopDetectedError as e:
            save_loop_log(work, ch, "2次修正", e)
            clear_partial(work, ch)
            set_chapter_status(ch["id"], "generated")
            raise
        except llm.JobCancelledError:
            clear_partial(work, ch)
            set_chapter_status(ch["id"], "generated")
            raise
        except Exception:
            clear_partial(work, ch)
            raise
        target = work_min_chars(work)
        if target > 0 and len(revised) < target:
            msg = (f"第{ch['number']}章の2次修正後の本文が最低{target}文字を下回りました"
                   f"（修正後{len(revised)}文字／1次出力{len(body)}文字）。"
                   "校正で短くなった可能性があります。章ページで文字数を確認してください。")
            add_notice(work, msg)
            notify(f"【小説ハーネス】「{work['title']}」{msg}")
        save_artifact(work, ch, "revised", f"ch{ch['number']:03d}.txt", revised)
        save_artifact(work, ch, "final", f"ch{ch['number']:03d}.txt", revised)
        set_chapter_status(ch["id"], "final")
    return True


def do_generate_thread(job, work):
    """スレッドモード：モデル切替なし。章ごとに独立したコンテクスト（スレッド）で進行する。

    各章のスレッドには、アップロードしたTXT・設定資料・全章概要・前章までの要約・その章の概要だけを入れる。
    他章の本文そのものは入れない（コンテクスト長節約）。生成→要約→同モデルで仕上げ修正まで一章ずつ完結させる。
    """
    try:
        run_term_check(work)
    except Exception:
        pass
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? AND status IN ('outlined','generated') ORDER BY number",
                    (work["id"],))
    for ch in chapters:
        if _cancelled(job):
            return
        if ch["status"] != "generated":
            set_chapter_status(ch["id"], "generating")
            pw = progress_writer(work, ch, "生成")
            try:
                instruction = design_prompt(work, ch, work["request"], use_tail=False) + plan_instruction(work, ch)
                used_model = llm.current_model()
                prev_state = current_state(work, ch["number"])
                body = llm.chat(
                    "auto", prompts.CHAPTER_SYSTEM + sys_note(work),
                    prompts.CHAPTER_USER.format(
                        instruction=instruction, settings=settings_text(work),
                        state=prev_state,
                        outlines=outlines_text(work),
                        summaries=summaries_text(work, ch["number"]),
                        prev_tail="",
                        number=ch["number"], title=ch["title"], outline=ch["outline"],
                        min_chars_note=min_chars_note(work)),
                    max_tokens=body_max_tokens(),
                    temperature=temperature(), progress=pw)
                save_think(work, ch)
                body = ensure_min_chars(job, work, ch, body, progress=pw)
                save_artifact(work, ch, "draft", f"ch{ch['number']:03d}.txt", body)
                clear_partial(work, ch)
                db.set_setting(f"work:{work['id']}:draft_model", used_model)
                update_state(work, ch, body, prev_state)
                summary = llm.chat(
                    "auto", prompts.SUMMARY_SYSTEM,
                    prompts.SUMMARY_USER.format(number=ch["number"], title=ch["title"], text=body),
                    max_tokens=max_tokens("max_tokens_summary", "MAX_TOKENS_SUMMARY", 4096),
                    temperature=temperature())
                save_artifact(work, ch, "summary", f"ch{ch['number']:03d}.txt", summary)
                set_chapter_status(ch["id"], "generated")
            except llm.LoopDetectedError as e:
                save_loop_log(work, ch, "スレッド生成", e)
                clear_partial(work, ch)
                set_chapter_status(ch["id"], "outlined")
                raise
            except llm.JobCancelledError:
                clear_partial(work, ch)
                set_chapter_status(ch["id"], "outlined")
                raise
            except Exception:
                clear_partial(work, ch)
                raise
            ch = dict(ch, status="generated")
        if not revise_chapters(job, work, [ch]):
            return
    set_stage(work["id"], "done")
    db.set_setting(f"work:{work['id']}:revise_model", str(llm.loaded_model() or ""))
    notify(f"【小説ハーネス】「{work['title']}」がスレッドモードで完成しました（使用モデル：{llm.loaded_model()}、モデル切替なし）。"
           "出力ページからダウンロードできます。")


def do_revise_all(job, work):
    """2次修正：モデルが切り替わったことを API で検知してから改修する。切替待ちは無期限。

    スレッドモードの作品では切替を待たず、現在のモデルのまま即時実行する。
    """
    if work_mode(work) == "thread":
        set_stage(work["id"], "revising")
        chapters = db.q("SELECT * FROM chapters WHERE work_id=? AND status='generated' ORDER BY number", (work["id"],))
        if not revise_chapters(job, work, chapters):
            return
        set_stage(work["id"], "done")
        db.set_setting(f"work:{work['id']}:revise_model", str(llm.loaded_model() or ""))
        notify(f"【小説ハーネス】「{work['title']}」の仕上げ修正が完了しました（モデル切替なし）。")
        return
    set_stage(work["id"], "wait_model")
    from_model = db.get_setting(f"work:{work['id']}:draft_model", "")
    hint = revise_hint()
    notify(f"【小説ハーネス】「{work['title']}」2次修正の待機中です。"
           f"現在のモデル（{llm.loaded_model() or from_model or '不明'}）から、修正用モデルに切り替えて下さい。")

    def on_wait(elapsed):
        notify(f"【小説ハーネス】「{work['title']}」はモデル切替待ちです（{int(elapsed // 3600)}時間経過）。")

    if from_model:
        new_model = llm.wait_for_model_change(
            from_model, hint=hint or None,
            should_stop=lambda: _cancelled(job), on_wait=on_wait)
        if not new_model:
            return
    elif hint:
        if not llm.wait_for_model(hint, should_stop=lambda: _cancelled(job), on_wait=on_wait):
            return
        new_model = llm.loaded_model()
    else:
        new_model = llm.loaded_model()

    set_stage(work["id"], "revising")
    db.set_setting(f"work:{work['id']}:revise_model", str(new_model or ""))
    chapters = db.q("SELECT * FROM chapters WHERE work_id=? AND status='generated' ORDER BY number", (work["id"],))
    if not revise_chapters(job, work, chapters, model_changed=True):
        return
    set_stage(work["id"], "done")
    notify(f"【小説ハーネス】「{work['title']}」の2次修正が完了しました（使用モデル：{new_model}）。"
           "出力ページからダウンロードできます。")


def glossary_get(term):
    row = db.one("SELECT * FROM glossary WHERE term=?", (term,))
    return row["memo"] if row else None


def glossary_put(term, memo):
    db.run("INSERT INTO glossary(term, memo, created_at, updated_at) VALUES(?,?,?,?) "
           "ON CONFLICT(term) DO UPDATE SET memo=excluded.memo, updated_at=excluded.updated_at",
           (term, memo, db.now(), db.now()))


def resolve_term(work, term):
    """用語DBにあれば再検索せずそれを使い、なければWeb検索して用語DBに覚える。

    戻り値は (memo, 新規追加かどうか)。用語DBはワークスペースを消しても残る。
    """
    memo = glossary_get(term)
    if memo:
        return memo, False
    memo = search.lookup_word(term)
    glossary_put(term, memo)
    return memo, True


def add_notice(work, body):
    db.run("INSERT INTO notices(work_id, body, created_at) VALUES(?,?,?)",
           (work["id"], body, db.now()))


def checked_terms(work):
    try:
        return json.loads(db.get_setting(f"work:{work['id']}:checked_terms", "[]"))
    except ValueError:
        return []


def run_term_check(work):
    """モデルに知らない単語を指摘させ、用語DB→Web検索の順で解決する。"""
    if not web_search_on(work):
        return
    texts = [work["settings"] or "", work["request"] or ""]
    for kind in ("source", "analysis"):
        for a in db.q("SELECT * FROM artifacts WHERE work_id=? AND kind=? ORDER BY id", (work["id"], kind)):
            texts.append(read_text(a["path"]))
    for ch in db.q("SELECT * FROM chapters WHERE work_id=? ORDER BY number", (work["id"],)):
        texts.append(ch["outline"] or "")
        d = latest_artifact(work["id"], ch["id"], "draft")
        if d:
            texts.append(read_text(d["path"]))
    blob = "\n\n".join(t for t in texts if t)[:ctx_budget_chars()]
    if not blob.strip():
        return
    known = [r["term"] for r in db.q("SELECT term FROM glossary ORDER BY term")]
    checked = checked_terms(work)
    known_all = sorted(set(known) | set(checked))
    out = llm.chat(
        "auto", prompts.TERM_EXTRACT_SYSTEM,
        prompts.TERM_EXTRACT_USER.format(known="\n".join(known_all) or "（なし）", text=blob),
        max_tokens=1024, temperature=0.2)
    terms = []
    for line in out.splitlines():
        t = re.sub(r"^[・\-*\s　]+", "", line).strip().strip("「」")
        if t and t != "なし" and 1 <= len(t) <= 50 and t not in known_all:
            terms.append(t)
    terms = list(dict.fromkeys(terms))[:10]
    if not terms:
        return
    added, cached = [], []
    existing = {a["path"] for a in db.q(
        "SELECT path FROM artifacts WHERE work_id=? AND kind='glossary'", (work["id"],))}
    for t in terms:
        checked.append(t)
        try:
            memo, is_new = resolve_term(work, t)
        except Exception:
            continue
        fname = "glossary_" + re.sub(r"[^\w]+", "_", t)[:40] + ".txt"
        if not any(p.endswith(fname) for p in existing):
            save_artifact(work, None, "glossary", fname, memo)
        (added if is_new else cached).append(t)
    db.set_setting(f"work:{work['id']}:checked_terms", json.dumps(checked, ensure_ascii=False))
    if added:
        body = "知らない言葉があったので検索し、用語DBに追加しました：" + "、".join(added)
        add_notice(work, body)
        notify(f"【小説ハーネス】「{work['title']}」{body}")
    if cached:
        add_notice(work, "用語DBに既存のメモがあり、再検索せず使用しました：" + "、".join(cached))


def do_term_check(job, work):
    run_term_check(work)


def do_search_term(job, work):
    """Web検索：用語の意味、またはキャラクター概要を調べて資料化する。

    外部へ送るのは検索語（単語・キャラ名）だけ。原稿本文は送らない。
    """
    if not web_search_on(work):
        raise RuntimeError("Web検索がOFFになっています（全体設定、またはこのワークスペースのトグル）。")
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    mode = payload.get("mode", "word")
    term = (payload.get("term") or "").strip()
    if not term:
        raise RuntimeError("検索語が空です。")
    if mode == "character":
        if not is_fanfic(work):
            raise RuntimeError("キャラクター検索は二次創作ONのワークスペースで使えます。")
        origin = (payload.get("origin") or "").strip()
        material = search.gather_character_material(term, origin)
        profile = llm.chat(
            "auto", prompts.CHARACTER_PROFILE_SYSTEM.replace("{name}", term) + sys_note(work),
            prompts.CHARACTER_PROFILE_USER.format(
                label=f"{origin} {term}".strip(), material=material),
            max_tokens=max_tokens("max_tokens_design", "MAX_TOKENS_DESIGN", 8192),
            temperature=temperature())
        fname = "character_" + re.sub(r"[^\w]+", "_", term)[:40] + ".txt"
        save_artifact(work, None, "character", fname, profile)
        notify(f"【小説ハーネス】「{work['title']}」のキャラクター概要（{term}）を作成しました。")
    else:
        memo, is_new = resolve_term(work, term)
        fname = "glossary_" + re.sub(r"[^\w]+", "_", term)[:40] + ".txt"
        save_artifact(work, None, "glossary", fname, memo)
        if is_new:
            add_notice(work, f"知らない言葉があったので検索し、用語DBに追加しました：{term}")
        notify(f"【小説ハーネス】「{work['title']}」の用語メモ（{term}）を作成しました。")


def do_regen_chapter(job, work):
    """章ページの「修正要綱」からの再生成：要綱を生成指示に含めて、その章だけを作り直す。

    入力状態（前章の終了状態／初期状態）は変えずに本文を作り直し、終了状態と要約も
    新しい本文に合わせて更新する。2次修正済みだった場合は revised/final が古くなるので消す。
    """
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    ch = db.one("SELECT * FROM chapters WHERE id=? AND work_id=?",
                (payload.get("chapter_id"), work["id"]))
    if not ch:
        raise RuntimeError("再生成する章が見つかりません。")
    notes = (payload.get("notes") or "").strip()
    prev_status = ch["status"]
    set_chapter_status(ch["id"], "generating")
    rev_outline = ""
    if payload.get("use_rev_outline"):
        rev_art = latest_artifact(work["id"], ch["id"], "rev_outline")
        if rev_art:
            rev_outline = read_text(rev_art["path"]).strip()
    if rev_outline:
        # 修正概要フロー：生成指示書も修正概要に合わせて作り直し（別スレッド実行）、
        # 本文は修正概要に従って生成する。正規の章概要（chapters.outline）は変えない。
        intent = (work["request"] or "")
        if notes:
            intent = (intent + f"\n\n【修正要綱】\n{notes}").strip()
        base_instruction = design_prompt(work, ch, intent)
        instruction = (base_instruction + plan_instruction(work, ch)
                       + "\n\n【重要】この章は下記の「修正概要」に従って書いてください。"
                         "全章概要にある同章の記述より修正概要を優先します。"
                       + f"\n\n【修正概要】\n{rev_outline}")
        if notes:
            instruction = (instruction + f"\n\n【修正要綱】\n{notes}").strip()
    else:
        design = latest_artifact(work["id"], ch["id"], "prompt")
        base_instruction = read_text(design["path"]) if design else ""
        instruction = base_instruction + plan_instruction(work, ch)
        if notes:
            instruction = (instruction + f"\n\n【修正要綱】\n{notes}").strip()
    prev_state = current_state(work, ch["number"])
    pw = progress_writer(work, ch, "再生成")
    try:
        body = llm.chat(
            "auto", prompts.CHAPTER_SYSTEM + sys_note(work),
            prompts.CHAPTER_USER.format(
                instruction=instruction, settings=settings_text(work),
                state=prev_state,
                outlines=outlines_text(work),
                summaries=summaries_text(work, ch["number"]),
                prev_tail=prev_tail(work, ch["number"]) if work_mode(work) != "thread" else "",
                number=ch["number"], title=ch["title"],
                outline=rev_outline or ch["outline"],
                min_chars_note=min_chars_note(work)),
            max_tokens=body_max_tokens(),
            temperature=temperature(), progress=pw)
        save_think(work, ch)
        body = ensure_min_chars(job, work, ch, body, progress=pw)
        save_artifact(work, ch, "draft", f"ch{ch['number']:03d}.txt", body)
        clear_partial(work, ch)
        for kind in ("revised", "final"):
            for a in db.q("SELECT * FROM artifacts WHERE work_id=? AND chapter_id=? AND kind=?",
                          (work["id"], ch["id"], kind)):
                try:
                    os.remove(a["path"])
                except OSError:
                    pass
                db.run("DELETE FROM artifacts WHERE id=?", (a["id"],))
        update_state(work, ch, body, prev_state)
        summary = llm.chat(
            "auto", prompts.SUMMARY_SYSTEM,
            prompts.SUMMARY_USER.format(number=ch["number"], title=ch["title"], text=body),
            max_tokens=max_tokens("max_tokens_summary", "MAX_TOKENS_SUMMARY", 4096),
            temperature=temperature())
        save_artifact(work, ch, "summary", f"ch{ch['number']:03d}.txt", summary)
    except llm.LoopDetectedError as e:
        save_loop_log(work, ch, "章の再生成", e)
        clear_partial(work, ch)
        set_chapter_status(ch["id"], prev_status)
        raise
    except llm.JobCancelledError:
        clear_partial(work, ch)
        set_chapter_status(ch["id"], prev_status)
        raise
    except Exception:
        clear_partial(work, ch)
        raise
    set_chapter_status(ch["id"], "generated")
    notify(f"【小説ハーネス】「{work['title']}」第{ch['number']}章を修正要綱つきで再生成しました。")


def do_revise_outline(job, work):
    """修正要綱フロー第1段：修正要綱に沿って、その章の「修正概要」を作り直す。

    正規の章概要（chapters.outline）は上書きせず、成果物 kind='rev_outline' に
    保存する。章ページで内容を確認・編集し、OKなら第2段（本文の再生成）へ進む。
    使った修正要綱は設定 rev_outline_notes:<章ID> に残し、画面に併記する。
    """
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    ch = db.one("SELECT * FROM chapters WHERE id=? AND work_id=?",
                (payload.get("chapter_id"), work["id"]))
    if not ch:
        raise RuntimeError("修正概要を作る章が見つかりません。")
    notes = (payload.get("notes") or "").strip()
    if not notes:
        raise RuntimeError("修正要綱が空です。直したい点を入力してください。")
    plan_secs = get_plan(work, ch)
    plan_text = plan_to_text(plan_secs) if plan_secs else "（なし）"
    try:
        text = llm.chat(
            "auto", prompts.REV_OUTLINE_SYSTEM + sys_note(work),
            prompts.REV_OUTLINE_USER.format(
                request=work["request"], settings=settings_text(work),
                outlines=outlines_text(work),
                state=current_state(work, ch["number"]),
                number=ch["number"], title=ch["title"], outline=ch["outline"],
                plan=plan_text, notes=notes),
            max_tokens=max_tokens("max_tokens_outline", "MAX_TOKENS_OUTLINE", 16384),
            temperature=temperature())
    except llm.LoopDetectedError as e:
        save_loop_log(work, ch, "修正概要の作成", e)
        raise
    if not text.strip():
        raise RuntimeError("修正概要の生成結果が空でした。もう一度お試しください。")
    save_artifact(work, ch, "rev_outline", f"ch{ch['number']:03d}_rev_outline.txt", text)
    db.set_setting(f"rev_outline_notes:{ch['id']}", notes)
    notify(f"【小説ハーネス】「{work['title']}」第{ch['number']}章の修正概要ができました。"
           "章ページで確認し、よければ本文を再生成してください。")


def do_outline_chapter(job, work):
    """章ごとの概要再生成：指定した1章の概要だけを作り直して chapters を更新する。

    全体生成の概要が途中で切れて1章だけ壊れた場合などの救済用。前後の章との
    つながりは現状の全章概要を渡して合わせる。パースできない場合は生出力を
    章のログ成果物に残してエラーにする（全体概要生成と同じ方針）。
    """
    try:
        payload = json.loads(job.get("payload") or "{}")
    except ValueError:
        payload = {}
    ch = db.one("SELECT * FROM chapters WHERE id=? AND work_id=?",
                (payload.get("chapter_id"), work["id"]))
    if not ch:
        raise RuntimeError("概要を再生成する章が見つかりません。")
    user = prompts.OUTLINE_ONE_USER.format(
        request=work["request"], settings=settings_text(work),
        outlines=outlines_text(work), number=ch["number"])
    text = llm.chat(
        "auto", prompts.OUTLINE_SYSTEM + sys_note(work), user,
        max_tokens=max_tokens("max_tokens_outline", "MAX_TOKENS_OUTLINE", 16384),
        temperature=temperature())
    found = parse_outline_text(text)
    picked = None
    for num, title, body in found:
        if int(num) == int(ch["number"]):
            picked = (title, body)
            break
    if picked is None and found:
        picked = (found[0][1], found[0][2])
    if picked is None:
        save_artifact(work, ch, "log", f"ch{ch['number']:03d}_outline_failed.txt", text)
        raise RuntimeError("概要のパースに失敗しました（モデルの生出力を章の成果物ログに保存しました）")
    db.run("UPDATE chapters SET title=?, outline=? WHERE id=?",
           (picked[0].strip(), picked[1].strip(), ch["id"]))
    notify(f"【小説ハーネス】「{work['title']}」第{ch['number']}章の概要を再生成しました。")


HANDLERS = {"analyze": do_analyze, "outline": do_outline,
            "generate_chapters": do_generate_chapters, "revise_all": do_revise_all,
            "search_term": do_search_term, "term_check": do_term_check,
            "generate_thread": do_generate_thread, "regen_chapter": do_regen_chapter,
            "expand_plans": do_expand_plans, "regen_plan": do_regen_plan,
            "revise_outline": do_revise_outline, "outline_chapter": do_outline_chapter}


def _cancelled(job):
    row = db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
    return not row or row["status"] == "cancelled"


def _usage_sink_for(job_id):
    """そのジョブ中の LLM 呼び出しの usage を jobs.usage に累計するコールバック。"""
    def sink(rec):
        row = db.one("SELECT usage FROM jobs WHERE id=?", (job_id,))
        try:
            acc = json.loads(row["usage"] or "{}") if row else {}
        except ValueError:
            acc = {}
        acc["calls"] = acc.get("calls", 0) + 1
        for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            v = rec.get(k)
            if isinstance(v, int):
                acc[k] = acc.get(k, 0) + v if isinstance(acc.get(k), int) else v
                acc["last_" + k] = v
        v = rec.get("completion_tokens")
        if isinstance(v, int):
            acc["peak_completion_tokens"] = max(acc.get("peak_completion_tokens", 0), v)
        acc["last_max_tokens"] = rec.get("max_tokens")
        if rec.get("model"):
            acc["model"] = rec["model"]
        db.run("UPDATE jobs SET usage=? WHERE id=?",
               (json.dumps(acc, ensure_ascii=False), job_id))
        # サイドバー表示用：直近の呼び出しのコンテキスト量と処理速度を全体にも残す
        try:
            if isinstance(rec.get("prompt_tokens"), int):
                db.set_setting("live:prompt_tokens", str(rec["prompt_tokens"]))
            if isinstance(rec.get("completion_tokens"), int):
                db.set_setting("live:completion_tokens", str(rec["completion_tokens"]))
            if isinstance(rec.get("prefill_tps"), (int, float)):
                db.set_setting("live:prefill_tps", f"{rec['prefill_tps']:.1f}")
            if isinstance(rec.get("decode_tps"), (int, float)):
                db.set_setting("live:decode_tps", f"{rec['decode_tps']:.1f}")
            if rec.get("model"):
                db.set_setting("live:model", str(rec["model"]))
            db.set_setting("live:call_at", str(time.time()))
        except Exception:
            pass
    return sink


def worker_loop():
    while True:
        try:
            job = db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not job:
                time.sleep(2)
                continue
            db.run("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (db.now(), job["id"]))
            work = db.one("SELECT * FROM works WHERE id=?", (job["work_id"],))
            llm.USAGE_SINK = _usage_sink_for(job["id"])
            llm.CANCEL_CHECK = (lambda jid: lambda: _cancelled({"id": jid}))(job["id"])
            try:
                HANDLERS[job["kind"]](job, work)
                if not _cancelled(job):
                    db.run("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (db.now(), job["id"]))
            except llm.JobCancelledError:
                # ユーザーの停止：生成中の接続は llm 側で切断済み。failed にはしない。
                db.run("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=? AND status='running'",
                       (db.now(), job["id"]))
                if work:
                    set_stage(work["id"], "cancelled")
                    add_notice(work, "工程が停止ボタンで中断されました。途中まで生成された章は再実行できます。")
            except Exception as e:
                traceback.print_exc()
                db.run("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                       (str(e)[:500], db.now(), job["id"]))
                if work:
                    if isinstance(e, llm.LoopDetectedError) and not e.saved_log:
                        # 章に紐づかない工程（概要・解析・状態・要約など）でループした場合は
                        # 作品レベルの異常終了ログを成果物に残す
                        try:
                            save_loop_log(work, None, job["kind"], e)
                        except Exception:
                            traceback.print_exc()
                    set_stage(work["id"], "failed")
                    notify(f"【小説ハーネス】「{work['title']}」の工程でエラーが発生しました：{str(e)[:200]}")
            finally:
                llm.USAGE_SINK = None
                llm.CANCEL_CHECK = None
        except Exception:
            # ワーカースレッド自体は絶対に落とさない（ジョブが running のまま固まる事故を防ぐ）
            traceback.print_exc()
            time.sleep(2)


def recover_stale_jobs():
    """起動時の回収：前プロセスで running のまま残ったジョブを failed に戻す。

    コンテナ再起動で中断されたジョブが running 表示のまま固まるのを防ぐ。
    途中状態のまま実行中ジョブが存在しない作品の stage も failed に戻す。
    """
    for j in db.q("SELECT * FROM jobs WHERE status='running'"):
        db.run("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
               ("コンテナ再起動により中断しました。工程を再実行してください。", db.now(), j["id"]))
    for w in db.q("SELECT * FROM works WHERE stage IN ('analyzing','outlining','generating','revising','wait_model')"):
        active = db.one("SELECT id FROM jobs WHERE work_id=? AND status IN ('queued','running') LIMIT 1",
                        (w["id"],))
        if not active:
            set_stage(w["id"], "failed")


def start_worker():
    recover_stale_jobs()
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
