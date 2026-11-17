<?php
// 小説ハーネス：まとめ出力ページ（Synology Web Station 用）
// final の各章を結合し、読みやすく整形して表示する。画面から .txt もダウンロードできる。
// 配置：Web Station のサイト直下。WORKS_ROOT は作品フォルダの実パスに合わせる。
// 作品タイトル・章タイトルは、可能なら DB（WORKS_ROOT の隣の data/harness.db）から引く。
// DB が読めない環境では、フォルダ名と章番号だけの表示に自動でフォールバックする。
declare(strict_types=1);

const WORKS_ROOT = '/volume1/docker/novel-harness/works';

function e(string $s): string {
    return htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
}

/** DB から作品タイトルと章タイトルを引く（失敗しても空で返す）。 */
function db_meta(string $slug): array {
    $meta = ['title' => null, 'chapters' => []];
    try {
        $dbPath = dirname(WORKS_ROOT) . '/data/harness.db';
        if (!is_file($dbPath) || !class_exists('PDO')) {
            return $meta;
        }
        $pdo = new PDO('sqlite:' . $dbPath);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $st = $pdo->prepare('SELECT id, title FROM works WHERE slug = ?');
        $st->execute([$slug]);
        $w = $st->fetch(PDO::FETCH_ASSOC);
        if ($w) {
            $meta['title'] = (string)$w['title'];
            $st2 = $pdo->prepare('SELECT number, title FROM chapters WHERE work_id = ? ORDER BY number');
            $st2->execute([$w['id']]);
            foreach ($st2->fetchAll(PDO::FETCH_ASSOC) as $c) {
                $meta['chapters'][(int)$c['number']] = (string)$c['title'];
            }
        }
    } catch (Throwable $ex) {
        // DB が読めなくても表示は続行する
    }
    return $meta;
}

/** 指定サブフォルダ（final / drafts）の章ファイルを [章番号 => パス] で返す。 */
function chapter_files(string $dir, string $sub = 'final'): array {
    $files = glob($dir . '/' . $sub . '/ch*.txt') ?: [];
    sort($files, SORT_STRING);
    $out = [];
    foreach ($files as $f) {
        if (preg_match('/ch(\d+)\.txt$/', $f, $m)) {
            $out[(int)$m[1]] = $f;
        }
    }
    ksort($out);
    return $out;
}

function list_works(): array {
    $works = [];
    foreach (glob(WORKS_ROOT . '/*', GLOB_ONLYDIR) ?: [] as $dir) {
        $chapters = chapter_files($dir);
        $isDraft = false;
        if (!$chapters) {
            $chapters = chapter_files($dir, 'drafts');
            $isDraft = $chapters !== [];
        }
        if ($chapters) {
            $slug = basename($dir);
            $meta = db_meta($slug);
            $works[] = [
                'slug' => $slug,
                'dir' => $dir,
                'title' => $meta['title'] ?? (preg_replace('/-\d+$/', '', $slug) ?: $slug),
                'chapters' => count($chapters),
                'draft' => $isDraft,
            ];
        }
    }
    return $works;
}

function chapter_heading(int $num, array $meta): string {
    $t = $meta['chapters'][$num] ?? '';
    return '第' . $num . '章' . ($t !== '' ? '　' . $t : '');
}

/** .txt ダウンロード用の結合テキスト。 */
function combined_text(array $files, array $meta, string $title): string {
    $out = [$title, ''];
    foreach ($files as $num => $f) {
        $out[] = chapter_heading($num, $meta);
        $out[] = '';
        $out[] = trim((string)file_get_contents($f));
        $out[] = '';
        $out[] = '';
    }
    return implode("\n", $out);
}

/** 本文を段落に整形して <p> の並びにする（空行＝段落区切り、1行内改行は <br>）。 */
function paragraphs_html(string $text): string {
    $text = str_replace(["\r\n", "\r"], "\n", trim($text));
    $blocks = preg_split('/\n{2,}/', $text) ?: [];
    $html = '';
    foreach ($blocks as $b) {
        $b = trim($b);
        if ($b === '') {
            continue;
        }
        $lines = [];
        foreach (explode("\n", $b) as $line) {
            $line = trim($line);
            if ($line !== '') {
                $lines[] = e($line);
            }
        }
        if ($lines) {
            $html .= '<p>' . implode('<br>', $lines) . '</p>' . "\n";
        }
    }
    return $html;
}

