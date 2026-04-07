# ApprovalFlow (3-step approval app)

A lightweight Python approval application with:
- Professional HTML UI
- Email/password login
- 3-step approval chain
- Next-approver assignment at each stage
- Request tracking for requester
- Salesforce-style integration event update on final approval

## Quick start

```bash
python app.py
```

Open: `http://127.0.0.1:8000`

## Demo users

- `alice@company.com` / `alice123`
- `manager1@company.com` / `manager123`
- `director@company.com` / `director123`
- `hr@company.com` / `hr123`

## Running tests

```bash
pytest -q
```

## Salesforce submit integration (no manual request entry)

Salesforce can create an approval request directly via API:

```bash
curl -X POST http://127.0.0.1:8000/api/integrations/salesforce/opportunity-submit \
  -H "Content-Type: application/json" \
  -d '{
    "token": "salesforce-demo-token",
    "requester_email": "alice@company.com",
    "opportunity_id": "OPP-3000",
    "title": "Q2 discount approval",
    "details": "Requested from Salesforce button",
    "first_approver": "manager1@company.com"
  }'
```

When manager approves step 1, the integration event status is `Manager Approved`.
