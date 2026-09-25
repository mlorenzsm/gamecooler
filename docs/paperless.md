# Paperless upload

After every sale, the invoice PDF is filed in Paperless-ngx automatically:

- document type **Rechnung**, tag **Wildbret**
- the buyer as **correspondent** (created on first use)
- title "Rechnung Nr. 7 – Erika Muster", created date = sale date

The receipt page shows where each invoice stands (filed, uploading, failed)
and has a button to send it (again). Without configuration the feature is
off and the app shows nothing about it.

## How it works

`app/paperless.py`, started as a background task when a sale is completed —
the sale never waits for Paperless or fails because of it.

1. Look up the document type, tags and correspondent by name; create the ones
   that don't exist yet (with matching "none", so Paperless doesn't start
   filing unrelated documents under a buyer's name).
2. Render the PDF and `POST /api/documents/post_document/`. Paperless answers
   with a task ID and consumes the file in its own time.
3. Poll the task for up to a minute and store the result on the sale in
   `data/sales.json` (`paperless.status`, `document_id` or `error`). If it
   takes longer (OCR on a busy server), the receipt page asks again whenever
   it's opened.

**No double documents.** Pressing "Send again" first asks Paperless about the
earlier task; if it did succeed after all, only the result is recorded. As a
second guard, the PDF is byte-identical for the same sale (its creation date
is the sale date), so Paperless recognises a re-upload by checksum and
refuses it — the app then records the existing document.

`dry_run` does not affect this: it only concerns the printer.

## Setup

### 1. In Paperless: a user for the app

Create a user **gamecooler** (Settings → Users & Groups) with these
permissions: Document *add*, Correspondent *view/add*, Tag *view/add*,
Document type *view/add*, and PaperlessTask *view*. Log in as that user once
and create its token (Profile → API Auth Token). Put the token straight into
the password manager.

Documents belong to the user that uploaded them, so without further setup
the invoices are invisible to you (unless you're a superuser). Add a
**workflow** (Manage → Workflows → Create), name it e.g. "Gamecooler
invoices":

- **Trigger:** type *Consumption Started*, sources only *API Upload*, filter
  filename `rechnung-*.pdf`. The app uploads every invoice under that name
  (`rechnung-7.pdf`), so other API uploads (e.g. a phone app) aren't touched.
- **Action:** type *Assignment*. Under permissions, give your own user (or
  group) *view* and *edit*, or set *Owner* to your user so the invoices are
  simply yours.

The workflow only applies to new uploads. For an invoice filed before it
existed, select it in the document list and use *Permissions* in the
bulk-edit bar.

### 2. In each container: address and token

```sh
install -d -m755 /etc/gamecooler
install -m640 -o root -g gamecooler /dev/null /etc/gamecooler/paperless.env
nano /etc/gamecooler/paperless.env      # see deploy/paperless.env.example
systemctl restart gamecooler
```

The service reads the file through `EnvironmentFile=` in
`deploy/gamecooler.service` (installed by the autodeploy). The file is
readable only by root and the service — the token never goes into the repo,
`config.yaml`, the logs or the UI.

### 3. Test container: mark its documents

So test invoices are easy to find and delete in Paperless, add a tag in
`/var/lib/gamecooler/config.yaml` on the test container:

```yaml
paperless:
  document_type: Rechnung
  tags: [Wildbret, Test]
  buyer_as_correspondent: true
```

then `systemctl restart gamecooler`. Prod keeps the defaults and needs no
entry.

### 4. Check

Settings → General → Paperless → **Test connection**. It reports one of:

| Message | Meaning |
|---|---|
| Connected, permissions are sufficient | ready |
| HTTP 401: token rejected | wrong or old token — create a new one for the user |
| HTTP 403: missing permission "…" | the user lacks that permission from step 1 — grant it and test again |
| unreachable: [Errno 111] Connection refused / Name or service not known | wrong URL, or Paperless down |
| unreachable: … CERTIFICATE_VERIFY_FAILED | see below |

**HTTPS with the home CA.** If Paperless runs behind Caddy with its local CA,
the container doesn't trust that certificate. Install the CA root (Caddy:
`/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` on the
Paperless host) into the container's store:

```sh
cp root.crt /usr/local/share/ca-certificates/home-caddy.crt
update-ca-certificates
systemctl restart gamecooler
```

## Troubleshooting

- **Receipt says "Failed"**: the reason is shown under it. Fix it, then press
  *Send again*.
- **Stuck on "Uploading …"**: Paperless still consuming (OCR). Reload the
  receipt later; check *Tasks* in Paperless.
- **Filed, but you can't see it in Paperless**: the workflow from step 1 is
  missing.
- Log lines: `journalctl -u gamecooler | grep paperless`.

## Not (yet) included

- Sending all older sales at once — each can be sent from its receipt.
- Custom fields (e.g. the amount) and archive serial numbers.
