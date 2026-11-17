"""OpenAI互換API（XeStrata）への薄いクライアント。MOCK_LLM=1 で疑似応答に切り替わる。

接続先URLとAPIキーは Web UI の設定画面（DB）優先、環境変数は初期値として使う。
GUI からコンテナを起動しただけでも、後から画面で設定できるようにするため。

生成は原則ストリーミングで受け、本文・thinking の繰り返しループを生成中に
検出したら即座に接続を切って LoopDetectedError で異常終了する（v1.13〜）。
ストリーミング非対応のサーバーでは従来の一括受信に自動でフォールバックし、
そちらでも受信後に同じループ検査を行う。
"""
import json
import os
import re
import time

import httpx

ENV_BASE_URL = os.environ.get("STRATA_BASE_URL", "")
ENV_API_KEY = os.environ.get("STRATA_API_KEY", "")
MOCK = os.environ.get("MOCK_LLM", "0") == "1"


def _setting(key):
    try:
        import db
        return db.get_setting(key, "")
    except Exception:
        return ""


def base_url():
    return (_setting("strata_base_url") or ENV_BASE_URL).rstrip("/")


def api_key():
    return _setting("strata_api_key") or ENV_API_KEY


def _headers():
    h = {"Content-Type": "application/json"}
    k = api_key()
    if k:
        h["Authorization"] = f"Bearer {k}"
    return h


# ジョブ実行中、pipeline がここに集計用コールバックを設定する（usage の記録用）。
USAGE_SINK = None

# ジョブ実行中、pipeline がここに「取消要求があるか」を返すコールバックを設定する。
# chat() は生成中もこれを定期的に確認し、取消されたら接続を切って
# JobCancelledError で中断する（従来は工程の合間でしか取消が効かなかった）。
CANCEL_CHECK = None


class JobCancelledError(RuntimeError):
    """ユーザーの停止（ジョブ取消）により生成を中断した。"""


def _cancel_requested():
    if not CANCEL_CHECK:
        return False
    try:
        return bool(CANCEL_CHECK())
    except Exception:
        return False

# 直近の chat() 呼び出しでモデルが返した thinking（思考過程）。
# XeStrata は message.reasoning_content に入れて返す。本文とは分けて保持する。
LAST_THINKING = ""


class LoopDetectedError(RuntimeError):
    """生成テキスト（本文または thinking）が繰り返しループに入ったため異常終了する。

    where: "本文" / "thinking"、unit: 繰り返し単位の抜粋、repeats: 推定繰り返し回数、
    chars: 検出時点の累積文字数、content_text / think_text: 検出時点の全文（ログ用）。
    saved_log: pipeline 側でログ成果物を保存済みなら True（二重保存防止）。
    """

    def __init__(self, where, info, content_text, think_text):
        self.where = where
        self.unit = info.get("unit", "")
        self.repeats = info.get("repeats", 0)
        self.chars = info.get("chars", 0)
        self.content_text = content_text or ""
        self.think_text = think_text or ""
        self.saved_log = False
        unit_disp = self.unit.replace("\n", " ")[:40]
        super().__init__(
            f"生成ループを検出したため異常終了しました（対象：{where}、"
            f"繰り返し単位「{unit_disp}…」が約{self.repeats}回、{self.chars}文字時点で検出）")


def _is_property_line(s):
    """「- 所持品: 特記なし」のようなキー：値の項目行かどうか。

    状態シート等では人物ごとに同じ項目行が正当に繰り返されるため、
    ループ判定（ルールA・C）の対象から外す（2026-11-18 の誤検出対策）。
    """
    s = s.strip()
    if re.match(r"^[-・*●]\s*[^:：。！？]{1,12}[:：]", s):
        return True
    return len(s) <= 24 and re.match(r"^[^:：。！？]{1,10}[:：]", s) is not None


