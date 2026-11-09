"""Renderer query replies are metadata, never executable native assignments."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import worker_input


class WorkerTerminalReplyTest(unittest.TestCase):
    def test_every_reply_chunk_boundary_preserves_the_exact_framed_task(self):
        replies = ('\x1b[?1;2c', '\x1b[>0;276;0c', '\x1b[12;34R', '\x1b[?1u',
                   '\x1b]10;rgb:ffff/ffff/ffff\x07', '\x1b]11;rgb:0000/0000/0000\x1b\\')
        for reply in replies:
            packet = reply + worker_input.START + 'Exact native fixture' + worker_input.END + '\r'
            for split in range(len(packet) + 1):
                with self.subTest(reply=repr(reply), split=split):
                    state, first = worker_input.feed(worker_input.initial(), packet[:split])
                    state, second = worker_input.feed(state, packet[split:])
                    self.assertEqual((*first, *second), ('Exact native fixture',))
                    worker_input.finish(state)

    def test_metadata_cannot_smuggle_control_text_into_a_task_or_hide_unframed_input(self):
        for text in ('\x1b[A', 'arbitrary command', '\x1b[?1;2c arbitrary command',
                     worker_input.START + '\x1b[?1;2c' + worker_input.END + '\r'):
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                worker_input.feed(worker_input.initial(), text)


if __name__ == '__main__':
    unittest.main()
