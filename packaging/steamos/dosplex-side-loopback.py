#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
"""Give one side its own IPv4 loopback address.

Both sides run in the host's network namespace, so they share 127.0.0.1. Steam listens on fixed loopback ports
(127.0.0.1:57343 among them): the Steam that starts second cannot have the port, and that side's games and web
helper then talk to the other account's Steam. A game started that way exits within seconds (T-452).

The cure keeps the one network namespace and moves a side's loopback instead. Four small cgroup socket-address
programs, attached to that side's session unit by systemd (BPFProgram=), turn 127.0.0.1 into the side's own address
(127.0.0.2 by default; all of 127.0.0.0/8 is local on Linux) whenever a process of the side binds, connects or
sends to it, and turn the address back in what a datagram receive reports. Inside the side everything still says
127.0.0.1; from the host the side's listeners are on its own address. A side with the programs cannot reach a
service that listens on the real 127.0.0.1 only.

Not covered: IPv6 (::1 is the only IPv6 loopback address), and what accept(), getsockname() and getpeername()
report (the side's own address, which is still a loopback address).

    dosplex-side-loopback.py pin --directory /sys/fs/bpf/dosplex-<id>/right [--address 127.0.0.2]
    dosplex-side-loopback.py properties --directory /sys/fs/bpf/dosplex-<id>/right
    dosplex-side-loopback.py unpin --directory /sys/fs/bpf/dosplex-<id>/right

`pin` loads the programs and pins them in the directory (root, on a bpf filesystem); `properties` prints the
systemd-run arguments that attach them; `unpin` removes the pins and the directory. A pinned program that is no
longer attached to any unit does nothing. Standard library only.
"""
import argparse
import ctypes
import errno
import ipaddress
import os
from pathlib import Path
import platform
import struct
import sys

LOOPBACK = '127.0.0.1'
DEFAULT_ADDRESS = '127.0.0.2'
BPF_FS_MAGIC = 0xCAFE4A11

# bpf(2) commands, program type and attach types (include/uapi/linux/bpf.h).
BPF_PROG_LOAD, BPF_OBJ_PIN = 5, 6
BPF_PROG_TYPE_CGROUP_SOCK_ADDR = 18
BPF_CGROUP_INET4_BIND, BPF_CGROUP_INET4_CONNECT = 8, 10
BPF_CGROUP_UDP4_SENDMSG, BPF_CGROUP_UDP4_RECVMSG = 14, 19
SYSCALL_BPF = {'x86_64': 321, 'aarch64': 280}
USER_IP4 = 4  # offsetof(struct bpf_sock_addr, user_ip4): the address in network byte order

# name: (attach type, systemd's name for it, True when the program leads into the side's address)
PROGRAMS = {
    'bind4': (BPF_CGROUP_INET4_BIND, 'bind4', True),
    'connect4': (BPF_CGROUP_INET4_CONNECT, 'connect4', True),
    'sendmsg4': (BPF_CGROUP_UDP4_SENDMSG, 'sendmsg4', True),
    'recvmsg4': (BPF_CGROUP_UDP4_RECVMSG, 'recvmsg4', False),
}


def side_address(text):
    """A loopback address a side may be given: in 127.0.0.0/8, not 127.0.0.1 itself, not the network or broadcast
    address, and outside 127.0.0.53/54, where systemd-resolved listens."""
    try:
        address = ipaddress.IPv4Address(text)
    except ValueError:
        raise ValueError(f'{text!r} is not an IPv4 address') from None
    network = ipaddress.IPv4Network('127.0.0.0/8')
    reserved = {ipaddress.IPv4Address(LOOPBACK), network.network_address, network.broadcast_address,
                ipaddress.IPv4Address('127.0.0.53'), ipaddress.IPv4Address('127.0.0.54')}
    if address not in network or address in reserved:
        raise ValueError(f'{text} cannot be a side\'s loopback address')
    return str(address)


def as_immediate(address):
    """An IPv4 address as the signed 32-bit immediate that equals a little-endian load of its network-order bytes."""
    return struct.unpack('<i', ipaddress.IPv4Address(address).packed)[0]


def instruction(code, destination=0, source=0, offset=0, immediate=0):
    return struct.pack('<BBhi', code, (source << 4) | destination, offset, immediate)


