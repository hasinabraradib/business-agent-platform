# n8n integration

`business-agent-events.json` is an n8n workflow that receives the platform's webhook events
(`reservation.created`, `lead.created`), verifies their signature, appends a row to a Google
Sheet and emails the owner.

> **Untested end to end.** This workflow was written without access to an n8n instance. It has
> only been checked to be valid JSON in n8n's workflow export format. Import it into a test
> n8n first, send a test event, and check each node before relying on it.

## Import

1. In n8n: **Workflows → Import from File**, choose `business-agent-events.json`.
2. Set environment variables on the n8n instance and restart it:
   - `BAP_WEBHOOK_SECRET`: the secret returned by `PUT /v1/webhooks/endpoint` (first setup) or
     `POST /v1/webhooks/endpoint/rotate-secret`.
   - `NODE_FUNCTION_ALLOW_BUILTIN=crypto`: lets the Code node use Node's `crypto` module.
3. **Append to Google Sheet**: add Google Sheets credentials, paste your spreadsheet URL, and
   create a sheet named `Events` (columns are created from the event fields).
4. **Email alert**: add SMTP credentials and set the from/to addresses.
5. Activate the workflow and copy its **Production URL** (path `business-agent-events`).
6. Point the platform at it (admin key):

   ```bash
   curl -X PUT -H "Authorization: Bearer bap_admin_..." -H 'content-type: application/json' \
        -d '{"url": "https://your-n8n.example.com/webhook/business-agent-events"}' \
        http://localhost:8000/v1/webhooks/endpoint
   ```

   The response contains the signing secret once; put it in `BAP_WEBHOOK_SECRET`. The URL must
   be publicly reachable: private and loopback addresses are refused, so a local n8n needs a
   public tunnel.

## Signature

Every delivery has `X-BAP-Timestamp` (unix seconds) and
`X-BAP-Signature: v1=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`. The Code node
recomputes it over the raw body (the webhook node keeps it with **Raw Body** on), compares in
constant time, and rejects timestamps older than five minutes so a captured request cannot be
replayed. `X-BAP-Delivery` stays the same across retries, so you can ignore duplicates.
