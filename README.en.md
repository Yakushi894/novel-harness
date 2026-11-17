# novel-harness

[Japanese README](README.md) / Design document: `DESIGN.md` (Japanese)

Prebuilt image (Docker Hub): https://hub.docker.com/r/yuichi894/novel-harness

## Overview

A controller container that runs novel generation **entirely on your local network**. It lives on a Synology NAS and drives an inference PC (XeStrata / Strata) over its OpenAI-compatible API, from per-chapter generation to revision and combined output. No manuscript text is ever sent to an external service, so there is no ban risk.

**It runs straight from the Synology Container Manager GUI:** start the image, then enter the server's address in the settings page. No terminal required.

### Workspaces and pipeline

Workspaces are **per story**: one workspace per story/document; a new story gets its own workspace. Each workspace follows a fixed pipeline:

1. **Load source**: Enter the concept and settings, or upload an existing text file (e.g. .txt) as source material for a continuation. Uploaded documents can be run through **initial document analysis**: the model extracts a foundation brief (premise, characters, plot so far, style and viewpoint, open threads) that is then included in every later generation step.
2. **Next-chapter content**: Generate all chapter outlines in one pass, then edit and approve them in the Web UI. An "expand chapters" step then chains per chapter: detailed outline, start state, end-state target, detailed content, end state, and next chapter's start state (plus an ending state for the final chapter); after body generation the plan's states sync to the actual text. Extra instructions regenerate that chapter onward, and once approved, the following chapters can be regenerated too.
3. **First pass (draft)**: Before writing each chapter, a *prompt-design layer* produces a chapter-specific instruction sheet (style/viewpoint consistency, elements to include or avoid), and the chapter is written from it. Each finished chapter is summarized for the next chapter's context (summary chaining).
4. **Second pass (revision)**: Once all chapters exist, the container posts "please switch the model" to Synology Chat. When the model on the inference server changes, the container detects it via `/v1/models` and starts the revision automatically. **The wait has no timeout** — if the notification is missed overnight, drafts stay put and work resumes at the switch (with re-notifications).
   - The second pass is a **proofreading** pass: it must not change the meaning, content, or plot. It only fixes particles and grammar, typos, unnatural Japanese, and stray Chinese/English (converted to Japanese) — except a foreign character's own lines.
   - **Thread mode (no switching)** is also available per work: with the model chosen at the start, each chapter runs in its own thread (independent context) from draft to finishing revision. Each thread receives the uploaded TXT and the chapter outlines, but not other chapters' body text, which keeps the context short.
   - **Single-chapter mode**: for one-chapter works; an "instructions (TXT attachable) → output" form on the work page (one submit runs outline through generation).
5. **PHP / TXT output**: A small PHP page (Synology Web Station) concatenates the final chapters for on-screen reading and .txt download.

### The model follows the remote API

The harness does not pin model names. Every request uses whatever model the inference server is currently serving. The second pass starts when the loaded model *changes* from the one used for the first pass (an optional hint substring can be configured instead).

### GUI (AnythingLLM-style layout)

A left sidebar always lists the workspaces and the chapters of the open workspace; the main area runs the pipeline. Chapters can also be deleted from the sidebar (✕). While a job runs, the work page updates asynchronously and shows elapsed time and token usage. Jobs interrupted by a container restart are recovered as failed. A per-work minimum chapter length (5,000–50,000 characters) can be set; drafts are continued until they reach it, otherwise the job errors. The Context Window can be set to 64k / 128k / 256k / 512k.

### Loop detection

If the body or Think output starts repeating while generating, reception stops immediately and the job fails. The looping text is not saved as the body; an incident log with details and a tail excerpt is kept in the artifacts and on the chapter page.

### Live streaming view and sidebar status

Generation runs in the container's worker, so inference and loop detection continue even if you close the browser. While a chapter generates, its page streams the body and Think in progress (Think is collapsible). The sidebar shows context fill, GPU prefill/decode speeds (servers reporting timings), and the generating chapter with its counts. Pages update asynchronously without a full reload, so reading the next chapter's outline during generation is never interrupted. The initial document analysis result and the state chain are now collapsible sections.

