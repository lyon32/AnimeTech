# source_audit

Technical reconnaissance / source-compatibility audit toolkit.

Purpose: establish reproducible, evidence-backed facts about how a video source
(https://voir-anime.to/) works technically — structure, identification, metadata,
media, language detection, caching behavior, change detection — so that a future
monitoring/publishing pipeline (out of scope here) can be designed on measured
facts instead of assumptions.

**This project is complete.** It does not implement any downloading, publishing,
or Telegram pipeline (see [SOURCE_PERMISSION_VERIFICATION.md](SOURCE_PERMISSION_VERIFICATION.md)
for why: no page on the target site establishes redistribution authorization,
only technical accessibility — those are two different things, and this project
deliberately stops at the first one).

## Start here

| Document | What it is |
|---|---|
| [MASTER_PLAN.md](MASTER_PLAN.md) | The governing plan: 16 phases, mandatory evidence statuses, hard rules (no DRM/CAPTCHA/auth bypass, no hard-coded per-title logic, evidence before conclusions) |
| [output/reports/SOURCE_TECHNICAL_REPORT.md](output/reports/SOURCE_TECHNICAL_REPORT.md) | The Phase 16 technical report — full findings across all 16 phases |
| [output/reports/SOURCE_AUDIT_FINAL_CLOSURE_REPORT.md](output/reports/SOURCE_AUDIT_FINAL_CLOSURE_REPORT.md) | **The final deliverable.** Closure pass resolving the 4 priority open items (cache TTL, ffprobe validation, long pagination, HLS rendition variance), plus security/observability review and the final GO decision |
| [SOURCE_PERMISSION_VERIFICATION.md](SOURCE_PERMISSION_VERIFICATION.md) | Direct, live verification of the target site's terms/copyright/licensing posture — the reason this project stops at technical analysis |
| [SESSION_REPORT.md](SESSION_REPORT.md) | Full session-by-session log: what was built, tested, found, and decided, in order |

## What this project established (summary)

- **Site structure**: homepage/pagination, anime pages, episode pages, player
  (JW Player via a third-party embed), and HLS media are all structurally
  mapped and parsed by tested code in `src/source_audit/`.
- **Identification**: `anime_key` (WordPress post ID) and `episode_key`
  (canonicalized URL) are both measured stable — stress-tested against a
  1208-episode real series with zero collisions.
- **Caching**: the site serves pages from an origin-side cache with a
  measured lower bound of ≥138 minutes (homepage) / ≥231 minutes (anime
  pages) — this materially changes any future polling-cadence design and was
  this project's single most consequential finding.
- **Media validation**: HLS manifest parsing is cross-confirmed by real
  `ffprobe` container inspection (2-second clips only, deleted immediately
  after probing — never a full download).
- **Discovery, deduplication, restart recovery, structure-change detection,
  multi-anime ordering, and concurrency behavior** are all tested with both
  offline unit tests and live integration tests against the real site.
- **Authorization**: robots.txt is permissive for crawling; no Terms of Use,
  copyright policy, or redistribution statement exists anywhere on the site
  (verified exhaustively via the site's own page sitemap). Technical
  accessibility was never treated as redistribution permission.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

`ffprobe`/`ffmpeg` (used only for the media-validation script) are not a core
dependency; `pip install -e ".[scripts]"` pulls in `psutil` for the concurrency
script, and a portable ffmpeg/ffprobe build is expected at `tools/ffmpeg/bin/`
(gitignored — see `scripts/validate_media_sample.py`'s docstring).

## Tests

```bash
python -m pytest -q              # everything, incl. live requests to the real site
python -m pytest -m "not integration" -q   # offline unit tests only
```

138 tests, all passing as of the final closure session — 24+ of them perform
real, live HTTP requests against the authorized target to verify findings
directly rather than from memory.

## Project layout

```
src/source_audit/
├── fetch/          HTTP client (Phase 1)
├── analysis/       homepage/anime/episode/player/media/identity parsers
└── detection/       discovery, deduplication, restart-recovery, fingerprint,
                     cache-signal, per-anime ordering

scripts/            CLI entry points for each analysis (analyze_*, simulate_*,
                    measure_*, validate_*)
tests/unit/         offline, fixture-based
tests/integration/  live, hit the real site (marked @pytest.mark.integration)
output/evidence/    dated, per-finding writeups (gitignored — contains
                    scraped structural data, not code)
output/reports/     the two reports listed above (tracked in git)
```

## Scope boundary

This project performs read-only structural/technical analysis only. It never
downloads full media, never bypasses any access-control/DRM/CAPTCHA/anti-bot
mechanism, and never assumes authorization it hasn't verified. See
`MASTER_PLAN.md` §49 and `SOURCE_PERMISSION_VERIFICATION.md` for the governing
rules and the authorization finding that keeps it that way.
