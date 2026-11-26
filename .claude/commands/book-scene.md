---
description: Turn scenes from an uploaded book (PDF/EPUB/DOCX/TXT) into short videos with consistent characters. Reads the book, proposes a menu of filmable scenes, the user picks, then casts, directs, renders and cuts.
argument-hint: [path to the book file] [optional: which scene / pages]
---

Run the `book-scene` pipeline. Read `skills/pipelines/book-scene/executive-producer.md` first and
follow it; read each stage's director skill before that stage.

1. **Ingest** the book with `book_ingest` (`ingest-director`). Reuse `books/<book_id>/` if it exists.
2. **Scout**: unless the request below already names the scene, read the whole book section by
   section and present a menu of 6-10 scenes (`scout-director`): title, pages, characters,
   one-line summary, length, and what you need from the user (photos of real people). Verify the
   quotes with `book_ingest verify_candidates`. **Stop and let the user pick.**
3. For the picked scene(s): cast (`casting-director`) → passage + cost (`proposal-director`) →
   shot list + fidelity gate + critic (`scene-director`, `fidelity-critic`) → sample shot, then the
   rest (`asset-director`, `book_shot_render`) → cut (`compose-director`, `book_scene_assemble`).
4. Deliver the final cut, plus the clean cut and the SRT for DaVinci Resolve.

Stop for the user at every approval gate. Never spend on generation before the user approved
the cost. Talk to the user in their language.

Request: $ARGUMENTS
