"""Web検索（用語の意味・キャラクター資料用）。

検索されるのはユーザーが入力した単語やキャラクター名だけ。本文（原稿）は外部に送らない。
キー不要のソースを使う：日本語 Wiktionary / Wikipedia（MediaWiki API）と DuckDuckGo HTML。
MOCK_LLM=1 のときは疑似結果を返す。
"""
import html as htmllib
import os
import re

import httpx

MOCK = os.environ.get("MOCK_LLM", "0") == "1"
UA = {"User-Agent": "novel-harness/0.4 (personal local research tool)"}
TIMEOUT = 20


def _get(url, params=None):
    with httpx.Client(timeout=TIMEOUT, headers=UA, follow_redirects=True) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r


def wikipedia_extract(term, lang="ja"):
    """Wikipedia の記事本文（冒頭中心）を返す。なければ None。"""
    if MOCK:
        return f"（モック）{term}は、作品内に登場する重要な要素である。詳細な設定が存在する。"
    try:
        r = _get(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "prop": "extracts", "explaintext": 1,
            "titles": term, "format": "json", "redirects": 1})
        pages = r.json()["query"]["pages"]
        for p in pages.values():
            ex = (p.get("extract") or "").strip()
            if ex:
                return ex
        return None
    except Exception:
        return None


def wikipedia_search_titles(query, lang="ja", n=5):
    """Wikipedia のタイトル検索（あいまい検索用）。"""
    if MOCK:
        return [query]
    try:
        r = _get(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "opensearch", "search": query, "limit": n, "format": "json"})
        return r.json()[1]
    except Exception:
        return []


def wiktionary_extract(term):
    """日本語 Wiktionary の項目本文を返す。なければ None。"""
    if MOCK:
        return f"{term}\n【名詞】（モック）新しい概念を指す造語。作品内で用いられる。"
    try:
        r = _get("https://ja.wiktionary.org/w/api.php", {
            "action": "query", "prop": "extracts", "explaintext": 1,
            "titles": term, "format": "json", "redirects": 1})
        pages = r.json()["query"]["pages"]
        for p in pages.values():
            ex = (p.get("extract") or "").strip()
            if ex:
                return ex
        return None
    except Exception:
        return None


def web_search(query, n=5):
    """DuckDuckGo HTML から検索結果（タイトル/URL/スニペット）を返す。"""
    if MOCK:
        return [{"title": f"{query}とは（モック百科）",
                 "url": "https://example.invalid/mock",
                 "snippet": f"（モック）{query}についての解説。言い回しや特徴がまとめられている。"}]
    try:
        r = _get("https://html.duckduckgo.com/html/", {"q": query})
        text = r.text
        results = []
        for m in re.finditer(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</',
                text, re.S):
            url = htmllib.unescape(m.group(1))
            mm = re.search(r"uddg=([^&]+)", url)
            if mm:
                from urllib.parse import unquote
                url = unquote(mm.group(1))
            title = re.sub(r"<[^>]+>", "", htmllib.unescape(m.group(2))).strip()
            snippet = re.sub(r"<[^>]+>", "", htmllib.unescape(m.group(3))).strip()
            if title:
                results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= n:
                break
        return results
    except Exception:
        return []


def lookup_word(term):
    """単語の意味を調べ、用語メモのテキストを返す。"""
    parts = [f"【用語】{term}"]
    found = False
    wk = wiktionary_extract(term)
    if wk:
        found = True
        parts.append(f"\n（出典：日本語 Wiktionary）\n{wk[:3000]}")
    wp = wikipedia_extract(term)
    if wp:
        found = True
        parts.append(f"\n（出典：Wikipedia）\n{wp[:3000]}")
    if not found:
        titles = wikipedia_search_titles(term)
        for t in titles[:2]:
            wp2 = wikipedia_extract(t)
            if wp2:
                found = True
                parts.append(f"\n（出典：Wikipedia「{t}」）\n{wp2[:2000]}")
                break
    web = web_search(term + " 意味 とは")
    if web:
        found = True
        parts.append("\n（Web検索の抜粋）")
        for r in web:
            parts.append(f"- {r['title']}：{r['snippet']}（{r['url']}）")
    if not found:
        raise RuntimeError("検索結果が得られませんでした。NAS のインターネット接続や単語の表記を確認してください。")
    return "\n".join(parts)


def gather_character_material(name, origin=""):
    """キャラクター資料の素材（Web上の公開情報）を集める。"""
    label = f"{origin} {name}".strip()
    parts = [f"【調査対象】{label}"]
    found = False
    for t in dict.fromkeys([name] + wikipedia_search_titles(label, n=4)):
        wp = wikipedia_extract(t)
        if wp:
            found = True
            parts.append(f"\n（Wikipedia「{t}」）\n{wp[:5000]}")
            break
    for q in (f"{label} キャラクター プロフィール", f"{label} 口癖 一人称 言い方"):
        web = web_search(q, n=4)
        if web:
            found = True
            parts.append(f"\n（Web検索：{q}）")
            for r in web:
                parts.append(f"- {r['title']}：{r['snippet']}（{r['url']}）")
    if not found:
        raise RuntimeError("キャラクター資料が見つかりませんでした。名前や作品名の表記を確認してください。")
    return "\n".join(parts)
