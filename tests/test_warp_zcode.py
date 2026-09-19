#!/usr/bin/env python3
"""Export/import of ZCode sessions for one workspace directory."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import warp_zcode  # noqa: E402

SCHEMA = """
CREATE TABLE session (
  id text primary key,
  project_id text not null,
  workspace_id text,
  parent_id text,
  slug text not null,
  directory text not null,
  path text,
  title text not null,
  version text not null,
  share_url text,
  summary_additions integer,
  summary_deletions integer,
  summary_files integer,
  summary_diffs text,
  revert text,
  permission text,
  time_created integer not null,
  time_updated integer not null,
  time_compacting integer,
  time_archived integer,
  task_type text not null default 'interactive',
  title_source text not null default 'first_input',
  title_message_id text,
  time_title_updated integer,
  trace_id text
);
CREATE TABLE message (
  id text primary key,
  session_id text not null,
  time_created integer not null,
  time_updated integer not null,
  data text not null,
  sequence integer
);
CREATE TABLE part (
  id text primary key,
  message_id text not null,
  session_id text not null,
  time_created integer not null,
  time_updated integer not null,
  data text not null,
  sequence integer
);
CREATE TABLE todo (
  session_id text not null,
  content text not null,
  status text not null,
  priority text not null,
  position integer not null,
  time_created integer not null,
  time_updated integer not null,
  primary key(session_id, position)
);
CREATE TABLE session_entry (
  id text primary key,
  session_id text not null,
  type text not null,
  time_created integer not null,
  time_updated integer not null,
  data text not null
);
CREATE TABLE session_input (
  id text primary key,
  session_id text not null,
  kind text not null,
  delivery text not null,
  payload text not null,
  admitted_sequence integer not null,
  promoted_sequence integer,
  promoted_message_id text,
  status text not null,
  status_reason text,
  time_created integer not null,
  time_updated integer not null
);
"""


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _session(conn: sqlite3.Connection, sid: str, directory: str) -> None:
    conn.execute(
        """INSERT INTO session (
             id, project_id, slug, directory, path, title, version,
             time_created, time_updated
           ) VALUES (?, ?, ?, ?, ?, ?, '1', 1, 2)""",
        (sid, "proj", "slug", directory, directory, sid),
    )
    conn.execute(
        """INSERT INTO message (id, session_id, time_created, time_updated, data, sequence)
           VALUES (?, ?, 1, 1, '{"role":"user"}', 0)""",
        (f"msg-{sid}", sid),
    )
    conn.execute(
        """INSERT INTO part (id, message_id, session_id, time_created, time_updated, data, sequence)
           VALUES (?, ?, ?, 1, 1, '{"text":"hello"}', 0)""",
        (f"part-{sid}", f"msg-{sid}", sid),
    )
    conn.execute(
        """INSERT INTO todo (session_id, content, status, priority, position, time_created, time_updated)
           VALUES (?, 'do it', 'pending', 'medium', 0, 1, 1)""",
        (sid,),
    )


class WarpZcodeTest(unittest.TestCase):
    def test_export_only_matching_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.db"
            bundle = Path(tmp) / "bundle.db"
            dest = Path(tmp) / "dest.db"
            keep = "/home/person/Projects/keep"
            drop = "/home/person/Projects/drop"

            conn = _open(src)
            _session(conn, "sess-keep", keep)
            _session(conn, "sess-drop", drop)
            conn.commit()
            conn.close()

            summary = warp_zcode.export_sessions(src, keep, bundle)
            self.assertEqual(summary["sessions"], 1)
            self.assertEqual(summary["messages"], 1)
            self.assertEqual(summary["parts"], 1)

            dconn = _open(dest)
            dconn.close()
            imported = warp_zcode.import_sessions(bundle, dest)
            self.assertEqual(imported["sessions"], 1)

            check = sqlite3.connect(dest)
            ids = [r[0] for r in check.execute("SELECT id FROM session")]
            self.assertEqual(ids, ["sess-keep"])
            self.assertEqual(check.execute("SELECT count(*) FROM message").fetchone()[0], 1)
            self.assertEqual(check.execute("SELECT count(*) FROM part").fetchone()[0], 1)
            self.assertEqual(check.execute("SELECT count(*) FROM todo").fetchone()[0], 1)
            check.close()

    def test_import_as_new_never_overwrites_existing_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.db"
            bundle = Path(tmp) / "bundle.db"
            dest = Path(tmp) / "dest.db"
            path = "/ws"

            s = _open(src)
            _session(s, "sess-a", path)
            s.commit()
            s.close()

            d = _open(dest)
            _session(d, "sess-a", path)
            # local copy has newer data that MUST survive the import
            d.execute("UPDATE message SET data='{\"local\":true}' WHERE id='msg-sess-a'")
            d.commit()
            d.close()

            warp_zcode.export_sessions(src, path, bundle)
            summary = warp_zcode.import_sessions(bundle, dest, as_new=True)

            check = sqlite3.connect(dest)
            ids = sorted(r[0] for r in check.execute("SELECT id FROM session"))
            # original untouched + one fresh replay session
            self.assertEqual(ids[0], "sess-a")
            self.assertEqual(len(ids), 2)
            self.assertTrue(all(i.startswith("sess-a") for i in ids))
            self.assertIn("replay", ids[1])
            data = check.execute(
                "SELECT data FROM message WHERE id='msg-sess-a'"
            ).fetchone()[0]
            self.assertIn('"local":true', data)  # not overwritten
            imported_msgs = check.execute(
                "SELECT count(*) FROM message WHERE id LIKE 'msg-sess-a-r%'"
            ).fetchone()[0]
            self.assertEqual(imported_msgs, 1)  # arrived under a new id
            check.close()
            self.assertEqual(summary["sessions"], 1)

    def test_import_replaces_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.db"
            bundle = Path(tmp) / "bundle.db"
            dest = Path(tmp) / "dest.db"
            path = "/ws"

            s = _open(src)
            _session(s, "sess-a", path)
            s.execute("UPDATE message SET data='{\"n\":2}' WHERE id='msg-sess-a'")
            s.commit()
            s.close()

            d = _open(dest)
            _session(d, "sess-a", path)
            d.execute("UPDATE message SET data='{\"n\":1}' WHERE id='msg-sess-a'")
            d.commit()
            d.close()

            warp_zcode.export_sessions(src, path, bundle)
            warp_zcode.import_sessions(bundle, dest)
            check = sqlite3.connect(dest)
            data = check.execute("SELECT data FROM message WHERE id='msg-sess-a'").fetchone()[0]
            self.assertIn('"n":2', data)
            check.close()


if __name__ == "__main__":
    unittest.main()
