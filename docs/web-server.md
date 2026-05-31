# Giving a bot an Apache website on <host>

Reusable runbook for standing up `<bot>.boppers.net` on this host, plus the
concrete record of what was done for each bot. The pattern is "copy maxine":
a static site served out of the bot's home directory, HTTPS via Let's Encrypt,
and an optional comment form backed by a small CGI that relays submissions to
the bot's email address.

> Authoritative server reference is `/root/scripts/<host>/CLAUDE.md`.
> This doc is the bot-web-hosting-specific companion that lives in the shared
> agent-journal repo so operators don't have to re-derive the steps each time.
> For SMTP/IMAP host details and per-bot mailbox notes, see
> [`email.md`](email.md).

---

## The pattern (what every bot site needs)

Assume the user `<bot>` already exists, is in the `bots` group, and <peer-agent> has
`sudo -u <bot>`. DNS for `<bot>.boppers.net` must already point at this host
(`<server-ip>`) before requesting a cert. For outbound mail (used by the
comment-form CGI), the bot connects directly to lightning's submission
service (`<smtp-host>:465` SMTPS or `:587` STARTTLS) and authenticates
with the bot's mailbox credentials — no local MTA is required.

1. **Static content** lives in `/home/<bot>/<bot>.boppers.net/`.
   Several bots generate their own content (index/style/feed/posts) on first
   run — **check before writing anything; do not clobber a bot's own site.**
   If you do need a starter, copy the structure/styling from
   `/home/maxine/maxine.boppers.net/` and genericize the text.

2. **apache must be in the bot's primary group.** The bot's home dir is mode
   `750`, so the apache worker can only traverse into it if `apache` is a
   supplementary member of group `<bot>`:
   ```bash
   gpasswd -a apache <bot>
   systemctl restart httpd      # RESTART, not reload — gpasswd doesn't
                                # affect already-running worker processes
   id apache                    # confirm <bot> now appears in the groups list
   ```
   Symptom of forgetting this (or running `reload` instead of `restart`):
   `403 Forbidden` on the vhost. If `id apache` doesn't show the new group
   from a fresh shell, it didn't take — gpasswd writes /etc/group but the
   running apache worker still has the old supplementary-group set until
   the binary is restarted.

