# Integration secrets and RF pilot

Integration credentials use EncryptedSecretField (Fernet, prefix luenc:v1:).
LOYALUP_SECRET_KEYS is a comma-separated key list in the server environment;
first key writes, all keys read. Keep keys outside git and backups of database
and key together under restricted permissions. Missing/wrong keys fail closed.
Never reuse or rotate CheckUp FERNET_KEY.

Rollout: back up database/env/source; build a committed image; configure the new
key in app containers; migrate shared and tenant schemas; preview
`python manage.py encrypt_integration_secrets`; then run with `--commit`.
The command only updates legacy plaintext secret columns with an optimistic
comparison; output contains counts, never values. Repeat preview must report
zero plaintext. Preserve keys when restoring a database.

RF opt-in fields: rf_reward_max_cost_rub and rf_daily_contact_limit; zero leaves
old behaviour. Cost includes catalog product fallback; unknown cost is rejected
when capped. PostgreSQL advisory lock serializes RF runs in each tenant. Daily
cap uses successful RF logs since local midnight. Dry run sends nothing.
Existing dedup/orchestrator, consent and gift eligibility remain effective.

LevOne pilot: cost <=100 RUB, <=10 RF contacts/day, October 5-11 2026.
G1 existing latte, G2 product 34 lemonade, G3 product 22 khachapuri.
Review current cost and branch availability before enabling; cap is rechecked
when selecting gifts. Enable rules individually after preview, never call
real send as a test. AI marketer: autopost_enabled=False, drafts for approval.

Remaining separate rollout: VK/Telegram identity proofs and delivery webhook
secrets still need compatible senders. Do not enable strict missing-proof
rejection before measuring real clients. Revoke historical exposed service
keys with their issuer; removing a file from HEAD does not revoke it.
