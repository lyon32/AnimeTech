"""Free-text request parser: "one piece 1150", "One Piece E1150", "One Piece Episode 1150", "one piece ep 1150",
"bleach saison 2 episode 5 vf", "bleach s01e05", "bleach saison 1", "naruto dernier épisode" -> one normalised query.

Case, accents and spacing never matter.  Nothing is guessed: what the text does not say stays None (the bot then asks).

Ambiguity note: a bare trailing number ("one piece 1150") is read as the EPISODE, because that is how people write.
A title that really ends with a number ("Mob Psycho 100") is handled one level up: the search tries the stripped
title first and falls back to `ParsedQuery.full_title` (the whole text minus version/season markers) when the stripped
title finds nothing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedQuery:
    title: str                 # normalised: lower case, no accents, single spaces (episode number removed)
    full_title: str            # same, but a bare trailing number is kept (title that ends with a number)
    season: int | None = None
    episode: int | None = None
    latest: bool = False       # "dernier épisode" — resolved later from the source listing, never guessed
    version: str | None = None  # VF | VOSTFR | None
    whole_season: bool = False  # "bleach saison 1" (a season, no episode)
    raw: str = ""

    def key(self) -> tuple:
        """Normalised identity of the query (for comparing formulations)."""
        return (self.title, self.season, self.episode, self.latest, self.version, self.whole_season)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[_\-–—]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_SXEX = re.compile(r"\bs(\d{1,2})\s*[ex\.]\s*e?(\d{1,5})\b")
_SEASON = re.compile(r"\b(?:saison|season|s)\s*\.?\s*(\d{1,2})\b")
_EPISODE = re.compile(r"\b(?:episodes?|epis|ep|e)\s*\.?\s*(?:n\s*°?\s*)?(\d{1,5})\b")
# "dernier épisode" / "last ep", or "dernier" as the LAST word.  NOT "Le Dernier Maître De L'air": there it belongs to the title.
_LATEST = re.compile(r"\b(?:dernier|derniere|last|latest)(?:\s+(?:episode|ep)\b|\s*$)")
_VERSION = re.compile(r"\b(vostfr|vf)\b")
_TRAILING = re.compile(r"(?:^|\s)(\d{1,5})$")


def parse_query(text: str) -> ParsedQuery:
    raw = text or ""
    s = " " + normalize(raw) + " "
    s = s.replace(" épisode", " episode")
    version = None
    m = _VERSION.search(s)
    if m:
        version = m.group(1).upper()
        s = s[:m.start()] + " " + s[m.end():]
    latest = False
    m = _LATEST.search(s)
    if m:
        latest = True
        s = s[:m.start()] + " " + s[m.end():]
    season = episode = None
    m = _SXEX.search(s)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = _SEASON.search(s)
        if m:
            season = int(m.group(1))
            s = s[:m.start()] + " " + s[m.end():]
        m = _EPISODE.search(s)
        if m:
            episode = int(m.group(1))
            s = s[:m.start()] + " " + s[m.end():]
    s = re.sub(r"[?!¿¡\"'`«»’‘,;]+", " ", s)                       # stray punctuation is not part of a title
    s = re.sub(r"\s+", " ", s).strip(" .,:;-")
    full_title = s
    if episode is None and not latest:
        m = _TRAILING.search(s)
        if m and s[:m.start()].strip() and season is None:
            episode = int(m.group(1))
            s = s[:m.start()].strip()
    whole_season = season is not None and episode is None and not latest
    return ParsedQuery(title=s, full_title=full_title, season=season, episode=episode, latest=latest,
                       version=version, whole_season=whole_season, raw=raw)
