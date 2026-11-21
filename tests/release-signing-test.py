#!/usr/bin/env python3
# dosplex-test: tier=unit
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Release signatures (T-702, PKG-011): hermetic, stdlib only, about a second.

  - the Ed25519 verifier inside `dosplex` against the RFC 8032 test vectors, against refusals that libsodium (and
    so minisign) makes and RFC 8032 does not require, and against signatures from a second implementation;
  - the minisign file format: a signature file made the way `minisign -S` makes it is accepted with the pinned
    key and refused with any other key, after any change, and for another kind of file.

tests/release-tool-test.py tests tools/release.py and tests/dosplex-update-test.py tests install, update and
rollback with signed releases; both use the TEST ONLY signer and the fake `minisign` that this file holds.

Everything about signing in this file is TEST ONLY. The signer below is a plain, slow implementation for keys
that are made here, at test time, in memory or in a temporary directory. No key is read from the repository
and none is ever committed; the public vectors are RFC 8032's and are kept in hex.
"""
import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


dosplex = load_module('dosplex_under_test', ROOT / 'tools' / 'dosplex')


# --- the TEST ONLY signer ------------------------------------------------------------------------------------
# Another formulation of the same curve than tools/dosplex uses (the dedicated doubling formula and the
# non-reduced addition of the Explicit-Formulas Database, not RFC 8032's unified one), so that a mistake in one
# of the two shows up as a disagreement. It signs with an Ed25519 seed that the test made a moment ago.

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_BASE = (15112221349535400772501151409588531511454012693041857206046113283949847762202,
         46316835694926478169428394003475163141307993866256225615783033603165251855960)


def _on_curve(x, y):
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _P == 0


def _add(p, q):
    (x1, y1, z1, t1), (x2, y2, z2, t2) = p, q
    a, b, c, d = x1 * x2 % _P, y1 * y2 % _P, t1 * _D % _P * t2 % _P, z1 * z2 % _P
    e = ((x1 + y1) * (x2 + y2) - a - b) % _P
    f, g, h = (d - c) % _P, (d + c) % _P, (b + a) % _P
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _double(p):
    x1, y1, z1, _ = p
    a, b, c = x1 * x1 % _P, y1 * y1 % _P, 2 * z1 * z1 % _P
    d = -a % _P
    e = ((x1 + y1) ** 2 - a - b) % _P
    g = (d + b) % _P
    f, h = (g - c) % _P, (d - b) % _P
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _multiply(scalar, point):
    result = (0, 1, 1, 0)
    for bit in bin(scalar)[2:]:
        result = _double(result)
        if bit == '1':
            result = _add(result, point)
    return result


def _encode(point):
    x, y, z, _ = point
    zinv = pow(z, _P - 2, _P)
    return (y * zinv % _P | (x * zinv % _P & 1) << 255).to_bytes(32, 'little')


_B = (_BASE[0], _BASE[1], 1, _BASE[0] * _BASE[1] % _P)
assert _on_curve(*_BASE) and _encode(_multiply(_L, _B)) == _encode((0, 1, 1, 0)), 'the base point constants are wrong'


def only_for_tests_public_key(seed):
    return _encode(_multiply(_secret_scalar(seed), _B))


def _secret_scalar(seed):
    scalar = int.from_bytes(hashlib.sha512(seed).digest()[:32], 'little')
    return scalar & ((1 << 254) - 8) | 1 << 254


def only_for_tests_sign(seed, message):
    """An Ed25519 signature (RFC 8032, 5.1.6). TEST ONLY: slow, and not constant time."""
    digest = hashlib.sha512(seed).digest()
    scalar, public = _secret_scalar(seed), only_for_tests_public_key(seed)
    nonce = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), 'little') % _L
    commitment = _encode(_multiply(nonce, _B))
    challenge = int.from_bytes(hashlib.sha512(commitment + public + message).digest(), 'little') % _L
    return commitment + ((nonce + challenge * scalar) % _L).to_bytes(32, 'little')


class OnlyForTestsKey:
    """A minisign-style key pair made at test time. Its "secret key file" is a JSON file that only the fake
    minisign command below understands; it is not a minisign key and holds nothing that outlives the test."""

    def __init__(self, seed=None, keynum=None):
        self.seed = seed if seed is not None else os.urandom(32)
        self.keynum = keynum if keynum is not None else os.urandom(8)
        self.public = only_for_tests_public_key(self.seed)

    @property
    def key_id(self):
        return dosplex.key_id_text(self.keynum)

    def public_file(self):
        """What minisign writes to its .pub file."""
        line = base64.b64encode(b'Ed' + self.keynum + self.public).decode()
        return 'untrusted comment: minisign public key: %s\n%s\n' % (self.key_id, line)

    def pin(self):
        """The RELEASE_KEYS entry for this key."""
        return {'id': self.key_id, 'public': self.public.hex()}

    def sign_text(self, message, trusted_comment, prehashed=True, untrusted='signature from minisign secret key'):
        """A signature file as `minisign -S` writes it (prehashed, "ED", is its default; "Ed" is the legacy one)."""
        signed = hashlib.blake2b(message, digest_size=64).digest() if prehashed else message
        signature = only_for_tests_sign(self.seed, signed)
        combined = only_for_tests_sign(self.seed, signature + trusted_comment.encode())
        first = base64.b64encode((b'ED' if prehashed else b'Ed') + self.keynum + signature).decode()
        return 'untrusted comment: %s\n%s\ntrusted comment: %s\n%s\n' % (
            untrusted, first, trusted_comment, base64.b64encode(combined).decode())

    def secret_file_text(self):
        return json.dumps({'format': 'test-only key for the fake minisign', 'seed': self.seed.hex(),
                           'keynum': self.keynum.hex()})


def fake_minisign(argv):
    """The `minisign` command for tests: only `-S -s KEYFILE -m FILE -x SIGFILE [-t COMMENT] [-c COMMENT]`.

    TEST ONLY. FAKE_MINISIGN_LOG names a file that gets every command line; FAKE_MINISIGN_FAIL makes it fail
    the way minisign does when the password is wrong."""
    if os.environ.get('FAKE_MINISIGN_LOG'):
        with open(os.environ['FAKE_MINISIGN_LOG'], 'a') as log:
            log.write(json.dumps(argv) + '\n')
    options, index = {}, 0
    while index < len(argv):
        flag = argv[index]
        if flag in ('-S', '-W', '-H'):
            options[flag] = True
            index += 1
        elif flag in ('-s', '-m', '-x', '-t', '-c') and index + 1 < len(argv):
            options[flag] = argv[index + 1]
            index += 2
        else:
            print('minisign: unsupported arguments for the test double: %s' % ' '.join(argv), file=sys.stderr)
            return 2
    if '-S' not in options or not all(flag in options for flag in ('-s', '-m', '-x')):
        print('minisign: the test double signs only', file=sys.stderr)
        return 2
    if os.environ.get('FAKE_MINISIGN_FAIL'):       # the way minisign fails for a wrong password
        return 1
    secret = json.loads(Path(options['-s']).read_text())
    key = OnlyForTestsKey(bytes.fromhex(secret['seed']), bytes.fromhex(secret['keynum']))
    text = key.sign_text(Path(options['-m']).read_bytes(),
                         options.get('-t', 'timestamp:1\tfile:' + Path(options['-m']).name),
                         untrusted=options.get('-c', 'signature from minisign secret key'))
    Path(options['-x']).write_text(text)
    return 0


FAKE_MINISIGN_SCRIPT = '''#!{python}
import importlib.util, sys
spec = importlib.util.spec_from_file_location("release_signing_test", {test!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.exit(module.fake_minisign(sys.argv[1:]))
'''


def install_fake_minisign(directory):
    """Put a `minisign` into `directory` (to go first on PATH) that signs with an OnlyForTestsKey file."""
    path = Path(directory) / 'minisign'
    path.write_text(FAKE_MINISIGN_SCRIPT.format(python=sys.executable, test=str(Path(__file__).resolve())))
    path.chmod(0o755)
    return path


# --- RFC 8032 section 7.1, in hex ------------------------------------------------------------------------------
# (seed, public key, message, signature). The vectors are public test data. Every field was also reproduced with
# OpenSSL when this file was written.
RFC_8032 = (
    ('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
     'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', '',
     'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    ('4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
     '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '72',
     '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
    ('c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
     'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025', 'af82',
     '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a'),
    ('833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42',
     'ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf',
     'ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f',
     'dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704'),
)

# The eight points of small order, as libsodium lists them (compressed). None of them may be a public key or an
# R, however the rest of the signature looks.
SMALL_ORDER = (
    '0100000000000000000000000000000000000000000000000000000000000000',
    'ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f',
    '0000000000000000000000000000000000000000000000000000000000000000',
    '0000000000000000000000000000000000000000000000000000000000000080',
    'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a',
    'c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa',
    '26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05',
    '26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85',
)


class Ed25519Verifier(unittest.TestCase):
    def test_the_rfc_8032_vectors_verify(self):
        for seed, public, message, signature in RFC_8032:
            with self.subTest(public=public[:8]):
                self.assertTrue(dosplex.ed25519_verify(bytes.fromhex(public), bytes.fromhex(message),
                                                      bytes.fromhex(signature)))

    def test_the_test_only_signer_makes_the_rfc_signatures(self):
        # The signer and the verifier are two implementations; the vectors tie both of them to the RFC.
        for seed, public, message, signature in RFC_8032:
            with self.subTest(public=public[:8]):
                self.assertEqual(only_for_tests_public_key(bytes.fromhex(seed)).hex(), public)
                self.assertEqual(only_for_tests_sign(bytes.fromhex(seed), bytes.fromhex(message)).hex(), signature)

    def test_a_changed_message_signature_or_key_is_refused(self):
        for index, (seed, public, message, signature) in enumerate(RFC_8032):
            another = bytes.fromhex(RFC_8032[(index + 1) % len(RFC_8032)][1])
            public, message, signature = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
            with self.subTest(public=public.hex()[:8]):
                self.assertFalse(dosplex.ed25519_verify(public, message + b'\0', signature))
                self.assertFalse(dosplex.ed25519_verify(public, b'x' + message, signature))
                for position in (0, 31, 32, 63):                   # R and S, first and last byte
                    changed = bytearray(signature)
                    changed[position] ^= 0x01
                    self.assertFalse(dosplex.ed25519_verify(public, message, bytes(changed)), position)
                changed = bytearray(public)
                changed[0] ^= 0x01
                self.assertFalse(dosplex.ed25519_verify(bytes(changed), message, signature))
                self.assertFalse(dosplex.ed25519_verify(another, message, signature))

    def test_lengths_that_are_not_an_ed25519_signature_or_key_are_refused(self):
        seed, public, message, signature = (bytes.fromhex(value) for value in RFC_8032[0])
        for bad in (b'', signature[:-1], signature + b'\0'):
            self.assertFalse(dosplex.ed25519_verify(public, message, bad))
        for bad in (b'', public[:-1], public + b'\0'):
            self.assertFalse(dosplex.ed25519_verify(bad, message, signature))

    def test_a_signature_scalar_that_is_not_reduced_is_refused(self):
        # S + L is the same signature for RFC 8032's equation, which is why a strict verifier refuses it:
        # otherwise one valid signature would have many forms.
        for seed, public, message, signature in RFC_8032:
            signature = bytes.fromhex(signature)
            scalar = int.from_bytes(signature[32:], 'little')
            self.assertLess(scalar, dosplex._L)
            padded = signature[:32] + (scalar + dosplex._L).to_bytes(32, 'little')
            self.assertFalse(dosplex.ed25519_verify(bytes.fromhex(public), bytes.fromhex(message), padded))

    def equation_holds(self, public, message, signature):
        """RFC 8032's bare check [S]B = R + [h]A, without the rules that refuse small-order points."""
        a_point, r_point = dosplex._point_decompress(public), dosplex._point_decompress(signature[:32])
        challenge = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), 'little') % _L
        scalar = int.from_bytes(signature[32:], 'little')
        return dosplex._point_equal(dosplex._point_mul(scalar, dosplex._BASE),
                                   dosplex._point_add(r_point, dosplex._point_mul(challenge, a_point)))

    def test_points_of_small_order_are_never_a_key_or_an_r(self):
        message = b'any message'
        seed = os.urandom(32)
        public = only_for_tests_public_key(seed)
        for encoded in SMALL_ORDER:
            point = bytes.fromhex(encoded)
            with self.subTest(point=encoded[:8]):
                self.assertTrue(dosplex._has_small_order(dosplex._point_decompress(point)))
                self.assertFalse(dosplex.ed25519_verify(point, message, point + bytes(32)))
                self.assertFalse(dosplex.ed25519_verify(public, message, point + RFC_8032_S))
        # For the identity the bare equation holds, as a public key and as an R (the signature of a signer whose
        # nonce is 0): only the rule about small order refuses these.
        identity = bytes.fromhex(SMALL_ORDER[0])
        self.assertTrue(self.equation_holds(identity, message, identity + bytes(32)))
        challenge = int.from_bytes(hashlib.sha512(identity + public + message).digest(), 'little') % _L
        nonce_zero = identity + (challenge * _secret_scalar(seed) % _L).to_bytes(32, 'little')
        self.assertTrue(self.equation_holds(public, message, nonce_zero))
        self.assertFalse(dosplex.ed25519_verify(identity, message, identity + bytes(32)))
        self.assertFalse(dosplex.ed25519_verify(public, message, nonce_zero))

    def test_encodings_that_are_not_reduced_are_refused(self):
        identity_again = (1 + dosplex._P).to_bytes(32, 'little')          # y = 1 + p stands for y = 1 for a lazy decoder
        self.assertIsNone(dosplex._point_decompress(identity_again))
        self.assertIsNone(dosplex._point_decompress(((1 << 255) - 1).to_bytes(32, 'little')))
        self.assertIsNone(dosplex._point_decompress(bytes.fromhex('0100000000000000000000000000000000000000000000000000000000000080')))
        seed, public, message, signature = (bytes.fromhex(value) for value in RFC_8032[0])
        self.assertFalse(dosplex.ed25519_verify(identity_again, message, signature))
        self.assertFalse(dosplex.ed25519_verify(public, message, identity_again + signature[32:]))

    def test_points_that_are_not_on_the_curve_are_refused(self):
        found = 0
        for y in range(2, 200):
            encoded = y.to_bytes(32, 'little')
            if dosplex._point_decompress(encoded) is None:
                found += 1
        self.assertGreater(found, 20)           # about half of all y have no x

    def test_random_keys_and_messages_agree_between_the_two_implementations(self):
        for index in range(40):
            seed, message = os.urandom(32), os.urandom(index * 3)
            public, signature = only_for_tests_public_key(seed), only_for_tests_sign(seed, message)
            self.assertTrue(dosplex.ed25519_verify(public, message, signature), index)
            self.assertFalse(dosplex.ed25519_verify(public, message + b'.', signature), index)
            changed = bytearray(signature)
            changed[index % 64] ^= 1 << (index % 8)
            self.assertFalse(dosplex.ed25519_verify(public, message, bytes(changed)), index)


