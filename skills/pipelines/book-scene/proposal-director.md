# Proposal Director (Producer) — Book Scene Pipeline

## Job
Pin down exactly which passage becomes the video, how long it runs, and what it costs.

## Steps
1. Locate the passage. The user may give pages, a quote, a chapter, or a description
   ("the scene where she finds the letter"). For a description, search `text.txt` and propose
   up to 3 candidate passages with page ranges and a one-line summary each — these are the
   `concept_options`. Quote the opening and closing sentence of each.
2. Size it: roughly one shot per 1-3 sentences of action, 5-10 s per shot, 3-8 shots
   (30-60 s) unless the user asks otherwise. Long passages: propose a tighter excerpt rather
   than skipping events silently.
3. Delivery promise: `motion_required: true` — every shot is a generated video clip, never a
   still slideshow.
4. Render runtime: present both composition runtimes and let the user pick, then record a
   `render_runtime_selection` decision in `decision_log` and set `render_runtime` in the packet:
   - `remotion` — straight cut of the clips with optional captions (verbatim book text, RTL
     aware) and a title card. The usual choice for book scenes.
   - `hyperframes` — when the user wants designed HTML/GSAP title cards or kinetic quotes
     around the clips.
   Never default silently; the compose stage must use the runtime chosen here.
5. Cost: total generated seconds × the current Kling O3 per-second price + any reference
   images still to generate + optional narration/music, as a per-item breakdown. fal tools do
   not ship price tables (`estimate_cost` raises `PriceQuoteRequired`): take the price from the
   model's fal page or ask the user, and say where the number came from.
6. Fill `proposal_packet` per `schemas/artifacts/proposal_packet.schema.json`; keep it
   `awaiting_human` until the user approves. Record the passage page range and quotes in
   `selected_concept` and the decision in `decision_log`.
