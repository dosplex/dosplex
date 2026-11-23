# Control interface

`dosplex control` is the one supported way for a user interface to ask DosPlex to do
something. It replaces ad-hoc commands and test hooks. It runs as root, like the rest of the `dosplex` program, and
answers every request with one JSON object.

```text
dosplex control ACTION [ARGUMENT...] [--json]
```

`--json` is accepted for clarity; the answer is always JSON. Standard output holds exactly one object and nothing
else. A malformed request never prints usage text: it is answered with the code `bad-request`.

## The answer

```json
{"api": 1, "ok": true, "code": "ok", "message": "DosPlex 0.2.0 is running.", "data": {}}
```

| Field | Meaning |
|---|---|
| `api` | The interface version, an integer. This page describes `1`. |
| `ok` | `true` only for the codes `ok` and `accepted`. |
| `code` | A stable machine code, listed below. Branch on this, never on `message`. |
| `message` | A sentence for a person. It can change between releases. |
| `data` | An object whose shape depends on the action. Always an object, `{}` when there is nothing to say. |

## Codes and exit statuses

The exit status of the program says the same as `code`, so a shell script can use either.

| Code | Exit | Meaning |
|---|---|---|
| `ok` | 0 | Done. |
| `accepted` | 0 | A background operation was started; follow it with `wait` or `operation`. |
| `failed` | 1 | The action failed. Read `message`. |
| `bad-request` | 2 | The action or an argument is not on the allow-list or is not valid. Nothing was done. |
| `refused` | 3 | A rule or a check refused it (preflight, untested versions, no known controller slots). |
| `not-installed` | 3 | DosPlex is not installed. |
| `interrupted` | 4 | An earlier operation was interrupted. Run `sudo dosplex repair`. |
| `busy` | 5 | Another operation is in progress. `data.operation` names it when it is a control operation. |
| `already-running` | 6 | `start` found DosPlex running. Nothing was done. |
| `not-running` | 7 | The action needs a running DosPlex (or, for `stop`, there is nothing to stop). |
| `timeout` | 8 | `wait` ran out of time. The operation goes on. |
| `not-found` | 9 | The operation or controller does not exist. |
| `permission-denied` | 10 | The program was not run as root, or the compositor refused the caller. |
| `unavailable` | 11 | A part DosPlex needs did not answer (for example a compositor that has no control socket). |

## Actions

Every action is an entry in an allow-list with typed arguments. Anything else is `bad-request`. Nothing is passed
through a shell.

### `dosplex control status`

`ok` (or `failed` when the install state is damaged; `permission-denied` when not root). `data`:

| Field | Type | Meaning |
|---|---|---|
| `program_version` | string | Version of the `dosplex` program. |
| `installed` | boolean | DosPlex is installed. |
| `version`, `commit` | string or null | The installed release; null when not installed. |
| `interrupted` | null or `{"op", "name"}` | An interrupted operation that needs `dosplex repair`. |
| `in_progress` | null or `{"op", "name"}` | An operation that is still running; wait for it to finish. |
| `running` | boolean | DosPlex started it and the session is alive. |
| `layout` | `"single"`, `"vertical"`, `"horizontal"`, `"guest"` (Guest full screen) or null | The layout in effect; null when not running. |
| `brightness` | integer 5-100 or null | The built-in screen's brightness in percent; null when not running or when the machine has no backlight. |
| `sides` | array | Empty when not running. Otherwise `{"name": "main"\|"guest", "side": "left"\|"right", "owner", "lifecycle", "mode"}`. `mode` is `"gaming"`, `"desktop"` or null. |
| `controllers` | array | `{"id", "name", "side", "connected", "routing_error"}`; see `devices`. Empty when not running. |
| `warnings` | array of strings | `"compositor-inactive"`, `"sleep-guard-inactive"`. |
| `update` | object | `{"checked": false, "available": null, "latest": null, "previous_version"}`. Checking for updates is not part of this interface; `previous_version` is the release a rollback would return to. |
| `operation` | null or `{"id", "action", "state"}` | The control operation in progress. |

