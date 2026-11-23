#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
# virgl-enable-qemu-args.sh
# Add these QEMU args to enable virgl GPU acceleration for the DosPlex VM.
# Requires host GPU with OpenGL 3+ support.

# These args should be ADDED to your existing QEMU command (or replace virtio-vga):
#
#   -device virtio-gpu-pci,virgl=on,blob=true \
#   -display gtk,gl=on \
#
# And REMOVE or COMMENT OUT:
#   -device virtio-vga \

# Example complete qemu_args section with virgl:
#
# qemu_args=(
#   qemu-system-x86_64
#   -name dosplex-dev
#   -machine q35,accel="$accel"
#   -cpu max
#   -smp "$cpu_count"
#   -m "$memory_mb"
#   -display gtk,gl=on
#   -device virtio-gpu-pci,virgl=on,blob=true
#   -device qemu-xhci
#   -device virtio-keyboard-pci
#   -device virtio-mouse-pci
#   -device ich9-intel-hda
#   -device hda-duplex
#   -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_forward_port}-:22
#   -device virtio-net-pci,netdev=net0
#   -drive if=pflash,format=raw,readonly=on,file="$firmware_code"
#   -drive if=pflash,format=raw,file="$firmware_vars"
#   -drive file="$disk_path",if=virtio,format=qcow2
#   -boot menu=on
# )

echo "To enable virgl GPU acceleration in your VM:"
echo ""
echo "1. Edit vm/run-dev-vm.sh"
echo "2. Change -device virtio-vga to:"
echo "   -device virtio-gpu-pci,virgl=on,blob=true"
echo "3. Change -display gtk,gl=off to:"
echo "   -display gtk,gl=on"
echo ""
echo "Requirements:"
echo "  - Host GPU with OpenGL 3+ support"
echo "  - QEMU built with virgl support (--enable-opengl)"
echo "  - Guest with mesa virgl driver (virglrenderer)"
echo ""
echo "After changes, in guest run:"
echo "  cat /sys/kernel/debug/dri/0/virtio-gpu-features | grep virgl"
echo "  should show: virgl            : yes"
