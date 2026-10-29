---
name: data.box_login
description: Make one node's Box login through the owner's own OAuth app, once, signing in as the Box account made for that node. Written to a file only the caller can read and never printed; a login has one live copy, so each node gets its own.
---

# data.box_login

`axi data box-login --app <vault entry> --out <new file> [--port 8717] [--no-browser]`

For the person who runs the Box side of a partner install (not the partner). Opens
Box's own sign-in page, catches the answer on `http://localhost:<port>` (the redirect
the owner's app registered), trades it for a login, and writes the login as one line
starting `{"access_token"` to a new file with mode 600. The login is never printed,
and the file is never overwritten.

The app's client id and secret come from the vault, as JSON `{"client_id": ..., "client_secret": ...}`
(store with `axi secrets set`); they are never typed on a command line.

Why one per node: Box renews a login on every use and cancels the old one, so two nodes
holding one login break each other. Sign in as the Box account made for this node, not
as a person.

Next: send the one line to the partner by a secure channel, then delete the file. The node
reads it with `axi data forward --box-login <name> --box-app <app entry>`.
