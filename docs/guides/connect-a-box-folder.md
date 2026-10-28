# Connect a Box folder

This guide connects one Box folder to an Axiom node, so the files in it are pulled into the
node's bronze tier. It is written for the person who owns the folder and its Box app, working
alone on a Linux or macOS machine, with no Box or Axiom experience assumed.

You will:

1. create a Box app that can sign in on its own (no browser, no password);
2. have a Box admin authorize it;
3. share your folder with the app;
4. run one command that checks everything live and registers the connector.

Allow about 20 minutes, most of it waiting for the admin in Part B.

Every value this guide asks you to type comes from an earlier step. Where a step names a
value, the step that produces it is listed beside it.

> The command is shown as `axi`. A distribution built on Axiom uses its own command name
> with the same verbs (for example `<cli> data connector add box`).

## What you need before you start

- A Box account in your organization's Box enterprise, with two-step verification turned on.
  Box only shows an app's secret to accounts with two-step verification: Box web app >
  your avatar > Account Settings > Authentication > 2-Step Verification.
- The name of someone who can act as a Box admin for your enterprise (for Part B).
- A terminal on the machine that will run the connector, with `axi` installed.
- Your site name: the short name your node uses for the place this data comes from (for
  example `site-a`). If this machine serves exactly one site, the command fills it in for
  you. If not, the person who runs the node gives it to you, and
  `axi data connector list` shows the site of any connector already registered here.

## Part A: create the Box app

1. Open the Box Developer Console at <https://app.box.com/developers/console> and sign in.
2. Click **Create New App** (or **Create Platform App**).
3. Choose **Custom App**.
4. For the authentication method, choose **Server Authentication (Client Credentials Grant)**.
5. Give the app a name people will recognize later, such as `Axiom data pull for site-a`,
   and click **Create App**.
6. Open the app's **Configuration** tab and set:
   - **App Access Level:** **App Access Only**. The app then sees only the folders you share
     with it, which is the narrowest access that works.
   - **Application Scopes:** tick **Read all files and folders stored in Box**. Tick
     **Write all files and folders stored in Box** only if your node writes receipts back
     into the folder; a plain pull needs read only.
7. Click **Save Changes** at the top right.
8. Still on **Configuration**, under **OAuth 2.0 Credentials**:
   - copy the **Client ID** into a note. This is **value 1, the Client ID**. It is not
     secret.
   - click **Fetch Client Secret**, confirm with your second factor, and keep the window
     open. This is **value 2, the Client Secret**. Do not paste it into a note, a chat or
     an email; Part D asks for it once and stores it in the vault.

## Part B: have a Box admin authorize the app

A client-credentials app cannot sign in at all until an admin authorizes it.

9. In the Developer Console, open the app's **Authorization** tab and click
   **Review and Submit**. Box sends the request to your enterprise's admins.
10. Ask your Box admin to approve it. The admin does this in the **Admin Console** >
    **Integrations** > **Platform Apps Manager** > **Server Authentication Apps**, finds the
    app (or clicks **Add App** and pastes **value 1**), and clicks **Authorize**.
11. Ask the admin for the **Enterprise ID** while they are there: Admin Console >
    **Account & Billing** > **Account Information** > **Enterprise ID**. This is
    **value 3, the Enterprise ID**, a number such as `123456789`.

If you change the app's scopes later, the admin must authorize it again.

## Part C: share the folder with the app

The app signs in as its own Box user, called the service account. It sees nothing until you
share a folder with it.

12. In the Developer Console, open the app's **General Settings** tab. Under
    **Service Account Info**, copy the service account's email address. It looks like
    `AutomationUser_1234567_AbCdEf@boxdevedition.com`. This is **value 4, the service
    account**. (It appears once the admin has authorized the app in step 10.)
13. In the Box web app, open the folder your data lands in. Click **Share** >
    **Invite People**, paste **value 4**, choose **Viewer** (or **Editor** if you ticked the
    write scope in step 6), and click **Send**.
14. Note the folder's number from the browser's address bar:
    `https://app.box.com/folder/987654321` means the folder id is `987654321`. This is
    **value 5, the folder id**. You can also skip it: the command lists the folders the app
    can see and lets you pick one.

