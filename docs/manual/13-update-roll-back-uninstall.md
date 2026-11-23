# Update, roll back, uninstall

Every command needs `sudo`.

| You want to | Run |
|---|---|
| Update | `sudo /opt/dosplex/bin/dosplex update` |
| Roll back | `sudo /opt/dosplex/bin/dosplex rollback` |
| Uninstall | `sudo /opt/dosplex/bin/dosplex uninstall` |

## Update

It finds a newer signed release, checks it and installs it. The release it replaces is kept. A running DosPlex restarts on the new release. Add `--no-restart` to stay on the current one until you stop and start DosPlex.

## Roll back

Nothing is downloaded. It switches back to the kept release and restarts DosPlex if it was running. A second rollback goes forward again. It refuses if the kept release was changed or your SteamOS version is not in its tested set.

## Uninstall

It stops DosPlex, removes the boot-start unit if you turned it on, undoes the install and removes `/opt/dosplex`.

> [!WARNING]
> The `guest` account and its files stay, because they hold the second player's library and saves. To remove them, run `sudo /opt/dosplex/bin/dosplex uninstall --remove-guest`. It asks you to type Guest's home path first.

- After a plain uninstall the program is gone. Take it out of the release archive as at install, then run `sudo ./dosplex-VERSION/dosplex uninstall --remove-guest`.
- DosPlex never removes a guest account that was already there, and never touches Main's files.
- It stops with `Guest still has running processes`? If Guest's account keeps running after sign-out, run `sudo loginctl disable-linger guest`. Then `sudo /opt/dosplex/bin/dosplex repair` finishes the uninstall and keeps Guest.
- At the end it lists what is left. Remove the DosPlex shortcut in Steam's library yourself.
- Turn the shared library off before you uninstall. Its folder `/home/dosplex-library` stays: delete it when you have what you want from `conflicts`.

More: [the installation guide](../steamos-installation.md#uninstall).
