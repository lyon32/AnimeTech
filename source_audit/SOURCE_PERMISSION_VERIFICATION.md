# SOURCE PERMISSION VERIFICATION — voir-anime.to

Verification date/time: 2026-09-18 (UTC, per live HTTP `Date` response headers
observed during this check)
Method: direct, live HTTP requests to the target site (no assumptions, no
inference from general WordPress/Madara-theme conventions — every claim below
is backed by a specific fetched URL and its actual content).

---

## 1. URLs verified

| URL | Result | What it is |
|---|---|---|
| `https://voir-anime.to/robots.txt` | 200 | Yoast-generated robots.txt |
| `https://voir-anime.to/sitemap_index.xml` | 200 | Sitemap index (9 shards) |
| `https://voir-anime.to/page-sitemap.xml` | 200 | **Complete list of every static WP page on the site** |
| `https://voir-anime.to/` (homepage, footer) | 200 | Checked for legal links/copyright text |
| `https://voir-anime.to/terms/` | 404 | — |
| `https://voir-anime.to/terms-of-service/` | 404 | — |
| `https://voir-anime.to/tos/` | 200 | **False positive**: matches an unrelated anime title's slug ("Toshokan Sensou"), not a terms page |
| `https://voir-anime.to/cgu/` | 404 | — |
| `https://voir-anime.to/conditions-generales/` | 404 | — |
| `https://voir-anime.to/conditions-utilisation/` | 404 | — |
| `https://voir-anime.to/dmca/` | 404 | — |
| `https://voir-anime.to/copyright/` | 404 | — |
| `https://voir-anime.to/copyright-policy/` | 404 | — |
| `https://voir-anime.to/mentions-legales/` | 404 | — |
| `https://voir-anime.to/legal/` | 404 | — |
| `https://voir-anime.to/about/` | 404 | — |
| `https://voir-anime.to/a-propos/` | 404 | — |
| `https://voir-anime.to/privacy/` | 404 | — |
| `https://voir-anime.to/privacy-policy/` | 404 | — |
| `https://voir-anime.to/faq/` | 404 | — |
| `https://voir-anime.to/contact/` | 404 | (contact is a footer link, not a page — see §4) |
| `https://voir-anime.to/disclaimer/` | 404 | — |

## 2. Pages inspected

- Homepage: full HTML parsed, `<footer>` extracted, all `<meta>` tags scanned
  for `license`/`copyright`/`author`/`publisher` attributes, full visible text
  searched for "Copyright", "DMCA", "license"/"licence", "rights reserved",
  "distributor", "official".
- `page-sitemap.xml`: this is not a guess-based probe — it is the site's own
  Yoast SEO-generated, exhaustive index of **every standalone WordPress page**
  that exists on the entire site (as opposed to anime/episode content, which
  lives in separate sitemap shards already documented in `source_audit`).

## 3. Rules found, classified

| Item | Classification | Evidence |
|---|---|---|
| Crawling/scraping via `robots.txt` | **EXPLICITLY_ALLOWED** (crawling only, not redistribution) | `robots.txt`: `User-agent: *` / `Disallow:` (empty) — permits crawling by any user-agent. This says nothing about downloading, storing, or redistributing content — see §9. |
| Automated bot access | NOT_FOUND (no explicit bot policy beyond robots.txt) | No `/dmca/`, `/terms/`, or similar page exists to state one (see full 404 list, §1) |
| Download of episodes | NOT_FOUND | No page anywhere states download terms |
| Redistribution of episodes | NOT_FOUND | No page anywhere states redistribution terms |
| Copyright/licensing statement for the anime content itself | NOT_FOUND | Only text found: `© 2026 Madara Inc. All rights reserved` in the footer — this is the theme/software vendor's own boilerplate copyright notice (Madara is the WP-Manga theme this site runs, independently confirmed throughout `source_audit`'s structural findings), **not a statement about who holds rights to the anime video content** |
| Official distributor / licensing partner named | NOT_FOUND | No such text found anywhere on the homepage or in any static page |
| DMCA takedown procedure | NOT_FOUND | No DMCA page exists (404) and no DMCA process is described anywhere found |
| Terms of Service / CGU | NOT_FOUND | No such page exists anywhere on the site (see §1 and the exhaustive sitemap in §4) |

## 4. Exhaustive proof that no legal/terms page exists anywhere on the site

`https://voir-anime.to/page-sitemap.xml` (fetched live, 200 OK) lists **all 7
static pages that exist on this WordPress site**:

```
https://voir-anime.to/
https://voir-anime.to/user-settings/
https://voir-anime.to/manga/
https://voir-anime.to/anime/
https://voir-anime.to/liste-danimes/
https://voir-anime.to/prochainement/
https://voir-anime.to/nouveaux-ajouts/
```

