"""Run with: python -m unittest discover tests"""
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chatvault as cv

CLAUDE = [{
    "uuid": "c1", "name": "Godot help",
    "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-01T10:05:00Z",
    "chat_messages": [
        {"sender": "human", "text": "how do I make a tilemap in godot"},
        {"sender": "assistant", "text": "Use a TileMapLayer node."},
    ],
}]
CHATGPT = [{
    "id": "g1", "title": "Roblox cars", "create_time": 1760000000, "update_time": 1760000100,
    "current_node": "n2",
    "mapping": {
        "n0": {"parent": None, "message": None},
        "n1": {"parent": "n0", "message": {"author": {"role": "user"},
               "content": {"parts": ["drift physics in roblox?"]}, "create_time": 1760000000}},
        "n2": {"parent": "n1", "message": {"author": {"role": "assistant"},
               "content": {"parts": ["Lower friction on rear wheels."]}, "create_time": 1760000100}},
        "dead": {"parent": "n1", "message": {"author": {"role": "assistant"},
                 "content": {"parts": ["abandoned branch"]}, "create_time": 1760000050}},
    },
}]


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = cv.connect(Path(self.tmp.name) / "v.db")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def load(self, data):
        return cv.ingest(self.con, cv.detect_and_parse(data))

    def test_import_is_incremental(self):
        self.assertEqual(self.load(CLAUDE)["new"], 1)
        self.assertEqual(self.load(CHATGPT)["new"], 1)
        self.assertEqual(self.load(CLAUDE), {"new": 0, "updated": 0, "same": 1})
        newer = json.loads(json.dumps(CLAUDE))
        newer[0]["updated_at"] = "2026-11-02T00:00:00Z"
        newer[0]["chat_messages"].append({"sender": "human", "text": "and collisions?"})
        self.assertEqual(self.load(newer)["updated"], 1)
        # the older export must not roll the conversation back
        self.assertEqual(self.load(CLAUDE)["same"], 1)
        self.assertEqual(len(cv.search(self.con, "collisions")), 1)

    def test_chatgpt_keeps_only_visible_branch(self):
        self.load(CHATGPT)
        self.assertEqual(cv.search(self.con, "abandoned"), [])
        self.assertEqual(len(cv.search(self.con, "friction")), 1)

    def test_search_survives_odd_queries(self):
        self.load(CLAUDE)
        for q in ('c++ "unclosed', "tile-map AND (", "100%", "   "):
            cv.search(self.con, q)  # must not raise
        self.assertEqual(len(cv.search(self.con, "tilemap godot", force_like=True)), 1)
        self.assertEqual(cv.search(self.con, "100%", force_like=True), [])

    def test_archive_round_trip_and_user_export(self):
        self.load(CLAUDE)
        self.load(CHATGPT)
        archive = list(cv.export_records(self.con, "archive"))
        other = cv.connect(Path(self.tmp.name) / "other.db")
        self.assertEqual(cv.ingest(other, cv.detect_and_parse(archive))["new"], 2)
        self.assertEqual(len(cv.search(other, "tilemap")), 1)
        other.close()
        mine = [r["text"] for r in cv.export_records(self.con, "user", min_chars=25)]
        self.assertEqual(mine, ["how do I make a tilemap in godot"])

    def test_zip_import(self):
        z = Path(self.tmp.name) / "export.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
            f.writestr("conversations.json", json.dumps(CLAUDE))
            f.writestr("users.json", "[]")
        docs = cv.load_export(z)
        self.assertEqual(len(docs), 1)
        self.assertEqual(self.load(docs[0])["new"], 1)


if __name__ == "__main__":
    unittest.main()
