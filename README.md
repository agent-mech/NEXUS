# NEXUS

An invitation-only chat platform with its own server.

No ads. No tracking. No corporation in the middle. Nothing here is funded
by watching you — there is no business model that needs your attention,
your contacts or your messages.

This repository holds the clients. The server is ours, and it stays ours.

---

## Where your data lives

**On the NEXUS server. Nowhere else.**

NEXUS runs on its own dedicated server, operated directly by the people
who run NEXUS. Not rented space inside an advertising company, not a
platform that reserves the right to change what it does with your
messages next quarter. There is no third party in the path, so there is
no third party to trust.

- **Messages, accounts and conversations** are held in a single encrypted
  store on that server. Strong, modern, well-established encryption — the
  kind that is public, scrutinised and relied on everywhere, not
  something invented here. It is encrypted at rest, so the stored data is
  unreadable to anyone who obtains the disk.
- **Unlocking it takes a passphrase** that exists in one person's head
  and is held nowhere on the machine. Without it the store is a file of
  noise. There is no recovery path, because a recovery path is also a way
  in.
- **Your identity is a passkey**, generated when your account is created
  and shown exactly once. It is never kept in any form that can be read
  back — not by anyone, at any level. Lose it and the account is gone.
  That is deliberate: it is also why nobody can be talked into handing
  your account to someone claiming to be you.
- **Everything between you and the server is encrypted** in transit.
- **Nothing is phoned home.** No analytics, no crash reporting, no
  telemetry, no advertising identifiers, no fonts or scripts loaded from
  anybody else's servers. Nothing about what you do here is measured and
  sent somewhere.
- **There is no profile of you.** No interest graph, no engagement
  history, no shadow copy assembled for anyone. The server holds what is
  needed to run a chat platform and nothing beyond it.
- **Deleting means deleting.** Remove an account and it goes. There is no
  archive kept for analytics and no copy in a warehouse somewhere,
  because there is no warehouse.

---

## Invitation only

There is no public sign-up, no "find people you may know", no discovery
feed. A new account needs a code from somebody already here, and a code
works once. Membership is a deliberate list rather than whoever happened
to find the place.

Ranks exist for running it — helper, moderator, admin, owner — and each
one stops at its own level. Nobody promotes themselves past the person
who let them in.

---

## Two ways to use it

**In a browser.** The server hosts the web client itself. Open the
address you were given and sign in; nothing to install.

**In a terminal.** A full-screen client with channels, direct messages
and the complete command set. Runs anywhere Python does.

A third way exists for hardware: a bridge lets a device post into a
channel and take messages back out — readings in, commands out — over the
same connection as everybody else, with a key that can be revoked on its
own.

One session per account at a time. Signing in somewhere new signs the
previous one out, so a session left open on a borrowed machine ends the
moment you sign in at home.

---

## Getting in

You need two things from whoever invited you: **the server address** and
**an invite code**. Codes work once.

### Browser

Open the address. Choose **I have an invite**, enter the code and pick a
username.

You will be shown a passkey once, and only once. Save it before you
continue — it is how you sign in from then on, and nobody can recover it
for you.

### Terminal

Requires Python 3.11 or newer.

```bash
pip install -r requirements.txt
python3 nexus.py --host <server-address>
```

Sign in with your passkey, or register with your invite code.

If the server uses its own certificate authority, trust it explicitly:

```bash
python3 nexus.py --host <server-address> --cafile certs/nexus-ca.pem
```

That is needed on macOS in particular, where Python does not read the
system keychain. Without it the client still connects and is still
encrypted — it remembers the server's certificate the first time and
refuses to continue if it ever changes.

### Using it

Type `/help` once you are signed in. The commands are identical in the
browser and the terminal.

---

## Said plainly

**This is not end-to-end encrypted.** Data is encrypted at rest and in
transit, but the server decrypts messages in order to store, search and
moderate them — which means whoever operates the server can read what is
on it. That is true of almost every chat platform; the difference here is
who that is, and that nobody is monetising it. It is not the guarantee
Signal makes, and it is not presented as one.

**Nobody has audited this.** It is built carefully, using standard
components rather than home-made cryptography, but it has not been
through a formal security review. Good for a group, a club, a team, a
family. Not the right tool for someone whose safety depends on it.

---

## Why

Most chat runs inside a company that is paid to study what you do there,
under terms you did not negotiate and can expect to change. The
alternative is not complicated: a server, a list of people who were
invited, and nobody in between selling anything.