def detect_loop(text):
    """テキスト末尾の繰り返しループを検出する。検出したら dict、なければ None。

    判定は保守的（通常の小説本文・思考ではまず発火しない強さ）：
    A) 同じ文（10字以上）が6回以上連続する
    B) 末尾が周期 p（8〜600字）の繰り返しで、800字以上の区間がその周期で一致する
       （繰り返し回数4回以上に相当する長さがある場合のみ）
    C) 短いフレーズ（10字以上）が直近4000字以内に5回以上再出現する
       （連続していなくてもよい。会話のたびに同じ台詞が戻るタイプのループ対策）
    D) 数字だけが違う行が6行以上連続する（「931 30」「932 30」のような
       カウンタ型の数値ループ。数字が増えていくため完全一致にならず、
       ルールA〜Cをすり抜ける。数字を伏せた形が同じ行の連続で判定する）
    ただし「- 所持品: 特記なし」のような項目行は、状態シートで人物ごとに
    繰り返されるのが正常なため、ルールA・C・Dの数え上げから除外する。
    """
    if not text:
        return None
    # D) 数字だけが違う行の連続（短いテキストでも判定できるため長さゲートの前に行う）
    run_key, run = None, 0
    for ln in text[-4000:].split("\n"):
        s = ln.strip()
        if (not s or _is_property_line(s)
                or not re.search(r"[0-9０-９]", s)):
            run_key, run = None, 0
            continue
        key = re.sub(r"[0-9０-９]+", "#", s)
        if len(key) < 3:
            run_key, run = None, 0
            continue
        run = run + 1 if key == run_key else 1
        run_key = key
        if run >= 6:
            return {"rule": "D", "unit": key, "repeats": run, "chars": len(text)}
    if len(text) < 500:
        return None
    tail = text[-4000:]
    # A) 文単位の連続繰り返し
    units = [u.strip() for u in re.split(r"(?<=[。！？])|\n", tail)]
    units = [u for u in units if len(u) >= 10 and not _is_property_line(u)]
    run, prev = 0, None
    for u in units:
        run = run + 1 if u == prev else 1
        prev = u
        if run >= 6:
            return {"unit": u, "repeats": run, "chars": len(text)}
    # C) 短いフレーズの高頻度再出現（鍵括弧内も区切りに含める）
    counts = {}
    for s in re.split(r"[。！？\n「」『』]", tail):
        s = s.strip()
        if len(s) >= 10 and not _is_property_line(s):
            counts[s] = counts.get(s, 0) + 1
    for s, c in counts.items():
        if c >= 5:
            return {"unit": s, "repeats": c, "chars": len(text)}
    # B) 周期的な繰り返し（末尾区間が p 字シフトした自分自身と一致）
    n = len(text)
    for length in (2400, 1600, 800):
        if n <= length:
            continue
        seg = text[-length:]
        for p in range(8, 601):
            if length < p * 4 or n - length - p < 0:
                continue
            if seg == text[-length - p:-p]:
                return {"unit": text[-p:], "repeats": max(2, length // p),
                        "chars": len(text)}
    return None


class _StreamUnsupported(Exception):
    """ストリーミング受信が使えない（一括受信にフォールバックする）。"""


def _split_thinking(message):
    """message から (本文, thinking) を取り出す。

    reasoning_content フィールドがあればそれを thinking とし、なければ
    content 内の <think>...</think> を分離する（サーバーの返し方の違いを吸収）。
    """
    content = message.get("content") or ""
    think = message.get("reasoning_content") or message.get("reasoning") or ""
    if not think and "<think>" in content:
        import re as _re
        m = _re.search(r"<think>(.*?)</think>", content, _re.S)
        if m:
            think = m.group(1).strip()
            content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.S).strip()
    return content, think


def reasoning_budget():
    """設定画面の思考予算（トークン）。0 ならサーバー任せ（指定なし）。"""
    try:
        return int(_setting("reasoning_budget_tokens") or 0)
    except ValueError:
        return 0


