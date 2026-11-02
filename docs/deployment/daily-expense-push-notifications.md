# Daily expense push notifications

## Behaviour

Minto sends one push notification at **00:00 in each user's saved device time zone**. It summarizes the previous calendar day using the transaction's `transaction_date`, not when the entry was created. If there were no outgoing expense transactions for that date, it sends a reminder instead. A unique user/date record prevents duplicate sends when the cron endpoint is retried.

Users opt in from Profile. The browser permission prompt is shown only after they enable the setting. Turning the setting off disables the account preference and removes that account's saved push subscriptions.

On iOS/iPadOS, users must install Minto as a Home Screen web app and use a supported OS version before enabling Web Push. Android browsers and installed PWAs use the browser's Web Push support.

## Supabase setup

Run `migrations/daily_expense_push_notifications.sql` in the Supabase SQL editor before deploying/enabling this feature.

## Railway variables

Set these server-side variables (never expose the private key in HTML or client JavaScript):

- `VAPID_PUBLIC_KEY`: base64url-encoded uncompressed P-256 public point.
- `VAPID_PRIVATE_KEY`: base64-encoded DER P-256 private key.
- `VAPID_SUBJECT`: a valid `mailto:` contact or HTTPS URL for the application operator.
- `DAILY_EXPENSE_CRON_SECRET`: a long random secret used by the scheduler.
- `SUPABASE_SERVICE_ROLE_KEY`: required by the server-side notification worker.

Generate a key pair with Python after installing the project requirements:

```python
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

def b64url(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")

key = ec.generate_private_key(ec.SECP256R1())
numbers = key.private_numbers()
public = numbers.public_numbers
x = public.x.to_bytes(32, "big")
y = public.y.to_bytes(32, "big")
private_der = key.private_bytes(
    serialization.Encoding.DER,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
)
print("VAPID_PUBLIC_KEY=" + b64url(b"\x04" + x + y))
print("VAPID_PRIVATE_KEY=" + base64.b64encode(private_der).decode())
```

Keep the private DER key secret. If keys are rotated, existing browser subscriptions may need to be recreated.

## Scheduler

Configure a Railway Cron service to call the deployed app every five minutes:

- Method: `POST`
- Path: `/internal/daily-expense-notifications`
- Header: `X-Minto-Cron-Secret: <DAILY_EXPENSE_CRON_SECRET>`
- Schedule: `*/5 * * * *`

Railway Cron supports a five-minute minimum interval, so the worker accepts the first ten minutes after midnight in each enabled user's local time zone. It summarizes the previous local calendar date. The unique `(user_id, summary_date)` claim ensures repeated or overlapping cron calls cannot send a duplicate for the same user and date.

## Verification checklist

1. Apply the Supabase migration.
2. Configure the Railway variables and the every-minute cron service.
3. Deploy the application with `pywebpush` installed from `requirements.txt`.
4. Install Minto to the Home Screen where required, then enable Daily expense summary in Profile.
5. Test one day with expenses, one day without expenses, a backdated entry, turning the preference off, and two devices on the same account.
