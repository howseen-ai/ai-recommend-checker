---
name: ai-recommend-checker
description: Check which brands ChatGPT recommends instead of yours. Asks 5 to 10 real buyer questions to an OpenAI model with live web search, counts which brands it names and in what order, lists the pages it cites, and renders a shareable PNG card. Use when someone asks "who does ChatGPT recommend instead of me", "am I visible in ChatGPT", "which competitors does AI recommend", or wants an AI visibility snapshot for a brand.
---

# ai-recommend-checker

You run a small, honest AI visibility check for one brand and hand back a PNG card plus the raw answers.

## 1. Collect the inputs (ask only for what is missing)

- brand name and domain (example: Glow Recipe, glowrecipe.com)
- category in 1 to 3 words (example: skincare)
- 3 competitors, each with a domain
- 5 to 10 buyer questions

Buyer questions are what a customer types before they know your brand. Write them like a buyer, not like a marketer:

- good: "Which moisturizer gives a glowy finish without feeling greasy?"
- bad: "Is Glow Recipe a good brand?" (names the brand, so it measures nothing)

Mix intents: "best X for Y", "X under $N", "alternative to [competitor]", "X for [specific person]". If the user has real data (Search Console queries, sales calls, support tickets, Reddit threads), start from it. Show the list and get a yes before spending money.

Write the config to `configs/<brand-slug>.json` using `examples/glow-recipe.json` as the template. Optional fields: `country` (ISO code, default US), `aliases` on any brand (example: "Peach and Lily").

## 2. Ask the questions

Default backend, the user's own OpenAI key:

```bash
export OPENAI_API_KEY=sk-...        # never print it, never write it to a file
python3 checker.py run configs/<brand-slug>.json --backend openai
```

This calls the OpenAI Responses API with the `web_search` tool (default model `gpt-5.5`, change it with `--model`).

Alternative backend, DataForSEO (pay per call, no OpenAI account needed):

```bash
export DATAFORSEO_LOGIN=... DATAFORSEO_PASSWORD=...
python3 checker.py run configs/<brand-slug>.json --backend dataforseo
```

Default model there is `gpt-4o-mini` with `force_web_search` on. Without that flag, or with a gpt-5 reasoning model, the answer often comes back with `web_search: false`, which means no live web at all. The script prints `web_search=True/False` for each question: if you see False, say so to the user, the run is not comparable.

Output: `runs/<brand>-<date>/answers.json` plus `raw/qNN.json` (the untouched API responses, keep them as proof).

## 3. Spot the other brands (your job, not the script's)

The script always counts the user's brand and the 3 competitors. The interesting part is who else the model names. Read `answers.json` (the `text` of each answer, ignore link titles in [brackets](urls), those are page titles, not recommendations) and write `runs/<folder>/brands_found.json`:

```json
{"brands": [
  {"name": "Laneige", "domain": "laneige.com"},
  {"name": "Paula's Choice", "domain": "paulaschoice.com", "aliases": ["Paula’s Choice"]},
  {"name": "Some Small Brand"}
]}
```

Rules:
- only brands (makers), not retailers (Sephora, Amazon) and not publications (Allure)
- the exact spelling used in the answer; add aliases for variants
- a domain only if you are sure of it (it is used for the logo); otherwise leave it out and the card shows a letter
- the script re-checks every name against the answer text, so a name that is not really there counts zero

## 4. Render the card

```bash
python3 checker.py card runs/<folder>
```

Writes `results.json`, `card.html` and `card.png` (the PNG needs `pip install playwright && playwright install chromium`; without it you still get the HTML).

How it counts:
- a brand is "named" in an answer if its name appears in the answer prose, citation links removed
- a brand typed in the question itself ("cheaper alternative to Tatcha") does not count for that answer
- position = order of first appearance among the brands found in that answer
- pages = cited URLs with tracking parameters removed, ranked by number of answers, then by number of citations

## 5. Report back

Open `card.png` and check it against `results.json` before you show it. Then give the user, in this order:

1. the card
2. one line: "<brand> named in X of N answers; most named instead: A (x/N), B (y/N)"
3. the 3 pages cited most, and whether the user's own site was cited at all
4. the total cost printed by the run

Always add these caveats in plain words:
- an API answer is close to the ChatGPT app, not identical (the app adds memory, location and its own model choice)
- one run is a snapshot: answers change from day to day, run it again before drawing a trend
- this covers one engine; Gemini, Perplexity and Google AI Overviews can say something else

## What NOT to do
- do not write questions that contain the user's brand name
- do not invent brands, counts or URLs; everything on the card must come from `answers.json`
- do not run more than 10 questions without asking (cost)
- do not commit `runs/` or any key to a public repo