def _report_usage(j, max_tokens, req_chars=0):
    """API応答の usage を取り出して USAGE_SINK に渡す。

    thinking（reasoning）トークンは、サーバーが usage に内訳を含めて返す場合だけ分かる。
    返らないサーバーでは reasoning_tokens=None になり、表示側は出力トークン総数で代替する。
    ストリーミング応答に usage / timings が含まれないサーバー（XeStrata 等）では、
    サイドバーの Fill・速度が永遠に更新されなくなるため、リクエスト文字数と
    受信チャンク数から推定して埋める（rec["estimated"]=True で区別する）。
    """
    if not USAGE_SINK:
        return
    u = j.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    rt = details.get("reasoning_tokens", u.get("reasoning_tokens"))
    # llama.cpp 系サーバーが返す処理速度（プリフィル／デコード）。無いサーバーでは None。
    tm = j.get("timings") or {}
    pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
    pre = tm.get("prompt_per_second") if isinstance(tm.get("prompt_per_second"), (int, float)) else None
    dec = tm.get("predicted_per_second") if isinstance(tm.get("predicted_per_second"), (int, float)) else None
    estimated = False
    stream = j.get("_stream") or {}
    if not isinstance(pt, int) and req_chars:
        pt = round(req_chars / 1.5)  # 日本語は概ね 1.5字/トークンで推定
        estimated = True
    if not isinstance(ct, int):
        if stream.get("chunks"):
            ct = int(stream["chunks"])  # 1チャンク≒1トークンとして数える
            estimated = True
        else:
            try:
                msg = j["choices"][0]["message"]
                out_chars = len(msg.get("content") or "") + len(msg.get("reasoning_content") or "")
            except Exception:
                out_chars = 0
            if out_chars:
                ct = round(out_chars / 1.5)
                estimated = True
    if dec is None and stream.get("seconds") and isinstance(ct, int) and stream["seconds"] > 1:
        dec = ct / stream["seconds"]
        estimated = True
    rec = {
        "prompt_tokens": pt if isinstance(pt, int) else None,
        "completion_tokens": ct if isinstance(ct, int) else None,
        "reasoning_tokens": rt if isinstance(rt, int) else None,
        "prefill_tps": pre,
        "decode_tps": dec,
        "max_tokens": max_tokens,
        "model": j.get("model"),
        "estimated": estimated,
    }
    try:
        USAGE_SINK(rec)
    except Exception:
        pass