RFC_8032_S = bytes.fromhex(RFC_8032[0][3])[32:]
_L = dosplex._L


class MinisignFormat(unittest.TestCase):
    COMMENT = 'dosplex manifest 0.2.0-test'

    def setUp(self):
        self.key = OnlyForTestsKey()
        self.keys = [dosplex.ReleaseKey(self.key.keynum, self.key.public)]
        self.message = b'{"version": "0.2.0-test"}\n'

    def verify(self, signature_text, message=None, keys=None, what='manifest'):
        return dosplex.verify_signature(self.message if message is None else message, signature_text,
                                       self.keys if keys is None else keys, what)

    def assertRefused(self, text, *needles, **kwargs):
        with self.assertRaises(dosplex.VerifyError) as caught:
            self.verify(text, **kwargs)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_a_signature_made_the_way_minisign_makes_it_is_accepted(self):
        for prehashed in (True, False):
            with self.subTest(prehashed=prehashed):
                signed = self.verify(self.key.sign_text(self.message, self.COMMENT, prehashed=prehashed))
                self.assertEqual((signed.key_id, signed.subject), (self.key.key_id, '0.2.0-test'))

    def test_the_key_id_is_printed_the_way_minisign_prints_it(self):
        self.assertEqual(dosplex.key_id_text(bytes.fromhex('1fe8b44218 0f62e7'.replace(' ', ''))), 'E7620F1842B4E81F')
        self.assertIn('minisign public key: ' + self.key.key_id, self.key.public_file())

    def test_public_keys_are_read_from_a_file_or_from_the_line_alone(self):
        text = self.key.public_file()
        self.assertEqual(dosplex.read_minisign_public_key(text), (self.key.keynum, self.key.public))
        self.assertEqual(dosplex.read_minisign_public_key(text.splitlines()[1]), (self.key.keynum, self.key.public))
        for bad in ('', 'untrusted comment: x\n', text.splitlines()[1][:-4], text + 'RWQ=\n',
                    base64.b64encode(b'ED' + self.key.keynum + self.key.public).decode()):
            with self.assertRaises(dosplex.VerifyError, msg=bad):
                dosplex.read_minisign_public_key(bad)

    def test_the_pinned_keys_are_read_from_release_keys(self):
        original = dosplex.RELEASE_KEYS
        try:
            dosplex.RELEASE_KEYS = (self.key.pin(),)
            (key,) = dosplex.pinned_keys()
            self.assertEqual((key.keynum, key.public, key.id), (self.key.keynum, self.key.public, self.key.key_id))
            for bad in ({'id': 'E7620F1842B4E81F'}, {'id': 'xyz', 'public': self.key.public.hex()},
                        {'id': self.key.key_id, 'public': self.key.public.hex()[:-2]}):
                dosplex.RELEASE_KEYS = (bad,)
                with self.assertRaises(dosplex.DosplexError):
                    dosplex.pinned_keys()
        finally:
            dosplex.RELEASE_KEYS = original

    def test_any_change_to_the_file_or_the_signature_is_refused(self):
        text = self.key.sign_text(self.message, self.COMMENT)
        lines = text.splitlines()
        self.assertRefused(text, 'not valid', message=self.message + b' ')
        self.assertRefused(text, 'not valid', message=b'')
        signature = bytearray(base64.b64decode(lines[1]))
        signature[40] ^= 1
        changed = '\n'.join([lines[0], base64.b64encode(bytes(signature)).decode(), *lines[2:]]) + '\n'
        self.assertRefused(changed, 'not valid')
        other_global = '\n'.join([*lines[:3], base64.b64encode(os.urandom(64)).decode()]) + '\n'
        self.assertRefused(other_global, 'signature comment')
        reworded = '\n'.join([lines[0], lines[1], 'trusted comment: dosplex manifest 9.9.9', lines[3]]) + '\n'
        self.assertRefused(reworded, 'signature comment')

    def test_a_key_that_is_not_pinned_is_refused_even_with_a_good_signature(self):
        stranger = OnlyForTestsKey()
        message = self.assertRefused(stranger.sign_text(self.message, self.COMMENT), 'does not trust')
        self.assertIn(stranger.key_id, message)
        self.assertIn(self.key.key_id, message)
        # The same key id with another key (someone guessing at the id): the signature does not verify.
        impostor = OnlyForTestsKey(keynum=self.key.keynum)
        self.assertRefused(impostor.sign_text(self.message, self.COMMENT), 'not valid')

    def test_two_pinned_keys_let_a_rotation_happen_and_a_dropped_key_stops_working(self):
        old, new = self.key, OnlyForTestsKey()
        both = [dosplex.ReleaseKey(old.keynum, old.public), dosplex.ReleaseKey(new.keynum, new.public)]
        for key in (old, new):
            self.assertEqual(self.verify(key.sign_text(self.message, self.COMMENT), keys=both).key_id, key.key_id)
        self.assertRefused(old.sign_text(self.message, self.COMMENT), 'does not trust', keys=both[1:])

    def test_without_a_pinned_key_nothing_verifies_and_the_message_says_what_to_do(self):
        self.assertRefused(self.key.sign_text(self.message, self.COMMENT), 'pins no release signing key',
                           'manifest digest', keys=[])

    def test_a_signature_for_one_kind_of_file_does_not_stand_for_another(self):
        text = self.key.sign_text(self.message, 'dosplex index preview')
        self.assertEqual(self.verify(text, what='index').subject, 'preview')
        self.assertRefused(text, 'does not say that it is for a dosplex manifest')
        for comment in ('timestamp:1555779966\tfile:manifest.json', 'dosplex manifest', 'dosplex manifest ',
                        'manifest 0.2.0', ''):
            self.assertRefused(self.key.sign_text(self.message, comment), 'does not say that it is for')

    def test_files_that_are_not_four_line_minisign_signatures_are_refused(self):
        text = self.key.sign_text(self.message, self.COMMENT)
        lines = text.splitlines()
        good = base64.b64decode(lines[1])
        cases = {
            'empty': '', 'one line': lines[1] + '\n', 'three lines': '\n'.join(lines[:3]) + '\n',
            'five lines': text + 'extra\n', 'no comment prefix': '\n'.join(['x', *lines[1:]]) + '\n',
            'no trusted prefix': '\n'.join([lines[0], lines[1], 'comment: x', lines[3]]) + '\n',
            'not base64': '\n'.join([lines[0], '!' * 100, *lines[2:]]) + '\n',
            'too short': '\n'.join([lines[0], base64.b64encode(good[:-1]).decode(), *lines[2:]]) + '\n',
            'unknown algorithm': '\n'.join([lines[0], base64.b64encode(b'XX' + good[2:]).decode(), *lines[2:]]) + '\n',
            'too large': text + 'x' * 5000,
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.assertRefused(content, 'minisign')

    def test_text_with_windows_line_ends_and_bytes_are_read(self):
        text = self.key.sign_text(self.message, self.COMMENT)
        self.assertEqual(self.verify(text.replace('\n', '\r\n')).subject, '0.2.0-test')
        self.assertEqual(self.verify(text.encode()).subject, '0.2.0-test')
        self.assertRefused(b'\xff\xfe', 'not text')


if __name__ == '__main__':
    unittest.main()
