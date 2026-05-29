# Email provider setup guide

JARLIS connects via standard IMAP + SMTP, so any provider that exposes those works. Most providers with 2FA require an *app password* (or *app-specific password*) instead of your account password: generate one in your provider's account-security panel before running `python -m jarlis.setup`.

This guide covers the providers JARLIS auto-detects from your email address. For other providers, follow your provider's third-party-mail-client documentation; the credentials field expects whatever they call an "IMAP password" or "app password".

---

## Gmail / Google Workspace

| Field | Value |
|---|---|
| IMAP server | `imap.gmail.com` |
| IMAP port   | `993` (SSL/TLS) |
| SMTP server | `smtp.gmail.com` |
| SMTP port   | `587` (STARTTLS) |
| Username    | your full email address |
| Password    | a 16-character **app password** (see below) |

### Generating an app password

1. **Enable 2-Step Verification** at <https://myaccount.google.com/security>. App passwords are *only* offered when 2-Step Verification is on.
2. Go to <https://myaccount.google.com/apppasswords>.
3. Enter a name (e.g. `jarlis`) and click **Create**. Google will display a 16-character password: copy it.
4. Paste it into `python -m jarlis.setup` when prompted.

> **Heads up**: Google's *Advanced Protection Program* disables app passwords entirely. If you're enrolled, you'll need to disable Advanced Protection or use a different account for JARLIS.

> **Heads up**: As of 2025, IMAP is always-on for Gmail accounts (the old toggle in Settings → Forwarding/POP/IMAP was removed). No extra step needed.

