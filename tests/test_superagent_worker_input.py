"""Native worker input accepts complete framed preambles, never partial shell text."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import worker_input

PROMPT = ('You are working inside Orca, a multi-agent IDE. You are a dispatched worker.\n'
          'Your task ID is: task_1\n\n=== CLI COMMANDS ===\n'
          'orca orchestration send --from term_1 --type worker_done '
          '--task-id task_1 --dispatch-id ctx_1 --outcome succeeded\n'
          '\n=== TASK ===\nRead approved fixture only.\n')


class WorkerInputTest(unittest.TestCase):
    def test_one_chunk_frame_and_submit_produce_exact_native_prompt(self):
        state = worker_input.initial()
        next_state, prompts = worker_input.feed(state, '\x1b[200~' + PROMPT + '\x1b[201~\r')
        self.assertEqual(prompts, (PROMPT,))
        self.assertEqual(next_state, worker_input.initial())
        self.assertEqual(worker_input.assignment(PROMPT), ('task_1', 'ctx_1', 'Read approved fixture only.\n'))

    def test_every_chunk_boundary_preserves_framing_without_mutating_input(self):
        text = '\x1b[200~' + PROMPT + '\x1b[201~\r'
        for index in range(len(text) + 1):
            state = worker_input.initial()
            before = deepcopy(state)
            second, first_prompts = worker_input.feed(state, text[:index])
            final, second_prompts = worker_input.feed(second, text[index:])
            self.assertEqual(state, before)
            self.assertEqual(first_prompts + second_prompts, (PROMPT,))
            self.assertEqual(final, worker_input.initial())

    def test_multiple_framed_prompts_stay_separate_and_wait_for_submit(self):
        state, prompts = worker_input.feed(worker_input.initial(), '\x1b[200~' + PROMPT + '\x1b[201~')
        self.assertEqual(prompts, ())
        final, prompts = worker_input.feed(state, '\r\x1b[200~' + PROMPT + '\x1b[201~\r')
        self.assertEqual(prompts, (PROMPT, PROMPT))
        self.assertEqual(final, worker_input.initial())

    def test_unframed_nested_overlarge_and_control_input_fail_closed(self):
        for text in ('echo unsafe\r', '\x1b[200~one\x1b[200~two',
                     '\x1b[200~' + 'x' * (worker_input.MAX_BYTES + 1),
                     '\x1b[200~null\x00\x1b[201~\r'):
            with self.subTest(text=text[:25]), self.assertRaises(ValueError):
                worker_input.feed(worker_input.initial(), text)

    def test_partial_frame_eof_is_not_a_dispatch(self):
        for text in ('\x1b[20', '\x1b[200~' + PROMPT, '\x1b[200~' + PROMPT + '\x1b[201~'):
            state, prompts = worker_input.feed(worker_input.initial(), text)
            self.assertEqual(prompts, ())
            with self.assertRaises(ValueError):
                worker_input.finish(state)
        self.assertIsNone(worker_input.finish(worker_input.initial()))

    def test_missing_conflicting_or_quoted_lifecycle_ids_are_not_native_authority(self):
        for text in ('Read something.', PROMPT.replace('--dispatch-id ctx_1', '--dispatch-id ctx_old --dispatch-id ctx_1'),
                     PROMPT.replace('--task-id task_1', '--task-id task_other'),
                     PROMPT.replace('Your task ID is: task_1', 'Your task ID is: "task_1"'),
                     PROMPT.replace('\n=== TASK ===\n', '\n=== NO TASK ===\n')):
            with self.subTest(text=text[:35]), self.assertRaises(ValueError):
                worker_input.assignment(text)


if __name__ == '__main__':
    unittest.main()
