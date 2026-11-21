# Update, roll back, uninstall

Every command needs `sudo` and runs from `/opt/dosplex/bin/dosplex`.

**Update.** Run `sudo /opt/dosplex/bin/dosplex update`. It looks for a newer signed release, checks the signature and every file, and installs it. The release it replaces is kept; the one before that is deleted. If Dosplex was running, it stops and restarts on the new release with the same layout. Use `--no-restart` to stay on the current release until you stop and start manually.

**Roll back.** Run `sudo /opt/dosplex/bin/dosplex rollback`. Nothing is downloaded. It switches back to the kept release and restarts Dosplex if it was running. The release you left is kept, so a second rollback goes forward again. It refuses if the kept release was changed or if your SteamOS version is not in its tested set.

**Uninstall.** Run `sudo /opt/dosplex/bin/dosplex uninstall`. It stops Dosplex, removes the boot-start unit if you turned that on, undoes the install from the journal, and removes `/opt/dosplex`. The `guest` account and its files are kept because they hold the second player's Steam library and saves. To remove them as well, run `sudo /opt/dosplex/bin/dosplex uninstall --remove-guest`. It prints the home path and asks you to type it before deleting anything. After a plain uninstall the installed program is gone; to remove Guest then, take the `dosplex` program out of the release archive as you did for the install and run `sudo ./dosplex-VERSION/dosplex uninstall --remove-guest`. Dosplex never removes a guest account that was already on the device, and never touches Main's files.

If you used the shared game library, turn it off before you uninstall (see the chapter on experimental features). Its folder, `/home/dosplex-library`, stays after the uninstall. It holds no games any more, only what the library set aside instead of overwriting, in `conflicts`. Nothing of Dosplex needs it; delete it when you have what you want from it.
