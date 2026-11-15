"""OpenAI互換API（XeStrata）への薄いクライアント。MOCK_LLM=1 で疑似応答に切り替わる。

接続先URLとAPIキーは Web UI の設定画面（DB）優先、環境変数は初期値として使う。
GUI からコンテナを起動しただけでも、後から画面で設定できるようにするため。
"""
import os
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

# 直近の chat() 呼び出しでモデルが返した thinking（思考過程）。
# XeStrata は message.reasoning_content に入れて返す。本文とは分けて保持する。
LAST_THINKING = ""


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


def _report_usage(j, max_tokens):
    """API応答の usage を取り出して USAGE_SINK に渡す。

    thinking（reasoning）トークンは、サーバーが usage に内訳を含めて返す場合だけ分かる。
    返らないサーバーでは reasoning_tokens=None になり、表示側は出力トークン総数で代替する。
    """
    if not USAGE_SINK:
        return
    u = j.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    rt = details.get("reasoning_tokens", u.get("reasoning_tokens"))
    rec = {
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "reasoning_tokens": rt if isinstance(rt, int) else None,
        "max_tokens": max_tokens,
        "model": j.get("model"),
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


def chat(model, system, user, max_tokens=8192, temperature=0.8, timeout_s=3600.0):
    """1回のチャット補完を行い、本文テキストを返す。model=None/'auto' ならロード中モデルを使う。"""
    global LAST_THINKING
    if model in (None, "", "auto"):
        model = current_model()
    if MOCK:
        text = _mock_response(system, user)
        LAST_THINKING = "（モック思考）この章では手紙の差出人への伏線を置き、灯台守の口調を資料どおりに保つ。視点は三人称のまま、場面は港の夕景から始める。"
        _report_usage({"usage": {"prompt_tokens": 1200, "completion_tokens": 800,
                                 "completion_tokens_details": {"reasoning_tokens": 35}},
                        "model": model}, max_tokens)
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
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(f"{base_url()}/v1/chat/completions", headers=_headers(), json=payload)
        r.raise_for_status()
        j = r.json()
    _report_usage(j, max_tokens)
    content, think = _split_thinking(j["choices"][0]["message"])
    LAST_THINKING = think
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
