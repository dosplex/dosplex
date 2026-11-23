# Contributing to DosPlex

Thank you for helping. Bug reports, game compatibility reports, documentation fixes and code
are all welcome. Please read the [code of conduct](CODE_OF_CONDUCT.md) first.

## Build and test

```bash
meson setup build compositor && meson compile -C build   # the compositor
./tests/source-check.sh                                  # the checks that run without a Steam Deck
python3 tests/run.py --tier fast                         # lint and unit tests, about 15 seconds
python3 tests/run.py --list                              # every test, with its tier and needs
```

Run `./tests/source-check.sh` before you open a pull request. Tests that need real hardware
(controllers, a display, Steam) are marked as such in `python3 tests/run.py --list`. If your change
touches that code and you cannot run those tests, say so in the pull request.

Please add a test for any bug you fix. [docs/testing.md](docs/testing.md) explains what counts as
evidence for a change in behavior.

## Licensing and sign-off (DCO)

DosPlex code is licensed under the [GNU Affero General Public License v3.0 or later](LICENSE) and the
documentation under [CC BY 4.0](LICENSE-DOCS.md). Your contribution comes in under the same terms as
the file you change (AGPL-3.0-or-later for code). You keep the copyright of your contribution.

### Contributor license agreement

Before your first pull request is merged, you sign the DosPlex Individual Contributor License Agreement
([CLA.md](CLA.md)). It is a draft based on the Apache ICLA; no lawyer has reviewed it yet, and it may change. In short:

- You keep your copyright. You do not assign it.
- You grant RoyÅke (Roy-Åke Martinsson) a license to use, relicense and commercially sublicense your
  contribution. RoyÅke offers commercial licenses for DosPlex (see [LICENSING.md](LICENSING.md)), and this
  grant is what lets your contribution be part of them.
- RoyÅke promises that every accepted contribution stays available under AGPL-3.0-or-later or another
  OSI-approved license.
- Ordinary contributions are unpaid. Commissioned or exclusive work, and any further rights, are agreed and
  paid separately.

A bot checks every pull request. If you have not signed yet, it asks you to post the one-line comment that
[CLA.md](CLA.md) gives. You sign once; it covers your later pull requests too. The CLA comes in addition to
the DCO sign-off below, not instead of it.

To say that you have the right to contribute it, sign off every commit with the
[Developer Certificate of Origin](https://developercertificate.org/) 1.1:

```bash
git commit -s -m "Describe the change"
```

This adds a line to the commit message with your real name and email address:

```text
Signed-off-by: Your Name <your email address>
```

The line means you agree to this text:

```text
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

Forgot the sign-off? Run `git commit --amend -s` for the last commit, or
`git rebase --signoff <base>` for several.

## Source file headers

Every source file starts with two comment lines:

```text
SPDX-License-Identifier: AGPL-3.0-or-later
Copyright 2026 RoyÅke (Roy-Åke Martinsson)
```

Copy them, in the comment style of the language, from a neighbouring file. Do not
add a header to code you copied from another project; keep its own license notice and list it in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Reporting game compatibility

Open an issue on the [issue tracker](https://github.com/dosplex/dosplex/issues) and use the list in
[docs/game-test-notes.md](docs/game-test-notes.md): the game and its Steam app id, your device and
SteamOS version, the DosPlex version, what you did, what happened, and a log or short video if
you have one. Never include passwords, Steam Guard codes or sign-in QR codes.

## Security problems and the name

Report security problems privately, as [SECURITY.md](SECURITY.md) describes. How the name
"DosPlex" may be used is in [TRADEMARKS.md](TRADEMARKS.md). The project lead makes the final
decisions ([MAINTAINERS.md](MAINTAINERS.md)).
