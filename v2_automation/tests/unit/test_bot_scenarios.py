"""The user bot used the way people use it — including the way they misuse it — against the RECORDED REAL answers of the site.
Every step runs the generic invariants of bot_harness.py (a reply to everything, callbacks valid, no repeated title, results contain the
words of the query or say "autre titre", no search for a bare number, clean series names, no English state, one active request).

A. the reported incidents replayed · B. relevance on every recorded real query · C. free typing at EVERY step · D. every kind of
request, watched or not · E. plausibility of the episode number · F. several users, membership, stale buttons, commands.
"""
import re

import pytest

from bot_harness import FIX, Bot
from v2support import BASE

U = 1001
RECORDED = sorted({p.name.split("__", 1)[1][:-4].replace("_", " ") for p in FIX.glob("VF__*.txt")}) if FIX.exists() else []


@pytest.fixture()
def bot(tmp_path):
    return Bot(tmp_path).join(U)


def go(bot, text, *, uid=U, s=0, n=0, v=None):
    """Say `text`, then answer each question the bot shows (series / season / version) like a person pressing the buttons."""
    bot.say(uid, text)
    for _ in range(4):
        kb = [b.data for row in (bot.last(uid)[2] or []) for b in row if b.data]
        if any(d.startswith("s:") for d in kb):
            bot.tap(uid, f"s:{s}")
        elif any(d.startswith("n:") for d in kb):
            bot.tap(uid, f"n:{n}")
        elif any(d.startswith("v:") for d in kb):
            bot.tap(uid, f"v:{v}" if v else next(d for d in kb if d.startswith("v:")))
        else:
            break


# ══ A. the incidents of the screenshots ════════════════════════════════════════════════════════════════════

def test_A1_bestiale_exists_only_in_the_vf_engine_and_is_found(bot):
    bot.say(U, "Bestiale")                              # was: "Aucun anime trouvé" (the native WordPress search does not know it)
    assert bot.labels(U) == ["🎬 Bestiale 🇫🇷"]
    assert {str(p["asid"]) for p in bot.world.posts} == {"2", "3"}                 # both of the site's engines were asked


def test_A2_wakfu_is_vf_only_with_four_seasons_and_a_clean_name(bot):
    bot.say(U, "wakfu")
    assert bot.labels(U)[0] == "🎬 Wakfu 🇫🇷 · 4 saisons"                          # was "Wakfu S2"
    bot.tap(U, "s:0")
    assert len(bot.labels(U)) == 4
    bot.tap(U, "n:3")
    assert bot.labels(U) == ["🇫🇷 VF"] and "seule version disponible" in bot.last(U)[1]     # was "Saison 4 · VOSTFR"


def test_A3_carmen_sandiego_is_one_series_with_three_seasons(bot):
    bot.say(U, "Carmen Sandiego")
    assert bot.labels(U) == ["🎬 Carmen Sandiego 🇫🇷 · 3 saisons"]                  # was "Carmen Sandiego S2"
    bot.tap(U, "s:0")
    assert [x.split(" · ")[0] for x in bot.labels(U)] == ["📅 Saison 1", "📅 Saison 2", "📅 Saison 3"]


def test_A4_avatar_lists_the_title_match_first_and_never_skips_a_step(bot):
    bot.say(U, "avatar")                                # was: straight to "Écrivez le numéro de l'épisode · VOSTFR"
    assert bot.labels(U)[0].startswith("🎬 Avatar, Le Dernier Maître") and bot.request(U) is None
    assert any("autre titre" in x for x in bot.labels(U)[1:])
    bot.tap(U, "s:0")
    assert "Quelle saison" in bot.last(U)[1]
    bot.tap(U, "n:0")
    assert "Quelle version" in bot.last(U)[1] and bot.labels(U) == ["🇫🇷 VF"]


def test_A5_the_full_title_with_dernier_and_a_typographic_apostrophe_is_found(bot):
    bot.say(U, "Avatar, Le Dernier Maître De L’air")     # was: "Aucun anime trouvé pour « avatar, le maitre de l'air »"
    assert bot.labels(U)[0].startswith("🎬 Avatar, Le Dernier Maître")