def rewrite_program(match, replacement):
    """If the context's IPv4 address is match, make it replacement; always let the call go on (return 1)."""
    return b''.join((
        instruction(0x61, destination=2, source=1, offset=USER_IP4),                  # r2 = *(u32 *)(r1 + user_ip4)
        instruction(0x56, destination=2, offset=2, immediate=as_immediate(match)),    # if (u32)r2 != match goto allow
        instruction(0xb4, destination=2, immediate=as_immediate(replacement)),        # r2 = (u32)replacement
        instruction(0x63, destination=1, source=2, offset=USER_IP4),                  # *(u32 *)(r1 + user_ip4) = r2
        instruction(0xb7, destination=0, immediate=1),                                # allow: r0 = 1
        instruction(0x95),                                                            # exit
    ))


def programs(address):
    """name -> (attach type, instructions) for a side whose loopback is address."""
    address = side_address(address)
    return {name: (attach, rewrite_program(LOOPBACK, address) if inward else rewrite_program(address, LOOPBACK))
            for name, (attach, _systemd, inward) in PROGRAMS.items()}


def systemd_properties(directory):
    """The systemd-run arguments that attach a pinned set to a unit before its first process runs."""
    directory = Path(directory)
    return [f'--property=BPFProgram={systemd}:{directory / name}' for name, (_attach, systemd, _inward) in PROGRAMS.items()]


def bpf(command, attributes):
    number = SYSCALL_BPF.get(platform.machine())
    if number is None:
        raise OSError(errno.ENOSYS, 'bpf(2) number unknown for ' + platform.machine())
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    buffer = ctypes.create_string_buffer(attributes, len(attributes))
    result = libc.syscall(ctypes.c_long(number), ctypes.c_int(command), buffer, ctypes.c_uint(len(attributes)))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def load(name, attach, code):
    """Load one program; return its descriptor. The verifier's text is in the error when it refuses."""
    instructions = ctypes.create_string_buffer(code, len(code))
    licence = ctypes.create_string_buffer(b'AGPL-3.0-or-later')
    log = ctypes.create_string_buffer(65536)
    attributes = struct.pack('<IIQQIIQII16sII', BPF_PROG_TYPE_CGROUP_SOCK_ADDR, len(code) // 8,
                             ctypes.addressof(instructions), ctypes.addressof(licence), 1, len(log),
                             ctypes.addressof(log), 0, 0, ('dosplex_' + name).encode()[:15], 0, attach)
    try:
        return bpf(BPF_PROG_LOAD, attributes)
    except OSError as error:
        raise OSError(error.errno, f'loading {name}: {error.strerror}: {log.value.decode(errors="replace").strip()}') from None


def pin(directory, address):
    """Load and pin the set. Nothing is left behind when a step fails."""
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('the pin directory must be an absolute path')
    if not on_bpf_filesystem(directory.parent):
        raise ValueError(f'{directory.parent} is not on a bpf filesystem')
    made = []
    os.mkdir(directory, 0o700)
    try:
        for name, (attach, code) in programs(address).items():
            descriptor = load(name, attach, code)
            try:
                path = ctypes.create_string_buffer(str(directory / name).encode())
                bpf(BPF_OBJ_PIN, struct.pack('<QII', ctypes.addressof(path), descriptor, 0))
                made.append(directory / name)
            finally:
                os.close(descriptor)
    except BaseException:
        for path in made:
            path.unlink(missing_ok=True)
        directory.rmdir()
        raise
    return made


def on_bpf_filesystem(path):
    """Whether path lies on a bpf filesystem, from /proc/self/mountinfo (the longest mount point that holds it)."""
    path = Path(path).resolve()
    best, kind = '', ''
    with open('/proc/self/mountinfo') as stream:
        for line in stream:
            fields = line.split()
            point, rest = fields[4], fields[fields.index('-') + 1:]
            if (str(path) == point or str(path).startswith(point.rstrip('/') + '/')) and len(point) >= len(best):
                best, kind = point, rest[0]
    return kind == 'bpf'


def unpin(directory):
    """Remove the set's pins and its directory; True when anything was there. Other files stop it."""
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        return False
    for name in PROGRAMS:
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('pin', 'properties', 'unpin'):
        command = commands.add_parser(name)
        command.add_argument('--directory', required=True, type=Path)
        if name == 'pin':
            command.add_argument('--address', default=DEFAULT_ADDRESS)
    arguments = parser.parse_args()
    try:
        if arguments.command == 'pin':
            for path in pin(arguments.directory, arguments.address):
                print(path)
        elif arguments.command == 'properties':
            print('\n'.join(systemd_properties(arguments.directory)))
        else:
            print('removed' if unpin(arguments.directory) else 'nothing to remove')
    except (OSError, ValueError) as error:
        raise SystemExit(f'dosplex-side-loopback: {error}')


if __name__ == '__main__':
    main()