## Part D: point the machine at its vault (once per machine)

The Box secret is kept in the node's vault, never in a file or an environment variable.

15. Run:

    ```bash
    axi secrets vault
    ```

    If it reports a backend (for example `openbao` with an address), skip to Part E.

16. If it does not, the person who runs your node gives you the vault's address and a vault
    token. Save the token in a file only you can read, then tell `axi` where both are:

    ```bash
    install -m 600 /dev/null ~/.axi/vault-token
    $EDITOR ~/.axi/vault-token        # paste the token, save
    axi secrets vault --backend openbao --url https://vault.example.org:8200 \
        --token-file ~/.axi/vault-token
    ```

    Run `axi secrets vault` again: it should now report the backend.

## Part E: the one command

17. Run:

    ```bash
    axi data connector add box
    ```

    It asks only for what it cannot work out:

    | It asks for | Where you got it |
    |---|---|
    | Site (only if this machine serves more than one) | "What you need before you start" |
    | Box app Client ID | value 1, step 8 |
    | Box app Client Secret (not shown as you type) | value 2, step 8 |
    | Box Enterprise ID | value 3, step 11 |
    | Which folder (a numbered list) | the folder you shared in step 13 |

    The connector is named `<site>-box` unless you pass `--name`. Files land under
    `~/.axi/bronze` unless you pass `--bronze-root`.

18. The command then checks everything against Box before it saves anything:

    1. **Sign in to Box**: exchanges the three values for a token and shows the service
       account it signed in as.
    2. **Open the folder**: confirms the app can see it.
    3. **Read one file**: fetches one file's name, size and checksum.
    4. **Dry-run pull**: lists what the first pull would land and where, without fetching a
       byte or writing anything.

    Only when all four pass does it store the app in the vault (as `box-app-<name>`),
    register the connector with a reference to that vault entry, and print a block for your
    site repository.

### What success looks like

```text
$ axi data connector add box
Setting up Box connector 'site-a-box' for site 'site-a'; files land under /home/you/.axi/bronze.
Box app Client ID: <value 1>
Box app Client Secret (not shown as you type):
Box Enterprise ID: <value 3>
  [ok  ] 1. Sign in to Box: signed in as AutomationUser_1234567_AbCdEf@boxdevedition.com
Folders the app can see:
   1. Instrument drops  (id 987654321)
Pick a folder [1-1] or paste a folder id: 1
  [ok  ] 2. Open the folder: 'Instrument drops' (987654321), 2 items at the top level
  [ok  ] 3. Read one file: run-0007.csv (2.0 KB, sha1 3f786850e387)
  [ok  ] 4. Dry-run pull: 2 files (2.0 KB) would land: 0 allowed, 2 quarantined, 0 excluded; nothing written
  Dry-run pull (nothing fetched, nothing written):
    quarantine /Instrument drops/Run 7/run-0007.csv  -> /home/you/.axi/bronze/site-a-box/_quarantine/2026-10-27
    quarantine /Instrument drops/notes.txt  -> /home/you/.axi/bronze/site-a-box/_quarantine/2026-10-27
• registered box connector 'site-a-box' → /home/you/.axi/plinth/connectors/site-a-box.toml
Registered 'site-a-box' (/home/you/.axi/plinth/connectors/site-a-box.toml). The Box app is in the vault as 'box-app-site-a-box';
the connector holds only the reference.

Add this to your site repository's connectors.toml:

  [connector.site-a-box]
  site = "site-a"
  # schema_ref = "<format>"   # the file format your site's reader declares; add it before conform can turn these files into readings

next: `axi data connector test site-a-box` re-checks it any time.
```

Any step that fails is marked `FAIL`, followed by a `fix:` line (marked "a Box admin" when
only an admin can make it), and nothing is stored or registered. Fix it and run the same
command again.

19. Copy the printed block into your site repository's `connectors.toml` and commit it, so
    the node's deploy declares what this connector is. If your site has a reader for these
    files, pass its format with `--schema-ref` and the block includes it.