def loaded_model():
    """現在ロードされているモデルIDを返す。サーバーに届かない場合は None。"""
    if MOCK:
        return os.environ.get("MOCK_LOADED_MODEL", "mock-q3")
    if not base_url():
        return None
    try:
        r = httpx.get(f"{base_url()}/v1/models", headers=_headers(), timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            return str(data[0].get("id", ""))
        return ""
    except Exception:
        return None


def current_model():
    """API が現在サーブしているモデルID（相手先API準拠）。"""
    m = loaded_model()
    return m if m else "strata"


# chat(body=True) で呼ばれた本文系呼び出しの間だけ True（呼び出しは直列前提）。
_BODY_CALL = False


def _check_loops(content, think):
    """本文・thinking の両方を検査し、ループがあれば LoopDetectedError を投げる。

    設定で無効化できる：loop_detect=0 で全停止、loop_body_only=1 で本文系
    呼び出し（chat の body=True）のみ検査する（状態シート等の構造化出力を守る）。
    """
    if _setting("loop_detect") == "0":
        return
    if _setting("loop_body_only") == "1" and not _BODY_CALL:
        return
    info = detect_loop(think)
    if info:
        raise LoopDetectedError("thinking", info, content, think)
    info = detect_loop(content)
    if info:
        raise LoopDetectedError("本文", info, content, think)


def _chat_streaming(payload, model, max_tokens, timeout_s, progress=None):
    """ストリーミング受信。戻り値は応答 JSON 相当の dict（message を組み立て済み）。

    生成中に一定量（200字）たまるたびループ検査と進捗通知（progress）をし、
    ループを検出したら接続を切って LoopDetectedError を投げる。
    ストリーミングが使えない応答（エラー応答、空応答、SSE でない形式など）の
    場合は _StreamUnsupported を投げ、呼び出し側が一括受信にフォールバックする。
    """
    body = dict(payload)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    content_parts, think_parts = [], []
    content_len = think_len = 0
    checked_content = checked_think = 0
    cancel_mark = 0
    cancel_clock = time.time()
    usage = None
    timings = None
    chunks = 0
    started = time.time()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            with client.stream("POST", f"{base_url()}/v1/chat/completions",
                               headers=_headers(), json=body) as r:
                if r.status_code != 200:
                    r.read()
                    raise _StreamUnsupported(f"status {r.status_code}")
                if "application/json" in (r.headers.get("content-type") or ""):
                    # ストリーム指定を無視して一括 JSON が返ってきた場合はそのまま使う
                    j = json.loads(r.read().decode("utf-8"))
                    return j
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    chunks += 1
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if chunk.get("timings"):
                        timings = chunk["timings"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    c = delta.get("content") or ""
                    t = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if c:
                        content_parts.append(c)
                        content_len += len(c)
                    if t:
                        think_parts.append(t)
                        think_len += len(t)
                    if content_len - checked_content >= 200 or think_len - checked_think >= 200:
                        checked_content, checked_think = content_len, think_len
                        _check_loops("".join(content_parts), "".join(think_parts))
                        if progress:
                            try:
                                progress("".join(content_parts), "".join(think_parts))
                            except Exception:
                                pass
                    if (content_len + think_len - cancel_mark >= 200
                            or time.time() - cancel_clock >= 2.0):
                        cancel_mark = content_len + think_len
                        cancel_clock = time.time()
                        if _cancel_requested():
                            raise JobCancelledError("ユーザーにより停止されました")
    except (LoopDetectedError, _StreamUnsupported, JobCancelledError):
        raise
    except httpx.HTTPError as e:
        if chunks == 0 and not content_parts and not think_parts:
            raise _StreamUnsupported(str(e))
        raise RuntimeError(f"ストリーミング受信が中断しました：{e}")
    if chunks == 0 and not content_parts and not think_parts:
        raise _StreamUnsupported("empty stream")
    j = {"model": model,
         "choices": [{"message": {"content": "".join(content_parts),
                                  "reasoning_content": "".join(think_parts)}}]}
    if usage:
        j["usage"] = usage
    if timings:
        j["timings"] = timings
    j["_stream"] = {"chunks": chunks, "seconds": time.time() - started}
    return j


def _chat_once(payload, timeout_s):
    """一括受信（フォールバック用）。別スレッドで待ち受け、取消されたら
    クライアントを閉じて接続を切り、JobCancelledError で中断する。"""
    import threading
    box = {}

    def run():
        try:
            with httpx.Client(timeout=timeout_s) as client:
                box["client"] = client
                r = client.post(f"{base_url()}/v1/chat/completions",
                                headers=_headers(), json=payload)
                r.raise_for_status()
                box["j"] = r.json()
        except Exception as e:  # 取消で閉じた場合のエラーも含む（呼び出し側で判定）
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)
        if _cancel_requested():
            client = box.get("client")
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            raise JobCancelledError("ユーザーにより停止されました")
    if "err" in box:
        raise box["err"]
    return box["j"]


def chat(model, system, user, max_tokens=8192, temperature=0.8, timeout_s=3600.0,
         progress=None, window=None, body=False):
    """1回のチャット補完を行い、本文テキストを返す。model=None/'auto' ならロード中モデルを使う。

    本文または thinking が繰り返しループに入った場合は LoopDetectedError で
    異常終了する（生成中のストリーミング検査＋受信後の検査）。
    progress があれば、生成中に (本文の途中経過, thinking の途中経過) を逐次通知する
    （逐次表示・ライブ状態の保存用。呼び出し側で間引くこと）。
    各呼び出しは独立した文脈（別スレッド）で実行されるため、出力上限は対象
    ウィンドウ（window 指定時はその値、なければ設定の Context Window）の
    90%で頭打ちにする（本文生成・2次修正は呼び出し側で80%に決めている）。
    body=True は本文系の呼び出し（本文生成・続き・2次修正）の印で、設定
    loop_body_only=1 のときループ検出をこの呼び出しに限るために使う。
    """
    global LAST_THINKING, _BODY_CALL
    _BODY_CALL = body
    if model in (None, "", "auto"):
        model = current_model()
    _win = window or 0
    if not _win:
        try:
            _win = int(_setting("ctx_window") or 0)
        except ValueError:
            _win = 0
    if _win > 0:
        max_tokens = min(max_tokens, int(_win * 0.9))
    if MOCK:
        text = _mock_response(system, user)
        LAST_THINKING = "（モック思考）この章では手紙の差出人への伏線を置き、灯台守の口調を資料どおりに保つ。視点は三人称のまま、場面は港の夕景から始める。"
        _report_usage({"usage": {"prompt_tokens": 1200, "completion_tokens": 800,
                                 "completion_tokens_details": {"reasoning_tokens": 35}},
                        "model": model}, max_tokens)
        if progress:
            try:
                progress(text, LAST_THINKING)
            except Exception:
                pass
        return text
    if not base_url():
        raise RuntimeError("推論サーバーのURLが未設定です。Web UI の「設定」画面で接続先（XeStrata）を設定してください。")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    rb = reasoning_budget()
    if rb > 0:
        payload["reasoning_budget_tokens"] = rb
    if _cancel_requested():
        raise JobCancelledError("ユーザーにより停止されました")
    try:
        j = _chat_streaming(payload, model, max_tokens, timeout_s, progress=progress)
    except _StreamUnsupported:
        j = _chat_once(payload, timeout_s)
    _report_usage(j, max_tokens, req_chars=len(system or "") + len(user or ""))
    content, think = _split_thinking(j["choices"][0]["message"])
    LAST_THINKING = think
    _check_loops(content, think)
    if progress:
        try:
            progress(content, think)
        except Exception:
            pass
    return content


def wait_for_model(model_hint, should_stop, on_wait=None, poll_s=60.0):
    """ロード中モデルが model_hint を含むようになるまで無期限に待つ。

    タイムアウトで失敗させないための要。should_stop() が True を返したら中断する。
    on_wait(elapsed_s) は再通知などのために定期的に呼ばれる。
    """
    if MOCK:
        return True
    start = time.time()
    last_notice = 0.0
    while True:
        if should_stop():
            return False
        cur = loaded_model()
        if cur and model_hint.lower() in cur.lower():
            return True
        elapsed = time.time() - start
        if on_wait and elapsed - last_notice >= 3 * 3600:
            last_notice = elapsed
            on_wait(elapsed)
        time.sleep(poll_s)


def wait_for_model_change(from_model, hint=None, should_stop=None, on_wait=None, poll_s=60.0):
    """ロード中モデルが from_model から変わるまで無期限に待つ。

    hint があれば「hint を含むモデルに変わったこと」を条件にする。
    戻り値は新しいモデルID、中断されたら None。
    """
    if MOCK:
        return os.environ.get("MOCK_LOADED_MODEL", "mock-q4")
    start = time.time()
    last_notice = 0.0
    while True:
        if should_stop and should_stop():
            return None
        cur = loaded_model()
        if cur:
            if hint:
                if hint.lower() in cur.lower():
                    return cur
            elif cur != from_model:
                return cur
        elapsed = time.time() - start
        if on_wait and elapsed - last_notice >= 3 * 3600:
            last_notice = elapsed
            on_wait(elapsed)
        time.sleep(poll_s)


_MOCK_COUNTER = {"n": 0}


def _mock_response(system, user):
    _MOCK_COUNTER["n"] += 1
    n = _MOCK_COUNTER["n"]
    if "構成作家" in system and "展開計画" in system:
        if "作り直し" in system:
            return (
                "## 章内容詳細\n（モック再生成）追加指定を反映した場面の流れ。灯台守は手紙を読み、決意を新たにする。\n\n"
                "## 章終了時の状態\n（モック再生成）灯台守：決意を固めた。主人公：港に残留。未解決：地図の印。\n\n"
                "## 次章開始状態\n（モック再生成）灯台守：決意を固めた。主人公：港に残留。未解決：地図の印。\n"
            )
        return (
            "## 詳細概要\n（モック）この章の詳細なあらすじ。場面は港から灯台へ移る。\n\n"
            "## 章開始時の状態\n（モック）主人公は港町に到着した直後。灯台守とは未対面。\n\n"
            "## 章終了時の目標状態\n（モック目標）主人公が灯台に到着し、灯台守と対面する直前で終わる想定。\n\n"
            "## 章内容詳細\n（モック）主人公が市場を歩き、手紙を受け取り、灯台へ向かう。\n\n"
            "## 章終了時の状態\n（モック）主人公：灯台に到着。灯台守：面会直前。未解決：手紙の差出人。\n\n"
            "## 次章開始状態\n（モック）主人公：灯台に到着。灯台守：面会直前。未解決：手紙の差出人。\n"
            + ("\n## 終了状態\n（モック）物語は港町の朝で締めくくられる。\n" if "最終章です" in user else "")
        )
    if "状態記録者" in system:
        return (
            "# 現在の状態（モック）\n"
            "## 物語の現在位置\n港町、夕方。主人公は灯台へ向かう途中。\n"
            "## 登場人物の状態\n主人公：手紙を所持、差出人を捜索中。灯台守：面会前。\n"
            "## 人物関係の現在\n主人公と灯台守は未対面。\n"
            "## 進行中の出来事・約束・期限\n嵐が近づいており、今夜中に船出の判断が必要。\n"
            "## 未解決の伏線・謎\n手紙の差出人、地図の印。\n"
            "## 直近の出来事\n港に到着し、手紙を受け取った。"
        )
    if "概要" in system and "章" in system:
        return (
            "第1章 始まりの街\n"
            "主人公が港町に到着し、謎の手紙を受け取る。\n\n"
            "第2章 古い灯台\n"
            "灯台守との出会いと、隠された地図の発見。\n\n"
            "第3章 嵐の夜\n"
            "嵐の中で船出の決断を迫られる。\n"
        )
    if "プロンプト設計者" in system:
        return (
            "【生成指示書】\n"
            "- 文体：三人称、落ち着いた地の文\n"
            "- 直前の流れ：主人公の到着直後から自然に受ける\n"
            "- 盛り込む要素：手紙の差出人への伏線\n"
            "- 避ける要素：急な視点変更\n"
            "- 長さの目安：4000字\n"
        )
    if "書き足し" in system:
        return "（モック続き）" + "物語の続きの文章がここに厚く書き足される。" * 70 + "\n"
    if "用語チェッカー" in system:
        return "テスト造語\n謎の手紙"
    if "キャラクター資料" in system:
        return (
            "# キャラクター概要：（モックキャラ）\n"
            "## 基本情報（所属・立場・外見の特徴）\n港町の灯台守の弟子。\n"
            "## 性格・立ち振る舞い\n物静かだが芯が強い。\n"
            "## 一人称・二人称の呼び方\n一人称は「私」、目上には敬語。\n"
            "## 語尾・口癖・言い回しの特徴\n語尾は丁寧で、決め台詞は「大丈夫、なんとかなる」。\n"
            "## 代表的なセリフ例（資料にある場合のみ）\n「大丈夫、なんとかなる」\n"
            "## 他キャラクターとの関係・呼び方の違い\n主人公を名前で呼ぶ。\n"
            "## 書く際の注意点\n敬語の崩しすぎに注意。\n"
        )
    if "解析" in system:
        return (
            "# 文書解析\n"
            "## 作品概要（ジャンル・舞台・時代）\n港町を舞台にした冒険譚。\n"
            "## 登場人物（名前・役割・現在の状態・関係）\n主人公：手紙を受け取り調査中。\n"
            "## ここまでのあらすじ（時系列）\n到着→手紙→灯台へ。\n"
            "## 世界観・設定上の重要事項\n地図に示された印。\n"
            "## 文体と視点（人称・語り口・特徴的な表現）\n三人称、落ち着いた地の文。\n"
            "## 未回収の伏線・今後のフック\n手紙の差出人。\n"
            "## 続きを書く際の注意点\n視点を維持すること。\n"
        )
    if "要約" in system:
        return f"（モック要約{n}）この章では主人公が新たな手がかりを得て、次の目的地を決めた。"
    if "改修" in system:
        return f"（モック改修済み本文{n}）整えられた文章がここに入る。元の内容を保ちつつ、表現を磨いた。"
    return f"（モック本文{n}）これはテスト生成された小説本文である。物語は静かに幕を開けた。"
