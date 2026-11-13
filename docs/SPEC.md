# Black Legislation Tracker — Build Spec

**Status:** Draft v1, written 2026-11-12 from Jared's direction (WhatsApp session, evening of Nov 12).
**Working title:** TBD — needs a product name before launch (see §9).
**One-line description:** A daily-updating system that tracks legislation affecting Black Americans across all 50 state legislatures (plus Congress), explains what each bill does in plain English, predicts where it is going, detects copycat bills moving state to state — and lets a visitor message their own representatives about a bill from the same screen.

## 1. Purpose

Two purposes, both load-bearing:

1. **Portfolio proof.** A step up from RideMetrics (static dashboard). This is a *system*: scheduled ingestion, change detection, NLP classification, text-similarity analysis, a validated predictive model, and a transactional action feature (the Act Box). It exists to show employers Jared can design and operate something at "big boys" level — the standard of Plural Policy / LegiScan-grade civic tech.
2. **Mission.** It advances the Black Wall Street / civic-engagement thread of Jared's work: legislation that affects Black Americans, made legible and actionable for ordinary people.

## 2. Design principles (non-negotiable)

- **Real data only.** Every bill, vote, address and statistic shown comes from a cited source system. No invented figures, no placeholder content presented as real.
- **Transparent methodology.** The classification rules, the prediction features, and the backtest accuracy are published on a methodology page. Defensibility is a feature.
- **Delivery honesty.** Any message the system sends carries a truthful status: confirmed / submitted-unconfirmed / bounced / blocked. Nothing is ever reported as "sent" on a guess. (Lesson carried from the November 2026 reparations-letter run: Gmail routes confirmed, two congressional forms submitted-but-unconfirmed, one form killed by a captcha.)
- **No guessed contact data presented as verified.** Pattern-constructed email addresses are a labelled lane, always paired with bounce tracking (see §7).
- **Ship in phases.** A finished base beats an unfinished everything. Each phase is independently demo-able.

## 3. Data sources (verified 2026-11-12)

| Source | Coverage | Cost / limits | Role |
|---|---|---|---|
| **LegiScan API** | All 50 states + US Congress, structured JSON | Free Public tier: 10,000 queries/month; registration free | Primary ingestion: sessions, master lists with change hashes, bill detail, bill text links, votes |
| **OpenStates API v3** (Plural Policy) | All 50 states + DC + PR | Free key; free tier reported ~500 requests/day (confirm at signup) | Cross-check + legislator profiles, committees, published contact info |
| **Congress.gov API** | Federal bills | Free key | Federal layer (Phase 4, optional) |
| **Official state legislature rosters** | Per state | Public web | Source of truth for the contact registry (§7.2) — patterns and published emails read from each legislature's own site |

**Rate-limit budget (LegiScan free tier math):** 10,000 queries/month ≈ 330/day. Daily run design: 51 session/master-list pulls (one per jurisdiction, returns change hashes for all bills) + detail pulls only for bills whose hash changed. Expected steady-state: well under 200 queries/day in active sessions, leaving headroom for backfills and the text-fetch jobs. If volume ever exceeds the free tier, Flex tier is the first upgrade — do not design around scraping state sites directly.

## 4. Architecture

```
[Render Cron Job — daily ~06:00 ET]
        │  Python ingestion script
        ▼
[LegiScan API] ──► change detection (bill change_hash)
        │                    │ only changed bills
        ▼                    ▼
[Postgres] ◄── bill text fetch ──► [Classification worker]
 (bills, versions, votes,            rules first → model pass →
  legislators, contacts,             summary, categories,
  classifications, predictions,      confidence, review queue)
  similarity links, message log)
        │
        ├──► [Similarity job — weekly] copycat detection (§6.3)
        ├──► [Prediction job — on change] passage probability (§6.4)
        ▼
[Public web app]  map / bill pages / legislator pages / Act Box
[Admin view]      review queue, run status, delivery log
```

