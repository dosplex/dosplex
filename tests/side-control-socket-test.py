#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Exercise real per-session sockets, kernel peer credentials, mode writes and the display protocol."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'packaging/steamos/dosplex-side-supervisor.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

LIVE = {'x': 960, 'y': 0, 'width': 960, 'height': 1080, 'game_width': 1280, 'game_height': 720,
        'scale_mode': 'fit', 'display_revision': 3, 'layout': 'vertical'}
DISPLAY_KEYS = ('display_applied_revision', 'display_applied_size', 'display_result', 'display_detail')


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dosplex-side-control-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.left = self.root / 'left'; self.left.mkdir()
        self.right = self.root / 'right'; self.right.mkdir()
        (self.right / 'mode').write_text('gaming\n')
        self.control = m.SideControl(self.left, os.getuid(), 'a' * 32)
        self.addCleanup(self.control.close)

    def exchange(self, raw, limit=128):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as c:
            c.settimeout(1); c.connect(str(self.control.path)); c.sendall(raw)
            self.control.poll()
            return c.recv(limit)

    def get(self, **changes):
        request = {'instance': 'a' * 32, 'display': 'get'}
        request.update(changes)
        return json.dumps(request).encode()

    def report(self, **changes):
        request = {'instance': 'a' * 32, 'display': 'report', 'revision': 4, 'game_width': 1280,
                   'game_height': 720, 'result': 'applied', 'detail': 'xrandr reports 1280x720'}
        request.update(changes)
        return request

    def start_session(self):
        """What the supervise loop publishes when it launches a session."""
        self.control.display_applied = m.launch_display_fields(LIVE)
        document = {'side': 'left', 'owner': 'tester', 'pid': 1, 'instance': 'a' * 32,
                    'lifecycle': 'running', 'active': True, 'mode': 'gaming', **self.control.display_applied}
        m.atomic_json(self.left / 'session.json', document)
        return document

    def snapshot(self):
        """Everything a rejected request must leave alone."""
        path = self.left / 'session.json'
        return (path.read_bytes() if path.exists() else None, sorted(os.listdir(self.left)),
                dict(self.control.display_applied) if self.control.display_applied is not None else None)

    def test_valid_request_updates_only_owner_and_rejects_invalid_or_stale(self):
        self.assertEqual(self.exchange(json.dumps({'instance': 'a'*32, 'mode': 'desktop'}).encode()), b'{"ok":true}')
        self.assertEqual((self.left / 'mode').read_text(), 'desktop\n')
        for raw in [b'[]', b'{', b'x'*2048,
                    json.dumps({'instance': 'a'*32, 'mode': 'gaming'}).encode()+b' '*2048,
                    json.dumps({'instance': 'b'*32, 'mode': 'gaming'}).encode(),
                    json.dumps({'instance': 'a'*32, 'mode': 'unknown'}).encode(),
                    json.dumps({'instance': 'a'*32, 'mode': 'gaming', 'side': 'right'}).encode()]:
            self.assertEqual(self.exchange(raw), b'{"ok":false}')
            self.assertEqual((self.left / 'mode').read_text(), 'desktop\n')
        self.assertEqual((self.right / 'mode').read_text(), 'gaming\n')

    def test_kernel_uid_rejection_precedes_state_write(self):
        self.control.owner_uid = os.getuid() + 1
        try:
            reply = self.exchange(b'{"instance":"'+b'a'*32+b'","mode":"desktop"}')
        except ConnectionResetError:
            reply = b''  # Peer is rejected before its packet is consumed.
        self.assertIn(reply, (b'', b'{"ok":false}'))
        self.assertFalse((self.left / 'mode').exists())

    def test_client_and_server_exchange_and_server_identity_check(self):
        stop = threading.Event()
        def serve():
            while not stop.is_set():
                self.control.poll(); time.sleep(.005)
        thread = threading.Thread(target=serve); thread.start()
        try:
            m.client_mode('desktop', str(self.control.path), 'a'*32, server_uid=os.getuid())
            self.assertEqual((self.left / 'mode').read_text(), 'desktop\n')
            with self.assertRaises(PermissionError):
                m.client_mode('gaming', str(self.control.path), 'a'*32, server_uid=os.getuid()+1)
            with self.assertRaises(RuntimeError):
                m.client_mode('gaming', str(self.control.path), 'b'*32, server_uid=os.getuid())
        finally:
            stop.set(); thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_idle_client_is_bounded_and_cleanup_preserves_replacement(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as c:
            c.connect(str(self.control.path)); start=time.monotonic(); self.control.poll()
            self.assertLess(time.monotonic()-start, .5)
        self.control.path.unlink(); self.control.path.write_text('replacement')
        self.control.close()
        self.assertEqual(self.control.path.read_text(), 'replacement')

    def test_display_get_returns_the_live_contract_to_the_owner_only(self):
        self.control.live = dict(LIVE)
        reply = self.exchange(self.get(), 1024)
        self.assertEqual(json.loads(reply), {'ok': True, 'live': LIVE})
        self.assertLess(len(reply), 512)
        # The kernel's peer credentials refuse any other uid before anything is read or sent.
        self.control.owner_uid = os.getuid() + 1
        try:
            reply = self.exchange(self.get(), 1024)
        except ConnectionResetError:
            reply = b''
        self.assertIn(reply, (b'', b'{"ok":false}'))
        self.assertFalse((self.left / 'mode').exists() or (self.left / 'session.json').exists())

    def test_display_get_without_a_known_contract_or_with_a_bad_request_answers_nothing(self):
        self.control.live = None
        self.assertEqual(self.exchange(self.get(), 1024), b'{"ok":false}')
        self.control.live = dict(LIVE)
        for raw in [self.get(instance='b' * 32), self.get(extra=1), self.get(display='set'),
                    self.get(display='GET'), self.get(display=None), self.get(display=['get']),
                    json.dumps({'display': 'get'}).encode(), json.dumps({'instance': 'a' * 32}).encode(),
                    json.dumps({'instance': 'a' * 32, 'display': 'get', 'mode': 'desktop'}).encode(),
                    self.get() + b' ' * 2048, b'[]', b'{']:
            with self.subTest(raw=raw[:60]):
                self.assertEqual(self.exchange(raw, 1024), b'{"ok":false}')
        self.assertFalse((self.left / 'mode').exists())

    def test_display_report_updates_session_json_and_keeps_the_rest_of_the_snapshot(self):
        document = self.start_session()
        self.assertEqual(self.exchange(json.dumps(self.report()).encode()), b'{"ok":true}')
        path = self.left / 'session.json'
        stored = json.loads(path.read_text())
        self.assertEqual({key: stored[key] for key in DISPLAY_KEYS},
                         {'display_applied_revision': 4, 'display_applied_size': [1280, 720],
                          'display_result': 'applied', 'display_detail': 'xrandr reports 1280x720'})
        self.assertEqual({k: v for k, v in stored.items() if k not in DISPLAY_KEYS},
                         {k: v for k, v in document.items() if k not in DISPLAY_KEYS})
        self.assertEqual(self.control.display_applied, {key: stored[key] for key in DISPLAY_KEYS})
        # The latest report wins, a failure included.
        failure = self.report(revision=5, game_width=1920, game_height=1080, result='failed',
                              detail='mode control timed out')
        self.assertEqual(self.exchange(json.dumps(failure).encode()), b'{"ok":true}')
        stored = json.loads(path.read_text())
        self.assertEqual((stored['display_applied_revision'], stored['display_applied_size'],
                          stored['display_result'], stored['display_detail']),
                         (5, [1920, 1080], 'failed', 'mode control timed out'))
        # Both ends of every range, and printable text beyond ASCII, are accepted and kept.
        for edge in (self.report(revision=0, game_width=1, game_height=1, detail=''),
                     self.report(revision=4294967295, game_width=16384, game_height=16384, detail='x' * 200),
                     self.report(detail='Gr\u00f6\u00dfe 1280\u00d7720 \u00fcbernommen')):
            self.assertEqual(self.exchange(json.dumps(edge).encode()), b'{"ok":true}')
            stored = json.loads(path.read_text())
            self.assertEqual((stored['display_applied_revision'], stored['display_applied_size'],
                              stored['display_detail']),
                             (edge['revision'], [edge['game_width'], edge['game_height']], edge['detail']))
        self.assertEqual({k: v for k, v in stored.items() if k not in DISPLAY_KEYS},
                         {k: v for k, v in document.items() if k not in DISPLAY_KEYS})
        # Two hundred characters count whatever their width: sent as UTF-8 they fit one packet, and the
        # same text escaped as ASCII does not, which the size limit refuses like any oversized request.
        wide = self.report(detail='\u00e9' * 200)
        self.assertEqual(self.exchange(json.dumps(wide, ensure_ascii=False).encode()), b'{"ok":true}')
        self.assertEqual(json.loads(path.read_text())['display_detail'], '\u00e9' * 200)
        self.assertEqual(self.exchange(json.dumps(wide).encode()), b'{"ok":false}')
        # No mode request, no leftover temporary file, nothing in the other side's directory.
        self.assertFalse((self.left / 'mode').exists())
        self.assertEqual(sorted(os.listdir(self.left)), ['control.sock', 'session.json'])
        self.assertEqual(list(self.right.iterdir()), [self.right / 'mode'])

    def test_malformed_oversized_or_unauthorised_display_reports_change_nothing(self):
        self.start_session()
        before = self.snapshot()
        good = self.report()
        cases = {'not an object': b'[]', 'truncated': b'{', 'junk': b'x' * 2048,
                 'oversized': json.dumps(good).encode() + b' ' * 2048,
                 'detail beyond the packet': json.dumps(self.report(detail='x' * 5000)).encode(),
                 'extra field': json.dumps(dict(good, side='right')).encode(),
                 'mode field as well': json.dumps(dict(good, mode='desktop')).encode(),
                 'wrong instance': json.dumps(self.report(instance='b' * 32)).encode()}
        for key in good:
            missing = {k: v for k, v in good.items() if k != key}
            cases['without ' + key] = json.dumps(missing).encode()
        for key, values in {
                'display': ['get', 'REPORT', 'report ', None],
                'revision': ['4', 4.0, True, None, -1, 2 ** 32, 10 ** 30, [4]],
                'game_width': [0, 16385, -1, '1280', 1280.0, True, None],
                'game_height': [0, 16385, -1, '720', 720.5, False, None],
                'result': ['ok', 'APPLIED', '', None, True, ['applied']],
                'detail': ['x' * 201, '\u00e9' * 201, 'two\nlines', 'tab\t', 'nul\x00', 'del\x7f', '\u202e',
                           '\u200b', 5, None, ['x']]}.items():
            for value in values:
                cases[f'{key}={value!r}'] = json.dumps(self.report(**{key: value})).encode()
        for name, raw in cases.items():
            with self.subTest(name[:70]):
                self.assertEqual(self.exchange(raw), b'{"ok":false}')
                self.assertEqual(self.snapshot(), before)
        # A valid report from another uid is refused before it is read.
        self.control.owner_uid = os.getuid() + 1
        try:
            reply = self.exchange(json.dumps(good).encode())
        except ConnectionResetError:
            reply = b''
        self.assertIn(reply, (b'', b'{"ok":false}'))
        self.assertEqual(self.snapshot(), before)
        self.control.owner_uid = os.getuid()
        self.assertEqual(self.exchange(json.dumps(good).encode()), b'{"ok":true}')

    def test_display_report_needs_a_session_and_the_snapshot_of_this_instance(self):
        report = json.dumps(self.report()).encode()
        path = self.left / 'session.json'
        # No session: nothing was launched, or the last one has been stopped. A late report of a
        # stopped session must not touch the snapshot, even though it is this instance's.
        self.assertIsNone(self.control.display_applied)
        self.assertEqual(self.exchange(report), b'{"ok":false}')
        self.assertFalse(path.exists())
        self.start_session()
        self.control.display_applied = None
        before = self.snapshot()
        self.assertEqual(self.exchange(report), b'{"ok":false}')
        self.assertEqual(self.snapshot(), before)
        path.unlink()
        # A session whose snapshot is gone (signed out): the file is never created again.
        self.control.display_applied = m.launch_display_fields(LIVE)
        self.assertEqual(self.exchange(report), b'{"ok":false}')
        self.assertFalse(path.exists())
        # A snapshot of another instance, or one that is not a snapshot, stays as it is.
        for content in (json.dumps({'side': 'left', 'owner': 'tester', 'instance': 'b' * 32}),
                        json.dumps({'side': 'left', 'owner': 'tester'}), '[]', '{', ''):
            with self.subTest(content=content):
                path.write_text(content)
                self.assertEqual(self.exchange(report), b'{"ok":false}')
                self.assertEqual(path.read_text(), content)
                self.assertEqual(self.control.display_applied, m.launch_display_fields(LIVE))

    def test_display_client_helpers_exchange_and_check_the_server_identity(self):
        self.control.live = dict(LIVE)
        self.start_session()
        endpoint, mine = str(self.control.path), os.getuid()
        stop = threading.Event()
        def serve():
            while not stop.is_set():
                self.control.poll(); time.sleep(.005)
        thread = threading.Thread(target=serve); thread.start()
        try:
            self.assertEqual(m.client_display_get(endpoint, 'a' * 32, server_uid=mine), LIVE)
            m.client_display_report(endpoint, 'a' * 32, 5, 1920, 1080, 'applied', 'ok', server_uid=mine)
            stored = json.loads((self.left / 'session.json').read_text())
            self.assertEqual((stored['display_applied_revision'], stored['display_applied_size'],
                              stored['display_result'], stored['display_detail']), (5, [1920, 1080], 'applied', 'ok'))
            m.client_display_report(endpoint, 'a' * 32, 6, 1280, 720, 'failed', server_uid=mine)  # No detail.
            stored = json.loads((self.left / 'session.json').read_text())
            self.assertEqual((stored['display_applied_revision'], stored['display_result'], stored['display_detail']),
                             (6, 'failed', ''))
            m.client_desktop_scale(endpoint, 'a' * 32, '1.5', server_uid=mine)
            self.assertEqual(json.loads((self.left / 'session.json').read_text())['desktop_scale_milli'], 1500)
            with self.assertRaises(ValueError):
                m.client_desktop_scale(endpoint, 'a' * 32, '5', server_uid=mine)
            with self.assertRaises(PermissionError):
                m.client_desktop_scale(endpoint, 'a' * 32, '2', server_uid=mine + 1)
            m.client_mode('desktop', endpoint, 'a' * 32, server_uid=mine)  # Same socket, same server check.
            self.assertEqual((self.left / 'mode').read_text(), 'desktop\n')
            before = self.snapshot()
            # A server that is not the trusted uid is never given the request.
            with self.assertRaises(PermissionError):
                m.client_display_get(endpoint, 'a' * 32, server_uid=mine + 1)
            with self.assertRaises(PermissionError):
                m.client_display_report(endpoint, 'a' * 32, 7, 1280, 720, 'applied', server_uid=mine + 1)
            with patch.dict(os.environ, {'DOSPLEX_SESSION_CONTROL_SERVER_UID': str(mine + 1)}):
                with self.assertRaises(PermissionError):
                    m.client_display_get(endpoint, 'a' * 32)
            with patch.dict(os.environ, {'DOSPLEX_SESSION_CONTROL_SERVER_UID': str(mine)}):
                self.assertEqual(m.client_display_get(endpoint, 'a' * 32), LIVE)
            # A refusal, from a stale instance or an unknown contract, is a RuntimeError.
            with self.assertRaises(RuntimeError):
                m.client_display_get(endpoint, 'b' * 32, server_uid=mine)
            with self.assertRaises(RuntimeError):
                m.client_display_report(endpoint, 'b' * 32, 7, 1280, 720, 'applied', server_uid=mine)
            self.control.live = None
            with self.assertRaises(RuntimeError):
                m.client_display_get(endpoint, 'a' * 32, server_uid=mine)
            # A request that cannot be valid never leaves the process.
            for call in (lambda: m.client_display_get(endpoint, 'short', server_uid=mine),
                         lambda: m.client_display_get('relative.sock', 'a' * 32, server_uid=mine),
                         lambda: m.client_display_report(endpoint, 'a' * 32, -1, 1280, 720, 'applied', server_uid=mine),
                         lambda: m.client_display_report(endpoint, 'a' * 32, 7, 0, 720, 'applied', server_uid=mine),
                         lambda: m.client_display_report(endpoint, 'a' * 32, 7, 1280, 720, 'done', server_uid=mine),
                         lambda: m.client_display_report(endpoint, 'a' * 32, 7, 1280, 720, 'applied', 'x' * 201,
                                                         server_uid=mine)):
                with self.assertRaises(ValueError):
                    call()
            self.assertEqual(self.snapshot(), before)
        finally:
            stop.set(); thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_desktop_scale_report_lands_in_session_json_and_is_validated(self):
        instance = 'a' * 32
        report = lambda milli: json.dumps({'instance': instance, 'desktop_scale_milli': milli}).encode()
        # No session yet: refused, nothing written.
        self.assertEqual(self.exchange(report(2000)), b'{"ok":false}')
        self.assertFalse((self.left / 'session.json').exists())
        document = self.start_session()
        self.assertEqual(self.exchange(report(2000)), b'{"ok":true}')
        stored = json.loads((self.left / 'session.json').read_text())
        self.assertEqual(stored, {**document, 'desktop_scale_milli': 2000})
        self.assertEqual(self.control.desktop_scale, {'desktop_scale_milli': 2000})
        for edge in (1000, 1250, 3000):
            self.assertEqual(self.exchange(report(edge)), b'{"ok":true}')
            self.assertEqual(json.loads((self.left / 'session.json').read_text())['desktop_scale_milli'], edge)
        before = self.snapshot()
        for bad in (999, 3001, 0, -2000, '2000', 2000.0, True, None):
            self.assertEqual(self.exchange(json.dumps({'instance': instance, 'desktop_scale_milli': bad}).encode()),
                             b'{"ok":false}', bad)
        self.assertEqual(self.exchange(json.dumps({'instance': instance, 'desktop_scale_milli': 2000,
                                                    'mode': 'desktop'}).encode()), b'{"ok":false}')
        self.assertEqual(self.snapshot()[0], before[0])
        self.assertFalse((self.left / 'mode').exists())

    def test_desktop_scale_text_becomes_thousandths(self):
        for text, milli in (('1', 1000), ('1.25', 1250), ('1.5', 1500), ('1.75', 1750), ('2', 2000),
                            ('2.5', 2500), ('3', 3000), ('3.0', 3000)):
            self.assertEqual(m.desktop_scale_milli(text), milli, text)
        for text in ('', 'x', '0.5', '4', '1.2345', 'NaN', 'Infinity', '-2', '2e0x', None):
            with self.assertRaises(ValueError, msg=text):
                m.desktop_scale_milli(text)

    def test_one_definition_of_the_control_requests(self):
        instance = 'a' * 32
        accepted = {'mode': [{'instance': instance, 'mode': 'gaming'}, {'instance': instance, 'mode': 'desktop'}],
                    'get': [{'instance': instance, 'display': 'get'}],
                    'report': [self.report(), self.report(result='failed', detail=''),
                               self.report(revision=0, game_width=1, game_height=16384, detail='x' * 200)],
                    'scale': [{'instance': instance, 'desktop_scale_milli': 1000},
                              {'instance': instance, 'desktop_scale_milli': 3000}]}
        for kind, requests in accepted.items():
            for request in requests:
                with self.subTest(kind=kind, request=request):
                    self.assertEqual(m.validate_control_request(request, instance), kind)
        for request in (None, [], 'get', 7, {}, {'instance': instance}, {'instance': 'b' * 32, 'mode': 'gaming'},
                        {'instance': instance, 'mode': 'unknown'}, {'instance': instance, 'mode': ['gaming']},
                        {'instance': instance, 'mode': 'gaming', 'display': 'get'},
                        {'instance': instance, 'display': 'set'}, {'instance': instance, 'display': ['get']},
                        {'instance': instance, 'display': 'get', 'revision': 1},
                        {'instance': instance, 'restart': True}, {'instance': instance, 'stop': True},
                        {'mode': 'gaming'}, {'display': 'get'}, self.report(instance='b' * 32),
                        dict(self.report(), extra=1), self.report(revision='4'), self.report(result='ok'),
                        self.report(detail='x' * 201), self.report(detail='two\nlines'),
                        {k: v for k, v in self.report().items() if k != 'detail'},
                        {'instance': instance, 'desktop_scale_milli': 999}, {'instance': instance, 'desktop_scale_milli': '2000'},
                        {'instance': instance, 'desktop_scale_milli': 2000, 'mode': 'desktop'}):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    m.validate_control_request(request, instance)

    def serve_once(self, replies):
        """A control server that answers the connections it gets with `replies`, in order."""
        self.fakes = getattr(self, 'fakes', 0) + 1
        received, path = [], str(self.root / f'fake-{self.fakes}.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(server.close)
        server.bind(path)
        server.listen(4)
        finished = threading.Event()
        def serve():
            for reply in replies:
                connection, _ = server.accept()
                with connection:
                    received.append(connection.recv(1024))
                    if reply is None:
                        finished.wait(5)  # Stay silent, with the connection open.
                    else:
                        try:
                            connection.sendall(reply)
                        except OSError:
                            pass
        thread = threading.Thread(target=serve)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(finished.set)
        return path, received

    def test_the_generic_client_call_checks_before_sending_and_judges_the_reply(self):
        instance, mine = 'a' * 32, os.getuid()
        mode = {'instance': instance, 'mode': 'desktop'}
        # Anything that is not a protocol message for this instance fails before any connection is made.
        nowhere = str(self.root / 'nowhere.sock')
        for request in ({'instance': instance, 'restart': True}, {'instance': 'b' * 32, 'mode': 'gaming'},
                        dict(mode, extra=1), self.report(detail='x' * 201), [], None):
            with self.subTest(request=request):
                with self.assertRaises(ValueError):
                    m.client_request(nowhere, instance, request, server_uid=mine)
        for endpoint, id_ in (('relative.sock', instance), (nowhere, 'short'), (nowhere, None)):
            with self.assertRaises(ValueError):
                m.client_request(endpoint, id_, mode, server_uid=mine)
        with self.assertRaises(FileNotFoundError):  # A valid request does try to connect.
            m.client_request(nowhere, instance, mode, server_uid=mine)
        # An accepted reply comes back whole; every other reply is a refusal.
        path, received = self.serve_once([b'{"ok":true}', b'{"ok":true,"live":{"width":1}}'])
        self.assertEqual(m.client_request(path, instance, mode, server_uid=mine), {'ok': True})
        get = {'instance': instance, 'display': 'get'}
        self.assertEqual(m.client_request(path, instance, get, server_uid=mine), {'ok': True, 'live': {'width': 1}})
        self.assertEqual([json.loads(raw) for raw in received], [mode, get])
        replies = [b'{"ok":false}', b'{"ok":"yes"}', b'{}', b'[]', b'"ok"', b'null', b'nonsense', b'\xff\xfe', b'']
        path, received = self.serve_once(replies)
        for reply in replies:
            with self.subTest(reply=reply):
                with self.assertRaises(RuntimeError):
                    m.client_request(path, instance, mode, server_uid=mine, timeout=.5)
        # The server is judged by its uid before the request is sent: an untrusted one is shown nothing.
        path, received = self.serve_once([b'{"ok":true}'])
        with self.assertRaises(PermissionError):
            m.client_request(path, instance, mode, server_uid=mine + 1)
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(received, [b''])
        # A server that never answers costs the caller its timeout and nothing more.
        path, received = self.serve_once([None])
        start = time.monotonic()
        with self.assertRaises(OSError):
            m.client_request(path, instance, mode, server_uid=mine, timeout=.2)
        self.assertLess(time.monotonic() - start, 2)
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual([json.loads(raw) for raw in received], [mode])


if __name__ == '__main__': unittest.main()
