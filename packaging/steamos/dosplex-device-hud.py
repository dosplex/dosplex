#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 RoyÅke (Roy-Åke Martinsson)
import sys
import os
import socket
import glob
import json
import subprocess

# Try to import Qt bindings
try:
    from PySide6.QtCore import QObject, Slot as pyqtSlot, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
except ImportError:
    try:
        from PyQt5.QtCore import QObject, pyqtSlot, QUrl
        from PyQt5.QtGui import QGuiApplication
        from PyQt5.QtQml import QQmlApplicationEngine
    except ImportError:
        print("Error: PySide6 or PyQt5 is required to run this HUD.")
        sys.exit(1)

HUD_IPC_SOCK = os.environ.get(
    "DOSPLEX_HUD_IPC_SOCK",
    os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/run/dosplex"), "hud-ipc.sock"),
)
AUDIO_ROUTER = os.environ.get("DOSPLEX_AUDIO_ROUTER", "/usr/local/bin/dosplex-audio-router.py")
SWITCH_MODE_SCRIPT = "/usr/local/bin/dosplex-switch-mode.sh"

class Backend(QObject):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    @pyqtSlot(str, str, str)
    def assignDevice(self, device_id, device_type, side):
        print(f"Backend: Assigning {device_id} ({device_type}) to {side}")
        if device_type == 'input':
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                route_side = "follow" if side in {"free", "follow"} else side
                cmd = f'ROUTE {device_id} {route_side}'
                if os.path.exists(HUD_IPC_SOCK):
                    sock.sendto(cmd.encode(), HUD_IPC_SOCK)
                    print(f"Sent IPC: {cmd}")
                else:
                    print(f"Warning: IPC socket {HUD_IPC_SOCK} not found.")
                sock.close()
            except Exception as e:
                print(f"Error sending to IPC: {e}")
        elif device_type == 'audio':
            try:
                result = subprocess.run(
                    [AUDIO_ROUTER, "assign", device_id, side],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                if result.stdout:
                    print(result.stdout.strip())
                if result.stderr:
                    print(result.stderr.strip())
            except Exception as e:
                print(f"Error routing audio: {e}")

    @pyqtSlot(str, str)
    def setMode(self, side, mode):
        print(f"Backend: Setting {side} to {mode} mode")
        try:
            # Call the switch mode script
            subprocess.run([SWITCH_MODE_SCRIPT, side, mode], check=False)
        except Exception as e:
            print(f"Error switching mode: {e}")

    @pyqtSlot()
    def quit(self):
        print("Quitting HUD...")
        QGuiApplication.quit()

def is_deck_onboard_name(name):
    lowered = name.lower()
    return "steam deck" in lowered


def is_builtin_touchscreen_name(name):
    return "FTS3528:00 2808:1015" in name and "UNKNOWN" not in name


def is_internal_input_name(name):
    internal = [
        "Power Button",
        "Sleep Button",
        "Video Bus",
        "Lid Switch",
        "Headset Jack",
        "PC Speaker",
        "AT Translated Set 2 keyboard",
        "steamos-manager",
        "DosPlex passthrough steamos-manager",
        "FTS3528:00 2808:1015 UNKNOWN",
        "HD-Audio Generic HDMI/DP",
    ]
    return any(token in name for token in internal)


def friendly_input_name(name):
    if is_deck_onboard_name(name):
        return "Deck onboard controls"
    if is_builtin_touchscreen_name(name):
        return "Touchscreen"
    return name


def input_icon(name):
    lowered = name.lower()
    if is_deck_onboard_name(name):
        return "gamepad"
    if "mouse" in lowered or "trackpad" in lowered or "touchpad" in lowered:
        return "mouse"
    if "controller" in lowered or "gamepad" in lowered:
        return "gamepad"
    if "touch" in lowered:
        return "touch"
    return "keyboard"

def get_input_devices(policy):
    devices = []
    # Search for input devices in /sys/class/input/event*/device/name
    seen_names = set()
    ignored_keywords = []
    try:
        # Sort to keep order consistent
        name_files = sorted(glob.glob("/sys/class/input/event*/device/name"))
        for name_file in name_files:
            try:
                with open(name_file, 'r') as f:
                    name = f.read().strip()
                    device_id = "__dosplex_deck_controls__" if is_deck_onboard_name(name) else name
                    if name and device_id not in seen_names:
                        if is_internal_input_name(name):
                            continue
                        
                        side = policy.get(device_id, policy.get(name, "left"))
                        devices.append({
                            "deviceId": device_id,
                            "name": friendly_input_name(name),
                            "deviceType": "input",
                            "side": side,
                            "icon": input_icon(name),
                        })
                        seen_names.add(device_id)
            except Exception:
                continue
    except Exception as e:
        print(f"Error reading devices: {e}")

    # Add Input Leap as a virtual device if it's in the policy
    remote_side = policy.get("__dosplex_remote_input__", "follow")
    devices.append({
        "deviceId": "__dosplex_remote_input__",
        "name": "Input Leap",
        "deviceType": "input",
        "side": remote_side,
        "icon": "input-leap",
    })

    return devices

def get_audio_devices():
    try:
        if not os.path.exists(AUDIO_ROUTER):
            return []
        result = subprocess.run(
            [AUDIO_ROUTER, "list-sinks"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            if result.stderr:
                print(result.stderr.strip())
            return []
        sinks = json.loads(result.stdout or "[]")
    except Exception as e:
        print(f"Error reading audio sinks: {e}")
        return []

    devices = []
    for sink in sinks:
        sink_id = sink.get("id")
        if not sink_id:
            continue
        devices.append({
            "deviceId": sink_id,
            "name": sink.get("name") or sink_id,
            "deviceType": "audio",
            "side": "free",
            "icon": sink.get("icon") or "speaker",
        })
    return devices

def get_modes():
    modes = {}
    for side in ["left", "right"]:
        mode_file = f"/var/lib/dosplex/sides/{side}/mode"
        try:
            if os.path.exists(mode_file):
                with open(mode_file, 'r') as f:
                    modes[side] = f.read().strip()
            else:
                modes[side] = "gaming"
        except Exception:
            modes[side] = "gaming"
    return modes

def get_roles():
    """Read which side is primary/guest from compositor state."""
    roles = {"left": "Main", "right": "Guest"}
    state_file = "/run/dosplex/state.json"
    try:
        if os.path.exists(state_file):
            with open(state_file, 'r') as f:
                state = json.load(f)
            # Check if guest_physical_side tells us guest is on the left
            guest_side = state.get("guest_physical_side", "right")
            if guest_side == "left":
                roles = {"left": "Guest", "right": "Main"}
    except Exception:
        pass
    return roles

def main():
    # Set environment variables for Wayland if not set
    if "XDG_RUNTIME_DIR" not in os.environ:
        os.environ["XDG_RUNTIME_DIR"] = "/run/dosplex"

    # Load input policy
    policy = {}
    policy_path = "/var/lib/dosplex/input-policy.json"
    if os.path.exists(policy_path):
        try:
            with open(policy_path, "r") as f:
                data = json.load(f)
                policy = data.get("devices", {})
        except Exception as e:
            print(f"Error loading input policy: {e}")
    
    QGuiApplication.setDesktopFileName("dosplex-hud")
    app = QGuiApplication(sys.argv)
    app.setApplicationName("dosplex-hud")
    engine = QQmlApplicationEngine()
    
    # Expose backend to QML
    backend = Backend(engine)
    engine.rootContext().setContextProperty("backend", backend)
    
    # Load QML
    qml_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dosplex-device-hud.qml")
    engine.load(QUrl.fromLocalFile(qml_file))
    
    if not engine.rootObjects():
        print(f"Error: Could not load QML from {qml_file}")
        sys.exit(-1)
        
    root_obj = engine.rootObjects()[0]
    
    # Initialize device list
    devices = get_input_devices(policy) + get_audio_devices()
    print(f"Found {len(devices)} devices.")
    
    # Call QML init functions
    root_obj.initDevices(devices)
    
    # Initialize modes
    modes = get_modes()
    if hasattr(root_obj, 'initModes'):
        root_obj.initModes(modes['left'], modes['right'])

    # Initialize roles (primary/guest position)
    roles = get_roles()
    if hasattr(root_obj, 'initRoles'):
        root_obj.initRoles(roles['left'], roles['right'])
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