None of these is a Terms of Service, Copyright, DMCA, Privacy Policy, About,
or FAQ page. Because this sitemap is the site's own generated, complete
inventory of its static pages (not a guess-based probe), this is **exhaustive
evidence**, not merely "we tried several paths and got 404" — there is no
unguessed path left to check within the WordPress page post-type. (A page
could theoretically exist outside WordPress entirely, e.g. hosted on a
different subdomain or off-site; that residual possibility is noted as
`INCONCLUSIVE` in §7, not ruled out to zero, but nothing on the site itself
links to one — see the footer link check directly below.)

## 5. Copyright / licensing information found

- Footer text: `© 2026 Madara Inc. All rights reserved`. "Madara" is the name
  of the commercial WordPress theme/plugin (WP-Manga) this site is built on —
  this is standard theme boilerplate, not a statement of ownership over the
  anime video content being served.
- Footer links (only 3 exist): `https://x.com/voiranime` (social media),
  `https://discord.com/invite/k3nDrtP` (community Discord), and a
  Cloudflare-obfuscated `mailto:` contact link. **None of these leads to a
  distributor, licensor, copyright policy, or takedown procedure.**
- No `<meta>` tag anywhere on the homepage references a license, copyright
  holder, author, or publisher.
- **No distributor, licensing partner, or official rights-holder relationship
  is named anywhere found on the site.**

## 6. External verification

No external, official source (a licensor's own site, an anime distributor's
press release, etc.) was searched for or found corroborating any claim of
authorized redistribution — **because the site itself makes no such claim to
begin with.** There is nothing on voir-anime.to asserting redistribution
rights that would need external corroboration. Per the instruction not to
treat absence-of-blocking as evidence, this section also explicitly excludes:
absence of DRM, absence of technical blocking, absence of any public takedown
notice, and absence of any complaint — none of these were used as evidence of
authorization anywhere in this report.

## 7. Conclusion table

| Question | Résultat | Preuve |
|---|---|---|
| Téléchargement explicitement autorisé ? | **NOT_FOUND** | No page states this anywhere (§1, §4) |
| Redistribution explicitement autorisée ? | **NOT_FOUND** | No page states this anywhere (§1, §4) |
| Usage automatisé autorisé ? | **NOT_FOUND** (beyond robots.txt's crawl permission) | `robots.txt` permits crawling only — silent on downloading/redistributing (§3, §9) |
| Scraping autorisé ? | **EXPLICITLY_ALLOWED** (for crawling, per robots.txt) | `robots.txt`: `Disallow:` empty |
| Bot autorisé ? | **NOT_FOUND** (no explicit bot-specific policy beyond robots.txt) | No terms/bot-policy page exists (§1) |
| Conditions d'utilisation disponibles ? | **NOT_FOUND** | Exhaustive sitemap check, §4 |
| Copyright/licence identifiable ? | **NOT_FOUND** (only generic theme-vendor boilerplate found) | Footer text, §5 |
| Contenu techniquement accessible ? | **TECHNICALLY_ACCESSIBLE** | Confirmed extensively throughout `source_audit` (200 OK on anime/episode/player/manifest URLs) |
| Autorisation de redistribution établie ? | **NOT ESTABLISHED** | No evidence found anywhere, positive or negative (§3-§6) |

## 8. Ambiguities

- The one residual, unresolved possibility (not ruled out to absolute zero):
  a legal page could theoretically exist on a different domain/subdomain
  entirely disconnected from this WordPress install, with no link to it
  anywhere on voir-anime.to. Nothing found in this verification points to
  such a page existing, and the site gives no indication of one — this is
  flagged as a residual gap, not evidence either way.
- `robots.txt`'s permissive crawl policy is real and verified, but it answers
  a narrower question (may a crawler request these URLs) than the one this
  verification was asked to answer (may downloaded content be redistributed).
  These are kept explicitly separate throughout this report, per the
  instruction not to conflate them.

## 9. Decision

# AUTHORIZATION_NOT_ESTABLISHED

The site does not prohibit crawling (robots.txt is permissive), and its
content is technically accessible — both confirmed directly. But **no page,
statement, license, or policy anywhere on the site establishes, addresses, or
even mentions download or redistribution rights for its video content**, and
no rights-holder, distributor, or licensing relationship is named anywhere.
This is not "the site says no" (that would be `PROHIBITED`) — it is "the site
says nothing," which per this verification's own governing rule must not be
read as "the site says yes."

**Consequence**: per §10 of the request, since `AUTHORIZATION_ESTABLISHED` was
not reached, no V1_POC test specification is prepared. This finding is
consistent with, and does not contradict, `source_audit`'s own Phase 0 finding
(`output/evidence/phase0_authorization_check.md`) and this project's later,
explicit `authorization_vs_redistribution.md` finding — this report adds an
exhaustive sitemap-based proof (§4) and a copyright/footer-specific check (§5)
that go beyond what Phase 0 originally checked, strengthening the same
conclusion rather than changing it.