[Google's official docs →](https://support.google.com/accounts/answer/185833)

---

## Outlook.com / Microsoft 365

| Field | Value |
|---|---|
| IMAP server | `outlook.office365.com` |
| IMAP port   | `993` (SSL/TLS) |
| SMTP server | `smtp.office365.com` |
| SMTP port   | `587` (STARTTLS) |
| Username    | your full email address |
| Password    | an **app password** (see below) |

### Generating an app password

1. Sign in at <https://myaccount.microsoft.com>.
2. **Security info** → **Add sign-in method** → **App password**.
3. (If you don't see *App password*, your tenant has it disabled: contact your administrator, or enable two-factor authentication first.)
4. Name it (e.g. `jarlis`) and click **Next**. Microsoft displays the password once: copy it.

### IMAP must be enabled at the mailbox level

Some Microsoft 365 tenants disable IMAP server-side. If JARLIS reports auth failures despite a fresh app password, ask your administrator (or check your own admin panel) to enable IMAP for the mailbox: Microsoft 365 admin center → **Active users** → your user → **Mail** → **Manage email apps** → tick **IMAP**.

> **Known issue (2025–2026)**: some users report that Microsoft app passwords intermittently stop working with IMAP even when configured correctly. If you hit this and your admin can't help, ProtonMail / Fastmail / a Gmail alias are common workarounds.

[Microsoft's official docs →](https://support.microsoft.com/en-us/account-billing/how-to-get-and-use-app-passwords-5896ed9b-4263-e681-128a-a6f2979a7944)

---

## Fastmail

| Field | Value |
|---|---|
| IMAP server | `imap.fastmail.com` |
| IMAP port   | `993` (SSL/TLS) |
| SMTP server | `smtp.fastmail.com` |
| SMTP port   | `587` (STARTTLS) |
| Username    | your full Fastmail email address |
| Password    | an **app password** (required, even without 2FA) |

### Generating an app password

1. Log in at <https://app.fastmail.com>.
2. **Settings** → **Privacy & Security** → **Connected apps & API tokens** → **Manage app passwords and access**.
3. Click **New app password**. Name it (e.g. `jarlis`) and choose **Mail, Contacts & Calendars** (the default). Click **Generate password**.
4. Copy the displayed password.

> Fastmail requires app passwords for *every* IMAP/SMTP connection, even if you don't have two-step verification enabled. Your normal login password will not work.

[Fastmail's official docs →](https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords)

---

## iCloud Mail

| Field | Value |
|---|---|
| IMAP server | `imap.mail.me.com` |
| IMAP port   | `993` (SSL/TLS) |
| SMTP server | `smtp.mail.me.com` |
| SMTP port   | `587` (STARTTLS) |
| Username    | your full iCloud email address |
| Password    | an **app-specific password** (see below) |

### Generating an app-specific password

1. Make sure two-factor authentication is enabled on your Apple Account.
2. Sign in at <https://account.apple.com> → **Sign-In and Security** → **App-Specific Passwords**.
3. Click **+** (or **Generate an app-specific password**), enter a label (e.g. `jarlis`), and confirm with your Apple Account password.
4. Copy the generated password: Apple won't show it again. (You can revoke and regenerate if you lose it.)

[Apple's official docs →](https://support.apple.com/en-us/102654)

---

## Yahoo Mail

| Field | Value |
|---|---|
| IMAP server | `imap.mail.yahoo.com` |
| IMAP port   | `993` (SSL/TLS) |
| SMTP server | `smtp.mail.yahoo.com` |
| SMTP port   | **`465`** (SSL: *not* 587) |
| Username    | your full Yahoo email address |
| Password    | an **app password** (required when 2-step verification is on) |

### Generating an app password

1. Open Yahoo Account Security at <https://login.yahoo.com/account/security>.
2. Scroll to **Other ways to sign in** → **Generate and manage app passwords**.
3. Choose **Other App** (or **Outlook Desktop**), name it (e.g. `jarlis`), and click **Generate**.
4. Copy the password.

> **Tip**: Yahoo sometimes blocks app-password generation from new browsers / Incognito sessions. Use a browser you've signed into Yahoo with regularly if you hit "Something went wrong".

[Yahoo's official docs →](https://help.yahoo.com/kb/SLN15241.html)

---

## ProtonMail (via Proton Mail Bridge)

ProtonMail doesn't expose direct IMAP/SMTP: you need the **Proton Mail Bridge** app running on the same machine as JARLIS.

| Field | Value |
|---|---|
| IMAP server | `127.0.0.1` |
| IMAP port   | `1143` (STARTTLS) |
| SMTP server | `127.0.0.1` |
| SMTP port   | `1025` (STARTTLS) |
| Username    | your Proton email address |
| Password    | the **Bridge password** (NOT your Proton account password) |

### Setup

1. Install Proton Mail Bridge from <https://proton.me/mail/bridge>. (Requires a *paid* Proton plan.)
2. Sign in to Bridge with your Proton credentials.
3. Bridge generates a unique IMAP/SMTP password. Copy it from the Bridge UI.
4. Keep Bridge running in the background: JARLIS can only fetch/send while Bridge is up.

> **Important**: Use the Bridge-generated password in `jarlis.setup`, not your normal Proton password. The latter will never work.

[Proton's official docs →](https://proton.me/support/comprehensive-guide-to-bridge-settings)

---

## Self-hosted IMAP / other providers

If your provider isn't auto-detected by `jarlis.setup`, just enter the host/port manually when prompted. Anything that supports standards-compliant IMAP + SMTP works: Dovecot, Postfix, Mailcow, mailu, custom corporate servers, etc.

If your server uses a **non-standard port** or a **non-TLS** configuration, edit `config.toml` after running `jarlis.setup` and adjust the `[imap]` and `[smtp]` sections directly. JARLIS uses Python's `imaplib.IMAP4_SSL` (which assumes TLS); for plain-text or STARTTLS you may need to edit `src/jarlis/imap_fetch.py`: open an issue if your provider needs first-class support.

---

## Where the password is stored

After you finish `python -m jarlis.setup`, your IMAP/SMTP password lives in your **OS keychain**:

| OS | Keychain |
|---|---|
| macOS   | Keychain Access → **login** keychain |
| Linux   | Secret Service (GNOME Keyring / KWallet) |
| Windows | Credential Manager → **Windows Credentials** |

The service is `jarlis`; the account is your IMAP username. You can inspect, rotate, or revoke the entry in your OS's normal keychain UI any time.
