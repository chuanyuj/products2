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

## Automatic Salesforce Opportunity status update

When approvals happen in this app, Salesforce-linked requests trigger automatic status push to Opportunity
via Salesforce REST API (if credentials are configured):

```bash
export SF_INSTANCE_URL="https://your-org.my.salesforce.com"
export SF_ACCESS_TOKEN="YOUR_OAUTH_ACCESS_TOKEN"
export SF_OPPORTUNITY_STATUS_FIELD="Approval_Status__c"  # optional, default shown
export SF_API_VERSION="v60.0"                            # optional
```

Status values pushed automatically include:
- `Submitted`
- `Manager Approved`
- `Manager Rejected: <comment>`
- `Approved`