### `dosplex control start` and `dosplex control stop`

Starting takes up to two minutes, so both run in the background as a transient systemd unit
(`dosplex-control-ACTION-ID`), and the command returns at once. The answer is `accepted` with
`data.operation` (an operation object, below). Before anything is launched the same checks as `dosplex start` run,
and they give these codes instead: `not-installed`, `interrupted`, `refused`, `busy`, `already-running` (start),
`not-running` (stop).

### `dosplex control operation ID`

Reads an operation. `ok` with `data.operation`, or `not-found`.

### `dosplex control wait ID [--timeout SECONDS]`

Waits for the operation to finish (default 120 seconds, at most 1800). When it has finished, the answer's `code` is
`ok` if it succeeded and the operation's own code (`failed`, `refused`, `interrupted`, ...) if it did not, with
`data.operation`. If time runs out the code is `timeout` and `data.operation` shows how far it got.

An operation object:

| Field | Meaning |
|---|---|
| `id` | 32 lowercase hex digits. |
| `action` | `"start"` or `"stop"`. |
| `state` | `"queued"`, `"running"`, `"succeeded"` or `"failed"`. |
| `code` | null until finished, then the result code. |
| `message` | null until finished, then a sentence. |
| `created_utc`, `started_utc`, `finished_utc` | Timestamps, null while not reached. |
| `steps` | `{"step", "status"}` per journal step so far (`intent`, `done`, ...). |
| `notes` | The lines the operation told the journal. |

The records live in `/var/lib/dosplex/operations/` (root only, mode 0700); the newest 20 finished ones are kept. A
service that ended without writing a result (killed, rebooted) is reported as `failed`, or `interrupted` when the
journal shows unfinished work.

### `dosplex control layout vertical|horizontal|single|guest`

Asks the compositor to change the layout. `ok` with `data.layout`, `not-running`, `unavailable` (no control socket:
an older compositor), or `permission-denied`. The compositor's control socket is described below.

### `dosplex control brightness PERCENT`