Each chapter also has its own **chapter page** (outline → generated text → **Think** (the model's reasoning) → state → revision notes). Writing **revision notes** there regenerates just that chapter with the notes included. A **state chain** keeps the story state explicit (see the flow above): each chapter takes its start state as input and outputs its end state for the next.

### Web search and derivative-work mode

- **Term lookup**: enter a word and the harness checks Japanese Wiktionary / Wikipedia / the web, saves a "term memo", and includes it in later generation.
- **Term check (automatic)**: before outline and draft generation, the model itself flags words it doesn't know; terms missing from the glossary DB are looked up and added.
- **Glossary DB**: learned meanings are stored in a separate dictionary. **They survive workspace deletion** and are reused by other stories (sidebar's Glossary DB page).
- **Derivative-work (fanfiction) mode**: a per-work ON/OFF toggle (can be hidden in Settings; shown by default). When ON, the AI is told the work is derivative. When OFF, it judges from the text alone.
- **Character profile search** (when derivative mode is ON): gathers public material for a character and summarizes a profile (pronouns, speech endings, catchphrases, mannerisms) for use in generation.

Only the search terms (words and character names) leave the NAS; **manuscript text is never sent out**. Web search can be disabled entirely in Settings.

### Settings page (AnythingLLM-style)

Editable in the Web UI, applied immediately (no restart):

- Inference server URL and API key (leave the key empty if your LAN setup doesn't need one)
- **Model Context Window** (live re-allocation of every Max Tokens, with a red over-limit warning)
- **Context Window (for the second pass)**: a separate budget for the revision pass on a different model (default 512k; a changed model gets 80% of it as Max Tokens). Same model: the regular allocation applies
- **Max Tokens** per stage (outline / instruction sheet / chapter body / summary / revision)
- Temperature, revision-model hint, Synology Chat webhook URL

The UI token is also optional on a LAN (no authentication while the `UI_TOKEN` environment variable is empty).

## Usage

### 1. Deploying to Synology DSM (Container Manager)

The procedure is identical on a physical DSM and on Virtual DSM (vDSM). The only prerequisite is that **Container Manager** is installed from Package Center.

#### Option A: GUI only (recommended)

1. In File Station, create two folders: `/volume1/docker/novel-harness/data` and `/volume1/docker/novel-harness/works`.
2. Log in to DSM and open **Container Manager**.
3. In "Registry", search for and download `yuichi894/novel-harness` (tag `latest`).
4. Create a container from the image:
   - Port settings: local port `18080` → container port `8080`
   - Volume settings: folder `/volume1/docker/novel-harness/data` → `/data`, folder `/volume1/docker/novel-harness/works` → `/works`
   - Enable the restart policy
5. Run the container and open `http://<NAS address>:18080/` in a browser.
6. On first run the page shows a first-time setup notice; open the **Settings** page and enter the inference server URL (e.g. `http://192.168.1.10:8080`).

No environment variables are required (they exist only as optional initial values for advanced users; see `.env.example`). The UI token is also unnecessary on a LAN-only setup.

#### Option B: Project (compose)

Creating a project with the bundled `docker-compose.yml` gives the same result:

```yaml
services:
  harness:
    image: yuichi894/novel-harness:latest
    container_name: novel-harness
    restart: unless-stopped
    ports:
      - "18080:8080"
    volumes:
      - /volume1/docker/novel-harness/data:/data
      - /volume1/docker/novel-harness/works:/works
```

#### Updating to a new version

1. In "Registry", download `yuichi894/novel-harness:latest` again (the image is replaced with the new one).
2. GUI containers: stop the container → "Reset" (recreate from image) → run. Projects: "Build" → "Run".
3. Your data (works, settings, glossary DB) lives entirely on the volumes (/data and /works), so rebuilding the container never deletes it.

You can also **download / restore a full backup (zip)** at the bottom of the Settings page.

### 2. Create a work and generate

1. Create a workspace on the top page (title, concept, settings, chapter count; a source TXT can be attached at creation or dragged & dropped).
2. Optionally upload a source text file.
3. Click "2. Next-chapter content (chapter outlines)", then edit and save (approve) each outline. Optionally run "2.5 Expand chapters" for detailed plans.
4. Click "3. First pass" — instruction sheet, body, and summary are produced per chapter automatically.
5. Click "4. Second pass" — when the Synology Chat notification arrives, switch the model on the inference PC. Revision starts automatically once detected.
6. When done, download the combined text from the PHP page (or the work page).

### 3. Deploying the PHP output page

Place `php/index.php` on a Web Station site and adjust `WORKS_ROOT` at the top to the real path of the works folder. Works with final chapters are listed, and the combined text can be downloaded as .txt from the page.

The output page is a formatted reading page (serif type, table of contents, horizontal/vertical writing toggle). When the PHP page is updated, replace this file on the Web Station.

## Layout

- `app/` — the FastAPI application (pipeline state machine, generation driver, prompt-design layer, settings UI, Synology Chat notifications, model-switch detection)
- `docker-compose.yml` — production (uses the Docker Hub image, no environment variables needed)
- `docker-compose.dev.yml` — development/testing (builds from source)
- `php/index.php` — combined-output page for Web Station
- State is stored in SQLite (`/data`) and per-work folders (`/works`)

## Notes

- Strata-family servers have **no model-switching API** (only `POST /load` / `POST /unload`). The revision pass is therefore "notify + manual switch + automatic detection" by design.
- Thinking tokens count toward the output cap. Caps live in Settings (Max Tokens) and are re-allocated automatically when the Context Window changes.
- The UI is for LAN / Tailscale access only. Do not expose it to the internet.
- Review by Muse is an opt-in lane: only works you explicitly mark as "Muse review OK" are ever sent; everything else never leaves the local network.
