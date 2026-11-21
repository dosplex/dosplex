#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Read approved bundle bytes without following symlinks; usable by root bootstrap."""
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat

MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024


def read_regular(root_fd, name, limit):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in ('.', '..') for p in path.parts) or str(path) != name:
        raise ValueError('invalid bundle path')
    directory = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                raise ValueError('invalid bundle file type or size')
            value = stream.read(limit + 1)
            if len(value) > limit:
                raise ValueError('bundle file exceeds limit')
            return value
    finally:
        os.close(directory)


def verified_files(bundle, expected_manifest):
    if len(expected_manifest) != 64 or any(c not in '0123456789abcdef' for c in expected_manifest):
        raise ValueError('invalid approved manifest digest')
    root = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = read_regular(root, 'manifest.json', 65536)
        if hashlib.sha256(raw).hexdigest() != expected_manifest:
            raise ValueError('manifest differs from approved candidate')
        manifest = json.loads(raw)
        entries = manifest['files']
        if not isinstance(entries, dict) or not 1 <= len(entries) <= 128:
            raise ValueError('invalid manifest file set')
        verified = {}
        total = 0
        for name, item in entries.items():
            if (not isinstance(item, dict) or type(item.get('bytes')) is not int or
                    not 0 <= item['bytes'] <= MAX_FILE or type(item.get('mode')) is not int or
                    item['mode'] not in (0o644, 0o755) or name == 'manifest.json'):
                raise ValueError('invalid manifest metadata')
            data = read_regular(root, name, min(MAX_FILE, MAX_TOTAL-total))
            total += len(data)
            if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('bundle file differs from approved candidate')
            verified[name] = (data, item['mode'])
        # Return the verified bytes, not paths that could change before copying.
        return verified
    finally:
        os.close(root)