Sets the built-in screen's brightness. `PERCENT` is a whole number from 0 to 100; the compositor raises anything below
5 to 5, so the screen never goes black. `ok` with `data.brightness`, the percent in effect. The screen is shared, so
this is Main's control: it is the same setting as the Brightness buttons in Main's menu, and DosPlex remembers it across
restarts (`/var/lib/dosplex/screen/brightness.conf`, root only). Other codes: `bad-request` (not a whole number from 0
to 100), `not-running`, `unavailable` (no control socket, or a machine with no backlight to set), `permission-denied`,
`failed`. The compositor writes the kernel's backlight device, preferring type `raw`, then `firmware`, then `platform`
(the Steam Deck's `amdgpu_bl0`).

### `dosplex control devices`

`ok`, with `data`:

| Field | Meaning |
|---|---|
| `running` | DosPlex is running. |
| `inventory` | `"fresh"`, `"stale"`, `"missing"` or `"not-running"`: whether the brokers' controller list is current (they publish it every second; the compositor ignores one older than 5 seconds). |
| `receivers` | `{"node", "vendor", "product", "role"}` for each Steam Controller receiver slot found; `role` is `"main"`, `"guest"` or null. |
| `controllers` | `{"id", "name", "side", "requested_side", "connected", "active", "routing_error"}`. `side` is where the controller is routed now (`"main"`, `"guest"`, `"unassigned"`), or where it will go when it wakes. `requested_side` is the saved assignment. `routing_error` is `"not-routed"` when a connected controller is not yet where it was asked to be, else null. |

### `dosplex control assign CONTROLLER-ID main|guest|unassigned`

`CONTROLLER-ID` is 32 lowercase hex digits, as in `devices`. It writes the same record the Devices tab writes: a
confirmed entry in `controller-preferences/assignments.json` (replacing any open preview), written atomically with
mode 0600. The controller broker applies it on its next look. `ok` with `data`
`{"id", "side", "previous_side", "applied"}`; `applied` is true when the broker already shows the controller there
(the command waits up to 3 seconds), false when it does not yet, and null when the controller is asleep, in which case
the assignment is saved and takes effect when it wakes. Other codes: `not-found` (unknown controller), `not-running`,
`unavailable`, `failed` (an assignment file that is not trustworthy).

### `dosplex control forget CONTROLLER-ID`

Removes the record of a controller that is not connected: its saved owner and its number go, the number is given to the
next new controller, and its row leaves the Devices tab and `devices`. If it is switched on again later it is a new
controller (Main's, with the smallest free number). The command asks the routing service, which makes the change, and
waits up to 3 seconds for it. `ok` with `data` `{"id", "name"}`. Other codes: `refused` (the controller is connected:
turn it off or unplug it first; or it is a receiver of the session, which stays listed), `not-found` (unknown
controller, or a record that is not a regular file of root), `not-running`, `unavailable` (the routing service did not
answer, or the controller directory is not trustworthy). Bluetooth pairing is not touched.

### `dosplex control eject MOUNT`

Ejects an SD card or USB drive that holds shared games (see the shared game library in `steamos-installation.md`).
`MOUNT` is where the drive is mounted, a plain absolute path as `dosplex library status` shows it. Steam's own eject
of such a drive fails with "target is busy", because DosPlex's overlays and binds hold it. This action releases them
first (every account's library on that drive; the manifests of shared games are set aside for an account whose Steam is
closed), then asks udisks to unmount the drive, as Steam would. If anything of the drive is in use (a game from it runs)
or udisks refuses, what was released is mounted again and nothing is changed.

`ok` with `data` `{"mount", "device", "already", "manifests_wait"}`. `already` is true when the drive was not mounted.
`manifests_wait` lists the accounts whose Steam was running: their manifests of shared games stay on the drive until
that Steam is closed and DosPlex is next started or the machine restarts. Other codes: `bad-request` (not a plain
absolute path), `not-found` (no drive with a store is listed at that path), `refused` (the library is off, or something
of the drive is in use or udisks said no; `message` says what), `failed`. It takes a few seconds and runs in the
foreground. Steam's Storage page still shows its own Eject, which fails for such a drive.

## The compositor's control socket

`layout` and `brightness` reach the compositor through `control.sock` in its runtime directory (`DOSPLEX_CONTROL_SOCK` overrides the
path). It is a `SOCK_SEQPACKET` socket of mode 0600 whose peer credentials are checked: only the compositor's own user
(root on a device) is served. Requests are fixed 8-byte records, never text:

```text
request: 'D' 'P' 'X' 'C'  version  command  argument  0
reply:   'D' 'P' 'X' 'R'  version  status   value     0
```

`version` is 1. Command 1 sets the layout; the argument is 1 single, 2 vertical, 3 horizontal, 4 guest (Guest full screen: Main goes to Standby), and the reply value is
the layout then in effect. Command 2 sets the brightness; the argument is a percent from 0 to 100 and the reply value
is the percent in effect. Command 3 reads it (argument 0). Status is 0 ok, 1 bad request, 2 denied, 3 unsupported
version, 4 unavailable (the machine has no backlight; only the brightness commands answer it). The older `LAYOUT` text
datagram on `hud-ipc.sock` stays for tests and tools; it is not part of this interface.

## Versioning

* Additive changes keep `api` at 1: a new action, a new field in `data`, a new value of an enumerated string where the
  field is documented as open, a new code. Clients ignore fields they do not know.
* Breaking changes bump `api`: renaming or removing an action, a field or a code, changing a field's type or meaning,
  or changing an exit status. The old version is kept for at least one release where it can be.
* `message` is for people and is not part of the contract.
