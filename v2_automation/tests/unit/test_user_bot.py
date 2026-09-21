"""User bot: parser, membership gate, the four-step flow (series -> season -> version -> episode, every step SHOWN), one active
request, history, notifications.  Real router, real SQLite, real parsers; the site is the recorded real one (bot_harness.py)."""
import pytest

from bot_harness import Bot
from v2_automation.parser import parse_query
from v2_automation.search import SourceSearch, base_title, clean_name, group, season_of_title, version_of
from v2support import BASE, cfg

U = 1001


@pytest.fixture()
def bot(tmp_path):
    return Bot(tmp_path, required=("@chan_a", "@chan_b")).join(U)


def go(bot, text, *, uid=U, s=0, n=0, v=None):
    """Say `text`, then answer each question the bot shows with the given choice (what a person does with the buttons)."""
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


# -- parser ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["one piece 1150", "One Piece E1150", "One Piece Episode 1150", "one piece ep 1150",
                                  "ONE PIECE  épisode 1150", "one piece e.1150"])
def test_all_formulations_normalise_to_the_same_query(text):
    assert parse_query(text).key() == parse_query("one piece 1150").key() == ("one piece", None, 1150, False, None, False)


@pytest.mark.parametrize("text,expect", [
    ("bleach saison 2 episode 5 vostfr", ("bleach", 2, 5, False, "VOSTFR", False)),
    ("Bleach S01E05", ("bleach", 1, 5, False, None, False)),
    ("Bleach Saison 1", ("bleach", 1, None, False, None, True)),
    ("naruto dernier épisode", ("naruto", None, None, True, None, False)),
    ("Re:Zero saison 4 ep 17 VF", ("re:zero", 4, 17, False, "VF", False)),
])
def test_parser_extracts_season_episode_version(text, expect):
    assert parse_query(text).key() == expect


def test_dernier_inside_a_title_is_not_the_last_episode():
    q = parse_query("Avatar, Le Dernier Maître De L’air")
    assert q.latest is False and "dernier" in q.title and q.title.startswith("avatar le dernier maitre")
    assert parse_query("naruto dernier").latest is True and parse_query("naruto dernier épisode").latest is True


def test_title_ending_with_a_number_keeps_a_fallback():
    q = parse_query("Mob Psycho 100")
    assert q.episode == 100 and q.title == "mob psycho" and q.full_title == "mob psycho 100"


def test_names_seasons_and_versions_from_titles():
    assert season_of_title("Wakfu S2") == 2 and season_of_title("Re:Zero Saison 4") == 4 and season_of_title("Overlord 2nd Season") == 2
    assert season_of_title("Kimetsu no Yaiba 2") is None and season_of_title("Tomb Raider King (VF)") is None       # never guessed
    assert clean_name("Wakfu S2") == "Wakfu" and clean_name("Carmen Sandiego S2 (VF)") == "Carmen Sandiego"
    assert clean_name("Wakfu (Saison 2)") == "Wakfu" and base_title("Bleach Kai (VF)") == base_title("Bleach Kai")
    assert version_of("Bleach Kai (VF)", "https://x/anime/bleach-kai-vf/") == "VF"


# -- membership -------------------------------------------------------------------------------

def test_non_member_gets_join_buttons_from_the_config_not_from_code(tmp_path):
    b = Bot(tmp_path, required=("@chan_a", "@chan_b"), auto_check=False)
    b.say(U, "one piece 1150")
    _, text, kb = b.last(U)
    assert "rejoignez" in text and [x.url for row in kb for x in row if x.url] == ["https://t.me/chan_a", "https://t.me/chan_b"]
    assert any(x.data == "chk" for row in kb for x in row)
    assert b.n("SELECT COUNT(*) FROM requests") == 0 and b.world.posts == [] and b.world.calls == []      # nothing touched the site


def test_member_of_any_required_channel_is_served(tmp_path):
    b = Bot(tmp_path, required=("@chan_a", "@chan_b"))
    b.members.member.add(U)                            # the fake answers "member" for every channel of the list
    b.say(U, "bestiale")
    assert b.labels(U)[0].startswith("🎬 Bestiale")


def test_user_who_leaves_is_refused_next_time_and_can_come_back(bot):
    go(bot, "bestiale 3")
    assert bot.request(U)["episode_number"] == 3
    bot.tap(U, f"xr:{bot.request(U)['id']}")
    bot.leave(U)
    bot.say(U, "wakfu")
    assert "rejoignez" in bot.last(U)[1]
    assert bot.n("SELECT access_status FROM users WHERE telegram_id=?", U) == "not_member"
    bot.join(U)
    bot.tap(U, "chk")
    assert "Accès confirmé" in bot.last(U)[1] and bot.n("SELECT access_status FROM users WHERE telegram_id=?", U) == "granted"


def test_membership_that_cannot_be_checked_is_never_a_yes(tmp_path):
    b = Bot(tmp_path, auto_check=False)
    b.router.members._status = lambda c, u: (_ for _ in ()).throw(RuntimeError("Bad Request: chat not found"))
    b.say(U, "bleach")
    assert "vérifier votre accès" in b.last(U)[1]
    assert b.n("SELECT access_status FROM users WHERE telegram_id=?", U) == "check_failed"