@pytest.mark.parametrize("text", ["?", "4", "!!!", "😀", "e", "  ", "3", "ep 5", "dernier"])
def test_A6_noise_and_bare_numbers_never_search_and_never_propose_anime(bot, text):
    bot.say(U, text)
    assert bot.world.searches() == 0 and not any(b.data and b.data.startswith("s:") for b in bot.buttons(U))
    assert bot.request(U) is None


def test_A7_a_number_answers_the_episode_question_only_at_that_step(bot):
    bot.say(U, "wakfu")
    searches = bot.world.searches()
    bot.say(U, "3")                                     # choosing the series: NOT a search, NOT an episode
    assert bot.world.searches() == searches and "Choisissez d'abord" in bot.last(U)[1] and bot.request(U) is None
    bot.tap(U, "s:0")
    bot.tap(U, "n:1")
    bot.say(U, "3")                                     # choosing the version: still not an episode
    assert "Choisissez d'abord" in bot.last(U)[1] and bot.request(U) is None
    bot.tap(U, "v:VF")
    bot.say(U, "3")                                     # the episode question: now it is the answer
    assert bot.request(U)["episode_number"] == 3 and bot.world.searches() == searches


def test_A8_one_piece_is_the_real_series_with_its_episode_count(bot):
    bot.say(U, "one piece")
    assert bot.labels(U)[0].startswith("🎬 One Piece 🇫🇷🇯🇵") or bot.labels(U)[0].startswith("🎬 One Piece 🇯🇵")   # the real One Piece
    bot.tap(U, "s:0")
    bot.tap(U, "n:0")
    assert "🇯🇵 VOSTFR" in [b.text for b in bot.buttons(U)]
    bot.tap(U, "v:VOSTFR")
    assert "1179 épisode(s)" in bot.last(U)[1] and "En Cours" in bot.last(U)[1]
    bot.say(U, "1150")
    assert bot.request(U)["episode_number"] == 1150 and bot.request(U)["state"] == "QUEUED"     # it exists: no 20-minute wait


def test_A9_naruto_puts_the_series_before_films(bot):
    bot.say(U, "Naruto")
    labels = bot.labels(U)
    assert labels[0].startswith("🎬 Naruto") and any("Films" in x for x in labels)
    assert not any("Movie" in x for x in labels[:-2])


# ══ B. relevance on every recorded real query ══════════════════════════════════════════════════════════════

@pytest.mark.skipif(not RECORDED, reason="recorded corpus missing")
@pytest.mark.parametrize("query", RECORDED)
def test_B_every_recorded_query_gets_coherent_results(bot, query):
    bot.say(U, query)
    text = bot.last(U)[1]
    if "Aucun anime trouvé" in text or "ne répond pas" in text or "pas compris" in text:
        assert not any(b.data and b.data.startswith("s:") for b in bot.buttons(U))
        return
    series = [b for b in bot.buttons(U) if b.data and b.data.startswith("s:")]
    assert series                                                                    # the invariants did the rest (I4, I8, I9)
    for b in series:
        assert re.search(r"\s(🇫🇷|🇯🇵)", b.text), f"no version shown on {b.text!r}"     # every entry says which versions exist


def test_B2_search_is_case_accent_and_spacing_independent(bot):
    firsts = set()
    for q in ("one piece", "ONE PIECE", "  One   Piece  ", "one-piece"):
        bot.tap(U, "xs")
        bot.say(U, q)
        firsts.add(bot.labels(U)[0])
    assert len(firsts) == 1


def test_B3_ranking_puts_title_matches_before_other_title_matches():
    from v2_automation.search import SearchHit, group
    hits = [SearchHit("Quanzhi Gaoshou", "u/1", "VOSTFR", None, alt=True, rank=0),
            SearchHit("Avatar, Le Dernier Maître De L’air", "u/2", "VF", None, alt=False, rank=3)]
    order = [s.name for s in group(hits, "avatar")]
    assert order[0].startswith("Avatar, Le Dernier") and order[-1] == "Quanzhi Gaoshou"


