---
name: multilevel-editor
description: View and edit any nested-bullet outline .md in the browser — collapsible tree, click-to-edit bullets, drag-to-move, changes staged and written back to the .md on Save. Use whenever the user wants to "open the outline", view an outline as HTML, collapse/expand bullets, or edit an outline interactively with changes syncing to the file. Fully self-contained — the script (outline_to_html.py) lives beside this SKILL.md (static export + editable --serve mode). Works on any '- ' bullet outline with 2-space indents.
---

# Multilevel editor

Browser view for any nested-bullet outline `.md` (2-space indents, `- `
bullets): a collapsible tree, and — in serve mode — in-place editing where
changes are staged and written back to the `.md` when the user presses Save.
Backed by `outline_to_html.py`, which lives **beside this SKILL.md** (stdlib
only, inline CSS/JS, no build step).

## Commands

```bash
# static export — writes <outline>.html beside the source, read-only
python3 <skill-dir>/outline_to_html.py <outline.md>

# editable — local server + browser tab; staged edits sync to the .md on Save
python3 <skill-dir>/outline_to_html.py <outline.md> --serve [--port 8383]
```

Run `--serve` in the background and give the user the URL
(`http://127.0.0.1:<port>/`). The script opens a browser tab itself.

## Behavior (serve mode)

**Changes are STAGED in the browser and written only when the user presses the
Save button (top right).** The button sits inactive until something changes,
then lights up; closing the tab with unsaved changes triggers a browser
warning. Save serializes the whole outline from the page and rewrites the
`.md` in one shot (frontmatter preserved), then reloads.

- **Click a bullet** → its text becomes editable in place (same font/position;
  a hairline outline + tint marks edit mode). **Enter or click away stages the
  edit** (re-rendered locally); **Esc cancels**. While editing, **Tab
  indents** the bullet (last child of its previous sibling, subtree included)
  and **Shift+Tab outdents** it (parent's next sibling); both keep edit mode
  and caret position, and flash a notice when not possible.
- **Written paragraphs are a draft, stored beside the outline.** In the
  editor a paragraph's written text is a child bullet of its topic sentence
  (level 5), but the outline `.md` keeps structure only: on Save the text is
  written to `<outline>.draft.md` — the whole outline with the prose nested
  under each paragraph, `status: draft` — and on load it is re-attached by a
  fingerprint of the topic sentence. Text whose paragraph has vanished is
  reported and kept at the end of the draft under "Orphaned draft text".
  Written text has no grip, dot or number, moves only with its paragraph,
  and nothing nests under it. Create it with the quill, Tab under the
  paragraph above, or in the markdown view. Treat the draft as part of the
  outline (move/rename both together).
- **Paragraph numbers are computed by the UI, never stored in the file.** A
  bullet gets a small sequential number chip (replacing its dot) when it sits
  directly under a heading bullet and is neither a heading (`#…`) nor a
  bracketed placeholder (`[…]`). Chips recompute on every
  reorder/edit/rebuild; the markdown view and the `.md` contain plain bullets
  with no number labels.
- **Quill button — write the paragraph.** Numbered rows carry a quill next to
  the trash (hover). It posts the staged outline with the paragraph marked to
  `/generate`; the server asks the model (the `anthropic` SDK with
  `ANTHROPIC_API_KEY` if installed, else the `claude` CLI on PATH, tool-less,
  `--model`, default `claude-opus-5`) for one paragraph that uses only the
  outline's content and keeps `[cite]`-style placeholders, then inserts it as
  written text under the paragraph (staged, ⌘Z undoes, Save writes; earlier
  written text stays below). The view jumps to the written level.
- **Skeleton variants.** The "Skeleton variant…" button (top right of the
  text) opens a dialog: an instruction box with an example (a different
  legal argumentation style), **Use default settings** runs the example,
  **Generate** runs the typed instruction. `POST /variant` reserves the next
  variant number, starts the generation in a background thread and answers at
  once; the page navigates to `?doc=<name>` where a read-only "being written"
  page polls `GET /variant/status?name=` (≈0.6 s) and rebuilds the tree from
  the streamed reply (the CLI runs with `--output-format stream-json
  --include-partial-messages`; the SDK path uses `messages.stream`), with a
  banner, a progress bar and fade-in for arriving bullets; when the job is
  done the file `<outline>.variant-N.md` exists (frontmatter: summary,
  variant_of, variant, title, prompt, created, status: draft) and the page
  reloads onto it; a failure shows the error with a link back. Variants are full skeletons with their own draft and tags.
  The navigator's top level lists the family (base + variants); the open file
  expands onto its outline. Start the server on the base skeleton; it serves
  the family and refuses any other path. Unsaved edits block generation.
- **Drag to move**: a grip (`⋮⋮`) appears left of a bullet on hover; dragging
  it moves the bullet **with its whole subtree** (indicator line shows the
  before/after drop position; the bullet adopts the target's indent). Chips
  recompute live. Dropping into a bullet's own subtree is refused.
- **Markdown view**: the header's Markdown button swaps the tree for a raw
  markdown textarea holding the current staged state; edit freely — including
  adding, deleting, and re-indenting bullets — then toggle back to Outline
  (the tree rebuilds, chips recompute) or Save directly. Only the outline body
  is shown; frontmatter stays untouched.
- **Navigator** (upstream v0.12.0) — a left sidebar (toggle: panel icon, top left) listing the
  outline as a tree that **descends with the level switch**, like walking
  down a directory: in mode N every node above level N is expanded and the
  level-N nodes sit collapsed, so the top mode shows the lone title, the
  next opens it onto its sections, and so on (paragraphs straight under a
  `##` stay out of a heading view). Carets expand a node by hand — showing
  all its children — until the next level change. Clicking a label scrolls
  the outline to that bullet and flashes it, switching to its level first
  when the current mode hides it. Written text is never listed. Open beside
  the text on wide screens (remembered), an overlay under 900px. Works in
  the static export too.
- **Collapse/expand** via the caret on branch bullets; the **level switch**
  centred at the bottom of the header has one icon button per heading level
  (n bulleted, stepped-in bars), then
  the paragraph level (¶), then the written-text level (prose lines) (`2` = `#`/`##` headings,
  `3` = plus `###`, `4` = paragraphs, `5` = fully written paragraphs — the
  bullets nested under each paragraph; `1` omitted for a lone title). Bullets below the
  chosen level are removed from view, not collapsed (branches with all
  children hidden show a dot; a bullet being edited always shows), and the
  choice is remembered per file across reloads. Text selection inside a
  bullet triggers edit mode — Esc backs out without saving.
- **Conflict safety**: the page carries a hash of the file it was built from;
  if the file changed on disk in the meantime, Save is refused (409) rather
  than clobbering — refresh and redo (staged changes are lost). Reloading
  re-reads the file, so external edits appear on refresh.
- **Normalisation on save**: multi-line bullets (continuation lines) are
  joined to a single line; exactly the bullets' text is saved — no number
  labels are added or maintained.

## Server lifecycle

- One server per file. Default port 8383; pick another with `--port` if busy.
- Restart after changing the script (the running process serves old code):
  `lsof -ti:<port> | xargs kill`, then relaunch.
- Stop when done the same way; nothing persists beyond the `.md` itself.

## Caveats

- **Edits go to the outline `.md` only** — never to whatever source document
  the outline was derived from. Carrying an outline change back into a source
  document is a separate, deliberate step.
- **The UI's number chips are positional** — they shift on any reorder and
  make no claim about any external numbering scheme.
- The static `.html` export goes stale the moment the `.md` changes;
  regenerate it, or use serve mode, which re-reads on every load.