**Quarantined or allowed?** A file no provenance rule matches is quarantined by default:
it lands in bronze for review and goes no further. If the folder holds only your own data
files, pass `--default-disposition allow` so they flow on.

## Afterwards

| To | Run |
|---|---|
| See every connector | `axi data connector list` |
| See one, with its declaration | `axi data connector show site-a-box` |
| Re-check it live (writes nothing) | `axi data connector test site-a-box` |
| Re-check from the setup command | `axi data connector add box --name site-a-box --check` |
| Replace it (new app or folder) | `axi data connector add box --name site-a-box --force` |
| Remove it, keeping the vault entry | `axi data connector remove site-a-box` |
| Remove it and its vault entry | `axi data connector remove site-a-box --forget-secret` |

None of these print the secret.

## Without a terminal prompt (scripts and automation)

Every question has a flag, and `--no-input` makes a missing value fail with the flag that
supplies it instead of asking. The secret is never accepted as an argument, where it would
sit in shell history and in every process listing. Two ways to give it:

- **Store the app in the vault once, then name it.** Run `axi secrets set box-app-site-a`
  at a terminal; it asks for the value without echo. Paste one line of JSON holding values
  1 to 3:
  `{"client_id": "<value 1>", "client_secret": "<value 2>", "enterprise_id": "<value 3>"}`.
  From then on, automation names the entry:

  ```bash
  axi data connector add box --no-input --site site-a \
      --app-ref box-app-site-a --folder-id <value 5>
  ```

- **Pipe it on stdin** from a program that already holds it, with `--client-secret-stdin`
  in place of `--app-ref`, plus `--client-id <value 1> --enterprise-id <value 3>`. The
  first line of stdin is the secret.

Add `--json` for a machine-readable result.

## Troubleshooting

Each failure names its cause and its fix, and says when the fix needs a Box admin.

| The command says | What it means | Fix |
|---|---|---|
| Box rejected the client id or client secret | value 1 or 2 is wrong or was copied with a space | Copy both again (step 8). If unsure, reset the secret in the Developer Console and run again. |
| the app is not allowed to sign in as the enterprise | the app is not a client-credentials app, or no admin has authorized it | Check step 4 and step 6, then have the admin do step 10. |
| Box did not accept the enterprise id for this app | value 3 is wrong, or the app is authorized in a different enterprise | Confirm the Enterprise ID with the admin (step 11); if it is right, the admin repeats step 10. |
| the app is signed in but cannot see folder N | the folder is not shared with the service account, or the id is wrong | Do step 13 with the address the command prints; check value 5 (step 14). |
| the app ... can see no folders | nothing is shared with the service account yet | Do step 13. |
| there is no vault on this machine to hold the Box secret | this machine has no vault configured | Do Part D. |
| network: this machine cannot reach Box | a firewall blocks outbound HTTPS | Allow 443 to `api.box.com`, `upload.box.com` and `dl.boxcloud.com`; set `HTTPS_PROXY` if you use a proxy. |
| network: the HTTP proxy refused or dropped the connection | `HTTPS_PROXY` points at the wrong place | Correct or unset it; `curl -sI https://api.box.com` from the same shell should answer. |
| network: TLS to Box failed | a proxy re-signs HTTPS traffic | Set `REQUESTS_CA_BUNDLE` to your organization's CA bundle, or ask IT to exempt the three Box hosts. |
| Box revoked the app's token | an admin disabled or re-scoped the app | The admin re-authorizes it (step 10). |
| a connector named X is already registered | you ran setup twice | `connector test X` to check it, `--force` to replace it, or `--name` for a second one. |
| the folder holds no files yet | the folder is empty | Nothing to fix: the connector is registered and pulls files as they arrive. |
| every file shows `quarantine` in the dry run | no provenance rule matched, so the default applied | Pass `--default-disposition allow` if the folder holds only your own data. |

## Collecting from an instrument into this folder

This guide covers the node side: pulling what lands in a Box folder. The collector that
writes an instrument's files into that folder belongs to the distribution you run, because
it reads that distribution's file formats. Its own guide covers installing the collector on
the instrument machine and pointing it at the folder you shared in step 13.