def test_no_required_channel_means_open_access(tmp_path):
    b = Bot(tmp_path, required=())
    b.say(U, "bestiale")
    assert b.labels(U)[0].startswith("🎬 Bestiale")


# -- the flow: every step is shown ---------------------------------------------------------------

def test_flow_shows_series_then_season_then_version_then_episode(bot):
    bot.say(U, "bestiale")
    assert bot.labels(U) == ["🎬 Bestiale 🇫🇷"]                                     # the list is shown even for ONE result
    bot.tap(U, "s:0")
    assert "Quelle saison" in bot.last(U)[1] and bot.labels(U) == ["📅 Saison unique · 7 ép. 🇫🇷"]   # ...and the season step too
    bot.tap(U, "n:0")
    assert "Quelle version" in bot.last(U)[1] and bot.labels(U) == ["🇫🇷 VF"] and "seule version disponible" in bot.last(U)[1]
    assert bot.request(U) is None                                                   # nothing was created yet
    bot.tap(U, "v:VF")
    assert bot.labels(U) == ["📺 Quel épisode ?"] and "Bestiale › 🇫🇷 VF" in bot.last(U)[1]
    bot.say(U, "3")
    assert (bot.request(U)["episode_number"], bot.request(U)["version"]) == (3, "VF")


def test_the_version_is_the_engine_that_answered_not_the_title(bot):
    """Wakfu S2 and Avatar have no (VF) in their title: they exist only in the VF engine."""
    go(bot, "wakfu", n=1)
    assert "🇫🇷 VF" in bot.last(U)[1] and "VOSTFR" not in bot.last(U)[1]
    bot.say(U, "5")
    r = bot.request(U)
    assert (r["title"], r["version"], r["episode_number"]) == ("Wakfu", "VF", 5)


def test_wakfu_lists_its_four_seasons_with_their_episode_counts(bot):
    bot.say(U, "wakfu")
    assert bot.labels(U)[0] == "🎬 Wakfu 🇫🇷 · 4 saisons"
    bot.tap(U, "s:0")
    assert bot.labels(U) == ["📅 Saison 1 · 26 ép. 🇫🇷", "📅 Saison 2 · 26 ép. 🇫🇷", "📅 Saison 3 · 12 ép. 🇫🇷", "📅 Saison 4 · 12 ép. 🇫🇷"]


def test_series_names_are_clean(bot):
    bot.say(U, "carmen sandiego")
    assert bot.labels(U) == ["🎬 Carmen Sandiego 🇫🇷 · 3 saisons"]                  # not "Carmen Sandiego S2"
    bot.tap(U, "s:0")
    assert [x.split(" · ")[0] for x in bot.labels(U)] == ["📅 Saison 1", "📅 Saison 2", "📅 Saison 3"]


def test_title_with_the_word_dernier_and_typographic_apostrophe_is_found(bot):
    bot.say(U, "Avatar, Le Dernier Maître De L’air")
    assert bot.labels(U)[0].startswith("🎬 Avatar, Le Dernier Maître De L") and "🇫🇷" in bot.labels(U)[0]
    assert "Nouvelle recherche" not in bot.last(U)[1] or bot.step(U) == "choose_series"


def test_bestiale_is_found_although_only_the_vf_engine_knows_it(bot):
    bot.say(U, "Bestiale")
    assert bot.labels(U) == ["🎬 Bestiale 🇫🇷"]
    assert [p["asid"] for p in bot.world.posts] in ([2, 3], [3, 2]) or {str(p["asid"]) for p in bot.world.posts} == {"2", "3"}   # both engines asked


def test_both_versions_are_offered_when_both_exist(bot):
    bot.say(U, "kimetsu no yaiba")
    assert bot.labels(U)[0] == "🎬 Kimetsu no Yaiba 🇫🇷🇯🇵"
    bot.tap(U, "s:0")
    bot.tap(U, "n:0")
    assert bot.labels(U) == ["🇫🇷 VF", "🇯🇵 VOSTFR"] and "Quelle version" in bot.last(U)[1]
    bot.tap(U, "v:VOSTFR")
    bot.say(U, "5")
    assert bot.request(U)["version"] == "VOSTFR"


def test_a_trailing_number_is_a_separate_entry_never_a_guessed_season(bot):
    bot.say(U, "kimetsu no yaiba")
    names = [x.split(" 🇫")[0].split(" 🇯")[0] for x in bot.labels(U)]
    assert "🎬 Kimetsu no Yaiba" in names and "🎬 Kimetsu no Yaiba 2" in names           # two entries, not one series with seasons


def test_prefilled_choices_are_announced_not_silent(bot):
    bot.say(U, "wakfu saison 2 vf")
    bot.tap(U, "s:0")
    assert "Saison 2 retenue d'après votre message" in bot.last(U)[1] and "Version VF retenue d'après votre message" in bot.last(U)[1]
    assert "saison complète (26 épisodes)" in bot.last(U)[1] and bot.request(U) is None       # asked, not created
    bot.tap(U, "all")
    assert bot.request(U)["kind"] == "season" and bot.n("SELECT COUNT(*) FROM episodes") == 26