/** 共通スタイル（画面表示と整形HTMLダウンロードで同じデザインを使う）。 */
function page_style(): string {
    return <<<'CSS'
  :root { --ink:#26241f; --paper:#faf7f0; --accent:#7a4a21; --muted:#8a8378; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font-family:"Hiragino Mincho ProN","Hiragino Mincho Pro","YuMincho","Yu Mincho","Noto Serif JP",serif; }
  .wrap { max-width:42em; margin:0 auto; padding:2.2em 1.3em 4em; }
  header.book { text-align:center; margin-bottom:2.4em; }
  header.book h1 { font-size:1.7em; letter-spacing:.06em; margin:.2em 0 .3em; }
  header.book .sub { color:var(--muted); font-size:.9em; }
  .toolbar { display:flex; gap:.6em; justify-content:center; flex-wrap:wrap; margin:1.1em 0 0; }
  .toolbar a, .toolbar button { font-family:sans-serif; font-size:.85em; color:var(--accent);
    background:#fff; border:1px solid #d8cfc0; border-radius:999px; padding:.45em 1.1em;
    text-decoration:none; cursor:pointer; }
  .toolbar a:hover, .toolbar button:hover { background:#f3ecdf; }
  nav.toc { background:#fff; border:1px solid #e3dacb; border-radius:10px; padding:1em 1.4em; margin-bottom:2.6em; }
  nav.toc h2 { font-size:1em; margin:.1em 0 .6em; letter-spacing:.2em; color:var(--accent); }
  nav.toc ol { margin:0; padding-left:1.4em; line-height:2; }
  nav.toc a { color:var(--ink); text-decoration:none; }
  nav.toc a:hover { text-decoration:underline; }
  article section { margin-bottom:3.4em; }
  article h2.chapter { text-align:center; font-size:1.35em; letter-spacing:.08em;
    margin:0 0 1.6em; padding-bottom:.7em; border-bottom:2px solid var(--accent); }
  article p { font-size:1.06em; line-height:2.05; margin:0 0 1.05em; text-align:justify; }
  .fin { text-align:center; color:var(--muted); letter-spacing:.5em; margin-top:3em; }
  .totoc { text-align:right; font-family:sans-serif; font-size:.8em; }
  .totoc a { color:var(--muted); text-decoration:none; }
  ul.works { list-style:none; padding:0; }
  ul.works li { background:#fff; border:1px solid #e3dacb; border-radius:10px;
    padding:1em 1.3em; margin-bottom:.9em; display:flex; justify-content:space-between; align-items:center; gap:1em; }
  ul.works .wt { font-size:1.1em; }
  ul.works .wc { color:var(--muted); font-size:.85em; font-family:sans-serif; }
  ul.works a { color:var(--accent); text-decoration:none; }
  /* 縦書きモード */
  #novel.vertical { writing-mode:vertical-rl; -webkit-writing-mode:vertical-rl;
    height:76vh; overflow-x:auto; overflow-y:hidden; padding:.4em 0; }
  #novel.vertical section { margin:0 0 0 3em; }
  #novel.vertical h2.chapter { border-bottom:none; border-left:2px solid var(--accent);
    padding:0 0 0 .6em; margin:0 0 0 1.4em; }
  @media print {
    .toolbar, nav.toc, .totoc { display:none; }
    body { background:#fff; }
    #novel.vertical { writing-mode:horizontal-tb; height:auto; overflow:visible; }
  }
CSS;
}

/** 本文中身（目次＋各章）のHTML。画面とダウンロードで共用する。 */
function book_body_html(array $files, array $meta): string {
    $toc = '<nav class="toc" id="toc"><h2>目次</h2><ol>';
    foreach ($files as $num => $f) {
        $toc .= '<li><a href="#ch' . $num . '">' . e(chapter_heading($num, $meta)) . '</a></li>';
    }
    $toc .= '</ol></nav>';
    $sections = '';
    foreach ($files as $num => $f) {
        $sections .= '<section id="ch' . $num . '"><h2 class="chapter">'
            . e(chapter_heading($num, $meta)) . '</h2>'
            . paragraphs_html((string)file_get_contents($f))
            . '<div class="totoc"><a href="#toc">目次へ戻る ↑</a></div></section>' . "\n";
    }
    return $toc . '<article id="novel">' . $sections . '<div class="fin">― 了 ―</div></article>';
}

/** 整形HTMLファイルとしてダウンロードさせる単体ドキュメント（同じデザイン）。 */
function standalone_html(array $files, array $meta, string $title, bool $isDraft): string {
    $sub = '全' . count($files) . '章' . ($isDraft ? '（下書き・1次出力）' : '');
    return '<!DOCTYPE html>' . "\n" . '<html lang="ja"><head><meta charset="utf-8">'
        . '<meta name="viewport" content="width=device-width, initial-scale=1">'
        . '<title>' . e($title) . '</title><style>' . page_style() . '</style></head><body>'
        . '<div class="wrap"><header class="book"><h1>' . e($title) . '</h1>'
        . '<div class="sub">' . e($sub) . '</div></header>'
        . book_body_html($files, $meta)
        . '</div></body></html>';
}

$slug = preg_replace('/[^a-zA-Z0-9\-_]/', '', (string)($_GET['work'] ?? ''));
$dir = $slug !== '' ? WORKS_ROOT . '/' . $slug : null;
if ($dir !== null && !is_dir($dir)) {
    $dir = null;
}

$meta = $dir !== null ? db_meta($slug) : ['title' => null, 'chapters' => []];
$title = $meta['title'] ?? (preg_replace('/-\d+$/', '', $slug) ?: $slug);

$files = $dir !== null ? chapter_files($dir) : [];
$isDraft = false;
if ($dir !== null && !$files) {
    $files = chapter_files($dir, 'drafts');
    $isDraft = $files !== [];
}

if ($dir !== null && isset($_GET['download'])) {
    if ($_GET['download'] === 'html') {
        header('Content-Type: text/html; charset=utf-8');
        header('Content-Disposition: attachment; filename="' . $slug . '.html"');
        echo standalone_html($files, $meta, $title, $isDraft);
        exit;
    }
    $text = combined_text($files, $meta, $title);
    header('Content-Type: text/plain; charset=utf-8');
    header('Content-Disposition: attachment; filename="' . $slug . '.txt"');
    echo $text;
    exit;
}
?>
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= $dir !== null ? e($title) . '｜' : '' ?>小説ハーネス出力</title>
<style><?= page_style() ?></style>
</head>
<body>
<div class="wrap">
<?php if ($dir === null): ?>
  <header class="book"><h1>小説ハーネス出力</h1><div class="sub">完成した作品の一覧</div></header>
  <ul class="works">
  <?php foreach (list_works() as $w): ?>
    <li>
      <span><a class="wt" href="?work=<?= e($w['slug']) ?>"><?= e($w['title']) ?></a>
      <span class="wc">（全<?= $w['chapters'] ?>章<?= $w['draft'] ? '・下書き' : '' ?>）</span></span>
      <span><a href="?work=<?= e($w['slug']) ?>&download=1">.txt</a> ／ <a href="?work=<?= e($w['slug']) ?>&download=html">整形HTML</a></span>
    </li>
  <?php endforeach; ?>
  </ul>
  <?php if (!list_works()): ?><p>完成した作品はまだありません。</p><?php endif; ?>
<?php else: ?>
  <header class="book">
    <h1><?= e($title) ?></h1>
    <div class="sub">全<?= count($files) ?>章<?= $isDraft ? '（下書き・1次出力）' : '' ?></div>
    <div class="toolbar">
      <a href="?">← 作品一覧</a>
      <a href="?work=<?= e($slug) ?>&download=1">.txt でダウンロード</a>
      <a href="?work=<?= e($slug) ?>&download=html">整形HTMLでダウンロード</a>
      <button type="button" id="vToggle">縦書きで読む</button>
    </div>
  </header>
  <?= book_body_html($files, $meta) ?>
  <script>
    document.getElementById('vToggle').addEventListener('click', function () {
      var on = document.getElementById('novel').classList.toggle('vertical');
      this.textContent = on ? '横書きで読む' : '縦書きで読む';
    });
  </script>
<?php endif; ?>
</div>
</body>
</html>
