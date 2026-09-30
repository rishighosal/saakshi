from field.app.policy import ItemFacts, PolicySettings, decide


def test_private_never_leaves_device():
    d = decide(ItemFacts(private=True, note="flood damage"))
    assert d.action == "local_only"
    assert not d.leaves_device


def test_burst_duplicate_of_synced_item_is_skipped():
    d = decide(ItemFacts(duplicate_of="abc", duplicate_similarity=0.97, duplicate_synced=True, duplicate_gap_s=20))
    assert d.action == "skip_duplicate"
    assert "97%" in d.reasons[0]


def test_same_spot_hours_later_is_a_new_observation():
    d = decide(ItemFacts(duplicate_of="abc", duplicate_similarity=0.98, duplicate_synced=True, duplicate_gap_s=6 * 3600, novelty=0.02))
    assert d.action == "sync"
    assert any("before/after" in r for r in d.reasons)


def test_similar_photo_with_a_new_report_still_syncs():
    d = decide(ItemFacts(duplicate_of="abc", duplicate_similarity=0.99, duplicate_synced=True, duplicate_gap_s=30,
                         new_info=True, status_claim="damaged"))
    assert d.action == "sync"


def test_duplicate_of_unsynced_item_still_syncs():
    d = decide(ItemFacts(duplicate_of="abc", duplicate_similarity=0.97, duplicate_synced=False, novelty=0.03))
    assert d.action == "sync"


def test_urgent_note_and_damage_claim_raise_priority():
    routine = decide(ItemFacts(note="saplings watered", novelty=0.2))
    urgent = decide(ItemFacts(note="embankment breach after the storm, flood risk", status_claim="damaged", novelty=0.2))
    assert urgent.priority > routine.priority
    assert urgent.priority >= 90
    assert any("Urgent" in r for r in urgent.reasons)


def test_faces_without_consent_are_blurred_by_default():
    d = decide(ItemFacts(faces=2))
    assert d.action == "sync_blurred"
    assert "2 faces" in d.reasons[0]


def test_faces_without_consent_held_in_hold_mode():
    d = decide(ItemFacts(faces=1), PolicySettings(privacy_mode="hold"))
    assert d.action == "hold"
    assert not d.leaves_device


def test_faces_with_consent_upload_as_is():
    d = decide(ItemFacts(faces=3, consent=True))
    assert d.action == "sync"


def test_metered_link_sends_compressed_copy_unless_urgent():
    s = PolicySettings(metered=True)
    assert decide(ItemFacts(note="routine", novelty=0.1), s).tier == "compressed"
    assert decide(ItemFacts(note="flood near the school", status_claim="damaged"), s).tier == "full"


def test_every_decision_explains_itself():
    for facts in (ItemFacts(), ItemFacts(faces=1), ItemFacts(has_gps=False), ItemFacts(note="urgent")):
        assert decide(facts).reasons


def test_byte_counts_read_like_the_device_ui():
    from field.app.sync import human_bytes

    assert [human_bytes(v) for v in (0, 999, 1000, 3174, 433664, 999700, 7010816, 2.04e9, None)] == \
        ["0 B", "999 B", "1.0 KB", "3.2 KB", "434 KB", "1.0 MB", "7.0 MB", "2.0 GB", "0 B"]
