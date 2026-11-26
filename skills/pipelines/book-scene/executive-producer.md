# Executive Producer — Book Scene Pipeline

## When to Use

The user uploaded a book (PDF / EPUB / TXT) and wants a short video of a scene from it:
"make a video of the chapter where...", "turn pages 40-42 into a scene", "סרטון של הסצנה שבה...".
You orchestrate a small crew of agents. Each stage below is one agent role with its own skill:

| Role | Stage | Skill | Output |
|------|-------|-------|--------|
| Librarian | `book_ingest` | `ingest-director` | `book_source` |
| Casting director | `character_bible` | `casting-director` | `character_bible` (book-level, reusable) |
| Producer | `proposal` | `proposal-director` | `proposal_packet` (passage, runtime, cost) |
| **Director** | `scene_plan` | `scene-director` | `scene_plan` + `book_shot_list` (one prompt per shot) |
| Fidelity critic | inside `scene_plan` | `fidelity-critic` | `book_shot_list.fidelity_review` |
| **Video agent** | `assets` | `asset-director` | one reference-to-video clip per shot |
| Editor / compositor | `edit`, `compose` | `pipelines/cinematic/edit-director`, `compose-director` | final video |

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
2. Reuse before you rebuild: if `books/<book_id>/book_source.json` and
   `character_bible.json` already exist, skip ingest and only cast characters that are new
   to this scene. Never regenerate a `locked` character.
3. Run the stages in order and write a checkpoint per stage (`meta/checkpoint-protocol`).
   Gates with `human_approval_default: true` (character_bible, proposal, scene_plan, assets)
   stop for the user. Show reference images and the shot list; don't summarise them away.
4. Spawn the fidelity critic as a **separate sub-agent** when your harness has one (it must not
   see the director's reasoning — only the passage, the bible and the shot list). Otherwise run
   `fidelity-critic` yourself after a deliberate reset: re-read the passage first.
5. Budget: video generation is the dominant cost. Before `assets`, state total seconds × the
   quoted per-second price (see `proposal-director`) and wait for the yes. Sample the first shot alone before rendering the
   rest so the user can confirm the look and the identity.

## Language

Books are often Hebrew/Arabic (RTL). Keep quotes, dialogue and captions in the book's
language exactly as printed; write `video_prompt` in English (video models follow English best).
Captions in RTL scripts must go through the caption RTL path (`tests/contracts/test_caption_rtl_direction_contract.py` documents it).

## Copyright

Adapting a book is the user's responsibility. If the user says the book is not theirs and
not public domain, mention once that publishing an adaptation may need the rights holder's
permission, then continue.
