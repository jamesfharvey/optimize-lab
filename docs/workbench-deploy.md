# Deploying the workbench

The workbench (`/workbench`) is fully static — vanilla HTML/CSS/JS, no build
step, no CDN, data bundled as script files. It works three ways: opened
directly from disk (`file://…/workbench/index.html`), served locally
(`python3 -m http.server` from the repo root → `http://localhost:8000/workbench/`),
or from GitHub Pages.

## GitHub Pages (one-time setup)

GitHub Pages' "deploy from branch" option only offers `/ (root)` or `/docs`
as the source folder — there is no `/workbench` folder option. The simple,
correct setup is to serve the repository root and link to the subfolder:

1. GitHub → `jamesfharvey/optimize-lab` → **Settings → Pages**.
2. Source: **Deploy from a branch** · Branch: **main** · Folder: **/ (root)**
   → Save.
3. Wait for the first Pages build (~1 minute, Actions tab shows it).
4. The workbench is then live at:

   **`https://jamesfharvey.github.io/optimize-lab/workbench/`**

   (Pattern: `https://<user>.github.io/<repo>/workbench/`.)

Serving the root also exposes the rest of the repository over Pages (it is
the same content as the repo itself). If that is ever unwanted, the
alternative is a GitHub Actions Pages workflow that uploads only
`workbench/` as the artifact — ask for it when needed; not set up by
default to keep the repo workflow-free.

All paths inside the app are relative (`data/…`, `style.css`, `app.js`),
so the subpath deployment needs no configuration.

## Updating the data

The site shows whatever is committed under `workbench/data/`. After any
engine/preset change:

```bash
.venv/bin/python scripts/precompute_web.py     # ~17 min, all three presets
git add workbench/data && git commit && git push
```

The script aborts if the engine no longer reproduces the committed goldens,
so the site can never silently drift from the engine.

To precompute an extra lever combination that the app reports as "not
precomputed" (the app shows this exact command with the right arguments):

```bash
.venv/bin/python scripts/precompute_web.py --preset preset-dmv --levers matching,prep_in_queue
```

`--full-grid` precomputes every subset — an overnight batch (~6 min per
variant at v1.4 quoting cost; 32 subsets each for University/DMV).

## Notion embed

1. Get the Pages URL above live.
2. In the Notion page (Documents & Resources → optimize-lab), type `/embed`,
   paste `https://jamesfharvey.github.io/optimize-lab/workbench/`.
3. Drag the embed full-width and tall (the app is desktop-first; the embed
   shows the same interactive view, including lever toggles).
4. Caveat: Notion embeds load in an iframe over https — the first paint
   needs Pages to be live and public. If the embed shows a blank frame,
   open the URL directly first to confirm Pages finished building.

## Customer engagements (no deploy needed)

`Load report…` in the header accepts any schema-valid ResultsReport JSON
produced by the CLI (`python -m optimize_lab run customer.json`) and renders
it in single-report mode — no code changes, no redeploy. For the full
toggle experience, precompute a bundle for the customer scenario and load
the generated `bundle.js` through the same button.

## Fonts

The design specifies Hanken Grotesk (body) / Fraunces (headings). To keep
the zero-dependency / no-CDN rule, the stylesheet declares them with
graceful system fallbacks — they render when installed locally and fall
back cleanly otherwise. If pixel-faithful typography matters for a demo,
install both families locally (Google Fonts → download) rather than adding
a CDN link.
