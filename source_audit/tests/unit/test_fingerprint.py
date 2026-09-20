from pathlib import Path

from source_audit.detection.fingerprint import (
    ANIME_PAGE_SELECTORS,
    EPISODE_PAGE_SELECTORS,
    HOMEPAGE_SELECTORS,
    compute_fingerprint,
    find_regressed_selectors,
    fingerprint_hash,
    is_structure_changed,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_homepage_fixture_matches_all_critical_selectors():
    html = (FIXTURES / "homepage_sample.html").read_text(encoding="utf-8")
    fp = compute_fingerprint(html, HOMEPAGE_SELECTORS)
    assert all(fp.values()), fp


def test_anime_fixture_matches_all_critical_selectors():
    html = (FIXTURES / "anime_page_sample.html").read_text(encoding="utf-8")
    fp = compute_fingerprint(html, ANIME_PAGE_SELECTORS)
    assert all(fp.values()), fp


def test_episode_fixture_matches_all_critical_selectors():
    html = (FIXTURES / "episode_page_sample.html").read_text(encoding="utf-8")
    fp = compute_fingerprint(html, EPISODE_PAGE_SELECTORS)
    assert all(fp.values()), fp


def test_identical_html_produces_identical_hash():
    html = (FIXTURES / "homepage_sample.html").read_text(encoding="utf-8")
    fp1 = compute_fingerprint(html, HOMEPAGE_SELECTORS)
    fp2 = compute_fingerprint(html, HOMEPAGE_SELECTORS)
    assert fingerprint_hash(fp1) == fingerprint_hash(fp2)


def test_mutated_html_is_detected_as_regressed():
    html = (FIXTURES / "homepage_sample.html").read_text(encoding="utf-8")
    baseline = compute_fingerprint(html, HOMEPAGE_SELECTORS)

    # Simulate a site redesign: the chapter-item class is renamed.
    mutated_html = html.replace('class="chapter-item"', 'class="ep-row"')
    current = compute_fingerprint(mutated_html, HOMEPAGE_SELECTORS)

    regressed = find_regressed_selectors(baseline, current)
    assert regressed == ["chapter_item"]
    assert is_structure_changed(baseline, current) is True


def test_unrelated_markup_addition_is_not_flagged():
    html = (FIXTURES / "homepage_sample.html").read_text(encoding="utf-8")
    baseline = compute_fingerprint(html, HOMEPAGE_SELECTORS)

    # Adding new, unrelated markup must not trigger a false "structure changed".
    augmented_html = html.replace("</body>", "<div class='new-widget'>ad</div></body>")
    current = compute_fingerprint(augmented_html, HOMEPAGE_SELECTORS)

    assert find_regressed_selectors(baseline, current) == []
    assert is_structure_changed(baseline, current) is False


def test_completely_empty_page_regresses_every_selector():
    baseline = compute_fingerprint(
        (FIXTURES / "homepage_sample.html").read_text(encoding="utf-8"), HOMEPAGE_SELECTORS
    )
    current = compute_fingerprint("<html><body></body></html>", HOMEPAGE_SELECTORS)

    assert set(find_regressed_selectors(baseline, current)) == set(HOMEPAGE_SELECTORS.keys())
    assert is_structure_changed(baseline, current) is True
