"""Covers MASTER_PLAN.md section 25's multi-anime ordering example directly:

Anime A: E120 -> download -> publish -> E121 -> download -> publish
Anime B: E050 -> download -> publish  (interleaved with A's processing)
"""
import pytest

from source_audit.detection.ordering import OutOfOrderError, PerAnimeQueue

ANIME_A = "postid:1001"
ANIME_B = "postid:2002"


def test_plan_example_anime_a_and_b_interleaved():
    q = PerAnimeQueue()
    # Arrival order interleaved, exactly as concurrent discovery would produce it.
    q.add(ANIME_A, "A-E120")
    q.add(ANIME_B, "B-E050")
    q.add(ANIME_A, "A-E121")

    # B's single episode can be processed at any time, independent of A.
    assert q.can_process("B-E050")
    q.process("B-E050")

    # A must be processed in order: E120 before E121.
    assert q.can_process("A-E120")
    assert not q.can_process("A-E121")
    q.process("A-E120")
    assert q.can_process("A-E121")
    q.process("A-E121")

    assert q.processed_order() == ["B-E050", "A-E120", "A-E121"]


def test_processing_out_of_order_within_same_anime_raises():
    q = PerAnimeQueue()
    q.add(ANIME_A, "A-E120")
    q.add(ANIME_A, "A-E121")

    with pytest.raises(OutOfOrderError):
        q.process("A-E121")  # E120 hasn't been processed yet


def test_different_anime_never_block_each_other():
    q = PerAnimeQueue()
    q.add(ANIME_A, "A-E1")
    q.add(ANIME_B, "B-E1")
    q.add(ANIME_A, "A-E2")
    q.add(ANIME_B, "B-E2")

    # Process B fully before touching A at all -- must not be blocked by A's queue.
    q.process("B-E1")
    q.process("B-E2")
    assert q.pending_for(ANIME_A) == ["A-E1", "A-E2"]  # untouched, still in order

    q.process("A-E1")
    q.process("A-E2")
    assert q.processed_order() == ["B-E1", "B-E2", "A-E1", "A-E2"]


def test_duplicate_add_is_a_no_op():
    q = PerAnimeQueue()
    q.add(ANIME_A, "A-E1")
    q.add(ANIME_A, "A-E1")  # re-discovered, e.g. still on the homepage feed
    assert q.pending_for(ANIME_A) == ["A-E1"]


def test_can_process_false_for_unknown_episode():
    q = PerAnimeQueue()
    assert q.can_process("never-added") is False


def test_process_unknown_episode_raises():
    q = PerAnimeQueue()
    with pytest.raises(OutOfOrderError):
        q.process("never-added")


def test_three_anime_fully_interleaved_stay_independently_ordered():
    """Simulates 3 concurrent anime streams, arrival fully interleaved, then
    processed in an arbitrary-but-per-anime-valid order; asserts no anime's
    internal order was violated."""
    q = PerAnimeQueue()
    anime_c = "postid:3003"

    arrival = [
        (ANIME_A, "A-E1"), (ANIME_B, "B-E1"), (anime_c, "C-E1"),
        (ANIME_A, "A-E2"), (anime_c, "C-E2"), (ANIME_B, "B-E2"),
        (anime_c, "C-E3"), (ANIME_A, "A-E3"),
    ]
    for anime_key, ep_key in arrival:
        q.add(anime_key, ep_key)

    # Process in an order that interleaves anime but respects each anime's FIFO.
    process_order = ["C-E1", "A-E1", "B-E1", "C-E2", "A-E2", "C-E3", "B-E2", "A-E3"]
    for ep_key in process_order:
        q.process(ep_key)  # would raise OutOfOrderError if this were invalid

    assert q.processed_order() == process_order
