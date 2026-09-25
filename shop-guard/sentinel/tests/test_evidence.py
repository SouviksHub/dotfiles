import json
import os

from sentinel.evidence import EvidenceLocker


def test_chain_verifies_and_detects_tampering(tmp_path):
    locker = EvidenceLocker(tmp_path)
    a = locker.store("e1", {"clip.mp4": b"clip-one", "snapshot.jpg": b"snap"}, {"score": 7})
    b = locker.store("e2", {"clip.mp4": b"clip-two"}, {"score": 4})
    assert b["prev"] == a["hash"]
    assert locker.verify() == (True, [])

    clip = next(tmp_path.glob("*/e1/clip.mp4"))
    assert not os.access(clip, os.W_OK) or os.geteuid() == 0  # written read-only
    os.chmod(clip, 0o644)
    clip.write_bytes(b"edited")
    ok, problems = locker.verify()
    assert not ok and any("e1/clip.mp4 was modified" in p for p in problems)


def test_edited_ledger_entry_detected(tmp_path):
    locker = EvidenceLocker(tmp_path)
    locker.store("e1", {"clip.mp4": b"x"}, {"score": 9})
    lines = locker.ledger.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["meta"]["score"] = 1
    locker.ledger.write_text(json.dumps(entry) + "\n")
    ok, problems = locker.verify()
    assert not ok and "entry 0: entry was modified" in problems


def test_deleted_entry_breaks_chain(tmp_path):
    locker = EvidenceLocker(tmp_path)
    for i in range(3):
        locker.store(f"e{i}", {"clip.mp4": bytes([i])}, {})
    lines = locker.ledger.read_text().splitlines()
    locker.ledger.write_text("\n".join([lines[0], lines[2]]) + "\n")
    ok, problems = locker.verify()
    assert not ok and any("chain broken" in p for p in problems)
