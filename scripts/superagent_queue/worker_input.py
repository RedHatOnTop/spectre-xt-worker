"""Decode bounded native bracketed-paste packets without executing terminal text."""
from dataclasses import dataclass
import re

from .model import identifier, MAX_JSON_BYTES
from . import terminal_reply

START = '\x1b[200~'
END = '\x1b[201~'
MAX_BYTES = MAX_JSON_BYTES
TASK = '\n=== TASK ===\n'
INTRO = 'You are working inside Orca, a multi-agent IDE. You are a dispatched worker.'


@dataclass(frozen=True)
class Input:
    pending: str = ''


def initial():
    return Input()


def content(value, partial=False):
    tail = next((len(END[:i]) for i in range(len(END) - 1, 0, -1)
                 if partial and value.endswith(END[:i])), 0)
    body = value[:-tail] if tail else value
    if len(body.encode()) > MAX_BYTES or any(ord(c) < 32 and c not in '\r\n\t' for c in body):
        raise ValueError('worker input contains control data or exceeds the packet limit')
    return body


def feed(state, chunk):
    if not isinstance(state, Input) or not isinstance(chunk, str):
        raise ValueError('worker input requires immutable state and decoded text')
    pending, prompts = state.pending + chunk, ()
    while pending:
        if pending[0] in '\r\n':
            pending = pending[1:]
            continue
        if START.startswith(pending):
            return Input(pending), prompts
        reply = terminal_reply.length(pending)
        if reply < 0:
            return Input(pending), prompts
        if reply:
            pending = pending[reply:]
            continue
        if not pending.startswith(START):
            raise ValueError('worker input requires a complete native bracketed-paste frame')
        end = pending.find(END, len(START))
        if end < 0:
            content(pending[len(START):], partial=True)
            return Input(pending), prompts
        body = content(pending[len(START):end])
        rest = pending[end + len(END):]
        if not rest:
            return Input(pending), prompts
        if rest[0] not in '\r\n':
            raise ValueError('worker input requires explicit submission after the frame')
        prompts, pending = (*prompts, body), rest[1:]
    return initial(), prompts


def finish(state):
    if state.pending:
        raise ValueError('worker input ended with an unsubmitted or incomplete frame')


def assignment(prompt):
    if not prompt.startswith(INTRO) or prompt.count(TASK) != 1:
        raise ValueError('worker input does not contain one complete native preamble')
    preamble, task = prompt.split(TASK, 1)
    header = re.findall(r'^Your task ID is: ([A-Za-z0-9_.:-]+)$', preamble, re.MULTILINE)
    tasks = set(re.findall(r'--task-id[ \t]+([A-Za-z0-9_.:-]+)(?=\s|$)', preamble))
    dispatches = set(re.findall(r'--dispatch-id[ \t]+([A-Za-z0-9_.:-]+)(?=\s|$)', preamble))
    if len(header) != 1 or tasks != {header[0]} or len(dispatches) != 1 or not task.strip():
        raise ValueError('worker input has missing or conflicting native lifecycle identities')
    return identifier(header[0]), identifier(next(iter(dispatches))), task
