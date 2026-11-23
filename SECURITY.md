# Security policy

## Reporting a vulnerability

Report a vulnerability privately by email to <roy@dosplex.org>. Do not
open a public issue, and do not post the details anywhere, until the problem has
been fixed.

Please include:

- what you found and what it lets someone do;
- the DosPlex version or commit, your device and your SteamOS version;
- the steps that reproduce it, and whether they need the second player's account;
- log lines or a short test program, if you have them.

Never send passwords, Steam Guard codes, sign-in QR codes or anyone's account data.

## What is in scope

DosPlex is an alpha. Only the current release is in scope: the latest release and
the current source in this repository. Earlier releases are not maintained.

DosPlex is not a security sandbox between the two players. The known gaps are
listed under "Security and isolation" in [docs/limitations.md](docs/limitations.md);
you do not need to report those again. A problem in how DosPlex uses Steam, SteamOS,
Gamescope, wlroots or another dependency is in scope. A problem in the dependency
itself belongs with its maintainers.

## What to expect

DosPlex is maintained by one person, so there is no guaranteed response time.
There is no bug bounty.
