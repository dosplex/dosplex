# Releasing Dosplex

How a release is built, signed offline and published, how devices find it, and
how the release key is replaced. Players use the
[installation guide](steamos-installation.md); this page is for whoever makes a
release.

## What a release is

One release is a set of files that belong together (`VERSION` is the release
name):

| File | What it is |
| --- | --- |
| `dosplex-VERSION.tar.gz` | `bundle/` (the payload and its `manifest.json`) and the `dosplex` program beside it |
| `dosplex-VERSION.manifest.json` | the manifest, byte for byte as in the archive: the bundle manifest plus a `release` entry (a sequence number and the hash of the `dosplex` program) |
| `dosplex-VERSION.manifest.json.minisig` | the detached [minisign](https://jedisct1.github.io/minisign/) signature of that manifest |
| `dosplex-VERSION.sbom.json`, `dosplex-VERSION.THIRD-PARTY-NOTICES.md`, `dosplex-VERSION.NOTICE` | the SBOM and notices, copies of files in the archive |
| `dosplex-VERSION.index-entry.json` | this release's entry for the channel index |

The channel is one more pair, `dosplex-index.json` and `dosplex-index.json.minisig`:
the signed list of releases that `dosplex update` reads.

A device trusts a release only through the signature. `dosplex` checks the
signature of the manifest with the key that is built into the program, then every
file by the manifest, and nothing from the archive runs before that. The manifest
digest is also what state, journal and `dosplex status` call the release.

## Build

1. Build the payload in the pinned container, from a commit
   ([packaging/release-build](../packaging/release-build/README.md)):
   `packaging/release-build/build.sh --out PAYLOAD`.
2. Make the bundle from the payload with the bundle builder:
   `packaging/release-build/make-bundle.py --payload PAYLOAD --output CANDIDATE`.
   That is the bundle you test on the devices. It has the SBOM, and at its top the
   project's licence and notice and the third-party notices that were generated
   from this payload's SBOM.
3. Make the release from the bundle. The signing machine needs `minisign`:

   ```bash
   tools/release.py build CANDIDATE/bundle --out RELEASE --sign-with release.key
   ```

   The sequence number is the commit date from the SBOM (`--sequence N` to give
   another). The same inputs give the same files, including the signature.
   `--base-url https://github.com/dosplex/dosplex/releases/download/TAG/` makes the
   index entry use absolute addresses, which a channel that lists several releases
   needs. The command ends by checking its own output the way a device does, with
   the program it packed, and prints the SHA-256 of that program.

## Sign offline

The secret key never goes on the build machine or into the repository, and
`release.py` never makes a key. Without `--sign-with`, `release.py build` writes
the files unsigned and prints the command to run on the machine that holds the
key:

```bash
minisign -S -s release.key -m dosplex-VERSION.manifest.json \
  -x dosplex-VERSION.manifest.json.minisig -t "dosplex manifest VERSION"
```

The trusted comment has to be exactly that: a signature made for another kind of
file or another version is refused. Check a signed release before you upload it:

```bash
tools/release.py verify RELEASE
minisign -V -p release.pub -m RELEASE/dosplex-VERSION.manifest.json
```

## Publish

1. Upload every file of the table to the GitHub release.
2. In the release notes put the SHA-256 of the `dosplex` program that `release.py`
   printed, the manifest SHA-256, the key id of the release key and the tested
   versions. Players check that program hash by hand; never tell anyone to run an
   unchecked installer or to pipe a download into `sudo sh`.
3. Make the channel index and upload it to the newest release, so that
   `https://github.com/dosplex/dosplex/releases/latest/download/dosplex-index.json`
   finds it. List every release you still offer, each with its entry file and
   signature beside it:

   ```bash
   tools/release.py index ENTRIES/dosplex-*.index-entry.json --out CHANNEL \
     --channel preview --sign-with release.key
   ```

The index says which key signed each release. `dosplex update` skips a release
that is signed with a key it does not have, takes the newest one it can check and
that is tested for the device, and never installs one whose sequence is not above
the installed one.

## Keys

Make the key pair on the machine that will keep the secret key, and keep a copy
of it offline:

```bash
minisign -G -p release.pub -s release.key
```

`tools/release.py pin release.pub` prints the `RELEASE_KEYS` lines for
`tools/dosplex`. The project's release key (id `A349720E6A674920`) is
pinned there, so every build from this repository trusts it and nothing else. Its
secret half is kept offline and is never in this repository. A test
build can pin a key of its own with `--pin-key test.pub` instead (see below).

**Replacing the key.** A device only trusts keys that are in the `dosplex` program
it runs, so the new key travels in a release that the old key signs:

1. Make the new pair. Keep the old one.
2. Build the bridge release with both keys (`--pin-key old.pub --pin-key new.pub`)
   and sign it, and the index, with the old key. Devices update to it and now trust
   both keys.
3. Sign later releases with the new key and keep both keys pinned. Keep signing
   the index with the old key while devices may still trust only that one: they
   skip the releases signed with the new key and take the bridge.
4. When devices have passed the bridge, pin only the new key and sign the index
   with it. A device that did not pass the bridge gets "does not trust" and installs
   the newest release by hand.

There is no revocation list. If a secret key leaks, release a new key the same
way and tell players to install it by hand; devices that trust only the leaked key
stay exposed until they do.

## Trying it without the real key

Make a key for testing and build with it. The program in the archive then trusts
only that key, so it cannot be mixed up with a real release. Put the release
files and the index in one directory and point a device at it with `--channel`
(or the environment variable `DOSPLEX_CHANNEL`):

```bash
minisign -G -p test.pub -s test.key
tools/release.py build CANDIDATE/bundle --out out --pin-key test.pub --sign-with test.key
tools/release.py index out/dosplex-VERSION.index-entry.json --out index \
  --channel preview --pin-key test.pub --sign-with test.key
mkdir channel && cp out/dosplex-VERSION.* index/* channel/
```

Install the first release on the device with the `dosplex` program from its
archive (`install --release`); after that `sudo /opt/dosplex/bin/dosplex update
--channel file:///path/to/channel/dosplex-index.json` finds the later ones.

## Not covered yet

The index has no expiry, so a stale copy of it can hide a new release; it cannot
cause a downgrade. Rollback does not mark a release as bad.