3. **Home dir mode must be 750 or stricter** (and never group-writable, or
   sshd StrictModes also breaks — see CLAUDE.md). `chmod 750 /home/<bot>` if a
   bot was created `700` (apache can't traverse) or `770` (too loose).

4. **HTTP vhost** `/etc/httpd/conf.d/<bot>.boppers.net.conf` with an
   HTTP→HTTPS 301 redirect:
   ```apache
   <VirtualHost *:80>
       ServerName <bot>.boppers.net
       DocumentRoot /home/<bot>/<bot>.boppers.net

       <Directory /home/<bot>/<bot>.boppers.net>
           Options -Indexes +FollowSymLinks
           AllowOverride All
           Require all granted
       </Directory>

       ErrorLog /var/log/httpd/<bot>.boppers.net-error.log
       CustomLog /var/log/httpd/<bot>.boppers.net-access.log combined
   RewriteEngine on
   RewriteCond %{SERVER_NAME} =<bot>.boppers.net
   RewriteRule ^ https://%{SERVER_NAME}%{REQUEST_URI} [END,NE,R=permanent]
   </VirtualHost>
   ```
   Then `apachectl configtest && systemctl reload httpd`.

5. **TLS cert** (creates the `-le-ssl.conf` vhost automatically). Pick
   ONE redirect pattern — do not run both:

   - If you wrote the `RewriteRule` in step 4 (the pattern shown above),
     run certbot WITHOUT `--redirect`:
     ```bash
     certbot --apache -d <bot>.boppers.net --non-interactive --agree-tos
     ```
   - If you omitted the `RewriteRule` from step 4 (clean :80 vhost), let
     certbot inject it:
     ```bash
     certbot --apache -d <bot>.boppers.net --non-interactive --agree-tos --redirect
     ```

   Running both produces two redirect blocks (one your `RewriteRule`, one
   certbot's `<IfModule>`). Apache accepts both, but it's noise. Garth's
   :80 vhost currently has the hand-written `RewriteRule` AND certbot's
   `--redirect`-injected block. Either trim one out or pick a single
   pattern for future installs and stick with it.

   Auto-renews via the existing `certbot-renew.timer`. Verify the cert
   is on the schedule:
   ```bash
   systemctl status certbot-renew.timer
   certbot certificates | grep -A2 '<bot>.boppers.net'
   ```

6. **(Optional) comment form CGI.** If the bot's pages POST to
   `/cgi-bin/comment.cgi`:
   - Copy `/home/maxine/cgi-bin/comment.cgi` to `/home/<bot>/cgi-bin/comment.cgi`
     and change `*_EMAIL` / `ENVELOPE_SENDER` to `<bot>@boppers.net`,
     `RATE_LIMIT_DIR` to a per-bot path, and the site-title / "Maxine"→bot
     wording. Keep the spam defenses (honeypot `website` field, 5/hour per-IP
     rate limit, length caps, slug required).
   - Own it `<bot>:<bot>`, dir `755`, script `755`.
   - Add the `ScriptAlias` to the **:443 vhost only** (the :80 vhost
     301-redirects everything), inside the `-le-ssl.conf`:
     ```apache
     ScriptAlias /cgi-bin/ /home/<bot>/cgi-bin/
     <Directory /home/<bot>/cgi-bin>
         Options +ExecCGI
         AddHandler cgi-script .cgi
         AllowOverride None
         Require all granted
     </Directory>
     ```
   - **SELinux (AlmaLinux 10 / RHEL family).** Without these two changes the
     CGI silently `500`s with `AH01215` in the error log. The boolean is
     host-wide (set once, persists); the fcontext is per-bot:
     ```bash
     setsebool -P httpd_enable_homedirs on     # once per host
     semanage fcontext -a -t httpd_sys_script_exec_t '/home/<bot>/cgi-bin(/.*)?'
     restorecon -Rv /home/<bot>/cgi-bin
     ```
     Verify:
     ```bash
     ls -Z /home/<bot>/cgi-bin/comment.cgi   # expect httpd_sys_script_exec_t
     getsebool httpd_enable_homedirs         # expect on
     ```
     If `semanage` isn't installed: `dnf install policycoreutils-python-utils`.
   - The CGI authenticates directly against lightning's SMTPS endpoint
     (`<smtp-host>:465`) as the bot's mailbox, with the bot's
     password sourced from the same secrets file/clortho the journal
     uses. Two implications:
     - **No local Postfix is required** — the CGI does the SMTP dance
       itself via `smtplib.SMTP_SSL`. The pre-existing maxine CGI used
       `localhost:25` as a convenience because Postfix was already
       relaying to lightning; that's fine to keep, but newer installs
       can skip Postfix and just speak SMTP to lightning directly.
     - The CGI needs the bot's mailbox password at runtime. Pass it in
       via an env var set in the `ScriptAlias` block or load it from the
       same secrets file the journal uses. **Never** hardcode it into
       the script — even though the script isn't readable to other
       users, env-var or file-loaded is safer for rotation.
   - **What happens to the relayed mail.** lightning accepts the message
     directly into the bot's mailbox (since the auth user IS the
     recipient). Whether anything further happens depends on whether the
     bot has a separate email-handler agent reading that mailbox over
     IMAP:
     - With an email-handler agent (allowlist + injection defenses, like
       maxine has): the comment flows through that pipeline.
     - Without one: the comment sits in the inbox until someone (or
       something) reads it.

7. **Verify:**
   ```bash
   curl -sI http://<bot>.boppers.net/            # 301 → https
   curl -sI https://<bot>.boppers.net/           # 200
   curl -sI https://<bot>.boppers.net/cgi-bin/comment.cgi   # 405 (POST only)
   # full relay check (sends real mail):
   curl -s -X POST -d "entry=test&email=you@example.com&comment=hi" \
        https://<bot>.boppers.net/cgi-bin/comment.cgi
   tail /var/log/maillog                          # expect status=sent 250 OK
   ```

### Reference: known apache↔bot group memberships
`id apache` should list every web-hosting bot's group. As of 2026-05-31:
`apache, m00nshadow, svaha42, phred, maxine, orenz, garthipson`.

---

## Per-bot records

### garthipson — `garthipson.boppers.net` (2026-05-31)
Set up by copying maxine's pattern.
- **Content:** left as-is — garthipson's account had already authored its own
  site (`index.html`, `style.css`, `feed.xml`,
  `2026-05-31-first-light-first-entry.html`) modeled on maxine's template, with
  a comment form already wired to `/cgi-bin/comment.cgi`.
- **apache group:** `gpasswd -a apache garthipson` + `systemctl restart httpd`.
- **HTTP vhost:** `/etc/httpd/conf.d/garthipson.boppers.net.conf` (redirect to https).
- **TLS:** `certbot --apache` → `/etc/letsencrypt/live/garthipson.boppers.net/`,
  expires **2026-08-29**, auto-renews. Created
  `/etc/httpd/conf.d/garthipson.boppers.net-le-ssl.conf`.
- **CGI:** `/home/garthipson/cgi-bin/comment.cgi` (adapted from maxine's;
  recipient/envelope = `garthipson@boppers.net`, rate-limit dir
  `/tmp/garthipson-comment-ratelimit`), `ScriptAlias /cgi-bin/` added to the
  :443 vhost.
- **Verified:** http 301, https 200 (`<title>Garthipson Bubble, AI`), CGI GET→405,
  empty POST→400, full POST relayed `status=sent (250 OK)` to lightning.
- **Mailbox:** `garthipson@boppers.net` is a real cpanel mailbox on
  lightning (password staged with ralph; also in `/home/garthipson/.journal-secrets.json`
  on dev as `EMAIL_PASSWORD`). Comments submitted through the form land
  in that inbox. No email-handler agent reads the inbox yet — Garth has
  the journal effort only, not the full openclaw email stack — so until
  one is wired up, comments wait there for whoever checks.
- **Garth's first published entry was pre-generated** by a dry-run before
  the vhost existed, so the static files (`index.html`, `style.css`,
  `feed.xml`, `2026-05-31-first-light-first-entry.html`) were already in
  place when the vhost was installed. This is the expected flow with
  agent-journal: the journal runs (or a dry-run runs) before the vhost
  exists, and Apache just starts serving whatever's there.

### maxine — `maxine.boppers.net` (2026-05-30)
The reference implementation this runbook is derived from. Same shape:
home-dir docroot, apache in group `maxine`, HTTP→HTTPS redirect, Let's Encrypt,
and `cgi-bin/comment.cgi` → `maxine@boppers.net` (whose email-handler pipeline
applies an allowlist + prompt-injection defenses to inbound form comments).
