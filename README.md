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
