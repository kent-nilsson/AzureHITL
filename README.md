# Study Planner with Human-in-the-Loop

An Azure AI demo. A learner answers three questions, an **Azure AI Foundry** agent
drafts a Microsoft certification study plan from the **Microsoft Learn Catalog API**,
and the plan is held until a **preassigned boss approves it by e-mail**. The learner
is notified in the UI and by e-mail when the decision lands.

```
Learner (browser, Entra ID)
      │
      ▼
FastAPI on Azure App Service ──► Azure AI Foundry Agent Service ──► Microsoft Learn Catalog API
      │      │                        (search_learn_catalog tool)
      │      └──► Azure Table Storage (sessions + guides)
      │
      └──► Azure Communication Services ──► boss (Approve / Reject links)
                                       └──► learner (decision notification)
```

Intake steps (asked **one at a time**): current certifications → background → goal.
Workflow: `ASK_CERTS → ASK_BACKGROUND → ASK_GOAL → GENERATING → PENDING_APPROVAL → APPROVED | REJECTED`.

### Why the approval is an app-level gate, not Foundry's built-in tool approval

Foundry's tool-approval HITL is for the *same* interactive user approving a tool call
*synchronously*. Here the approver is a **different person** acting **asynchronously**
from an e-mail link, so the app persists the guide as `PendingApproval`, e-mails the
boss an HMAC-signed single-use link, and resumes only when the decision arrives. The
agent still calls a `submit_study_guide` tool so the hand-off point is explicit.

---

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate            # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.sample .env             # bash: cp .env.sample .env
```

Defaults in `.env.sample` run fully offline:
`FAKE_AGENT=true` (plan built straight from the catalog, no Foundry call),
in-memory storage, and `DEV_EMAIL_TO_CONSOLE=true` (e-mails printed to the console).

```bash
python -m uvicorn app.main:app --reload --app-dir src
# open http://localhost:8000
```

Walkthrough:
1. Answer the three questions. The draft plan renders with real `learn.microsoft.com`
   links and a banner: *Waiting for approval from boss@example.com*.
2. The server console prints the approval e-mail. Copy the **APPROVE** URL and open it.
3. The banner flips to *Plan approved ✅* and the console prints the learner
   notification. Opening the link again returns **409** (single-use); the **REJECT**
   link then also returns 409.

### Use a real Foundry agent locally

Set `FAKE_AGENT=false`, `PROJECT_ENDPOINT=https://<res>.services.ai.azure.com/api/projects/<project>`,
`MODEL_DEPLOYMENT_NAME=gpt-4o`, then `az login` (the app uses `DefaultAzureCredential`).

### Tests

```bash
pytest -q          # 20 tests, no network or Azure needed
```

`test_learn_catalog.py` (mocked API — filtering, trimming, caching),
`test_approvals.py` (token sign/verify/expiry/tamper, approver mapping, single-use),
`test_intake_flow.py` (full HTTP flow with a canned guide).

---

## Deploy to Azure

Prerequisites: [Azure Developer CLI (`azd`)](https://aka.ms/azd), an Azure subscription
with quota for a `gpt-4o` deployment, and permission to create role assignments.

```bash
azd auth login
azd env new study-planner
azd env set DEFAULT_APPROVER_EMAIL boss@contoso.com
# optional per-learner routing:
azd env set APPROVERS_JSON '{"alice@contoso.com":"mgr-a@contoso.com"}'
azd up
```

`azd up` provisions (see `infra/`):

| Resource | Purpose |
| --- | --- |
| User-assigned managed identity | One identity for the web app; all RBAC granted to it |
| Log Analytics + Application Insights | Telemetry, agent run traces |
| Storage account + `sessions`/`guides` tables | State store (RBAC: Storage Table Data Contributor) |
| Azure AI Foundry account + project + `gpt-4o` | Agent Service (RBAC: Azure AI Developer, Cognitive Services User) |
| Communication Services + Email + Azure-managed domain | Approval / notification e-mail |
| App Service (Linux, Python 3.12, B1) | Hosts the FastAPI app |

The signing key is generated automatically by the `preprovision` hook.

### After first deploy

- **Recipients on the Azure-managed e-mail domain are restricted.** For a quick demo,
  in the portal open the Email Communication Service → your domain and confirm sending
  works, or connect a verified custom domain and update `ACS_SENDER_ADDRESS`. Some
  tenants also need the boss/learner addresses allow-listed.
- **Enable learner sign-in (optional).** Register an Entra app (redirect URI
  `https://<app>.azurewebsites.net/.auth/login/aad/callback`, ID token enabled), then:
  ```bash
  azd env set AUTH_CLIENT_ID <app-client-id>
  azd provision
  ```
  Without it the app runs open and uses the `DEV_USER_*` identity.
- `MODEL_VERSION` / region: if `gpt-4o` `2024-11-20` is unavailable in your region,
  `azd env set MODEL_VERSION <version>` (or `MODEL_NAME`) and re-run `azd provision`.

### Demo script (Azure)

1. Browse to the App Service URL, sign in, answer the three questions.
2. Draft plan appears; the boss receives the approval e-mail.
3. Boss clicks **Approve** → learner gets the confirmation e-mail and the UI updates
   to *Plan approved*.
4. Show the agent run + `search_learn_catalog` tool calls in Application Insights.

```bash
azd down        # tear everything down
```

---

## Project layout

```
azure.yaml               azd service + hooks
infra/                    Bicep (main + modules/)
src/app/
  config.py              settings (env / .env)
  models.py              intake, StudyGuide, stored records, state machine
  identity.py            learner from Easy Auth headers (or DEV_USER_* locally)
  learn_catalog.py       Microsoft Learn Catalog API client (fetch-once + cache)
  agent.py               Foundry agent run + tools; offline catalog-only fallback
  approvals.py           HMAC links, ACS e-mail, decision handling
  prompts.py             agent instructions
  main.py                FastAPI routes + static UI mount
src/web/                 vanilla-JS chat UI (marked via CDN)
src/tests/               pytest suite
```
