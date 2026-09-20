# SESSION REPORT

This is the project's incremental working log — built up turn-by-turn across
16 sessions, never rewritten after the fact. Two documents synthesize it into
the actual deliverables and should be read first:
[output/reports/SOURCE_TECHNICAL_REPORT.md](output/reports/SOURCE_TECHNICAL_REPORT.md)
and [output/reports/SOURCE_AUDIT_FINAL_CLOSURE_REPORT.md](output/reports/SOURCE_AUDIT_FINAL_CLOSURE_REPORT.md).

**Known ordering note**: because this file was extended via targeted text
edits rather than rewritten, two blocks near the end are chronologically
earlier than their physical position suggests: the `## Final Decision
(MASTER_PLAN.md §54 ...)` heading appearing after "Session 16" is actually the
tail end of Session 14's own Phase 16 report-assembly work, and the final
`## Session 1 (cont'd)` heading documents Phase 0's authorization check and
Phase 1's HTTP client — chronologically the second thing done in this project,
right after Session 1's scaffolding. No content is duplicated or missing;
these two blocks simply read out of physical order in this file. Flagging this
directly rather than risking a manual reorder of an 1800+ line file that could
silently corrupt content — consistent with this project's own rule to document
limitations rather than paper over them.

## Session index (chronological, by content — not always by physical position in this file)