def test_season_that_does_not_exist_is_not_invented(bot):
    bot.say(U, "wakfu saison 7")
    bot.tap(U, "s:0")
    assert "La saison 7 n'existe pas" in bot.last(U)[1] and "Saison 4" in bot.last(U)[1] and bot.request(U) is None


def test_version_that_does_not_exist_is_said(bot):
    bot.say(U, "wakfu vostfr")
    bot.tap(U, "s:0")
    bot.tap(U, "n:0")
    assert "La version VOSTFR n'existe pas" in bot.last(U)[1] and bot.labels(U) == ["🇫🇷 VF"]


def test_other_title_matches_are_marked(bot):
    bot.say(U, "avatar")
    labels = bot.labels(U)
    assert labels[0].startswith("🎬 Avatar, Le Dernier") and "autre titre" not in labels[0]
    assert any("Quanzhi Gaoshou" in x and "autre titre" in x for x in labels)


def test_alternative_titles_are_found_like_the_site_does(bot):
    bot.say(U, "attack on titan")
    assert "autre titre" in bot.last(U)[1] and bot.labels(U)[0].startswith("🎬 Shingeki no Kyojin")


def test_nothing_found_says_both_engines_were_asked(bot):
    bot.say(U, "zzzzqqqq")
    assert "Aucun anime trouvé" in bot.last(U)[1] and "ni en VF ni en VOSTFR" in bot.last(U)[1]


def test_source_down_is_reported_not_hidden(bot):
    bot.router.search.asp._post = lambda url, data: (_ for _ in ()).throw(RuntimeError("HTTP 503 timed out"))
    bot.say(U, "one piece")
    assert "La recherche du site ne répond pas" in bot.last(U)[1] and bot.request(U) is None


# -- episodes, requests, cancel, history -------------------------------------------------------------

def test_episode_picker_is_paginated_and_latest_is_the_highest_listed(bot):
    go(bot, "wakfu", n=0)
    bot.tap(U, "ep")
    bot.tap(U, "list:0")
    assert "page 1/2" in bot.last(U)[1]
    bot.tap(U, "list:1")
    assert "E26" in [x.text for x in bot.buttons(U)]
    bot.tap(U, "last")
    assert bot.request(U)["episode_number"] == 26


def test_whole_season_is_one_request_with_one_job_per_listed_episode(bot):
    go(bot, "wakfu saison 3 vf")
    bot.tap(U, "all")
    assert bot.n("SELECT COUNT(*) FROM requests") == 1 and bot.n("SELECT COUNT(*) FROM episodes") == 12


def test_second_request_is_refused_with_a_cancel_button_then_possible(bot):
    go(bot, "bestiale 2")
    go(bot, "wakfu 2")
    assert "déjà une demande en cours" in bot.last(U)[1]
    bot.tap(U, bot.last(U)[2][0][0].data)
    assert "annulée" in bot.last(U)[1]
    bot.tap(U, f"xr:{bot.request(U)['id']}")                                            # a second press
    assert bot.last(U)[1] == "ℹ️ Cette demande est déjà annulée."                        # French, no raw "cancelled"


def test_history_comes_from_the_database(bot):
    go(bot, "bestiale 1")
    bot.say(U, "/cancel")
    go(bot, "bestiale 2")
    bot.say(U, "/history")
    text = bot.last(U)[1]
    assert text.startswith("📚 Mes demandes") and "🚫 Bestiale E1 — VF" in text and "⏳ Bestiale E2 — VF" in text
    bot.conn.execute("UPDATE requests SET state='COMPLETED' WHERE episode_number=2")
    bot.conn.commit()
    bot.say(U, "/history")
    assert "✅ Bestiale E2 — VF" in bot.last(U)[1]


def test_expiry_is_notified_once(bot):
    go(bot, "bestiale 9")                                                             # 7 listed: E9 = just beyond -> waits
    assert bot.request(U)["state"] == "WAITING_FOR_MEDIA"
    bot.router.notify_changes()
    bot.clock.advance(21 * 60)
    bot.mgr.tick()
    assert bot.router.notify_changes() == 1 and "pas apparu à temps" in bot.last(U)[1]
    assert bot.router.notify_changes() == 0 and bot.request(U)["state"] == "EXPIRED"


def test_conversation_survives_a_restart(bot):
    bot.say(U, "wakfu")
    bot.tap(U, "s:0")
    from v2_automation.user_bot import Incoming, UserBotRouter
    fresh = UserBotRouter(bot.conn, bot.cfg, bot.out, bot.mgr, bot.router.search, bot.router.members,
                          identify=bot._identify, now=bot.clock)                      # new process, same DB
    before = len(bot.out.sent)
    fresh.handle(Incoming(U, U, "u", callback="n:1", callback_id="c"))
    assert "Quelle version" in bot.out.sent[before][1] and "Saison 2" in bot.out.sent[before][1]
