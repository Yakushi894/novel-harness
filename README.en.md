# novel-harness

[Japanese README](README.md) / Design document: `DESIGN.md` (Japanese)

## Overview

A controller container that runs novel generation **entirely on your local network**. It lives on a Synology NAS and drives an inference PC (XeStrata / Strata) over its OpenAI-compatible API, managing the whole pipeline from per-chapter generation to revision and combined output. No manuscript text is ever sent to an external service, so there is no account-suspension (ban) risk.

**It runs straight from the Synology Container Manager GUI.** Download the image, start the container, then enter the inference server's address in the web settings page. No terminal work and no environment-variable editing required.

### Workspaces and pipeline

Workspaces are **per story**: one workspace per story/document; a new story gets its own workspace. Each workspace follows a fixed pipeline:

1. **Load source**: Enter the concept and setting documents, or upload an existing text file (e.g. .txt) as source material — the starting point for generating a continuation. Uploaded documents can be run through **initial document analysis**: the model extracts a foundation brief (premise, characters, plot so far, style and viewpoint, open threads) that is then included in every later generation step.
2. **Next-chapter content**: Generate an outline for every chapter in one pass, then edit and approve the outlines in the Web UI.
3. **First pass (draft)**: Before writing each chapter, a *prompt-design layer* produces a chapter-specific instruction sheet (style and viewpoint consistency, how to pick up the previous flow, elements to include or avoid), and the chapter is written from it. Each finished chapter is summarized and the summary is fed into the next chapter's context (summary chaining).
4. **Second pass (revision)**: Once all chapters exist, the container posts "please switch the model" to Synology Chat. When the model on the inference server changes, the container detects it via `/v1/models` and starts the revision automatically. **The wait has no timeout** — if the notification is missed overnight, the generated drafts stay put and work resumes when the switch happens (with re-notifications).
   - The second pass is a **proofreading** pass: it must not change the overall meaning, content, or plot. It only fixes particles and grammar, typos, unnatural Japanese, and stray Chinese/English mixed into the text (converted to Japanese) — except lines where a foreign character naturally speaks that language, which are left as-is.
   - **Thread mode (no switching)** is also available per work: with the model chosen at the start, each chapter runs in its own thread (independent context) from draft to finishing revision. Each thread receives the uploaded TXT and the chapter outlines, but not other chapters' body text, which keeps the context short.
5. **PHP / TXT output**: A small PHP page (Synology Web Station) concatenates the final chapters for on-screen reading and .txt download.

### The model follows the remote API

The harness does not pin model names. Every request uses whatever model the inference server is currently serving. The second pass starts when the loaded model *changes* from the one used for the first pass (an optional hint substring can be configured instead).

### GUI (AnythingLLM-style layout)

A left sidebar always lists the workspaces and the chapters of the open workspace; the main area runs the pipeline. Chapters can also be deleted from the sidebar (✕). While a job runs, the work page refreshes itself and shows each job's elapsed time and token usage as a meter (`thinking: X of budget B (max M) tokens`). Jobs interrupted by a container restart are recovered as failed, with the reason shown. A per-work minimum chapter length (5,000–50,000 characters) can be set; if a draft comes up short, the harness automatically writes continuations until it reaches the minimum (up to 5 rounds). The Context Window can be set to 64k / 128k / 256k / 512k, and the per-stage Max Tokens defaults are generous.

Each chapter also has its own **chapter page** (outline → generated text → **Think** (the model's reasoning, returned as `reasoning_content`) → state → revision notes). Writing **revision notes** on the chapter page and pressing the button regenerates just that chapter with the notes included. For continuation writing, a **state chain** keeps the story state explicit: right after the initial document analysis, the harness outputs the "current state" at the end of the source document (location, character states, relationships, ongoing events, unresolved threads); every chapter receives the state at its start as input and outputs its end state for the next chapter. Each chapter's input/output state can be inspected on its chapter page.

### Web search and derivative-work mode

A web search mode covers words the local model doesn't know — new terms and coined words (mainly Japanese).

- **Term lookup**: enter a word and the harness checks Japanese Wiktionary / Wikipedia / the web, saves a "term memo", and includes it in later generation.
- **Term check (automatic)**: before outline and draft generation, the model itself flags words it doesn't know; terms missing from the glossary DB are looked up and added, with a notice on the work page.
- **Glossary DB**: learned meanings are stored in a dictionary separate from workspaces. **They survive workspace deletion**, and the same word in another story reuses the memo instead of searching again (manage it from the sidebar's Glossary DB page).
- **Derivative-work (fanfiction) mode**: a per-work ON/OFF toggle (can be hidden in Settings; shown by default). When ON, the AI is told the work is derivative. When OFF, it judges from the text alone.
- **Character profile search** (when derivative mode is ON): gathers public material for a character and summarizes it into a profile — first-person pronoun, forms of address, speech endings, catchphrases, mannerisms, behavior — which is then included in generation.

Only the search terms (words and character names) leave the NAS; **manuscript text is never sent out**. Web search can be disabled entirely in Settings.

### Settings page (AnythingLLM-style)

Editable in the Web UI, applied immediately (no restart):

- Inference server URL and API key (leave the key empty if your LAN setup doesn't need one)
- **Model Context Window** (live re-allocation of every Max Tokens; defaults use ~90% of the window, with a red over-limit warning)
- **Context Window (for the second pass)**: a separate budget for the revision pass on a different model (defaults to the maximum, 512k; a changed model gets 80% of it as Max Tokens). When the model does not change, the regular allocation applies
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

You can also **download / restore a full backup (zip of the DB + generated files)** at the bottom of the Settings page — useful when moving to another host or recreating the volumes from scratch.

### 2. Create a work and generate

1. Create a workspace on the top page (title, concept, settings, chapter count; a source TXT can be uploaded directly at creation).
2. Optionally upload a source text file (step 1: load source).
3. Click "2. Next-chapter content (chapter outlines)", then edit and save (approve) each outline.
4. Click "3. First pass" — instruction sheet, body, and summary are produced per chapter automatically.
5. Click "4. Second pass" — when the Synology Chat notification arrives, switch the model on the inference PC. Revision starts automatically once detected.
6. When done, download the combined text from the PHP page (or the work page).

### 3. Deploying the PHP output page

Place `php/index.php` on a Web Station site and adjust `WORKS_ROOT` at the top to the real path of the works folder. Works with final chapters are listed, and the combined text can be downloaded as .txt from the page.

The output page is a formatted reading page (serif type, paragraph formatting, table of contents, chapter headings, horizontal/vertical writing toggle). Work and chapter titles are read from the harness DB when available (falling back to folder names and chapter numbers). When the PHP page is updated, replace this file on the Web Station.

## Layout

- `app/` — the FastAPI application (pipeline state machine, generation driver, prompt-design layer, settings UI, Synology Chat notifications, model-switch detection)
- `docker-compose.yml` — production (uses the Docker Hub image, no environment variables needed)
- `docker-compose.dev.yml` — development/testing (builds from source)
- `php/index.php` — combined-output page for Web Station
- State is stored in SQLite (`/data`) and per-work folders (`/works`)

## Notes

- Strata-family servers have **no model-switching API** (only `POST /load` / `POST /unload` for the already-configured model). The revision pass is therefore "notify + manual switch + automatic detection" by design.
- Thinking tokens count toward the output cap. Caps live in Settings (Max Tokens) and are re-allocated automatically when the Context Window changes.
- The UI is intended for LAN / Tailscale access only. Do not expose it directly to the internet.
- Review by Muse is an opt-in lane: only works you explicitly mark as "Muse review OK" are ever sent; everything else never leaves the local network.