1. Session 1 — project scaffolding (Phase 0)
2. Session 1 (cont'd), at the end of this file — Terms/authorization check + HTTP client (Phase 1)
3. Session 2 — Homepage analysis (Phase 2)
4. Session 3 — Anime page analysis (Phase 3)
5. Session 4 — Episode, player, media analysis (Phases 4-6)
6. Session 5 — VOSTFR/VF sampling at scale (Phase 7)
7. Session 6 — Unique identification (Phase 8)
8. Session 7 — New-episode detection strategies (Phase 9)
9. Session 8 — Deduplication (Phase 10)
10. Session 9 — Restart/recovery simulation (Phase 11)
11. Session 10 — Structure change detection (Phase 12)
12. Session 11 — Resilience tests (Phase 13)
13. Session 12 — Multi-anime test (Phase 14)
14. Session 13 — Concurrency simulation (Phase 15)
15. Session 14 — Final report (Phase 16), including its own `## Final Decision`
    section physically located at the end of this file
16. Session 15 (closure pass 1, embedded under the Session 14 heading without
    its own subheading) — cache TTL/ffprobe/pagination/HLS/VF-VOSTFR closure
17. Session 16 — closure pass 2 (security/observability/authorization framing,
    final report restructure)

---

## Session 1 — 2026-09-17

### Phase completed

**Phase 0 — Initialization**

### Work done

- Created full project structure under `source_audit/` (`src/source_audit/{fetch,analysis,detection,evidence,reporting}`,
  `tests/{unit,integration,fixtures}`, `scripts/`, `output/{raw,html,media,screenshots,evidence,reports}`, `config/`).
- Created `pyproject.toml` (Python 3.12+ target, pydantic/httpx/bs4/lxml/pyyaml/python-dotenv deps, pytest config
  pointed at `src/`), `requirements.txt`, `.gitignore` (excludes raw HTML, media, evidence, logs, `.env`).
- Created `config/config.example.yaml` (base_url, http timeout/retry, evidence storage toggles, log level).
- Created `src/source_audit/config.py` (`SourceAuditConfig.load()` from YAML, `.env` support via python-dotenv).
- Created `src/source_audit/logging_config.py` (stdlib logging, INFO default, explicit format with timestamps).
- Created `src/source_audit/models.py`:
  - `EvidenceStatus` enum — the exact 6 statuses mandated by MASTER_PLAN.md §5.
  - `Confidence` enum (HIGH/MEDIUM/LOW/UNKNOWN).
  - `Language` enum (VF/VOSTFR/UNKNOWN).
  - `HomepageEntry`, `AnimeRecord`, `EpisodeRecord`, `MediaObservation` pydantic models —
    all optional fields default to `None` (never a guessed value), per §34.
- Created `README.md` and this `SESSION_REPORT.md`.
- Created `MASTER_PLAN.md` (summary + phase list + rules, referencing the full plan the
  owner supplied).

### Tests executed

Environment: Python 3.14.7 (Windows), installed via `pip install -e ".[dev]"`.

```
python -m pytest -q
```

Result: **5 passed** (`tests/unit/test_models.py` — 4 tests; `tests/unit/test_config.py` — 1 test).

Covered:
- `EvidenceStatus` enum exact value set.
- `HomepageEntry` rejects empty URL (validation).
- `HomepageEntry` leaves unknown fields as `None`.
- `EpisodeRecord` accepts `Language` enum.
- `SourceAuditConfig.load()` correctly parses `config/config.example.yaml`.

### Bugs found

None.

### Decisions

- Python 3.14.7 is the available interpreter in this environment; `pyproject.toml`
  declares `requires-python = ">=3.12"` per the plan (3.14 satisfies this).
- Used `pydantic` (v2 API: `field_validator`) rather than plain dataclasses for
  `HomepageEntry`/`AnimeRecord`/`EpisodeRecord`/`MediaObservation` because the plan's
  §34 requires strict validation (non-empty title, valid URL, coherent number) which
  pydantic validators express directly; `SourceAuditConfig` uses a plain dataclass
  since it has no such validation needs beyond a required-key check.

## Session 2 — 2026-09-17

### Phase completed

**Phase 2 — Homepage analysis**

### Work done

Full findings: [output/evidence/phase2_homepage_structure.md](output/evidence/phase2_homepage_structure.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- Fetched and saved live homepage, `/page/2/`, `/nouveaux-ajouts/`, `/prochainement/`
  (`output/raw/*.html`) and inspected structure with BeautifulSoup/lxml.
- Confirmed anime-block container `.page-item-detail.video` (16 per page), title
  `.item-summary .post-title a`, episodes `.list-chapter .chapter-item` (label + URL
  via `a.btn-link`, date via `.post-on` — **two date formats observed**: relative
  "1 second ago" and absolute "September 10, 2026"), VF badge `.manga-vf-flag`
  (anime-block-level only), thumbnail `.item-thumb img`.
- Confirmed `/page/N/` continues the **same** latest-update feed (page 2 dates start
  at "1 day ago" where page 1 ends at "hours ago"; zero URL overlap between the two
  pages fetched in-session) — "Page 1 of 100" in the footer implies up to ~1600
  (anime, latest-episode) records reachable this way.
- **Compared `/`, `/nouveaux-ajouts/`, `/prochainement/` directly instead of assuming
  they're interchangeable "new stuff" feeds** — they are not:
  - `/` (paginated): anime sorted by latest **episode** update (seconds-old on page 1).
  - `/nouveaux-ajouts/`: anime **series** recently added to the catalog (dates
    observed weeks/months old in the sample), not necessarily new episodes.
  - `/prochainement/`: upcoming anime with **zero chapters** in all 16 sampled blocks
    — not a source of watchable/downloadable episodes at all.
  - Consequence for Phase 9: the homepage/`page/N/` feed is the correct signal for
    "new episode" detection; `/nouveaux-ajouts/` answers a different question
    ("new series"); `/prochainement/` is not an episode source.
- Determined language detection has two independent signals, cross-checked:
  1. Episode URL slug suffix `-vf` / `-vostfr` — **64/64 (100%) match** across the two
     homepage pages sampled → HIGH confidence.
  2. Film episodes use a *different* slug shape instead (`film-vf-...` /
     `film-vostfr-...` as the last path segment, prefix not suffix) — 4/24 URLs on
     `/nouveaux-ajouts/` → MEDIUM confidence (real pattern, smaller sample).
  3. Anime-level `.manga-vf-flag` badge kept only as a LOW-confidence fallback when no
     URL signal is available (it can't distinguish per-episode language within a
     mixed-language anime block).
  - `INCONCLUSIVE`: whether a third URL-slug shape exists beyond these two — only
    partially sampled so far; full sampling deferred to Phase 7.
- Implemented `src/source_audit/analysis/homepage.py`:
  `parse_homepage(html) -> list[HomepageEntry]` (one entry per episode link, not per
  anime) and `detect_language(url, anime_has_vf_badge) -> LanguageDetection` with an
  explicit priority order (URL suffix > URL film-prefix > badge fallback > UNKNOWN),
  each result carrying its `basis` string for traceability.
- Built `tests/fixtures/homepage_sample.html`, a hand-trimmed but structurally
  faithful excerpt (VOSTFR show, VF show, VF film, and a no-chapters edge case) for
  deterministic offline unit tests.
- `scripts/analyze_homepage.py`: CLI (`--url`, `--page`, `--timeout`, `--output`,
  `--verbose`) that fetches + parses + emits JSON. Run live against the real site:
  32 entries parsed from the homepage in 1.5s, first entry's `language` and
  `source_selector` (incl. `lang_basis`) verified by hand.

### Tests executed

```
python -m pytest -q
```

Result: **29 passed** (24 unit incl. 9 new homepage-parser tests, 5 live integration
incl. 2 new homepage-parsing-against-live-HTML tests).

New unit tests (`tests/unit/test_homepage.py`): entry count from the fixture, VOSTFR
episode via URL suffix, VF episode via URL suffix, film language via URL prefix,
thumbnail capture, no-chapters block contributes nothing, `detect_language()`
priority order (suffix beats badge; badge fallback; UNKNOWN when no signal at all).

New integration tests (`tests/integration/test_homepage_live.py`): live homepage
parses to a non-trivial entry count with well-formed URLs/titles and a mix of
languages; `/page/2/` returns a genuinely different URL set than page 1 (confirms
real pagination, not a caching artifact).

### Bugs found

None.

### Decisions

- `parse_homepage()` emits one `HomepageEntry` per **episode** link (not per anime),
  since each anime block can carry 1-2 distinct chapters with distinct dates/URLs —
  downstream dedup/discovery logic (Phase 9/10) needs episode granularity, not
  anime granularity.
- Language detection deliberately prioritizes the per-episode URL slug over the
  anime-level VF badge, because the badge cannot express "this specific chapter is
  VF" for an anime block that could in principle mix languages across its listed
  chapters — the URL is the most specific signal actually observed.
- Kept the homepage-vs-`/nouveaux-ajouts/`-vs-`/prochainement/` distinction as a
  documented fact rather than picking one arbitrarily, per MASTER_PLAN.md's
  "never assume a single page is representative" rule — this directly shapes what
  Phase 9 (new-episode detection) is allowed to assume.

## Session 3 — 2026-09-17

### Phase completed

**Phase 3 — Anime page analysis**

### Work done

Fetched and structurally compared 6 live anime pages (exceeds the §14 minimum of 5),
deliberately covering: a VOSTFR-only ongoing show (12 ep.), a VF/"JAP" language pair
of the same title ("Tomb Raider King"), a small VOSTFR-only show (11 ep.), and a
VOSTFR/VF pair of a season-4 franchise entry ("Re:Zero ... S4").

**Findings (all TESTED/MEASURED, HIGH confidence — 6/6 pages agree):**

- Title: `.post-title h1`. Metadata: `.post-content_item` pairs of
  `.summary-heading` (label) / `.summary-content` (value). **The label set is not
  fixed** — observed labels: Native, Romaji, English, Note, Type, Status, Studios,
  Episodes, Start date, Genre(s). "English" was absent on "Mebius Dust"; "Episodes"
  (total planned count) was absent on both "The Exiled Heavy Knight..." and "Mebius
  Dust". Parsing must key off label text, never assume a fixed field order/set.
- **"Episodes" = total planned count for the season, not episodes released so far**
  (MEASURED): "Tomb Raider King" VF and JAP pages both declare **12** planned, while
  the VF page currently lists 8 released episodes and the JAP page lists 11 — i.e.
  the dub visibly lags the sub in release count on this title. Same pattern on
  Re:Zero S4: 19 declared, 17 released VOSTFR / 14 released VF.
- Episode list: `.listing-chapters_wrap li.wp-manga-chapter`, each `a[href]` (verbose
  link text) + `.chapter-release-date i` (date, same two formats as the homepage).
  Descending order (newest first) on every page checked. **No chapter-list
  pagination control found on any of the 6 pages** (max 17 episodes seen).
  `INCONCLUSIVE`: whether very long-running shows (100+ episodes) paginate this list
  — not sampled; no such title was found on this site in the pages checked so far.
- **No season-selector element exists on any anime page** (checked `.season-name`,
  `.wp-manga-season`, `.select-season`, etc. — all 0 matches on all 6 pages) —
  confirms each season of a franchise is its own separate anime page/post
  (e.g. "Re:Zero ... S4" is a distinct anime entity, not a tab within one "Re:Zero"
  page).
- **VF and VOSTFR/"JAP" of the same title are two entirely separate anime pages**,
  with independently numbered, non-overlapping episode-URL sets and independent
  release counts — confirmed both by direct inspection and by a live integration
  test (`test_live_vf_and_jap_variants_are_independent_anime_entities`) asserting
  `vf_urls.isdisjoint(jap_urls)`. Direct consequence for Phase 8 (unique
  identification): the anime_key must be scoped per language variant; there is no
  single anime page that spans languages to key off of.
- **Episode URL slugs are not derivable from the anime URL slug by concatenation**:
  anime slug `rezero-kara-hajimeru-isekai-seikatsu-s4` produces episode URLs under
  `re-zero-kara-hajimeru-isekai-seikatsu-saison-4-{N}-vostfr` — extra hyphen in
  "re-zero", "s4" spelled out as "saison-4". Episode URLs must always be read from
  the page's own `.wp-manga-chapter a[href]`, never constructed from the anime slug.

### Work done — implementation

- Extended `models.py`: `AnimeEpisodeLink` (label/url/published_at_raw) and expanded
  `AnimeRecord` with `native_title`, `romaji_title`, `english_title`, `status_raw`,
  `anime_type_raw`, `studios`, `start_date_raw`, `genres`, `total_episodes_declared`,
  `episode_links` — kept `season_labels`/`episode_count_observed` (pre-existing
  fields) rather than removing them, per the "never delete prior knowledge" rule,
  with a docstring recording that seasons are expected empty given this evidence.
- `src/source_audit/analysis/anime.py`: `parse_anime_page(html, url) -> AnimeRecord`,
  label-keyed metadata extraction (not position-based), genre-list splitting,
  "Episodes" total parsed via regex digit extraction, full episode-link extraction.
- Fixtures: `tests/fixtures/anime_page_sample.html` (full field set, VF episode
  numbering) and `anime_page_sample_missing_optional_fields.html` (reproduces the
  "Mebius Dust" case: no English title, no Episodes-total field) — both hand-trimmed
  but structurally faithful, with invented (non-real) content to avoid embedding a
  full copy of live site data.
- `tests/unit/test_anime.py` (5 tests): title/core metadata, genre-list parsing,
  declared-vs-observed episode count divergence, episode link URL/date extraction,
  missing-optional-fields case leaves `None` rather than guessing.
- `tests/integration/test_anime_live.py` (2 tests, live): all 6 real anime URLs parse
  with a non-empty title and ≥1 episode link; VF/JAP variant episode URL sets are
  disjoint (the "separate entities" finding, asserted programmatically, not just
  observed by eye).
- `scripts/analyze_anime.py` CLI (`--url`, `--timeout`, `--output`, `--verbose`) — run
  live against "Mebius Dust", output matched the manual inspection exactly (11
  episodes observed, no declared total).

### Tests executed

```
python -m pytest -q
```

Result: **36 passed** (29 unit incl. 5 new anime-parser tests, 7 live integration
incl. 2 new anime-page tests covering 6 distinct anime URLs).

### Bugs found

None.

### Decisions

- Metadata parsing is label-driven (case-insensitive match on `.summary-heading`
  text) rather than positional, because the field set demonstrably varies between
  pages (English/Episodes both optional) — a positional parser would have
  mis-assigned values the moment a field was missing.
- Kept `season_labels`/`episode_count_observed` in `AnimeRecord` unchanged rather
  than removing them now that seasons are known not to appear on-page, since a
  future observation could still populate them (e.g. if a different show template is
  found) — removing fields based on one round of evidence would violate the
  "don't destroy prior knowledge" rule as much as ignoring new evidence would.
- Did not attempt to fetch a 100+ episode show to resolve the chapter-pagination
  INCONCLUSIVE this session — no such title was surfaced by the search/homepage
  pages checked; flagged for a future session rather than guessing.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection (homepage) | TESTED (URL suffix) / MEASURED (film prefix) | HIGH (suffix) / MEDIUM (film) | `output/evidence/phase2_homepage_structure.md`, `test_homepage.py` | See Phase 2 below; full Phase 7 sampling still pending |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed page deep-dive + cross-checked domain/pattern across all 7 episode pages, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests parsed end-to-end, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode (1 vs 2 seen), documented not assumed; ffprobe unavailable in this environment |

## Session 4 — 2026-09-17

### Phases completed

**Phase 4 — Episode page analysis**, **Phase 5 — Player analysis**, **Phase 6 — Media analysis**

(Combined into one session: all three findings came from following the same real
request chain — episode page → embed iframe → HLS manifest — end to end, so
splitting them across separate sessions would have meant re-fetching the same live
evidence three times.)

### Work done

Full findings: [output/evidence/phase4_5_6_episode_player_media.md](output/evidence/phase4_5_6_episode_player_media.md)
(gitignored evidence dir — summarized here so it survives without that directory).

**Phase 4 (episode page, 7 pages fetched across 5 anime):**
- `<title>` tag: `"{AnimeTitle} - {AnimeTitle} - {N} {LANG} - {N} - Voiranime"` on
  all 7 — consistent, usable as a fallback identity signal.
- No `<h1>`/synopsis/description anywhere on an episode page — all descriptive
  metadata lives on the anime page (Phase 3), not here.
- Exactly one `<iframe>` per page, always `voembed.net/embed-{id}.html`, on all 7 —
  no alternate-server selector UI found anywhere, no direct download link/button
  found anywhere either.
- `.nav-links a` (duplicated block, identical hrefs both times) is boundary-correct:
  episode 1 → "Next" only, latest episode → "Prev" only (checked on 2 pages).

**Phase 5 (player/embed, 1 page deep-dived, domain pattern cross-checked on 7):**
- `voembed.net/robots.txt` → 404, same "no explicit policy" situation as the main
  site (Phase 0).
- No `<video>`/`<source>` tag; playback is entirely JS-driven (15 `<script>` tags on
  the sampled page). One script matches the Dean Edwards JS-packer signature
  (`eval(function(p,a,c,k,e,d)`) — **recorded as a fact, deliberately never
  deobfuscated or executed**, per MASTER_PLAN.md §16/§49.
- A separate, plain ~20KB script contains a literal `jwplayer(...)` call and a
  `sources: [{ file: '...master.m3u8?...' }]` config — **JW Player**, with the
  manifest URL handed to any viewer's browser in cleartext during normal page load
  (not extracted by bypassing anything).
- The manifest URL carries a signed, time-limited token on a **third** domain
  (`*.vmget.online`, distinct from `voir-anime.to` and `voembed.net`) — 3 domains
  involved end-to-end. Token expiry `e=43200` (~12h) on every sample fetched.

**Phase 6 (media/HLS, 3 manifests fetched via 3 separate full chains):**
- Protocol: HLS on all 3. Audio: AAC-LC (`mp4a.40.2`) on every rendition. Frame
  rate: 23.974 fps on every rendition. Every manifest also carries an
  `EXT-X-I-FRAME-STREAM-INF` trick-play playlist.
- **Rendition count varies per episode — documented as a correction, not a
  replacement, of the first observation** (MASTER_PLAN.md §36): the very first
  manifest sampled ("The Exiled Heavy Knight...", ep 12) had only **1** rendition
  (1080p, ~3.85 Mbps, `avc1.640028`). Two further samples ("Mebius Dust" ep 1, "Tomb
  Raider King (VF)" ep 1) each had **2** renditions: 1080p (~3.0-3.1 Mbps,
  `avc1.640028`) + 480p (~0.48-0.50 Mbps, `avc1.4d401f`). `INCONCLUSIVE` what
  determines the difference — only 3 samples so far; the parser makes no assumption
  about rendition count either way.
- `ffprobe` was checked and is **not installed** in this environment
  (`ffprobe -version` → command not found) — flagged `BLOCKED`/`NOT_EVALUATED`
  rather than silently skipped or worked around by installing new system binaries
  unprompted. Duration, exact segment bitrate, and subtitle-track presence are
  therefore `NOT_EVALUATED` this session; the manifest-text parsing above stands on
  its own as a lighter, spec-based alternative requiring no downloaded video bytes.

### Work done — implementation

- `models.py`: added `PlayerObservation` (iframe domain/title, player library,
  `has_obfuscated_script` flag, manifest URL when found in cleartext) and
  `MediaRendition` (per-`EXT-X-STREAM-INF` resolution/bandwidth/fps/codecs/URL);
  extended `EpisodeRecord` with `page_title_raw`, `prev_episode_url`,
  `next_episode_url`; extended `MediaObservation` with `renditions: list[MediaRendition]`.
- `analysis/episode.py`: `parse_episode_page(html, url) -> EpisodeRecord`.
- `analysis/player.py`: `parse_embed_page(html, iframe_url) -> PlayerObservation` —
  detects player library and packed-script signature via plain string/regex
  matching only (no execution), extracts the manifest URL only when it appears in
  cleartext.
- `analysis/media.py`: `parse_hls_master_manifest(text) -> list[MediaRendition]` —
  parses `EXT-X-STREAM-INF` lines per RFC 8216, ignores the I-frame trick-play
  entries, makes no assumption about rendition count.
- Fixtures (all hand-trimmed, invented non-real slugs/tokens, structurally
  faithful): `episode_page_sample.html`, `embed_page_sample.html` (includes a
  minimal but real-shaped packed-JS snippet and a `jwplayer(...)`/`master.m3u8`
  config), `hls_master_sample.m3u8`.
- Unit tests: `test_episode.py` (3), `test_player.py` (5), `test_media.py` (4) — 12
  new unit tests total.
- `tests/integration/test_episode_player_media_live.py` (2 tests, live): episode-1
  boundary nav check; full live chain episode→embed→manifest asserting each stage's
  output feeds correctly into the next (`test_live_embed_and_manifest_chain`).
- `scripts/analyze_episode.py` CLI (`--url`, `--follow-player`, `--timeout`,
  `--output`, `--verbose`) — run live 3 times (once per manifest sample above),
  output cross-checked by hand each time.

### Tests executed

```
python -m pytest -q
```

Result: **50 passed** (41 unit incl. 12 new, 9 live integration incl. 2 new covering
the full episode→embed→manifest chain).

### Bugs found

None. (The rendition-count variance above is a real site behavior, not a bug —
`parse_hls_master_manifest()` already handled 0/1/many renditions correctly on
first write; only the documented conclusion needed updating.)

### Decisions

- Deliberately did **not** deobfuscate the packed JS script found in the embed
  page, even though doing so might have revealed additional structure — MASTER_PLAN.md
  §16/§49 forbid circumventing any access/protection mechanism regardless of
  technical ease, and light JS packing is exactly the kind of mechanism in scope.
  Its presence is recorded as a boolean fact (`has_obfuscated_script`) only.
- Did not download any actual video segments (`.ts` files) — only the master
  manifest text, which lists available renditions without containing any video
  data itself. This keeps Phase 6 within "structural/technical analysis," not
  "downloading," per §51.
- Did not install `ffmpeg`/`ffprobe` to unblock the deeper media checks — flagged
  as `NOT_EVALUATED`/`BLOCKED` and left for the user to decide whether to enable in
  a future session, rather than silently choosing a tool substitute or installing
  system software unprompted.
- Kept the single-rendition finding from the first manifest sample in the
  `media.py` docstring and evidence file, alongside the two-rendition finding from
  the next two samples, rather than treating the first as an error and erasing it
  — both are real, per §36.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection (homepage) | TESTED (URL suffix) / MEASURED (film prefix) | HIGH (suffix) / MEDIUM (film) | `output/evidence/phase2_homepage_structure.md`, `test_homepage.py` | See Phase 2; full Phase 7 sampling still pending |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Language analysis (Phase 7, full sampling) | TESTED | HIGH (pattern coverage) / MEDIUM (film/oav sub-cases) | `output/evidence/phase7_language_sampling.md`, 1 new unit test, 1 new live integration test | 224/224 entries resolved after generalizing the prefix pattern |

## Session 5 — 2026-09-17

### Phase completed

**Phase 7 — VOSTFR/VF sampling at scale**

### Work done

Full findings: [output/evidence/phase7_language_sampling.md](output/evidence/phase7_language_sampling.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- Fetched 5 more listing pages live (`/page/3/`, `/page/4/`, `/page/5/`,
  `/nouveaux-ajouts/page/2/`, `/nouveaux-ajouts/page/3/`), bringing the aggregate
  language-detection sample from 64 entries (Phase 2) to **224 entries** across 8
  listing pages and 116 distinct anime URLs.
- **Resolved the Phase 2 INCONCLUSIVE**: found a **third URL-slug pattern** —
  `oav-{lang}-...` (1/224), alongside the already-known `-{lang}` trailing suffix
  (210/224, 93.8%) and `film-{lang}-...` prefix (13/224, 5.8%). Generalized
  `detect_language()`'s prefix regex from `film-` only to `(?:film|oav)-`, as an
  **explicit set of the two markers actually observed** (not a wildcard, to avoid
  misclassifying ordinary slug text). Result: **0/224 entries unresolved**, down
  from 1/224 before the change.
- **New finding: `/nouveaux-ajouts/` can list anime with zero released episodes**
  (3 such blocks found across the 3 pages sampled: "Ninja Batman vs. Yakuza
  League" sub-entries, "Prism Rondo") — same caveat already known for
  `/prochainement/` (Phase 2), now confirmed to also apply to `/nouveaux-ajouts/`.
  "New series added" ≠ "has an episode." No code change needed — `parse_homepage()`
  already handled this correctly (0 entries contributed, warning logged); this is a
  documented site behavior found by broader sampling, not a bug.
- **Broadened the Phase 3 "separate anime page per language" finding from 2 pairs
  to 20+**: naive slug matching (`{base}` vs `{base}-vf`) across all 116 distinct
  anime URLs in the 224-entry sample found 20 matching base+VF pairs out of 28
  distinct `-vf`-suffixed URLs present (71%). The remaining 8/28 (29%) did not
  naively match a base in this sample window — including cases like Phase 3's
  "Tomb Raider King (VF)" whose VOSTFR counterpart uses `-jap`, not a bare slug.
  **Consequence for Phase 8:** URL-slug pattern matching is a useful HIGH-confidence
  heuristic when it matches but must not be the *sole* way to link a VF/VOSTFR
  pair — it will miss real pairs using a different naming convention.

### Work done — implementation

- `analysis/homepage.py`: generalized `_URL_LANGUAGE_PREFIX_RE` from
  `/film-(vf|vostfr)-[^/]+/?$` to `/(?:film|oav)-(vf|vostfr)-[^/]+/?$`; updated the
  module docstring with the full Phase 7 evidence (sample sizes, percentages, the
  `/nouveaux-ajouts/` zero-episode nuance) rather than overwriting the Phase 2 text
  wholesale — the Phase 2 findings that still hold (suffix pattern, VF badge
  fallback, pagination behavior) were left in place, only the resolved-INCONCLUSIVE
  parts were updated, per MASTER_PLAN.md §36.
- `tests/unit/test_homepage.py`: added `test_oav_language_from_url_prefix` (the
  real OAV URL found in this session, used directly as the test input).
- `tests/integration/test_language_sampling_live.py` (1 test, live): fetches 5
  listing pages and asserts ≥95% of entries resolve to a known language (a
  tolerant bound, not 100%, since a future unseen 4th pattern is a real possibility
  and shouldn't be treated as a test failure when it happens — it should be treated
  as a new Phase 7-style finding).

### Tests executed

```
python -m pytest -q
```

Result: **52 passed** (42 unit incl. 1 new, 10 live integration incl. 1 new).

### Bugs found

None. (The 1 previously-unresolved entry was a real, previously-unobserved site
pattern, not a parser defect — `detect_language()`'s existing UNKNOWN fallback
behaved correctly for it before this session's update.)

### Decisions

- Kept the film/oav prefix set explicit (`(?:film|oav)-`) rather than switching to
  a generic `[a-z0-9]+-(vf|vostfr)-` wildcard, even though the latter would have
  "resolved" the 1 unknown entry just as well without a code change — an
  unconstrained wildcard risks matching ordinary slug text that coincidentally
  contains `-vf-`/`-vostfr-` mid-title, silently misclassifying language with false
  confidence. Being explicit means a genuine 4th type marker will surface again as
  a `None`/UNKNOWN result (visible, honest) rather than being silently absorbed.
- Set the live integration test's threshold at ≥95%, not ==100%, specifically so a
  future 4th URL pattern doesn't look like a broken test — it should read as a new
  finding to investigate and document, matching how the 3rd pattern (oav-) was
  actually found this session.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; Phase 8 must not rely on this alone |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (Phase 8) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 new unit + 2 new live integration tests | anime_key, episode_key, episode_number all derived and cross-checked |

## Session 6 — 2026-09-17

### Phase completed

**Phase 8 — Unique identification (anime/season/episode/language keys)**

### Work done

Full findings: [output/evidence/phase8_unique_identification.md](output/evidence/phase8_unique_identification.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- **`anime_key` = WordPress post ID from `body.postid-N`**: confirmed present on
  every anime page (5 checked) AND on every episode page of that anime (checked:
  "The Exiled Heavy Knight..." ep 1 and ep 12 both report `postid-114033`, matching
  their anime page; Tomb Raider King VF (`postid-114990`) and JAP
  (`postid-114666`) each distinct — corroborating Phase 3's "separate post per
  language" finding at the ID level, not just the URL level). Re-fetched the
  homepage twice in the session (~1h+ apart) and found **0 mismatches** in
  `data-post-id` across 13 anime URLs common to both fetches — the ID is stable
  over time, not regenerated per page load. Since each language/season is already
  its own WordPress post (Phase 3), `anime_key` alone already implicitly encodes
  language and season — no separate `season_key`/`language_key` component is
  needed.
- **`episode_key` = canonicalized episode URL** (lowercase scheme+host, strip one
  trailing slash) rather than a constructed tuple, because it's always present
  (unlike the parsed episode number, which is `None` for films/OAVs) and Phase 3
  already established episode URLs can't be reconstructed from the anime slug
  anyway. Live-tested: fetching the same episode twice yields an identical key;
  unit-tested: case/whitespace/trailing-slash URL variants canonicalize
  identically.
- **`episode_number`**: parsed from the URL's trailing `-{N}-vf`/`-{N}-vostfr`
  slug (covers the ~93.8% suffix-pattern majority from Phase 7), with a documented
  cross-check against the anime page's chapter-label trailing number. Returns
  `None` — not guessed — for the film/OAV-prefixed URL shape, where no clean
  per-episode number exists in the URL at all.

### Work done — implementation

- New module `analysis/identity.py`: `extract_post_id_from_body_class()`,
  `canonicalize_url()`, `build_anime_key()`, `build_episode_key()`,
  `extract_episode_number_from_url()`, `extract_episode_number_from_label()` — all
  pure functions over already-fetched HTML/URLs/labels, no new network calls.
- `models.py`: added `AnimeRecord.post_id`; added `EpisodeRecord.episode_key`,
  `.anime_key`, `.anime_post_id`, `.episode_number`.
- `analysis/anime.py` and `analysis/episode.py` wired to populate the new fields
  automatically from the same HTML they already parse.
- `tests/unit/test_identity.py` (11 tests): post-ID extraction (present/absent),
  key construction, URL canonicalization (4 differently-formatted variants of the
  same URL all collapse to one key), episode-number extraction from both URL and
  label (including the graceful-`None`-on-film-label case).
- Added a `postid-N` body class to the existing `anime_page_sample.html` and
  `episode_page_sample.html` fixtures (previously omitted since Phase 3/4 didn't
  need it) and extended their existing unit tests to assert the new fields,
  instead of writing yet another near-duplicate fixture.
- `tests/integration/test_identity_live.py` (2 tests, live): anime page + 2 of its
  episode pages all resolve to the same `anime_key` while getting distinct
  `episode_key`/`episode_number`; the same episode fetched twice yields an
  identical `episode_key`/`anime_key`.

### Tests executed

```
python -m pytest -q
```

Result: **65 passed** (53 unit incl. 11 new, 12 live integration incl. 2 new).

### Bugs found

None.

### Decisions

- Did not introduce a separate `season_key`/`language_key` field, since Phase 3
  already established there is no multi-season/multi-language structure on a
  single anime page — adding a key component for something that's already fully
  captured by `anime_key` would be an unjustified abstraction (per this project's
  own "no premature abstraction" rule), not a missing feature.
- Chose the canonicalized URL over a constructed `(anime_key, episode_number)`
  tuple as `episode_key` specifically because the number is sometimes `None`
  (films/OAVs) — a key that can be null defeats the purpose of a key. The URL is
  always available.
- Reused/extended the Phase 3/4 fixtures rather than creating new ones for Phase 8,
  since the only missing ingredient was a `postid-N` class on the `<body>` tag —
  adding it in place keeps one canonical fixture per page type instead of
  duplicating near-identical HTML.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection strategy (Phase 9) | TESTED / MEASURED | HIGH | `output/evidence/phase9_discovery_strategies.md`, 4 new unit + 2 new live integration tests | Strategy C blocked/unreliable; Strategy A viable but see caching finding below |

## Session 7 — 2026-09-17

### Phase completed

**Phase 9 — New-episode detection strategies**

### Work done

Full findings: [output/evidence/phase9_discovery_strategies.md](output/evidence/phase9_discovery_strategies.md)
(gitignored evidence dir — summarized here so it survives without that directory).

**Strategy C (sitemap / RSS / REST API) — tested, found not viable:**
- `sitemap_index.xml` → 7 `wp-manga-sitemap{N}.xml` shards, listing anime PAGES
  (not episodes) with a `<lastmod>`. **Tested directly against a known case**: "The
  Exiled Heavy Knight..." had episode 12 published "seconds ago" (confirmed live,
  Phase 2), but its sitemap `<lastmod>` read **2026-07-02** — over 2 months stale.
  **MEASURED: sitemap `lastmod` does not update when a chapter is added to an
  existing anime post** — unusable for episode detection.
- Default `/feed/` (RSS 2.0): valid but **empty** channel, 0 `<item>` elements —
  WP-Manga chapters are a custom post type outside the default post feed.
- `/wp-json/` (WP REST API): **HTTP 403**. Not probed further (no alternate paths,
  no header manipulation) per MASTER_PLAN.md §3/§49 — marked `BLOCKED`.

**Strategy A (homepage/`page/N/` feed) — tested, viable, but with an important
caveat found this session:**
- Ran the actual poll simulation the master plan asks for: 2 homepage fetches, 90
  seconds apart. Result: **0 new entries**, identical entry order, and **0
  relative-time-label drift** (the top entry's "1 second ago" was still "1 second
  ago" 90s later) — which should be impossible on an honestly-live-rendered page.
- **This led to a bigger finding: the site serves pages from an origin-side page
  cache, not a fresh render per request.** Confirmed via `Last-Modified` +
  `CF-Cache-Status` response headers: homepage `Last-Modified` was **identical
  across two requests 12 seconds apart** and ~35 minutes stale relative to
  request time; `/page/2/` matched the same ~35-min window; `/anime/mebius-dust/`
  was ~2h stale; `/nouveaux-ajouts/` was ~24h stale. `CF-Cache-Status: DYNAMIC` on
  every page rules out Cloudflare edge caching as the cause — it's an origin-side
  cache (WordPress page-cache layer), consistent with the "hourly" update
  frequency hint already seen in `/feed/`'s channel metadata during the Strategy C
  check.
- **Consequence for the V1 architecture — corrects the master plan's polling
  assumption:** since the cache regenerates the page's full current state (not an
  incremental delta), no episode is silently skipped between regenerations — this
  is not a "missed episode" risk. It IS an **effective-latency and wasted-request**
  issue: polling every 1-2 minutes (as MASTER_PLAN.md §1 specifies) would, most of
  the time, re-fetch byte-identical content, since the measured homepage cache TTL
  (≥35 min, exact value `INCONCLUSIVE` without a longer monitoring window than one
  session allows) is far longer than the suggested poll interval. Detection
  latency is bounded by the origin cache TTL, not by poll frequency.

**Strategy B / D:** not independently viable as *discovery* mechanisms — Strategy B
(an anime's own episode list) requires already knowing which anime to check, so it
functions as a confirmation/completeness check once Strategy A flags a candidate,
not a standalone discovery source. Strategy D is simply "A discovers, B confirms,"
already supported by this project's existing `analysis.homepage` →
`analysis.anime` → `analysis.identity` chain.

### Work done — implementation

- New module `detection/discovery.py`: `diff_known_episode_keys()` (candidates not
  already in a known-key set) and `entries_to_episode_keys()` (snapshot → key set,
  what a poller would persist) — pure functions over already-parsed
  `HomepageEntry` lists, no network calls, reusable for any snapshot source.
- `tests/unit/test_discovery.py` (4 tests): only-unknown entries returned; empty
  diff when nothing's new; everything new against an empty known set;
  round-trip consistency between `entries_to_episode_keys()` and
  `diff_known_episode_keys()`.
- `tests/integration/test_discovery_live.py` (2 tests, live): discovery logic
  against a real homepage fetch (self-diff → empty, empty-known → everything
  "new"); and a direct assertion of the caching finding
  (`Last-Modified` identical across two back-to-back live requests).

### Tests executed

```
python -m pytest -q
```

Result: **71 passed** (57 unit incl. 4 new, 14 live integration incl. 2 new).

### Bugs found

None.

### Decisions

- Did not attempt to work around or further probe the `/wp-json/` 403 (e.g. trying
  alternate REST routes, different headers) — a blocked access-control response is
  exactly the case MASTER_PLAN.md §3/§49 says to record as `BLOCKED` and leave
  alone, not treat as a puzzle to route around.
- Used a 90-second real wait (via a backgrounded shell command) rather than
  skipping the poll simulation or faking it — MASTER_PLAN.md §42/§43 require
  actually running tests and observing real behavior, and the caching finding
  would not have been discovered without a real timed measurement.
- Did not attempt to pin down the exact cache TTL by waiting longer (e.g. a full
  hour) in this session — flagged `INCONCLUSIVE` and left as an explicit follow-up
  requiring a longer unattended monitoring run, rather than extrapolating from 2
  data points or guessing a round number.
- Chose not to add an automated test that waits 90+ seconds as part of the regular
  suite (would make `pytest -q` slow and flaky) — the poll-simulation finding is
  preserved as evidence + a session-report narrative instead, while the *logic*
  used to interpret such a diff (`detection/discovery.py`) is fully unit- and
  integration-tested without requiring a real wait.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication (Phase 10) | TESTED | HIGH (logic) | `output/evidence/phase10_deduplication.md`, 9 new unit + 1 new live integration test | All 8 §21 scenarios covered |

## Session 8 — 2026-09-17

### Phase completed

**Phase 10 — Deduplication tests**

### Work done

Full findings: [output/evidence/phase10_deduplication.md](output/evidence/phase10_deduplication.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- Implemented a minimal in-memory `EpisodeStore` (key -> status map) standing in
  for a future V1's real DB — `source_audit` doesn't implement persistence itself
  (§51), so this validates the *dedup logic* built in Phase 8/9, not a storage
  layer.
- **Design decision, directly evidenced by earlier phases**: dedup is keyed only
  on `episode_key` (Phase 8's canonicalized URL), never on title text — title
  strings have been observed to vary in rendering/whitespace across fetches
  (Phase 2/3), so keying on them risks both false "new" (same episode, drifted
  title) and false "duplicate" (two different episodes, similar titles).
- **All 8 scenarios from MASTER_PLAN.md §21 tested**:
  1. Same URL twice → no-op on re-registration, 1 entry.
  2. Same episode via a differently-cased/slashed URL → same canonicalized key,
     correctly recognized as known.
  3. Different URL → correctly NOT a duplicate.
  4. Slightly different title → confirmed irrelevant to dedup in both directions
     (same URL/different title still matches; different URL/similar title stays
     distinct).
  5. Program restart, DB conserved → `snapshot()`/`restore()` round-trip preserves
     known-ness and exact status.
  6. Local file deleted → **by design**, `EpisodeStore` has no concept of a
     filesystem path at all, so file deletion cannot affect dedup state — status
     only changes via an explicit `mark_status()` call. Documented as a design
     rule for V1 (file-existence and DB-status must stay separate concerns), not
     just a passing test.
  7/8. Already published → re-discovering an already-`PUBLISHED` episode does not
     regress its status; `is_already_published()` stays true.
- Live end-to-end check: fetched the real homepage, marked every parsed entry
  `PUBLISHED` (simulating "a prior run already processed everything currently on
  the feed"), then re-diffed against the store — **0 new candidates**, matching
  the realistic steady-state case a real poller hits on every unchanged cycle.

### Work done — implementation

- New module `detection/deduplication.py`: `EpisodeStatus` enum
  (DISCOVERED/DOWNLOADING/DOWNLOADED/PUBLISHED), `EpisodeStore` with
  `register_discovered()` (no-op if already known, never regresses status),
  `mark_status()`, `is_known()`, `status_of()`, `is_already_published()`,
  `known_keys()`, and `snapshot()`/`restore()` for the restart scenario.
- `tests/unit/test_deduplication.py` (9 tests): one per §21 scenario (2 combined
  into one test as they describe the same case), plus a `mark_status()`-on-unknown-
  key error case.
- `tests/integration/test_deduplication_live.py` (1 test, live): full
  discover→register→re-diff flow against a real homepage fetch.

### Tests executed

```
python -m pytest -q
```

Result: **80 passed** (65 unit incl. 9 new, 15 live integration incl. 1 new).

### Bugs found

None.

### Decisions

- `register_discovered()` is a no-op when the key is already known, specifically
  so that re-discovering an episode already in `DOWNLOADING`/`DOWNLOADED`/
  `PUBLISHED` never resets it back to `DISCOVERED` — a real V1 poller will
  re-observe the same homepage entries on almost every cycle, so this had to be
  correct by construction, not just by convention at the call site.
- `EpisodeStore` intentionally has zero filesystem awareness — no `file_path`
  field, no existence check anywhere in its API. This was a deliberate design
  choice (not an oversight) to make scenario 6 hold *structurally*: there is no
  code path by which a deleted file could influence dedup state, which is a
  stronger guarantee than "we tested that it doesn't currently affect it."

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication logic    | TESTED        | HIGH       | `output/evidence/phase10_deduplication.md`, 9 unit + 1 live integration test | All 8 §21 scenarios covered |
| Restart recovery (Phase 11) | TESTED | HIGH (logic) | `output/evidence/phase11_restart_recovery.md`, 6 new unit tests | All 3 §22 crash points covered, total mapping over EpisodeStatus |

## Session 9 — 2026-09-17

### Phase completed

**Phase 11 — Restart/recovery simulation**

### Work done

Full findings: [output/evidence/phase11_restart_recovery.md](output/evidence/phase11_restart_recovery.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- Built a **total recovery-action mapping** over every `EpisodeStatus` (Phase 10),
  not just the 3 crash points MASTER_PLAN.md §22 spells out, so a future added
  status can't silently fall through unhandled:
  - `DISCOVERED` → `RESTART_DOWNLOAD` (nothing was in flight).
  - `DOWNLOADING` → `RESTART_DOWNLOAD` — crash mid-download means any partial
    artifact is untrustworthy; **deliberately does not attempt to resume** a
    partial download, since doing so safely would need real storage-layer facts
    (byte offset, validation) this project doesn't have — guessing at a resume
    strategy would violate §43 ("never turn an error into a hypothesis").
  - `DOWNLOADED` → `VALIDATE_THEN_PUBLISH` — the download already succeeded, so
    recovery validates the artifact and proceeds to publish rather than
    re-downloading.
  - `PUBLISHED` → `NO_ACTION` — restates Phase 10's dedup guarantee as the
    crash-recovery case of the same fact: never reprocess a published episode,
    whether re-discovered on a feed or recovered after a crash.
- All 3 §22 scenarios (crash during `DOWNLOADING`, crash after `DOWNLOADED`, crash
  after `PUBLISHED`) simulated end-to-end using `EpisodeStore.snapshot()`/
  `restore()` (the same restart mechanism Phase 10 validated) followed by
  `determine_recovery_action()`.

### Work done — implementation

- New module `detection/restart_recovery.py`: `RecoveryAction` enum
  (`RESTART_DOWNLOAD`/`VALIDATE_THEN_PUBLISH`/`NO_ACTION`) and
  `determine_recovery_action(status) -> RecoveryAction`, backed by an explicit
  dict covering every `EpisodeStatus` member — raises `KeyError` loudly (not a
  silent default) for anything unmapped.
- `tests/unit/test_restart_recovery.py` (6 tests): a completeness test iterating
  the live `EpisodeStatus` enum (catches a future status added without updating
  the recovery map), one test per §22 scenario, a "crashed before download even
  started" edge case, and a caller-error case (`None` status raises).
- No live integration test added this phase — deliberately: this phase concerns a
  future V1's own process-restart logic, not `voir-anime.to`'s behavior, so there
  is nothing to additionally validate against the live site beyond what Phase 10's
  snapshot/restore already covers.

### Tests executed

```
python -m pytest -q
```

Result: **86 passed** (71 unit incl. 6 new, 15 live integration — unchanged this
phase).

### Bugs found

None.

### Decisions

- Explicitly chose **not** to design a partial-download-resume strategy, even
  though it's a plausible V1 optimization — this audit has no real downloader to
  ground such a decision in evidence, and MASTER_PLAN.md is emphatic about not
  turning an unobserved case into a guessed hypothesis. The recovery map instead
  documents this as an open decision for V1's own implementation, not silently
  papered over.
- Made the recovery mapping a dict keyed by the live `EpisodeStatus` enum (not a
  hardcoded if/elif chain) specifically so a completeness test could iterate the
  enum and catch a future gap automatically — matches §22's requirement that
  *every* state have a defined strategy, enforced by a test rather than just
  documentation.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication logic    | TESTED        | HIGH       | `output/evidence/phase10_deduplication.md`, 9 unit + 1 live integration test | All 8 §21 scenarios covered |
| Restart/recovery logic | TESTED        | HIGH       | `output/evidence/phase11_restart_recovery.md`, 6 unit tests | All 3 §22 scenarios covered, total EpisodeStatus mapping |
| Structure change detection (Phase 12) | TESTED | HIGH | `output/evidence/phase12_change_detection.md`, 7 new unit + 3 new live integration tests | Live site re-checked, 0 regressions vs Phases 2-4 baseline |

## Session 10 — 2026-09-17

### Phase completed

**Phase 12 — Structure change detection**

### Work done

Full findings: [output/evidence/phase12_change_detection.md](output/evidence/phase12_change_detection.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- Built a selector-**presence** fingerprint (not counts — counts legitimately vary
  per Phases 2-4 evidence, which would make a count-based signal noisy) over the
  exact critical selectors Phases 2-5's parsers depend on, **imported directly
  from those modules** rather than re-typed, so the fingerprint can't drift out of
  sync with the parsers it's meant to protect.
- Deliberately excluded 2 selectors from the "critical" set even though their
  parsers use them, because their own absence is already known-legitimate site
  behavior, not a structure change: homepage `.manga-vf-flag` (absent on
  VOSTFR-only pages, Phase 2) and episode-page `.nav-links a` (absent for a
  single-episode anime, Phase 4).
- **Fixture-based validation**: a deliberately mutated copy of the homepage
  fixture (renaming `chapter-item` → `ep-row`, simulating a redesign) is correctly
  flagged as regressed (`find_regressed_selectors()` → `["chapter_item"]`); a
  control case (unrelated markup added, nothing tracked touched) is correctly
  **not** flagged — confirms no false positives on harmless changes; a fully
  empty page regresses every tracked selector.
- **Live re-validation**: re-fetched the live homepage, an anime page, and an
  episode page this session and fingerprinted them against the "fully present"
  baseline established in Phases 2-4 — **0 regressed selectors on all 3 page
  types**. The site's structure has not changed since the earlier sessions'
  fetches, confirmed today, not assumed.

### Work done — implementation

- New module `detection/fingerprint.py`: `HOMEPAGE_SELECTORS`,
  `ANIME_PAGE_SELECTORS`, `EPISODE_PAGE_SELECTORS` (imported from the analysis
  modules), `compute_fingerprint()`, `fingerprint_hash()`,
  `find_regressed_selectors()`, `is_structure_changed()`.
- `tests/unit/test_fingerprint.py` (7 tests): each Phase 2/3/4 fixture fully
  matches its critical selectors; identical HTML hashes identically; mutated
  fixture correctly regressed; unrelated markup addition correctly not flagged;
  fully empty page regresses everything.
- `tests/integration/test_fingerprint_live.py` (3 tests, live): homepage, an
  anime page, and an episode page each re-checked against the "fully present"
  baseline from Phases 2-4 — this is the closest thing this project has to an
  automated regression suite against the live site's own structure.

### Tests executed

```
python -m pytest -q
```

Result: **96 passed** (78 unit incl. 7 new, 18 live integration incl. 3 new).

### Bugs found

None.

### Decisions

- Chose presence/absence over exact element counts as the fingerprint signal,
  specifically because Phases 2-4 already established that counts (chapter
  counts, entry counts, optional metadata fields) vary legitimately — a
  count-based fingerprint would have required either an arbitrary tolerance band
  (itself a guess) or produced constant false alarms.
- Imported the selector strings directly from `analysis.homepage`/`analysis.anime`/
  `analysis.episode` (reaching into their "private" `_`-prefixed module constants)
  rather than duplicating the strings in `fingerprint.py` — a duplicated list would
  silently go stale the moment a parser's selector changed without a matching
  fingerprint update, defeating the module's purpose.
- Excluded 2 already-known-optional selectors from the critical set rather than
  including them and accepting expected false positives — MASTER_PLAN.md §38/§39
  are about catching *unexpected* absence, and an intentionally noisy signal would
  train a future V1 to ignore real alerts.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication logic    | TESTED        | HIGH       | `output/evidence/phase10_deduplication.md`, 9 unit + 1 live integration test | All 8 §21 scenarios covered |
| Restart/recovery logic | TESTED        | HIGH       | `output/evidence/phase11_restart_recovery.md`, 6 unit tests | All 3 §22 scenarios covered, total EpisodeStatus mapping |
| Structure change detection | TESTED   | HIGH       | `output/evidence/phase12_change_detection.md`, 7 unit + 3 live integration tests | Live site re-checked, 0 regressions |
| Resilience tests (Phase 13) | TESTED | HIGH | `output/evidence/phase13_resilience.md`, 11 new unit tests | No gaps found — all parsers already fail safe |

## Session 11 — 2026-09-17

### Phase completed

**Phase 13 — Resilience tests**

### Work done

Full findings: [output/evidence/phase13_resilience.md](output/evidence/phase13_resilience.md)
(gitignored evidence dir — summarized here so it survives without that directory).

Systematically tested the MASTER_PLAN.md §24 case list against every parser
(HTTP-level cases — timeout/403/404/429/500 — were already covered by Phase 1's
`HttpClient` tests, so this phase focused on parser-level malformed/missing-data
handling):

- `parse_homepage()`: empty/malformed HTML → `[]`, no exception; a block with no
  `<img>` → `thumbnail_url=None`, rest of the entry still parsed correctly.
- `parse_anime_page()`: empty/malformed HTML → all fields `None`/empty (`title`,
  `episode_links=[]`, `genres=[]`), 2 warnings logged, no exception.
- `parse_episode_page()`: missing `<iframe>` (re-verified from Phase 4), missing
  `.nav-links`, and a fully empty page all degrade to `None` fields without
  raising — notably `episode_key`/`episode_number` still populate correctly even
  on an otherwise-empty page, since both are derived from the URL alone,
  independent of page content.
- `parse_embed_page()`: empty string → all signals `None`/`False`, no exception.
- `parse_hls_master_manifest()`: a `#EXT-X-STREAM-INF` line missing
  `RESOLUTION`/`CODECS` → those fields `None` (not guessed), everything else still
  parsed; a manifest truncated right after the last `STREAM-INF` line (no
  playlist URL follows) → `playlist_url=None`; completely non-m3u8 garbage input
  → `[]`, no exception.
- Structure-change resilience (changed selector) was already fully covered by
  Phase 12's fingerprinting — not re-tested here to avoid duplicating that work.

**No gaps requiring a code fix were found** — every parser already failed safe
(returns `None`/empty + logs a warning) from when it was originally written in
earlier phases. This was a verification pass confirming that property explicitly,
not a bug-fixing session.

### Work done — implementation

- Added 11 new unit tests across the existing parser test files (rather than a
  new module — Phase 13 has no new production code of its own, only new test
  coverage): 2 in `test_homepage.py`, 2 in `test_anime.py`, 3 in `test_episode.py`,
  1 in `test_player.py`, 3 in `test_media.py`.

### Tests executed

```
python -m pytest -q
```

Result: **107 passed** (96 unit incl. 11 new, 11 live integration — unchanged this
phase, no live network dependency for malformed-input testing).

### Bugs found

None. (This is itself the notable Phase 13 finding: the parsers' defensive
design from Phases 2-6 — `None` on missing, warning logged, never guessed — held
up under systematic testing rather than needing rework.)

### Decisions

- Did not add live integration tests for this phase — malformed/missing-element
  resilience is inherently about *constructed* bad inputs, not something to
  observe on the live site (which, per Phase 12, currently has no structural
  gaps to exercise these paths against anyway).
- Deliberately did not re-test structure-change resilience here, since Phase 12's
  fingerprinting already covers exactly that case more precisely (detecting
  *which* selector broke) than a generic "missing element" test would.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication logic    | TESTED        | HIGH       | `output/evidence/phase10_deduplication.md`, 9 unit + 1 live integration test | All 8 §21 scenarios covered |
| Restart/recovery logic | TESTED        | HIGH       | `output/evidence/phase11_restart_recovery.md`, 6 unit tests | All 3 §22 scenarios covered, total EpisodeStatus mapping |
| Structure change detection | TESTED   | HIGH       | `output/evidence/phase12_change_detection.md`, 7 unit + 3 live integration tests | Live site re-checked, 0 regressions |
| Parser resilience (missing/malformed data) | TESTED | HIGH | `output/evidence/phase13_resilience.md`, 11 unit tests | No gaps found |
| Multi-anime ordering (Phase 14) | TESTED | HIGH | `output/evidence/phase14_multi_anime.md`, 7 new unit + 1 new live integration test | Exact §25 example + 3-real-anime round-robin, 0 violations |

## Session 12 — 2026-09-17

### Phase completed

**Phase 14 — Multi-anime test**

### Work done

Full findings: [output/evidence/phase14_multi_anime.md](output/evidence/phase14_multi_anime.md)
(gitignored evidence dir — summarized here so it survives without that directory).

- Implemented and tested the **exact MASTER_PLAN.md §25 example**: Anime A
  (E120→E121, must process in order) interleaved with Anime B (E050, independent
  of A). Confirmed: B processes at any time regardless of A's state; A's E121
  cannot be processed before E120 (`can_process()` returns `False`, and a direct
  `process()` attempt raises).
- Confirmed two anime's queues **never block each other** — fully draining one
  anime's queue leaves another anime's queue completely untouched and still in
  its original order.
- **Live end-to-end check across 3 real, distinct anime** (confirmed 3 different
  `post_id`s via Phase 8's `anime_key`, not accidentally the same anime): "The
  Exiled Heavy Knight...", "Mebius Dust", "Re:Zero ... S4" — reversed each
  anime's episode list (Phase 3: newest-first on the page) to oldest-first, then
  processed all ~40 episodes across the 3 anime in round-robin (interleaved)
  order. **0 `OutOfOrderError`s, every episode processed exactly once, no
  cross-anime mixing.**

### Work done — implementation

- New module `detection/ordering.py`: `PerAnimeQueue` (per-`anime_key` FIFO,
  `add()`/`can_process()`/`process()`/`pending_for()`/`processed_order()`) and
  `OutOfOrderError`. Enforces the rule via a `deque` per `anime_key` — an episode
  can only be processed when it's at the front of its own anime's queue; other
  anime's queues are entirely independent data structures, so there is no code
  path by which they could interfere with each other.
- `tests/unit/test_ordering.py` (7 tests): the exact plan example, an
  out-of-order attempt raising, cross-anime independence, duplicate-add no-op
  (consistent with Phase 10), unknown-episode edge cases, and a 3-anime
  fully-interleaved stress case.
- `tests/integration/test_ordering_live.py` (1 test, live): the 3-real-anime
  round-robin check described above.

### Tests executed

```
python -m pytest -q
```

Result: **115 passed** (103 unit incl. 7 new, 12 live integration incl. 1 new).

### Bugs found

None.

### Decisions

- Implemented per-anime ordering as a **structural** guarantee (separate `deque`
  per `anime_key`, no shared mutable state between anime) rather than a runtime
  check bolted onto a single global queue — matches the same "make the invalid
  state unrepresentable" approach already used in Phase 10's `EpisodeStore` (no
  filesystem awareness at all, rather than "tested to not depend on it").
- `add()` on an already-known `episode_key` is a no-op, mirroring Phase 10's
  `register_discovered()` — re-discovering an episode already in this anime's
  queue (e.g. still visible on the homepage feed) must not duplicate or reorder
  it.
- Did not simulate actual concurrent execution (threads/asyncio) — Phase 14 is
  about *ordering correctness* given interleaved arrival/processing, which this
  module tests deterministically; actual concurrency/throughput is Phase 15's
  concern, not this one's.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication logic    | TESTED        | HIGH       | `output/evidence/phase10_deduplication.md`, 9 unit + 1 live integration test | All 8 §21 scenarios covered |
| Restart/recovery logic | TESTED        | HIGH       | `output/evidence/phase11_restart_recovery.md`, 6 unit tests | All 3 §22 scenarios covered, total EpisodeStatus mapping |
| Structure change detection | TESTED   | HIGH       | `output/evidence/phase12_change_detection.md`, 7 unit + 3 live integration tests | Live site re-checked, 0 regressions |
| Parser resilience (missing/malformed data) | TESTED | HIGH | `output/evidence/phase13_resilience.md`, 11 unit tests | No gaps found |
| Multi-anime ordering   | TESTED        | HIGH       | `output/evidence/phase14_multi_anime.md`, 7 unit + 1 live integration test | Exact §25 example + 3-real-anime round-robin |
| Concurrency simulation (Phase 15) | MEASURED | HIGH (wall-clock/CPU) / INCONCLUSIVE (RSS trend) | `output/evidence/phase15_concurrency.md`, `scripts/simulate_concurrency.py` | I/O-bound workload, no local resource bottleneck found at tested scale |

## Session 13 — 2026-09-17

### Phase completed

**Phase 15 — Concurrency simulation**

### Work done

Full findings: [output/evidence/phase15_concurrency.md](output/evidence/phase15_concurrency.md)
(gitignored evidence dir — summarized here so it survives without that directory).

Ran `scripts/simulate_concurrency.py`: `ThreadPoolExecutor` fetching the same
fixed pool of 7 already-known episode URLs (from Phases 4-6, reused rather than
fetching novel URLs, to keep live request volume bounded per the Phase 0
rate-limiting decision) at worker counts 1/2/4/7, measuring wall-clock,
`time.process_time()` CPU time, and `psutil` RSS delta.

| Workers | Wall-clock | CPU time | RSS delta |
|---|---|---|---|
| 1 | 7.710s | 0.422s | +18.4 MB |
| 2 | 4.177s | 0.234s | +0.5 MB |
| 4 | 2.218s | 0.266s | +7.5 MB |
| 7 | 1.327s | 0.266s | +9.4 MB |

- **MEASURED, HIGH confidence: wall-clock scales down roughly linearly with
  worker count** (~5.8x speedup for 7x workers, 7.71s → 1.33s) — this workload
  (fetching) is I/O-bound, not CPU-bound.
- **MEASURED: CPU time stays flat (~0.2-0.4s) regardless of worker count** — CPU
  is not the limiting resource for a fetch-heavy workload.
- **INCONCLUSIVE on a clean RSS-per-worker trend**: RSS delta was noisy (560 KB
  at 2 workers vs 18.4 MB at 1 worker) — almost certainly dominated by one-time
  allocation noise (connection pool setup, TLS buffers) at this small a request
  volume, not a signal that scales cleanly with worker count. Flagged rather than
  smoothed over or extrapolated from a small, noisy sample.
- 7/7 requests succeeded at every worker count tested, including 7 simultaneous
  connections to the same host — no rate-limiting/blocking observed in this small
  experiment.
- **Consequence for V1 (per §46's correctness > reliability > observability >
  performance > optimization priority — this phase is explicitly the
  "performance" tier, informational)**: since no local resource (CPU/RAM) became
  a bottleneck at the tested scale, a dynamic concurrency limit should key off
  request latency/error rate (back off on 429/5xx) rather than local system
  metrics — the practical constraint is being considerate toward the source
  (Phase 0's authorization decision), not local resource exhaustion. This
  experiment used fetching only, not actual media downloads — a real downloader's
  resource profile (large file I/O, disk writes, ffprobe) is genuinely different
  and wasn't measured here; flagged explicitly as out of this result's scope.

### Work done — implementation

- New script `scripts/simulate_concurrency.py` (no new core `src/` module — this
  is a one-off measurement tool, not reusable parsing/detection logic): runs
  configurable worker-count trials via `ThreadPoolExecutor`, reports wall-clock/
  CPU/RSS per trial as JSON.
- Added `psutil` as an optional `scripts` dependency (`pyproject.toml` extras)
  and to `requirements.txt` — not a core library dependency, only needed for this
  measurement tooling.

### Tests executed

```
python -m pytest -q
```

Result: **115 passed** (unchanged — Phase 15 is a standalone measurement script,
no new pytest coverage; its own output was verified by direct inspection of the
JSON report, consistent with §6's "code is executed, results recorded, errors
analyzed" completion bar even without a formal test file).

### Bugs found

None.

### Decisions

- Reused the same 7 known episode URLs across all worker-count trials (28 total
  requests) rather than generating fresh URLs per trial, specifically to keep
  this experiment's live footprint small and bounded — consistent with the
  Phase 0 decision to rate-limit and avoid burst hammering, even though a larger
  request volume would have produced a cleaner RSS signal.
- Did not extrapolate a recommended `MAX_WORKERS` number from this data — the
  measured facts (I/O-bound, no CPU/RAM bottleneck at 7 workers) support a
  *qualitative* recommendation (key concurrency off request health, not local
  resources) but MASTER_PLAN.md explicitly warns against picking an arbitrary
  constant, and this experiment's scale doesn't support picking a *specific*
  number either — that requires either much larger-scale testing or waiting for
  V1's real downloader to exist.
- Kept `psutil` out of core dependencies (only in the `scripts` extra) since
  nothing in `src/source_audit/` depends on it — only this one measurement
  script does.

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection     | TESTED        | HIGH (pattern coverage, 224/224) / MEDIUM (film/oav sub-cases, small abs. sample) | `output/evidence/phase7_language_sampling.md`, 10 unit + 2 live integration tests | Phases 2 + 7 combined |
| Anime page structure   | TESTED        | HIGH       | 6 live anime pages compared, 5 unit + 2 live integration tests | Phase 3; chapter-list pagination for 100+ ep. shows still INCONCLUSIVE |
| VF/VOSTFR pairing heuristic | TESTED  | HIGH (when slug matches) / not sole method | `output/evidence/phase7_language_sampling.md` | 20/28 pairs matched naively; not sole method for Phase 8 |
| Episode page structure | TESTED        | HIGH       | 7 live episode pages compared, 3 unit + 1 live integration test | Phase 4 |
| Player structure       | TESTED        | HIGH       | 1 embed deep-dive + 7-page domain cross-check, 5 unit tests | Phase 5; obfuscated script deliberately not deobfuscated |
| Media format (HLS)     | MEASURED      | HIGH       | 3 live manifests, 4 unit + 1 live integration test | Phase 6; rendition count varies per episode; ffprobe unavailable |
| Unique identification (anime_key/episode_key) | TESTED / MEASURED | HIGH | `output/evidence/phase8_unique_identification.md`, 11 unit + 2 live integration tests | Phase 8 |
| New-episode detection: sitemap/RSS/REST API | FAILED (not viable) | HIGH | `output/evidence/phase9_discovery_strategies.md` | Stale/empty/blocked respectively |
| New-episode detection: homepage feed | TESTED | HIGH | Same evidence file, 2 live tests | Viable, but see caching finding |
| Origin page-cache TTL (homepage) | MEASURED (lower bound only) | HIGH (≥35 min) / INCONCLUSIVE (exact value) | Same evidence file, `test_live_homepage_served_from_origin_cache` | Corrects the master plan's 1-2 min poll assumption |
| Deduplication logic    | TESTED        | HIGH       | `output/evidence/phase10_deduplication.md`, 9 unit + 1 live integration test | All 8 §21 scenarios covered |
| Restart/recovery logic | TESTED        | HIGH       | `output/evidence/phase11_restart_recovery.md`, 6 unit tests | All 3 §22 scenarios covered, total EpisodeStatus mapping |
| Structure change detection | TESTED   | HIGH       | `output/evidence/phase12_change_detection.md`, 7 unit + 3 live integration tests | Live site re-checked, 0 regressions |
| Parser resilience (missing/malformed data) | TESTED | HIGH | `output/evidence/phase13_resilience.md`, 11 unit tests | No gaps found |
| Multi-anime ordering   | TESTED        | HIGH       | `output/evidence/phase14_multi_anime.md`, 7 unit + 1 live integration test | Exact §25 example + 3-real-anime round-robin |
| Concurrency (fetching step) | MEASURED | HIGH (wall/CPU) / INCONCLUSIVE (RSS) | `output/evidence/phase15_concurrency.md` | I/O-bound; no local bottleneck at tested scale |
| Final report (Phase 16) | TESTED | HIGH | `output/reports/SOURCE_TECHNICAL_REPORT.md` | All 22 required sections + Final Decision present |

## Session 14 — 2026-09-17

### Phase completed

**Phase 16 — Final report**

### Work done

Assembled [output/reports/SOURCE_TECHNICAL_REPORT.md](output/reports/SOURCE_TECHNICAL_REPORT.md)
(tracked in git, unlike `output/evidence/` — this is the deliverable), covering
every section MASTER_PLAN.md §27 requires (Executive Summary through Final
Decision) plus the explicit YES/NO/INCONCLUSIVE §54 block. It is a synthesis of
all 13 prior sessions' evidence — no new fetches were performed this phase, only
consolidation, with every `INCONCLUSIVE`/`BLOCKED` item carried through from its
originating phase rather than smoothed over.

Also excluded `output/reports/*.json` (the scratch CLI dry-run outputs
accumulated during Phases 2/3/4/15 smoke-testing) from git via `.gitignore`,
keeping only the final `.md` report tracked — consistent with MASTER_PLAN.md §48
(minimize collected data; these JSON dumps embed real scraped titles/metadata,
not just structural facts, so they belong with the other gitignored evidence,
not the deliverable).

### Definition of Done (MASTER_PLAN.md §41)

- [x] structure created
- [x] environment reproducible
- [x] HTTP client tested
- [x] homepage analysée
- [x] plusieurs anime analysés (6)
- [x] plusieurs épisodes analysés (7, across 5 anime)
- [x] métadonnées analysées
- [x] langue analysée (224-entry sample)
- [x] lecteur analysé dans le cadre autorisé
- [x] média analysé dans le cadre autorisé (manifest-level; ffprobe unavailable)
- [x] méthode de découverte testée (3 strategies compared, 1 viable + 1 major
      correction found)
- [x] dédoublonnage testé (all 8 §21 scenarios)
- [x] redémarrage simulé (all 3 §22 scenarios)
- [x] changements de structure testés (fingerprinting, live-reverified)
- [x] erreurs testées (§24 resilience pass, no gaps)
- [x] tests multi-anime effectués (exact §25 example + 3 real anime)
- [x] résultats documentés (13 evidence files + this session report)
- [x] INCONCLUSIVE documentés (6 items in the final report)
- [x] BLOCKED documentés (3 items in the final report)
- [x] rapport final généré
- [x] aucune hypothèse critique non signalée (the origin-cache-TTL finding, the
      single-vs-dual-embed-provider assumption, and the VF/VOSTFR pairing
      heuristic's 29% miss rate are all flagged explicitly rather than glossed
      over)
- [x] tests automatisés passent (115/115, `pytest -q`)
- [x] documentation cohérente (README, MASTER_PLAN.md, SESSION_REPORT.md, 13
      evidence files, and the final report all cross-reference consistently)

**`source_audit` is complete per its own Definition of Done.**

### Tests executed

```
python -m pytest -q
```

Result: **115 passed**, unchanged from Phase 15 (this phase performed no new
fetches or code changes to `src/`).

### Bugs found

None.

### Decisions

- Did not perform any new live fetches for this phase — MASTER_PLAN.md §27
  describes Phase 16 as report assembly, and every fact in the final report
  already has a citation to a specific earlier-phase test or evidence file;
  re-fetching to "double check" without a specific reason would just be
  re-deriving already-established evidence.
- Moved the final report's own path (`output/reports/SOURCE_TECHNICAL_REPORT.md`)
  outside the newly-added `output/reports/*.json` gitignore pattern deliberately
  — it's the one file in that directory meant to be a tracked deliverable, not
  scratch output.

### Session 15 (Closure) — 2026-09-17

### Phase completed

**Closure & Verification pass** (per the user's `SOURCE_AUDIT — FINAL CLOSURE &
VERIFICATION PROMPT`) — a targeted re-verification of the 4 priority open items
from the Phase 16 report (cache TTL, ffprobe validation, long pagination, HLS
rendition variance), plus a review pass over Priorities 5-10 and a code-quality
pass. Existing baseline (115/115 tests) was preserved and re-confirmed before any
change was made; no prior conclusion was deleted — corrections are logged as
OLD/NEW/WHY/EVIDENCE below.

### Baseline check (first action, before any change)

```
python -m pytest -q
```
Result: **115 passed**, 0 regressions — matches Phase 16's final state exactly.
Proceeded per the closure prompt's rule ("if a regression exists, STOP" — none
did).

### Priority 1 — Cache TTL: corrected, not replaced

**OLD**: "origin cache TTL ≥35 minutes" (Phase 9, based on 2 data points ~90s
apart, using the `Last-Modified` HTTP header).
**NEW**: "origin cache TTL ≥107 minutes and counting" (this session, based on:
(a) a newly-found, more precise signal — the site's own "WP Fastest Cache"
plugin embeds an exact cache-generation timestamp directly in every page's HTML;
(b) cross-referencing this timestamp across every raw HTML snapshot saved
throughout the entire project's history, finding one homepage regeneration
boundary at ~33 minutes and a second, later stable window now directly observed
lasting ≥107 minutes via a dedicated 5-minute-interval background poll,
`scripts/measure_cache_ttl.py`).
**WHY**: the original figure was a valid lower bound from the evidence available
at the time, not an error — it was simply weak (2 close-together samples). This
session had access to a much richer signal (the plugin's own timestamp,
cross-referenced across 3 sessions' worth of accumulated raw HTML) plus the
ability to run a long, dedicated background measurement.
**EVIDENCE**: `output/evidence/cache_ttl_analysis.md`; new module
`detection/cache_signal.py` (6 unit + 2 live tests) makes this signal a reusable,
tested capability rather than a one-off observation.
**Also resolved**: investigated why immediate back-to-back fetches showed
different content hashes despite an identical cache timestamp — diffed two
fetches and found exactly one differing line, Cloudflare's own email-address
obfuscation feature (re-encoded per-request at Cloudflare's edge, unrelated to
origin caching) — ruled out as a competing explanation, not left unexplained.
**Still INCONCLUSIVE**: whether the TTL is a fixed value or variable — the
evidence (one ~33min gap, one ≥107min stable window) does not support inventing
a single number, and this report doesn't.

### Priority 2 — FFprobe media validation: unblocked

`ffprobe` was still not installed (`ffprobe -version` → not found). Per the
closure prompt ("install only if the environment/rights allow"), asked the user
first: approved a `choco install ffmpeg -y`, which **failed** — Chocolatey
requires admin rights this session doesn't have
(`UnauthorizedAccessException` on `C:\ProgramData\chocolatey\lib-bad`, not a
decision this project made). Asked the user again for a portable-build
alternative, approved: downloaded the official gyan.dev static Windows build
into `tools/ffmpeg/` (gitignored — a binary tool, not project code; no
system-wide change made).

Built `scripts/validate_media_sample.py`: a **real** pipeline — episode page →
player iframe → HLS manifest URL (same one any viewer's browser receives) →
`ffmpeg -t 2 -c copy` cuts a 2-second clip to a temp directory → `ffprobe`
probes the local file → temp directory deleted immediately (confirmed no
leftovers). Ran against the same 3 episodes as Phase 6's manifest-text sample:
**all 3 VALID**, and `ffprobe`'s independent container inspection **exactly
confirmed** Phase 6's manifest-based codec/resolution conclusions (H.264/AAC,
1080p top tier).

**New finding this session**: none of the 3 probed clips contain a subtitle
stream — **VOSTFR subtitles on this site are almost certainly hardcoded/burned
into the video image**, not a selectable soft-subtitle track. This could not
have been observed from manifest-text parsing alone (Phase 6); it required an
actual container probe.

**Evidence**: `output/evidence/media_ffprobe_validation.md`; verdict logic
extracted into a pure, tested function (`determine_verdict()`, 6 unit tests).
`ffprobe` availability is now `TESTED`/`MEASURED` where it was `BLOCKED`.

### Priority 3 — Long pagination: resolved definitively

**OLD**: "chapter-list pagination for 100+ episode shows: INCONCLUSIVE — no
such title found on this site" (Phase 3).
**NEW**: "confirmed NO pagination exists at any scale, up to 1208 episodes"
(this session).
**WHY**: found "Détective Conan" (VOSTFR), a 1208-episode series, via `/?s=`
search — the largest title on this site by a wide margin (previous max sample:
17 episodes). Its entire chapter list renders on a single page (620KB HTML), no
pagination control found (`.chapters-pagination`, `.wp-manga-nav`, etc. — all 0
matches, same as every smaller anime checked).
**EVIDENCE**: `output/evidence/long_pagination.md`; new live regression test
(`test_long_pagination_live.py`) asserting >500 episodes parse with 0
unparseable numbers, correct descending order, and 0 duplicates on this real
1208-episode page — a much stronger regression guard than any prior sample.
Also found: 4 episode numbers missing from the 1-1212 range (149, 152, 1052,
1063) — documented as an observed fact, `INCONCLUSIVE` on cause, not explained
away. Also tested homepage `/page/N/` edge cases (`/page/9999/`, `/page/0/`,
`/page/abc/`) — all handled safely by the existing, unmodified
`parse_homepage()` (0 entries / redirected-to-page-1 / fell through to
unrelated content, respectively — no crash, no fabricated data in any case).

### Priority 4 — HLS rendition variance: broadened, one new nuance found

**OLD**: "1 sample had 1 rendition, 2 samples had 2 renditions — INCONCLUSIVE
what drives the difference" (Phase 6).
**NEW**: same conclusion, **now backed by 5 samples instead of 3** (4/5 have 2
renditions, 1/5 has 1) — the original finding holds up under a larger sample,
not overturned.
**Additional finding, not previously observed**: fetched Détective Conan ep 158
(an old-catalog dub)'s manifest — its top-tier rendition is **832x624** (not
1080p at all, a genuinely lower-resolution source), and its two renditions have
**different frame rates from each other** (24.39 vs 23.974 fps) — the first
time frame rate was observed to vary between renditions of the same episode.
**WHY it matters**: rules out an implicit assumption that renditions of one
episode are simple bitrate re-encodes of a single common master; they may come
from different source encodes entirely.
**EVIDENCE**: `output/evidence/hls_rendition_analysis.md`; new regression test
(`test_renditions_can_have_different_fps`) using the real observed values.

### Priority 5 — VF/VOSTFR pairing: stronger signal found

**OLD**: "URL-slug matching, 20/28 (71%) — real but not the sole method" (Phase
7).
**NEW**: the anime page's own `native_title`+`romaji_title` fields (Phase 3)
match **exactly on all 3 known real pairs checked** ("Tomb Raider King" VF/JAP,
"Re:Zero ... S4" VF/VOSTFR, "Détective Conan" VF/VOSTFR) — a stronger signal
than slug matching, since these fields describe the original work and aren't
re-worded for a dub release (unlike a URL slug, which can diverge, e.g.
"Tomb Raider King (JAP)" vs a bare-slug VF page).
**WHY the old finding is kept, not replaced**: slug matching is still useful as
a fallback signal when metadata is missing; it isn't wrong, just weaker than
this newly-tested alternative.
**EVIDENCE**: `output/evidence/vf_vostfr_pairing_review.md`; new function
`analysis.identity.language_pair_confidence()` returning an explicit
HIGH/MEDIUM/LOW/UNKNOWN (never a bare boolean, per MASTER_PLAN.md §20), 5 unit
+ 2 live tests (3 known real pairs correctly resolve HIGH; 1 unrelated-anime
control case correctly does NOT resolve HIGH).

### Priorities 6-9 — reviewed, baseline confirmed unchanged

Per the closure prompt's baseline-preservation rule, these were **confirmatory
re-checks**, not re-audits: homepage/`page/2/` re-checked live for 0 URL
overlap (Priority 6); all 9 dedup + 6 restart-recovery tests re-run and passing
unchanged, plus an incidental large-scale confirmation (0 key collisions across
1208 real episode URLs) (Priority 7); fingerprint live tests re-run (0
regressions, plus the 1208-episode page incidentally exercised
`ANIME_PAGE_SELECTORS` at a much larger scale with 0 regressions) (Priority 8);
Phase 15's concurrency conclusion re-affirmed, no new measurement needed
(Priority 9). See `output/evidence/closure_reviews_6_to_9.md`.

### Priority 10 / Phase 14 (code quality)

Ran `pyflakes` across `src/`, `tests/`, `scripts/` — found and fixed 2 real,
minor issues: an unused `field` import in `detection/deduplication.py` and an
unused `pytest` import in `tests/unit/test_http_client.py`. Re-ran the full
suite after each fix (no regressions). Checked for silent exception handling
(`except Exception: pass` / bare `except:`) — **none found anywhere in `src/`**.
Checked for per-title hardcoded logic and fragile `nth-child` selectors —
**none found**. No further code-quality issues were identified that warranted a
change.

### Tests executed

```
python -m pytest -q
```

Result: **138 passed** (baseline 115 + 23 new this session: 6 cache_signal + 6
media-validation-verdict + 1 fps-variance regression + 5 language-pairing unit
tests, plus 5 new live integration tests — cache_signal ×2, long_pagination ×1,
language_pairing ×2 — some counts folded into the totals above).

### Bugs found

2 unused imports (see Code Quality above) — both fixed, both regression-safe
(confirmed via full suite re-run after each).

### Decisions

- Asked the user before each system-affecting step (choco install, then the
  portable-binary download) rather than choosing one unprompted — both are
  actions with effects outside the project directory, matching this session's
  own operating rules around confirming before install/download actions.
- Did not invent a single "the TTL is N minutes" figure despite having much
  richer data than Phase 9 — the evidence (a ~33min gap and a ≥107min stable
  window) genuinely doesn't support one value, and MASTER_PLAN.md §7/§40 are
  explicit that a report must reflect exactly what was demonstrated, not what
  would be convenient to state.
- Kept the original Phase 6/7/9 findings' evidence files untouched and added
  session-dated corrections alongside them, rather than editing them in place,
  so the OLD/NEW/WHY/EVIDENCE trail required by the closure prompt (and
  MASTER_PLAN.md §36) stays inspectable.

### Final V1 readiness answers (per the closure prompt's own §36 checklist)

- Discovery: **YES** (homepage feed; latency now bounded by ≥107min measured
  cache window, strengthening rather than weakening the original correction).
- Identity: **YES** (anime_key/episode_key; additionally stress-tested at 1208
  episodes with 0 collisions this session).
- Metadata: **YES**, with documented optional fields; VF/VOSTFR pairing now has
  a stronger primary signal (native/romaji) plus a documented fallback (slug).
- Media: **YES** for structural validation — now **directly confirmed via real
  ffprobe**, not just manifest-text parsing; still `BLOCKED` for deobfuscating
  the embed's packed script (by design, unchanged).
- Deduplication: **YES** (unchanged, re-confirmed).
- Recovery: **YES** (unchanged, re-confirmed).
- Change detection: **YES** (unchanged, re-confirmed, plus a new reusable
  cache-generation-comparison signal added).
- Multi-anime: **YES** (unchanged from Phase 14; not specifically re-tested
  this session, no new evidence needed).

### Final GO / NO-GO

**GO.** All critical components remain sufficiently demonstrated after this
closure pass; every remaining open item (exact numeric cache TTL, deobfuscating
the embed script, the 4 missing Détective Conan episode numbers, whether every
anime reliably populates Native/Romaji) is a genuinely `INCONCLUSIVE`/`BLOCKED`
detail that does not block V1 architecture design — it is documented, not
glossed over, per the closure prompt's explicit warning against confusing a GO
with "nothing left unknown."

## Session 16 (Closure, second pass) — 2026-09-17

The user re-issued an expanded closure prompt mid-session (same 4 priorities
plus explicit new requirements: security review, observability review, an
explicit authorization-vs-redistribution distinction, and a new mandatory
26-section final report structure with an expanded final table). Rather than
redo Session 15's substantive work, this pass:

- **Finished the in-progress cache TTL measurement**: the dedicated background
  poll (`measure_cache_ttl.py`) completed its full planned run — **11 samples
  over 50m22s, zero regeneration observed on either page**. Final, fully-
  measured lower bounds: **≥138 minutes (homepage)** and **≥231 minutes (the
  anime page tested)** — both raised again from the mid-run interim figures
  quoted at the end of Session 15 (≥107min homepage only), now backed by a
  *completed* observation window rather than a still-running one. Updated
  `output/evidence/cache_ttl_analysis.md` and the closure report accordingly
  (OLD → NEW → CAUSE → EVIDENCE, per the prompt's rule) rather than treating
  the interim figure as final.
- **Added an explicit Security review**: no `.env`, no credential-shaped
  strings in `src/`/`scripts`/`config/`, `.gitignore` correctly excludes all
  sensitive/large paths including the new `tools/ffmpeg/` portable binary —
  PASS, nothing found requiring a fix.
- **Added an explicit Observability review**: documented the 13 existing
  warning/info log call sites across the HTTP client and parsers as adequate
  for this project's own scope, and explicitly noted that the prompt's
  suggested V1 pipeline-stage event vocabulary
  (DISCOVERED/IDENTIFIED/QUEUED/.../PUBLISHED/CLEANED/STRUCTURE_CHANGED)
  describes a pipeline this project deliberately doesn't build — carried
  forward as a named recommendation for V1, not retrofitted here.
- **Added an explicit Cleanup review**: confirmed no files >5MB, no leftover
  `.mp4`/`.ts`/`.m3u8` outside gitignored/test-fixture paths, and that the
  Session 15 media-validation temp clips were already confirmed deleted.
- **Added an explicit authorization-vs-redistribution distinction**
  (`output/evidence/authorization_vs_redistribution.md`): technical
  accessibility (permissive robots.txt, reachable pages/media) is explicitly
  documented as NOT evidence of redistribution rights — this was implicit in
  earlier phrasing ("rather than bulk downloading or redistribution") but is
  now a standalone, explicit `INCONCLUSIVE` finding of its own, per the
  prompt's specific requirement not to conflate the two.
- **Rewrote `output/reports/SOURCE_AUDIT_FINAL_CLOSURE_REPORT.md`** to the
  newly-specified 26-section structure (Executive Summary → Previous Baseline
  → Environment → Cache/TTL → ... → Final Decision), with the expanded final
  table (added "Remaining limitation" and "V1 impact" columns) and an explicit
  GO/CONDITIONAL GO/NO-GO decision with reasoning for why CONDITIONAL GO was
  *not* chosen despite open `INCONCLUSIVE` items. The prior session's version
  of this file is superseded by this rewrite (the underlying evidence files it
  cited are all still present and unchanged); [SOURCE_TECHNICAL_REPORT.md](output/reports/SOURCE_TECHNICAL_REPORT.md)
  remains the separate, unmodified Phase 16 deliverable.

### Tests executed

```
python -m pytest -q
```

Result: **138 passed** — unchanged from Session 15's final count; this pass
added no new code (only evidence/documentation), so no new tests were needed.

### Bugs found

None.

### Decisions

- Treated the newly-specified 26-section report structure as a request to
  restructure the *deliverable file*, not a request to redo the underlying
  investigation — the technical content is identical to Session 15's findings,
  reorganized and given the additionally-required sections (Observability,
  Security, explicit authorization-vs-redistribution) that Session 15's
  version had only implicitly or not at all.
- Did not re-run any live site checks in this pass beyond letting the
  already-in-flight background measurement finish — the prompt's own baseline-
  preservation rule ("ne pas refaire inutilement ces tests") applied directly,
  since nothing about this pass's new requirements (security/observability/
  authorization framing) needed new live evidence to satisfy.

## Final Decision (MASTER_PLAN.md §54 — reproduced in full in the report itself)

- Can the source be reliably monitored? **YES** (via the homepage feed; latency
  bounded by the measured ≥35min origin cache TTL, not a 1-2min assumption).
- Can episodes be identified reliably? **YES** (anime_key/episode_key, both
  measured stable).
- Can metadata be extracted reliably? **YES**, with 2 fields (English title,
  total-episodes-planned) documented as legitimately optional.
- Can authorized media be handled reliably? **YES** for structural analysis;
  **INCONCLUSIVE/BLOCKED** beyond that (script deobfuscation out of scope,
  ffprobe unavailable).
- Can duplicates be detected reliably? **YES** (all 8 §21 scenarios pass).
- Can site changes be detected? **YES** (fingerprinting mechanism built and
  live-reverified).

## Session 1 (cont'd) — 2026-09-17

### Phase completed

**Terms-of-Use / robots.txt authorization check** (MASTER_PLAN.md §3, prerequisite for Phase 1)
**Phase 1 — HTTP client**

### Work done — authorization check

Full findings: [output/evidence/phase0_authorization_check.md](output/evidence/phase0_authorization_check.md)
(this file is evidence output and is gitignored, but its content is summarized here so
the decision survives even without that directory).

- `https://voir-anime.to/robots.txt` (TESTED, HIGH): `User-agent: *`, `Disallow:` (empty)
  — no path is disallowed to any crawler; sitemap published at `/sitemap_index.xml`.
- Homepage footer (TESTED, HIGH): only a `mailto:voiranime@gmail.com` contact link and
  a `© 2026 Madara Inc.` copyright line — no link to Terms of Use, DMCA policy, or
  legal notice.
- Direct probes of `/terms`, `/dmca`, `/mentions-legales`, `/cgu` (TESTED, HIGH): all 4
  returned the site's custom 404 page. No dedicated terms page found at these paths.
  (INCONCLUSIVE/LOW: a terms page might exist at some other, unguessed path — not
  exhaustively searched via the sitemap.)
- **Decision:** in the absence of any explicit Terms of Use, and given robots.txt
  places no restriction on crawling, `source_audit` will (a) perform only read-only,
  low-rate GET requests to publicly served pages, (b) identify itself with a
  descriptive `User-Agent` including contact info, (c) never attempt to bypass any
  auth/CAPTCHA/DRM/access-control mechanism encountered (mark `BLOCKED` instead), and
  (d) stay limited to structural/technical analysis, not bulk downloading or
  redistribution.

### Work done — Phase 1 HTTP client

- Implemented `src/source_audit/fetch/http_client.py`:
  - `FetchErrorType` enum: `NONE`, `TIMEOUT`, `DNS`, `HTTP_403`, `HTTP_404`,
    `HTTP_429`, `HTTP_500`, `HTTP_OTHER`, `NETWORK_OTHER` — each handled distinctly,
    never collapsed into one generic error.
  - `HttpClient.get()`: retries only transient conditions (timeout, 429, 5xx) with
    linear backoff (`retry_backoff_seconds * attempt`); does **not** retry 403/404/
    other 4xx (not transient; retrying a 403 looks like probing).
  - `FetchResult`: url, status_code, error_type, elapsed_seconds, content_bytes,
    attempts, text, headers — enough to measure response time/size per MASTER_PLAN.md §12.
  - Optional evidence saving (raw HTML to a configured directory) gated by
    `save_evidence=True`, off by default.
  - DNS failures are distinguished from generic connection errors by inspecting
    `ConnectError.__cause__` for `socket.gaierror`.
- Unit tests (`tests/unit/test_http_client.py`, offline via `httpx.MockTransport`):
  200 OK, 404 (not retried), 403 (not retried), 429 (retried then succeeds), 500
  (retries exhausted, reports last status), timeout (classified + retried), DNS
  failure (classified), empty response body, malformed/unclosed HTML (still returned
  as text — no exception), evidence-saving writes the expected file.
- Integration tests (`tests/integration/test_http_client_live.py`, real network,
  marked `@pytest.mark.integration`): live homepage returns 200 with real HTML body
  and non-zero elapsed time; a deliberately nonexistent path returns 404; `robots.txt`
  is reachable and its body is returned. **All 3 passed against the live site.**

### Tests executed

```
python -m pytest -q                     # unit + integration
```

Result: **18 passed** (15 unit incl. models/config/http_client, 3 live integration).

### Bugs found

None.

### Decisions

- Retry policy only covers 429/5xx/timeout — explicitly not 403/404 — to avoid
  hammering the server on non-transient responses and to keep behavior conservative
  given there is no published rate-limit policy to defer to.
- Evidence saving is opt-in per call (`save_evidence=True`) rather than global, so
  later phases can choose which fetches are worth persisting (keeping with §48's
  "minimize collected data" rule) instead of writing every response to disk.
- Live integration tests are kept in `tests/integration/` and excluded from the
  default fast unit-test loop only by directory convention (both currently run in
  `pytest -q` since no network-skipping flag was configured yet — acceptable for now
  since the live site responded quickly and reliably in this session; revisit if this
  becomes flaky/slow).

### Status table (partial — will be finalized in Phase 16)

| Component            | Status        | Confidence | Evidence                    | Notes |
|-----------------------|---------------|------------|------------------------------|-------|
| Project scaffolding   | TESTED        | HIGH       | `pytest -q` → 5 passed        | Phase 0 |
| Terms/authorization check | TESTED    | HIGH       | `output/evidence/phase0_authorization_check.md` | robots.txt permissive, no ToS page found |
| HTTP client            | TESTED        | HIGH       | 15 unit + 3 live integration tests, all passing | Phase 1 |
| Homepage discovery     | TESTED        | HIGH       | 5 unit + 2 live integration tests, `scripts/analyze_homepage.py` dry-run | Phase 2 |
| Language detection (homepage) | TESTED (URL suffix) / MEASURED (film prefix) | HIGH (suffix) / MEDIUM (film) | `output/evidence/phase2_homepage_structure.md`, `test_homepage.py` | See Phase 2 below; full Phase 7 sampling still pending |
| Anime identification   | NOT_EVALUATED | UNKNOWN    | —                              | Phase 3 |
| Episode identification | NOT_EVALUATED | UNKNOWN    | —                              | Phase 4 |
| Player structure       | NOT_EVALUATED | UNKNOWN    | —                              | Phase 5 |
| Media format            | NOT_EVALUATED | UNKNOWN    | —                              | Phase 6 |
| Language detection      | NOT_EVALUATED | UNKNOWN    | —                              | Phase 7 |
| New episode detection   | NOT_EVALUATED | UNKNOWN    | —                              | Phase 9 |
| Duplicate detection     | NOT_EVALUATED | UNKNOWN    | —                              | Phase 10 |
| Change detection        | NOT_EVALUATED | UNKNOWN    | —                              | Phase 12 |