- **Hosting:** Render (Jared's existing account; same pattern as RideMetrics). Web service + Cron Job + Postgres. Free tiers sleep — acceptable for v1; a paid web service (~$7/mo) is the known fix if cold-start hurts demos. Flag, don't pre-buy.
- **Language:** Python throughout (Jared's strength: Python/SQL). Ingestion, classification glue, prediction (scikit-learn) all in his demonstrable stack.
- **Front end — OPEN DECISION (§9):** Streamlit is fastest but its design ceiling is low, and Jared has explicitly said he wants a fluid, polished visitor experience and may bring in another model/tool for the design pass. Spec therefore treats the UI as a replaceable layer over the Postgres data: (a) Streamlit for the Phase 1 internal/demo build; (b) a lightweight custom front end (FastAPI + server-rendered/React) for the public launch face. Data model and jobs are identical either way.

## 5. Data model (core tables)

- `jurisdictions` — state/congress, chamber, session, source IDs
- `legislators` — name, state, chamber, district, party, photo URL (sourced), OpenStates/LegiScan IDs
- `legislator_contacts` — email (published), email (pattern-constructed), district mailbox, form URL, staff links; each with `source` and `verified` flag
- `staff` (Phase 3) — legislator FK, name, role (Chief of Staff, Legislative Director, issue-area aide), email, source — only where officially published
- `contact_patterns` — per state+chamber: domain, pattern template, roster URL, notes (the 50-state registry, §7.2)
- `bills` — jurisdiction, number, title, status, status date, sponsor FK, source IDs, change hash
- `bill_versions` — version label, text URL, text hash, fetched text (for diffing)
- `bill_classifications` — categories (multi-label), direction (expands/restricts/neutral per category), plain-English summary, confidence, method (rule/model/human), reviewer status
- `bill_similarity` — bill pair, similarity score, method, detected date
- `predictions` — bill FK, probability, model version, feature snapshot, outcome (filled when resolved)
- `votes` — roll calls on flagged bills; per-legislator votes for scorecards
- `act_messages` — user (name/address snapshot), recipient, bill, letter body as sent, lane used, status, timestamps
- `pipeline_runs` — job, started/finished, counts, errors (feeds the public "last updated" and run-status displays)

## 6. Feature spec by phase

### Phase 1 — The engine (ships first)
1. Daily ingestion per §3/§4 with idempotent upserts keyed on source bill ID + change hash.
2. Bill status normalisation across states into one ladder: Introduced → Committee → Passed chamber → Passed both → Signed/Vetoed → Dead. (States name stages differently; one public ladder is the product.)
3. Basic public pages: US map with per-state counts of flagged bills; bill list filterable by state, category, status; bill page with official title, sponsors, status timeline, link to official text.
4. Public stats header: bills tracked, states covered, last run time/status — the "it runs without me" proof.
5. Rules-based classification v1 (subjects + keyword taxonomy, §6.2) — enough to flag the initial bill set honestly as "rules-flagged."

### Phase 2 — The intelligence layer
6. **Full-text classification + summaries.** Fetch bill text; rules score first; a model pass handles borderline bills and writes the plain-English summary ("what this bill does / who it affects"). Confidence score on every classification; low-confidence items enter a human review queue (admin view). Methodology page documents the taxonomy and the confidence policy.
7. **Amendment diffing.** On each new bill version, diff against the prior version and show material changes on the bill page (sections added/struck), summarised in one line.
8. **Copycat detection.** Weekly job: text-similarity (embeddings + token overlap) across all flagged bills nationally; pairs above threshold become "traveling bill" clusters with a spread map and timeline (first introduced → where it spread → where it passed). This is the signature feature.
9. **Passage prediction.** scikit-learn model trained on completed prior sessions: features = sponsor party/chair status, committee, chamber control/trifecta, co-sponsor count and bipartisanship, category, similarity-cluster membership. Output = probability the bill becomes law, shown with the model's **backtested accuracy on last session** — the real number, published, whatever it is.
10. **Legislator scorecards.** Roll-call votes on flagged bills rolled up per legislator: votes with/against the tracked categories, shown on the legislator page.

### Phase 3 — The power map
11. **Staff layer.** Legislator pages add chief of staff, legislative director, and relevant aides *where officially published* (federal: directories + @mail.house.gov pattern; state: whatever each legislature publishes — many publish nothing, and blank is the honest answer). Staff get contact resolution through the same lanes as members (§7).
12. **Act Box targeting upgrade:** message the LD directly with the member cc'd — the staffer who writes the bill memo is the real reader.

### Phase 4 — Federal + alerts (optional, after public launch)
13. Congress via LegiScan/Congress.gov; federal Act Box lanes per §7.3.
14. Email alerts: "a flagged bill in your state moved" for registered users.

## 6.2 Classification taxonomy (v1 draft — Jared to approve/edit)

Categories (multi-label; each also gets a direction tag where determinable: expands / restricts / funds / studies):
- Voting access & election administration
- Redistricting & representation
- DEI / affirmative action / contracting set-asides
- Education: curriculum & book restrictions; HBCU & minority-serving funding
- Criminal justice & policing (incl. qualified immunity, sentencing, cash bail)
- CROWN Act / hair & appearance discrimination
- Employment & housing discrimination enforcement
- Health equity & maternal health
- Reparations / study commissions / historical redress
- Small business & Black-owned business programs
- Monuments, names & public memory

Rules layer: LegiScan/OpenStates subjects + curated keyword sets per category, maintained as data (editable without code). Model layer: only for bills the rules leave ambiguous or high-impact, and always with confidence + review path.

## 7. The Act Box (action feature)

**User flow:** visitor enters name + home address once → system resolves their districts and lists their state senator, state representative (Phase 3: plus their staff; Phase 4: federal delegation) → visitor ticks one or more tracked bills → system drafts a personal letter per bill from the tracker's own plain-English summary and direction tag (supports or opposes, visitor chooses) → visitor reviews/edits → sends → every recipient shows a live status.

### 7.1 Constituent principle
Default targets are the visitor's **own** representatives — offices act on constituents; that is what makes the tool legitimate and effective. Optional add: the bill's **sponsor** (and Phase 3, the sponsor's LD). No blast-to-everyone mode.

### 7.2 The 50-state contact registry
A build-once data asset: for each state + chamber, record from the legislature's **own official site**:
- email domain and pattern (they vary — e.g., Mississippi Senate is first-initial+lastname@senate.ms.gov, Mississippi House the same @house.ms.gov; Texas House is firstname.lastname@house.texas.gov, with District{n}.{Lastname}@house.texas.gov district mailboxes alongside)
- official roster URL (source of published individual addresses)
- form URL pattern where forms are the channel

Registry rows are sourced, dated, and re-checkable. Having built this registry is itself a portfolio talking point.

### 7.3 Sending lanes (resolution order per recipient)
1. **Verified email** — address published by the legislature/office (roster, OpenStates). Sends automatically; status: confirmed on acceptance, bounced on bounce.
2. **Pattern email** — constructed from the state's published pattern (state level) or firstname.lastname@mail.house.gov (US House, where no public listing exists by House policy). Sends automatically but permanently labelled *best effort*; bounce tracking on; a bounced pattern address is demoted and the district mailbox or form lane takes over next time.
3. **Form assist** — offices reachable only via web form (the congressional norm; captchas exist specifically to block automation). The box pre-fills everything and deep-links the form; the visitor clicks the final submit. Status: assisted, never claimed as system-sent.

Sending identity: messages go out in the visitor's own name, from the visitor's own email where the architecture allows, or via a sending service with the visitor as named sender and reply-to. No forged or bulk-blast sending. Per-message records in `act_messages` back the status UI.

## 8. Presentation layer ("look like the big boys")

- **A real name and front door** — named product, clean landing page, not a repo link.
- **Freshness on display** — "Updated today 06:0x ET · N bills tracked · 50 states" in the header; run status from `pipeline_runs`, real numbers only.
- **Fluid visitor experience** — Jared's explicit requirement: smooth, modern, click-friendly design. The front-end decision in §4 exists because of this; if a design pass with another model/tool produces the public face, it builds against this spec's data layer unchanged.
- **Methodology page** — taxonomy, confidence policy, prediction features + backtest result, contact-lane honesty (§7.3). This is what policy shops have and hobby projects don't.
- **README with architecture diagram**, tests, CI badge on GitHub. Hiring managers open the repo before the app.
- **90-second demo script** (written at launch): the copycat moment — "this Florida bill reappeared in Texas" — plus one Act Box send. For LinkedIn.

## 9. Open decisions for Jared

1. **Product name.** (Working title only so far.)
2. **Front end:** Streamlit-only (fast, design ceiling), or Streamlit for Phase 1 + custom front end for public launch (slower, matches the fluid-design requirement). Spec assumes the second; confirm.
3. **Taxonomy sign-off** (§6.2) — categories and the direction-tag approach.
4. **Prediction display:** show probabilities publicly from day one of Phase 2, or hold until the backtest number is respectable? (Spec default: publish with the real backtest number beside it.)
5. **Accounts:** Act Box without accounts (address entered per session) for v1 vs saved profiles. Spec default: no accounts in v1.

## 10. Build order & risks

**Order:** registry research (§7.2) can start in parallel with Phase 1 ingestion — it is desk research, no code dependency. Then: schema → ingestion → status ladder → basic pages → rules classification → text fetch + model pass → diffing → similarity → prediction (needs one completed session of history — backfill from LegiScan archives at build time) → scorecards → staff layer → Act Box (registry-dependent) → federal/alerts.

**Risks, stated plainly:**
- **Free-tier ceilings** (LegiScan 10k/month) — mitigated by change-hash design (§3); monitor in `pipeline_runs`.
- **State data messiness** — statuses, subjects and text formats differ per state; normalisation is real work, budgeted in Phase 1.
- **Classification is a judgement call** — mitigated by published taxonomy, confidence scores, review queue, and direction tags that cite the bill text. The tool must be able to be wrong *visibly and correctably*.
- **Staff data sparsity** (Phase 3) — publish-what-exists only.
- **Scope creep** — this spec is the fence. New ideas go in a parking list, not into the current phase.

## 11. Interview talking points (why this project exists)

- "Scheduled ETL over a rate-limited API using change-hash delta pulls — it runs daily without me."
- "I built a 50-state contact registry from primary sources, with a three-lane delivery system that reports its own failures honestly."
- "Text-similarity clustering to detect model legislation spreading between states."
- "A passage-prediction model I backtested against last session — the accuracy number is on the methodology page."
- "Rules-first, model-second classification with confidence scores and a human review queue — because in civic data, a confident wrong answer is worse than an admitted uncertain one."