def test_B4_the_engine_ids_are_read_from_the_home_page_not_assumed(bot):
    engines = bot.router.search.asp.engines()
    assert engines["VF"]["asid"] == 2 and engines["VOSTFR"]["asid"] == 3 and not engines["VF"].get("fallback")
    assert "aspf%5Bvf__1%5D=vf" in engines["VF"]["options"] and "asp_gen%5B%5D=title" in engines["VF"]["options"]


def test_B5_if_the_home_page_changes_the_fallback_ids_are_used_and_flagged(bot):
    from v2_automation.search import AspEngine
    eng = AspEngine(bot.cfg, get=lambda u: "<html>nothing here</html>", post=bot.world.post)
    e = eng.engines()
    assert e["VF"]["asid"] == 2 and e["VF"]["fallback"] is True and e["VOSTFR"]["asid"] == 3


def test_B6_results_are_cached_for_ten_minutes(bot):
    bot.say(U, "wakfu")
    n = bot.world.searches()
    bot.tap(U, "xs")
    bot.say(U, "Wakfu")
    assert bot.world.searches() == n                                                  # same query: no new call to the site


# ══ C. free typing at EVERY step ═══════════════════════════════════════════════════════════════════════════

STEPS = ["idle", "choose_series", "choose_season", "choose_version", "choose_episode", "await_number", "after_request"]
INPUTS = ["3", "e5", "ep 5", "épisode 12", "dernier", "last", "?", "!!!", "😀", "", "x", "zz", "a" * 150, "/start", "/history",
          "/status", "/help", "/cancel", "/unknown", "ONE PIECE", "wakfu", "bleach saison 1", "naruto dernier épisode",
          "one piece 1150 vf", "  espaces  ", "é à ü ç", "1;2;3", "DROP TABLE requests;--", "<b>gras</b>", "0", "99999", "-5"]


def reach(bot, step):
    if step == "idle":
        return
    if step == "choose_series":
        return bot.say(U, "one piece")
    if step == "choose_season":
        bot.say(U, "wakfu")
        return bot.tap(U, "s:0")
    if step == "choose_version":
        bot.say(U, "kimetsu no yaiba")
        bot.tap(U, "s:0")
        return bot.tap(U, "n:0")
    go(bot, "bestiale")                                # choose_episode
    if step == "await_number":
        bot.tap(U, "num")
    if step == "after_request":
        bot.say(U, "2")


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("text", INPUTS)
def test_C_any_input_at_any_step_gets_a_coherent_reply(bot, step, text):
    reach(bot, step)
    before = bot.n("SELECT COUNT(*) FROM requests")
    bot.say(U, text)                                   # the harness runs every invariant on this reply
    assert bot.n("SELECT COUNT(*) FROM requests") - before in (0, 1)
    r = bot.request(U)
    if r:
        assert r["version"] in ("VF", "VOSTFR") and (r["episode_number"] is None or r["episode_number"] >= 1)


@pytest.mark.parametrize("data", ["ep", "last", "all", "num", "list:0", "list:9", "e:3", "wait:9", "s:0", "s:99", "n:0", "n:9",
                                  "v:VF", "v:VOSTFR", "sp:main:0", "sp:other:5", "sv:other", "xs", "reset", "xr:999"])
@pytest.mark.parametrize("step", STEPS)
def test_C2_any_button_at_any_step_is_handled(bot, step, data):
    reach(bot, step)
    bot.tap(U, data)                                   # a stale / forged / out-of-range button never crashes or stays silent
    assert not any("Traceback" in t or "erreur est survenue" in t for t in bot.texts(U))


# ══ D. every kind of request, watched or not ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("watched", [False, True])
@pytest.mark.parametrize("how,check", [
    ("bestiale 2", lambda r: (r["kind"], r["episode_number"]) == ("episode", 2)),
    ("bestiale ep 3", lambda r: r["episode_number"] == 3),
    ("bestiale dernier épisode", lambda r: r["episode_number"] == 7),
])
def test_D_episode_and_last_for_watched_and_unwatched(bot, watched, how, check):
    if watched:
        bot.watch("bestiale-vf", "Bestiale (VF)", 105982)
    go(bot, how)
    r = bot.request(U)
    assert r and check(r), (how, r)
    assert bot.n("SELECT publish_channel FROM episodes LIMIT 1") == (1 if watched else 0)
    assert bot.n("SELECT COUNT(*) FROM animes WHERE enabled=1") == (1 if watched else 0)     # never added to the watched list


