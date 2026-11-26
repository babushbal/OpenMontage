# Compose Director — Book Scene Pipeline

## Job
Cut the rendered shots into the scene, with the book's own words on screen, and hand the
user files they can keep editing.

## Route by `render_runtime`
Read `render_runtime` from the approved `proposal_packet` and honour it — a silent swap is a
governance violation.
- `ffmpeg` (the usual choice for book scenes): everything below, via `book_scene_assemble`.
- `remotion`: still run `book_scene_assemble` for the clean cut + SRT, then build the designed
  titles/captions in Remotion (`video_compose`) on top of the clean cut, following
  `pipelines/cinematic/compose-director`.
- `hyperframes`: same, with `hyperframes_compose` for HTML/GSAP title cards or kinetic quotes.

## Run
`book_scene_assemble` with:
```json
{
  "project_id": "<project>",
  "name": "<scene-name>",
  "order": ["s1", "s2", "..."],
  "language": "he",
  "end_card": {"title": "<optional>", "subtitle": "<optional, prefer a verbatim book line>"}
}
```
It produces, under `projects/<project>/renders/`:
- `<name>.mp4` — final cut, burned-in captions (each shot's verbatim `source_quote`), end card
- `<name>_clean.mp4` — no captions, no end card
- `<name>.<lang>.srt` — timed exactly to the clean cut, RTL-safe, UTF-8 with BOM

Shots with `text_overlays` (letters, diaries, dedications) get the real text composited over
a slowed, softened copy of the generated footage, with the writer's name.

## Rules
- **Order** follows the passage unless the user re-orders. When the user re-orders (e.g. moves
  the opening shot to the end), re-run with `order` — the SRT is regenerated to match.
- **End card**: offer it, don't impose it. For a memorial book, a verbatim closing line from the
  book fits; ask before adding anything not in the book.
- **Check before sending**: grab one frame per shot plus a close crop of a caption. In
  Hebrew/Arabic confirm periods, quotes and parentheses sit on the correct side, and that no
  overlay text runs out of its box.
- **Deliver**: the final cut, plus the clean cut + SRT for DaVinci Resolve / Premiere
  (Resolve: File → Import → Subtitle, drop on the subtitle track at 00:00).
- `projects/` is not committed — tell the user to download the files.
