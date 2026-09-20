# SOURCE AUDIT — MASTER DEVELOPMENT PLAN

Version: 1.0
Projet: source_audit
Type: Technical reconnaissance / source compatibility audit
Objectif: établir les faits techniques nécessaires avant le développement de la V1

This file is the verbatim governing plan provided by the project owner on 2026-09-17.
It defines: the 16 execution phases (Phase 0 → Phase 16), the mandatory evidence
statuses (`TESTED`, `MEASURED`, `NOT_EVALUATED`, `INCONCLUSIVE`, `BLOCKED`, `FAILED`),
the non-negotiable rules (no DRM/auth/CAPTCHA bypass, evidence before conclusions,
no per-title hard-coding, never delete prior observations — only compare and document
deltas), the target project structure, and the Definition of Done.

Full source text is kept with the project owner's original message; this repository
implements it phase by phase, tracked in [SESSION_REPORT.md](SESSION_REPORT.md).

## Phase order (must not be skipped without recording NOT_EVALUATED / BLOCKED + reason)

0. Initialization
1. HTTP client
2. Homepage analysis
3. Anime page analysis
4. Episode page analysis
5. Player analysis
6. Media analysis
7. Language (VF/VOSTFR) analysis
8. Unique identification (anime/season/episode/language keys)
9. New-episode detection strategies
10. Deduplication tests
11. Restart/recovery simulation
12. Site structure change detection
13. Resilience tests (errors, timeouts, missing fields)
14. Multi-anime ordering tests
15. Concurrency simulation (measurement only, no arbitrary constants)
16. Final technical report (`output/reports/SOURCE_TECHNICAL_REPORT.md`)

## Absolute rules

- Never invent information; never assume behavior of an unobserved part of the site.
- Never treat a single page as representative of the whole site.
- Test before concluding; every important conclusion needs reproducible evidence.
- If unknown: write `INCONCLUSIVE` or `BLOCKED`, never guess.
- Stay within content/streams the user is authorized to access.
- Never bypass DRM, auth, CAPTCHA, or anti-bot protections — mark `BLOCKED` and document.
- Never silently swallow exceptions (`except Exception: pass` is forbidden).
- Never delete prior observations when a new one contradicts them — document both plus
  the differing test conditions and a reasoned conclusion.
- No hard-coded per-anime logic unless justified by documented, generalized analysis.
- `source_audit` builds no V1/V2 functionality (no downloader pipeline, no Telegram bot,
  no payments, no user accounts) — it only produces the technical findings for their design.

Out-of-scope starting URL for the audit: https://voir-anime.to/ — Terms of Use and
authorization scope must be checked before any automated retrieval (Phase 0/1).