@pytest.mark.parametrize("watched", [False, True])
def test_D2_whole_season_is_confirmed_then_one_request_with_one_job_per_listed_episode(bot, watched):
    if watched:
        bot.watch("wakfu-s3", "Wakfu S3", 777)
    go(bot, "wakfu saison 3")
    assert "saison complète (12 épisodes)" in bot.last(U)[1] and bot.request(U) is None        # asked first
    bot.tap(U, "all")
    assert bot.n("SELECT COUNT(*) FROM requests") == 1 and bot.n("SELECT COUNT(*) FROM episodes") == 12


def test_D3_a_season_that_does_not_exist_is_not_invented(bot):
    bot.say(U, "wakfu saison 7")
    bot.tap(U, "s:0")
    assert "n'existe pas" in bot.last(U)[1] and "Saison 4" in bot.last(U)[1] and bot.request(U) is None


def test_D4_title_ending_with_a_number(bot):
    bot.say(U, "mob psycho 100")
    assert bot.request(U) is None and bot.labels(U)[0].startswith("🎬 Mob Psycho 100")


def test_D5_pick_from_the_episode_list_and_paginate(bot):
    go(bot, "one piece", v="VOSTFR")
    bot.tap(U, "ep")
    bot.tap(U, "list:0")
    assert "page 1/59" in bot.last(U)[1]
    bot.tap(U, "list:58")
    assert "E1179" in [b.text for b in bot.buttons(U)]
    bot.tap(U, "e:1100")
    assert bot.request(U)["episode_number"] == 1100


def test_D6_typing_a_new_title_in_the_middle_replaces_the_search(bot):
    go(bot, "bestiale")
    bot.say(U, "wakfu")
    assert bot.labels(U)[0].startswith("🎬 Wakfu") and bot.step(U) == "choose_series"


def test_D7_both_versions_kept_apart(bot):
    bot.join(2)
    go(bot, "kimetsu no yaiba", v="VF")
    bot.say(U, "3")
    go(bot, "kimetsu no yaiba", uid=2, v="VOSTFR")
    bot.say(2, "3")
    assert (bot.request(U)["version"], bot.request(2)["version"]) == ("VF", "VOSTFR")
    assert bot.n("SELECT COUNT(DISTINCT media_key) FROM episodes") == 2               # VF and VOSTFR = two media


# ══ E. plausibility of the episode number ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n,expected", [(1, "QUEUED"), (7, "QUEUED"), (8, "WAITING_FOR_MEDIA"), (10, "WAITING_FOR_MEDIA"),
                                        (11, "ask"), (500, "ask"), (0, "invalid")])
def test_E_episode_number_versus_what_the_page_lists(bot, n, expected):
    go(bot, "bestiale")                                # 7 episodes listed
    bot.say(U, str(n))
    r = bot.request(U)
    if expected == "invalid":
        assert r is None and "1 ou plus" in bot.last(U)[1]
    elif expected == "ask":
        assert r is None and "n'existe pas encore" in bot.last(U)[1]
    else:
        assert r["episode_number"] == n and r["state"] == expected


# ══ F. several users, membership, stale buttons, commands ══════════════════════════════════════════════════

def test_F1_three_users_same_episode_one_job(bot):
    for u in (2001, 2002, 2003):
        bot.join(u)
        go(bot, "bestiale 2", uid=u)
    assert bot.n("SELECT COUNT(*) FROM episodes") == 1 and bot.n("SELECT COUNT(*) FROM request_items") == 3
    assert bot.n("SELECT COUNT(*) FROM queue_items") == 1


def test_F2_users_do_not_see_each_others_conversations(bot):
    bot.join(2)
    bot.say(U, "one piece")
    bot.say(2, "wakfu")
    bot.tap(U, "s:0")
    assert bot.step(2) == "choose_series" and bot.step(U) == "choose_season"
    bot.say(2, "3")
    assert bot.request(2) is None and "Choisissez d'abord" in bot.last(2)[1]


