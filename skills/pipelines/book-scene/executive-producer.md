# Executive Producer — Book Scene Pipeline

## When to Use

The user uploaded a book (PDF / EPUB / DOCX / TXT) and wants short videos of scenes from it:
"suggest scenes from this book", "make a video of the chapter where...", "turn pages 40-42 into
a scene", "סרטון של הסצנה שבה...". `/book-scene <file>` starts here.
You orchestrate a small crew of agents. Each stage below is one agent role with its own skill:

| Role | Stage | Skill | Output |
|------|-------|-------|--------|
| Librarian | `book_ingest` | `ingest-director` | `book_source` |
| **Scene scout** | `scene_scout` | `scout-director` | `scene_candidates` — the menu the user picks from |
| Casting director | `character_bible` | `casting-director` | `character_bible` (book-level, reusable) |
| Producer | `proposal` | `proposal-director` | `proposal_packet` (passage, runtime, cost) |
| **Director** | `scene_plan` | `scene-director` | `scene_plan` + `book_shot_list` (one prompt per shot) |
| Fidelity critic | inside `scene_plan` | `fidelity-critic` | `book_shot_list.fidelity_review` |
| **Video agent** | `assets` | `asset-director` | one reference-to-video clip per shot (`book_shot_render`) |
| Editor / compositor | `edit`, `compose` | `pipelines/cinematic/edit-director`, `compose-director` | final cut + clean cut + SRT (`book_scene_assemble`) |

## The three promises of this pipeline

1. **Same character, every shot, every scene.** Identity comes only from locked reference
   images in `books/<book_id>/character_bible.json`, passed to Kling O3 as `elements`.
   Prompts never re-describe a face; they say `@Element1`.
2. **Close to the source.** Every shot is anchored to a verbatim quote of the passage.
   No invented events, objects or dialogue. Unstated visual details are filled only from
   the bible's `visual_style` and listed in `invented_details`.
3. **The book's own pictures win.** If the book illustrates a character, that illustration is
   the identity. Text-generated identities are the fallback.

## Orchestration

1. Read `pipeline_defs/book-scene.yaml`, then each stage skill before running that stage.
   The default entry is **scout first**: ingest → `scene_scout` menu → the user picks → only
   then cast, plan and render. If the user already named the scene, skip the menu.
2. Reuse before you rebuild: if `books/<book_id>/book_source.json` and
   `character_bible.json` already exist, skip ingest and only cast characters that are new
   to this scene. Never regenerate a `locked` character.
3. Run the stages in order and write a checkpoint per stage (`meta/checkpoint-protocol`).
   Gates with `human_approval_default: true` (scene_scout, character_bible, proposal, scene_plan, assets)
   stop for the user. Show reference images and the shot list; don't summarise them away.
4. Spawn the fidelity critic as a **separate sub-agent** when your harness has one (it must not
   see the director's reasoning — only the passage, the bible and the shot list). Otherwise run
   `fidelity-critic` yourself after a deliberate reset: re-read the passage first.
5. Budget: video generation is the dominant cost. Before `assets`, state total seconds × the
   quoted per-second price (see `proposal-director`) and wait for the yes. Sample the first shot alone before rendering the
   rest so the user can confirm the look and the identity.

6. Several selected scenes: run casting once for all of them (shared bible), then plan →
   render → compose per scene. Render a scene in the background while you plan the next.
7. If the plan changes while shots are rendering (a new character, a new style), stop the
   render first and cancel any submitted fal job (the job file in `assets/shots/` holds its
   `request_id`) before re-planning, so the user doesn't pay for shots that will be replaced.

## Lessons from production (keep them)

- **Real people** (memoirs, biographies, family books): never invent a face. Ask the user
  for a photo of every on-screen person, including the narrator/author. Until then, show
  the narrator only from behind or as hands.
- **Age variants**: a childhood scene of an adult whose photo you have needs a separate
  identity (e.g. `abi_child`) generated from the adult photo with image edit; the user
  picks between 2 candidates before it is locked.
- **Style follows the references**: photo references pull the video model toward realism,
  sketches toward illustration. Choose the scene's style from the first rendered shot and
  keep it for every shot of that scene; don't fight the references mid-scene.
- **Readable text never comes from the video model** — it writes fake letters. Put real
  book text in `text_overlays`; the assembler composites it.
- **fal errors**: a 403 with "Exhausted balance" means the account needs a top-up; a lone
  403 on one shot can be transient — check the account, then resubmit that shot once.

## Language

Books are often Hebrew/Arabic (RTL). Keep quotes, dialogue and captions in the book's
language exactly as printed; write `video_prompt` in English (video models follow English best).
Captions in RTL scripts go through `book_scene_assemble`, which wraps every line in RTL
embedding marks; without them, periods, quotes and parentheses land on the wrong side.

## Copyright

Adapting a book is the user's responsibility. If the user says the book is not theirs and
not public domain, mention once that publishing an adaptation may need the rights holder's
permission, then continue.
