"""The site's search protocol (Ajax Search Pro) against REAL recorded answers, plus a LIVE contract test (V2_LIVE=1) that replays the
exact request the site's own search boxes send and checks the shape of the answer — it fails the day the site changes."""
import os
from pathlib import Path

import pytest

from bot_harness import FIX
from v2_automation.search import AspEngine, SourceSearch, parse_asp_response, slugify_query
from v2support import BASE, cfg

pytestmark = pytest.mark.skipif(not FIX.exists(), reason="recorded corpus missing (scripts/record_asp_corpus.py)")


def raw(version, query):
    return (FIX / f"{version}__{slugify_query(query)}.txt").read_text(encoding="utf-8")


def test_parse_real_answer_bestiale_only_the_vf_engine_knows_it():
    vf, vostfr = parse_asp_response(raw("VF", "bestiale"), "VF"), parse_asp_response(raw("VOSTFR", "bestiale"), "VOSTFR")
    assert [(h.title, h.url, h.version) for h in vf] == [("Bestiale (VF)", f"{BASE}/anime/bestiale-vf/", "VF")] and vostfr == []


def test_parse_real_answer_wakfu_pages_have_no_vf_tag_but_come_from_the_vf_engine():
    hits = parse_asp_response(raw("VF", "wakfu"), "VF")
    assert {h.title for h in hits} >= {"Wakfu S1", "Wakfu S2", "Wakfu S3", "Wakfu S4"} and all(h.version == "VF" for h in hits)
    assert parse_asp_response(raw("VOSTFR", "wakfu"), "VOSTFR") == []


def test_titles_are_unescaped_and_order_and_relevance_are_kept():
    hits = parse_asp_response(raw("VF", "avatar"), "VF")
    assert hits[0].title == "Avatar, Le Dernier Maître De L’air" and "&#" not in hits[0].title and hits[0].rank == 0


def test_html_is_the_fallback_when_the_json_block_is_missing():
    html_only = "___ASPSTART_HTML___" + raw("VF", "bestiale").split("___ASPEND_HTML___")[0].split("___ASPSTART_HTML___")[1] + "___ASPEND_HTML___"
    assert [h.title for h in parse_asp_response(html_only, "VF")] == ["Bestiale (VF)"]
    assert parse_asp_response("", "VF") == [] and parse_asp_response("garbage", "VF") == []


def test_the_payload_is_the_one_the_browser_sends():
    """Captured in the built-in browser typing "bestiale" in the VF box: action, aspp, asid, asp_inst_id, options."""
    eng = AspEngine(cfg(), get=lambda u: (FIX / "home.html").read_text(encoding="utf-8"), post=lambda u, d: "")
    vf = eng.engines()["VF"]
    p = eng.payload("bestiale", vf)
    assert p["action"] == "ajaxsearchpro_search" and p["aspp"] == "bestiale" and p["asid"] == 2 and p["asp_inst_id"] == "2_1"
    opts = dict(x.split("=") for x in p["options"].split("&"))
    assert opts["current_page_id"] == "15" and opts["qtranslate_lang"] == "0" and opts["aspf%5Bvf__1%5D"] == "vf"
    assert "asp_gen%5B%5D" in opts


def test_engines_are_told_apart_by_their_own_placeholder_not_by_position():
    eng = AspEngine(cfg(), get=lambda u: (FIX / "home.html").read_text(encoding="utf-8"), post=lambda u, d: "")
    e = eng.engines()
    assert (e["VF"]["asid"], e["VOSTFR"]["asid"]) == (2, 3)
    swapped = (FIX / "home.html").read_text(encoding="utf-8").replace("Rechercher en VF...", "@@").replace(
        "Rechercher en VOSTFR...", "Rechercher en VF...").replace("@@", "Rechercher en VOSTFR...")
    assert AspEngine(cfg(), get=lambda u: swapped, post=lambda u, d: "").engines()["VF"]["asid"] == 3      # follows the site


def test_a_failing_engine_raises_instead_of_answering_with_half_the_catalogue():
    def post(url, data):
        if data["asid"] == 3:
            raise RuntimeError("HTTP 503")
        return raw("VF", "wakfu")
    s = SourceSearch(cfg(), None, fetch=lambda u: (FIX / "home.html").read_text(encoding="utf-8"), post=post)
    with pytest.raises(RuntimeError):
        s.search("wakfu")


def test_the_cap_of_the_site_is_detected():
    s = SourceSearch(cfg(), None, fetch=lambda u: (FIX / "home.html").read_text(encoding="utf-8"),
                     post=lambda u, d: raw("VF" if d["asid"] == 2 else "VOSTFR", "a"))
    s.search("a")
    assert s.truncated is True


@pytest.mark.skipif(os.getenv("V2_LIVE") != "1", reason="live contract test: set V2_LIVE=1")
def test_LIVE_contract_the_site_still_answers_the_way_we_expect():
    s = SourceSearch(cfg(), None)                                     # real HTTP, same User-Agent as the watcher
    engines = s.asp.engines(refresh=True)
    assert set(engines) == {"VF", "VOSTFR"} and not engines["VF"].get("fallback") and not engines["VOSTFR"].get("fallback")
    res = s.asp.search("bestiale")
    assert [h.title for h in res["VF"]] == ["Bestiale (VF)"] and res["VOSTFR"] == []
    assert all(h.url.startswith("https://voir-anime.to/anime/") for hs in res.values() for h in hs)