def test_F3_leaving_the_channel_mid_flow_resets_the_conversation(bot):
    go(bot, "bestiale")
    bot.leave(U)
    bot.say(U, "3")
    assert "rejoignez" in bot.last(U)[1] and bot.request(U) is None
    bot.join(U)
    bot.tap(U, "chk")
    bot.say(U, "3")                                    # the old choice is gone: it asks for a title, it does not guess
    assert bot.request(U) is None and "Écrivez d'abord le nom" in bot.last(U)[1]


def test_F4_stale_buttons_explain_and_offer_to_restart(bot):
    bot.say(U, "wakfu")
    bot.tap(U, "xs")
    for data in ("s:0", "ep", "e:3", "last", "n:0", "v:VF", "wait:5"):
        bot.tap(U, data)
        assert "plus valable" in bot.last(U)[1] or "ne correspond plus" in bot.last(U)[1]
        assert [b.data for b in bot.last(U)[2][0]] == ["reset"]
    bot.tap(U, "reset")
    assert "Écrivez le nom" in bot.last(U)[1]


def test_F5_double_tap_creates_one_request(bot):
    go(bot, "bestiale")
    bot.tap(U, "e:2")
    bot.tap(U, "e:2")
    assert bot.n("SELECT COUNT(*) FROM requests") == 1


def test_F6_second_request_refused_then_cancel_then_possible(bot):
    go(bot, "bestiale 2")
    go(bot, "wakfu 2")
    assert "déjà une demande en cours" in bot.last(U)[1]
    bot.say(U, "/cancel")
    assert "annulée" in bot.last(U)[1]
    go(bot, "bestiale 3")
    assert bot.request(U)["episode_number"] == 3


def test_F7_commands_in_the_middle_of_a_flow_do_not_break_it(bot):
    go(bot, "bestiale")
    bot.say(U, "/history")
    assert bot.last(U)[1].startswith("📚 Mes demandes")
    bot.say(U, "/status")
    bot.say(U, "5")
    assert bot.request(U)["episode_number"] == 5


def test_F8_source_down_is_reported_not_hidden(bot):
    bot.router.search.asp._post = lambda url, data: (_ for _ in ()).throw(RuntimeError("HTTP 503 timed out"))
    bot.say(U, "one piece")
    assert "La recherche du site ne répond pas" in bot.last(U)[1] and bot.request(U) is None


def test_F9_no_message_ever_shows_an_english_state(bot):
    go(bot, "bestiale 2")
    bot.tap(U, f"xr:{bot.request(U)['id']}")
    bot.tap(U, f"xr:{bot.request(U)['id']}")
    bot.say(U, "/history")
    bot.say(U, "/status")
    assert not any(re.search(r"\b(cancelled|completed|failed|expired|queued|pending)\b", t) for t in bot.texts(U))


# ══ G. found by testing against the real site ══════════════════════════════════════════════════════════════

def test_G1_spy_x_family_is_found_although_the_site_writes_a_multiplication_sign(bot):
    bot.say(U, "spy x family")
    assert bot.labels(U)[0].startswith("🎬 SPY×FAMILY") and "autre titre" not in bot.labels(U)[0]


def test_G2_optional_words_do_not_hide_a_title(bot):
    bot.say(U, "my hero academia")
    assert bot.labels(U) and any("Hero Academia" in x for x in bot.labels(U))


def test_G3_a_number_that_is_part_of_the_title_stays_in_the_title(bot):
    bot.say(U, "mob psycho 100")
    assert bot.request(U) is None and bot.labels(U)[0].startswith("🎬 Mob Psycho 100")


def test_G4_a_number_that_is_an_episode_stays_an_episode(bot):
    go(bot, "bestiale 2")
    assert bot.request(U)["episode_number"] == 2


def test_G5_the_site_cap_is_announced(bot):
    bot.say(U, "a")                                    # the site answers with its maximum for a one-letter query
    assert "précisez le titre" in bot.last(U)[1] or bot.world.searches() == 0
