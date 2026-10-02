#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path("/mnt/pcloud/EA/browseract_templates")
ONEMIN_LOGIN_URL = "https://app.1min.ai/login"
ONEMIN_APP_URL = "https://app.1min.ai/"
ONEMIN_BILLING_USAGE_URL = "https://app.1min.ai/billing-usage"
ONEMIN_MEMBERS_URL = "https://app.1min.ai/members"
COMMON_CLOSE_SELECTORS = [
    "button[aria-label='Close']",
    "button[title='Close']",
    "[data-testid='close']",
]
ONEMIN_AUTH_FAILURE_SELECTORS = [
    ".ant-message-notice-content",
    ".ant-message-custom-content",
    ".ant-notification-notice-message",
    ".ant-notification-notice-description",
    ".ant-form-item-explain-error",
    "[role='alert']",
]
ONEMIN_INVALID_CREDENTIAL_MARKERS = [
    "the email or password you entered is incorrect",
    "email or password you entered is incorrect",
    "incorrect email or password",
    "invalid email or password",
    "invalid credentials",
]


def onemin_login_modal_inputs() -> list[dict[str, str]]:
    return [
        {
            "name": "browseract_username",
            "description": "1min login email for the selected account.",
        },
        {
            "name": "browseract_password",
            "description": "1min login password for the selected account.",
        },
    ]


def onemin_login_modal_prefix() -> tuple[list[dict[str, object]], list[list[str]]]:
    nodes = [
        {
            "id": "open_login",
            "type": "visit_page",
            "label": "Open Login",
            "config": {"url": ONEMIN_LOGIN_URL},
        },
        {
            "id": "wait_login_entry",
            "type": "wait",
            "label": "Wait Login Entry",
            "config": {
                "selector": 'button:has-text("Log In"), a:has-text("Log In"), [role=button]:has-text("Log In")',
                "timeout_ms": 5000,
                "optional": True,
            },
        },
        {
            "id": "open_login_modal",
            "type": "click",
            "label": "Open Login Modal",
            "config": {
                "selector": 'button:has-text("Log In"), a:has-text("Log In"), [role=button]:has-text("Log In")',
                "optional": True,
                "wait_timeout_ms": 2500,
                "dom_click": True,
                "react_click": True,
                "post_click_wait_ms": 1200,
            },
        },
    ]
    edges = [
        ["open_login", "wait_login_entry"],
        ["wait_login_entry", "open_login_modal"],
    ]
    previous = "open_login_modal"
    for index, selector in enumerate(COMMON_CLOSE_SELECTORS, start=1):
        wait_id = f"wait_pre_auth_dismiss_overlay_{index:02d}"
        click_id = f"pre_auth_dismiss_overlay_{index:02d}"
        nodes.extend(
            [
                {
                    "id": wait_id,
                    "type": "wait",
                    "label": f"Wait Pre-Auth Dismiss Overlay {index}",
                    "config": {"selector": selector, "timeout_ms": 2500, "optional": True},
                },
                {
                    "id": click_id,
                    "type": "click",
                    "label": f"Pre-Auth Dismiss Overlay {index}",
                    "config": {"selector": selector, "optional": True, "wait_timeout_ms": 1500},
                },
            ]
        )
        edges.extend([[previous, wait_id], [wait_id, click_id]])
        previous = click_id
    nodes.extend(
        [
            {
                "id": "wait_login_form",
                "type": "wait",
                "label": "Wait Login Form",
                "config": {
                    "selector": 'input[placeholder="Email"], input[aria-label="Email"], input[type=email]',
                    "timeout_ms": 45000,
                },
            },
            {
                "id": "email",
                "type": "input_text",
                "label": "Email",
                "config": {
                    "selector": 'input[placeholder="Email"], input[aria-label="Email"], input[type=email]',
                    "value_from_secret": "browseract_username",
                },
            },
            {
                "id": "password",
                "type": "input_text",
                "label": "Password",
                "config": {
                    "selector": 'input[placeholder="Password"], input[aria-label="Password"], input[type=password]',
                    "value_from_secret": "browseract_password",
                },
            },
            {
                "id": "submit",
                "type": "submit_login_form",
                "label": "Submit",
                "config": {
                    "selector": 'button[type=submit], button:has-text("Log In")',
                    "password_selector": 'input[placeholder="Password"], input[aria-label="Password"], input[type=password]',
                    "form_selector": "form[name='login'], .ant-modal form, .ant-modal-root form, form",
                    "react_click": True,
                    "auth_advance_timeout_ms": 12000,
                    "pre_submit_cookie_name": "cf_clearance",
                    "pre_submit_cookie_timeout_ms": 25000,
                    "pre_submit_wait_ms": 3000,
                    "submit_retry_count": 1,
                    "submit_retry_backoff_ms": 8000,
                    "auth_failure_code": "invalid_credentials",
                    "auth_failure_selectors": ONEMIN_AUTH_FAILURE_SELECTORS,
                    "auth_failure_text_markers": ONEMIN_INVALID_CREDENTIAL_MARKERS,
                },
            },
            {
                "id": "wait_dashboard",
                "type": "wait",
                "label": "Wait Dashboard",
                "config": {
                    "selector": 'input[placeholder="Password"], input[aria-label="Password"], input[type=password]',
                    "state": "hidden",
                    "timeout_ms": 45000,
                    "optional": True,
                },
            },
        ]
    )
    edges.extend(
        [
            [previous, "wait_login_form"],
            ["wait_login_form", "email"],
            ["email", "password"],
            ["password", "submit"],
            ["submit", "wait_dashboard"],
        ]
    )
    return nodes, edges


def onemin_daily_bonus_workflow_spec() -> dict[str, object]:
    nodes, edges = onemin_login_modal_prefix()
    nodes.extend(
        [
            {
                "id": "open_billing_usage",
                "type": "visit_page",
                "label": "Open Billing Usage",
                "config": {"url": ONEMIN_BILLING_USAGE_URL},
            },
            {
                "id": "wait_dismiss_overlay_01",
                "type": "wait",
                "label": "Wait Dismiss Overlay 1",
                "config": {"selector": COMMON_CLOSE_SELECTORS[0], "timeout_ms": 2500, "optional": True},
            },
            {
                "id": "dismiss_overlay_01",
                "type": "click",
                "label": "Dismiss Overlay 1",
                "config": {"selector": COMMON_CLOSE_SELECTORS[0], "optional": True, "wait_timeout_ms": 1500},
            },
            {
                "id": "wait_dismiss_overlay_02",
                "type": "wait",
                "label": "Wait Dismiss Overlay 2",
                "config": {"selector": COMMON_CLOSE_SELECTORS[1], "timeout_ms": 2500, "optional": True},
            },
            {
                "id": "dismiss_overlay_02",
                "type": "click",
                "label": "Dismiss Overlay 2",
                "config": {"selector": COMMON_CLOSE_SELECTORS[1], "optional": True, "wait_timeout_ms": 1500},
            },
            {
                "id": "wait_dismiss_overlay_03",
                "type": "wait",
                "label": "Wait Dismiss Overlay 3",
                "config": {"selector": COMMON_CLOSE_SELECTORS[2], "timeout_ms": 2500, "optional": True},
            },
            {
                "id": "dismiss_overlay_03",
                "type": "click",
                "label": "Dismiss Overlay 3",
                "config": {"selector": COMMON_CLOSE_SELECTORS[2], "optional": True, "wait_timeout_ms": 1500},
            },
            {
                "id": "wait_billing_usage",
                "type": "wait",
                "label": "Wait Billing Usage",
                "config": {"selector": "main, body"},
            },
            {
                "id": "unlock_free_credits",
                "type": "click",
                "label": "Unlock Free Credits",
                "config": {
                    "selector": 'button:has-text("Unlock Free Credits"), [role=button]:has-text("Unlock Free Credits")',
                    "optional": True,
                    "wait_timeout_ms": 1500,
                },
            },
            {
                "id": "wait_bonus_surface",
                "type": "wait",
                "label": "Wait Bonus Surface",
                "config": {"selector": "main, body"},
            },
            {
                "id": "extract_daily_bonus",
                "type": "extract",
                "label": "Extract Daily Bonus",
                "config": {
                    "selector": "main, body",
                    "field_name": "daily_bonus_page",
                    "mode": "text",
                },
            },
            {
                "id": "output_result",
                "type": "output",
                "label": "Output Result",
                "config": {
                    "field_name": "daily_bonus_page",
                    "description": "Publish the daily bonus surface for API callers.",
                },
            },
        ]
    )
    edges.extend(
        [
            ["wait_dashboard", "open_billing_usage"],
            ["open_billing_usage", "wait_dismiss_overlay_01"],
            ["wait_dismiss_overlay_01", "dismiss_overlay_01"],
            ["dismiss_overlay_01", "wait_dismiss_overlay_02"],
            ["wait_dismiss_overlay_02", "dismiss_overlay_02"],
            ["dismiss_overlay_02", "wait_dismiss_overlay_03"],
            ["wait_dismiss_overlay_03", "dismiss_overlay_03"],
            ["dismiss_overlay_03", "wait_billing_usage"],
            ["wait_billing_usage", "unlock_free_credits"],
            ["unlock_free_credits", "wait_bonus_surface"],
            ["wait_bonus_surface", "extract_daily_bonus"],
            ["extract_daily_bonus", "output_result"],
        ]
    )
    return {
        "workflow_name": "1min Daily Bonus Check-in",
        "description": "Sign in to 1min.AI, open the billing surface, trigger the free-credit unlock path, and extract the visible daily bonus/check-in state for later normalization.",
        "publish": True,
        "mcp_ready": False,
        "inputs": onemin_login_modal_inputs(),
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "slug": "onemin_daily_bonus_checkin_live",
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "status": "pending_browseract_seed",
            "workflow_kind": "page_extract",
        },
    }


def onemin_billing_usage_workflow_spec() -> dict[str, object]:
    nodes, edges = onemin_login_modal_prefix()
    nodes.extend(
        [
            {
                "id": "open_billing_usage",
                "type": "visit_page",
                "label": "Open Billing Usage",
                "config": {"url": ONEMIN_BILLING_USAGE_URL},
            },
            {
                "id": "wait_dismiss_overlay_01",
                "type": "wait",
                "label": "Wait Dismiss Overlay 1",
                "config": {"selector": COMMON_CLOSE_SELECTORS[0], "timeout_ms": 2500, "optional": True},
            },
            {
                "id": "dismiss_overlay_01",
                "type": "click",
                "label": "Dismiss Overlay 1",
                "config": {"selector": COMMON_CLOSE_SELECTORS[0], "optional": True, "wait_timeout_ms": 1500},
            },
            {
                "id": "wait_dismiss_overlay_02",
                "type": "wait",
                "label": "Wait Dismiss Overlay 2",
                "config": {"selector": COMMON_CLOSE_SELECTORS[1], "timeout_ms": 2500, "optional": True},
            },
            {
                "id": "dismiss_overlay_02",
                "type": "click",
                "label": "Dismiss Overlay 2",
                "config": {"selector": COMMON_CLOSE_SELECTORS[1], "optional": True, "wait_timeout_ms": 1500},
            },
            {
                "id": "wait_dismiss_overlay_03",
                "type": "wait",
                "label": "Wait Dismiss Overlay 3",
                "config": {"selector": COMMON_CLOSE_SELECTORS[2], "timeout_ms": 2500, "optional": True},
            },
            {
                "id": "dismiss_overlay_03",
                "type": "click",
                "label": "Dismiss Overlay 3",
                "config": {"selector": COMMON_CLOSE_SELECTORS[2], "optional": True, "wait_timeout_ms": 1500},
            },
            {
                "id": "wait_billing_usage",
                "type": "wait",
                "label": "Wait Billing Usage",
                "config": {"selector": "main, body"},
            },
            {
                "id": "extract_billing_settings",
                "type": "extract",
                "label": "Extract Billing Settings",
                "config": {
                    "selector": "main, body",
                    "field_name": "billing_settings_page",
                    "mode": "text",
                },
            },
            {
                "id": "extract_usage_records",
                "type": "extract",
                "label": "Extract Usage Records",
                "config": {
                    "selector": "table, main, body",
                    "field_name": "usage_records_page",
                    "mode": "text",
                },
            },
            {
                "id": "extract_pre_bonus_page",
                "type": "extract",
                "label": "Extract Billing Page Before Bonus",
                "config": {
                    "selector": "main, body",
                    "field_name": "billing_usage_pre_bonus_page",
                    "mode": "text",
                },
            },
            {
                "id": "unlock_free_credits",
                "type": "click",
                "label": "Unlock Free Credits",
                "config": {
                    "selector": 'button:has-text("Unlock Free Credits"), [role=button]:has-text("Unlock Free Credits")',
                },
            },
            {
                "id": "wait_bonus_surface",
                "type": "wait",
                "label": "Wait Bonus Surface",
                "config": {"selector": "main, body"},
            },
            {
                "id": "extract_billing_bonus_page",
                "type": "extract",
                "label": "Extract Billing Page After Bonus",
                "config": {
                    "selector": "main, body",
                    "field_name": "billing_usage_bonus_page",
                    "mode": "text",
                },
            },
            {
                "id": "output_result",
                "type": "output",
                "label": "Output Result",
                "config": {
                    "field_name": "billing_usage_bonus_page",
                    "description": "Publish the combined 1min billing page after opening the free-credit surface so API callers can answer balance, burn, and bonus questions from one run.",
                },
            },
        ]
    )
    edges.extend(
        [
            ["wait_dashboard", "open_billing_usage"],
            ["open_billing_usage", "dismiss_overlay_01"],
            ["dismiss_overlay_01", "dismiss_overlay_02"],
            ["dismiss_overlay_02", "dismiss_overlay_03"],
            ["dismiss_overlay_03", "wait_billing_usage"],
            ["wait_billing_usage", "extract_billing_settings"],
            ["extract_billing_settings", "extract_usage_records"],
            ["extract_usage_records", "extract_pre_bonus_page"],
            ["extract_pre_bonus_page", "unlock_free_credits"],
            ["unlock_free_credits", "wait_bonus_surface"],
            ["wait_bonus_surface", "extract_billing_bonus_page"],
            ["extract_billing_bonus_page", "output_result"],
        ]
    )
    return {
        "workflow_name": "1min Billing Usage Reader",
        "description": "Sign in to 1min.AI, extract the visible billing settings and usage records, capture the full pre-bonus billing page, then trigger the free-credit unlock path and publish the post-click page so one BrowserAct run supports balance, burn, plan, and bonus questions.",
        "publish": True,
        "mcp_ready": False,
        "inputs": onemin_login_modal_inputs(),
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "slug": "onemin_billing_usage_reader_live",
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "status": "pending_browseract_seed",
            "workflow_kind": "page_extract",
            "blocked_url_markers": [
                "tawk.to",
                "growthbook",
                "google-analytics.com",
                "region1.google-analytics.com",
                "otlp.1min.ai",
                "appleid.cdn-apple.com",
            ],
        },
    }


def onemin_members_workflow_spec() -> dict[str, object]:
    nodes, edges = onemin_login_modal_prefix()
    nodes.extend(
        [
            {
                "id": "open_members",
                "type": "visit_page",
                "label": "Open Members",
                "config": {"url": ONEMIN_MEMBERS_URL},
            },
            {
                "id": "dismiss_overlay_01",
                "type": "click",
                "label": "Dismiss Overlay 1",
                "config": {"selector": COMMON_CLOSE_SELECTORS[0]},
            },
            {
                "id": "dismiss_overlay_02",
                "type": "click",
                "label": "Dismiss Overlay 2",
                "config": {"selector": COMMON_CLOSE_SELECTORS[1]},
            },
            {
                "id": "dismiss_overlay_03",
                "type": "click",
                "label": "Dismiss Overlay 3",
                "config": {"selector": COMMON_CLOSE_SELECTORS[2]},
            },
            {
                "id": "wait_members",
                "type": "wait",
                "label": "Wait Members",
                "config": {"selector": "main, body"},
            },
            {
                "id": "extract_members",
                "type": "extract",
                "label": "Extract Members",
                "config": {
                    "selector": "main, body",
                    "field_name": "members_page",
                    "mode": "text",
                },
            },
            {
                "id": "output_result",
                "type": "output",
                "label": "Output Result",
                "config": {
                    "field_name": "members_page",
                    "description": "Publish the members surface for API callers.",
                },
            },
        ]
    )
    edges.extend(
        [
            ["wait_dashboard", "open_members"],
            ["open_members", "wait_dismiss_overlay_01"],
            ["wait_dismiss_overlay_01", "dismiss_overlay_01"],
            ["dismiss_overlay_01", "wait_dismiss_overlay_02"],
            ["wait_dismiss_overlay_02", "dismiss_overlay_02"],
            ["dismiss_overlay_02", "wait_dismiss_overlay_03"],
            ["wait_dismiss_overlay_03", "dismiss_overlay_03"],
            ["dismiss_overlay_03", "wait_members"],
            ["wait_members", "extract_members"],
            ["extract_members", "output_result"],
        ]
    )
    return {
        "workflow_name": "1min Members Reconciliation Reader",
        "description": "Sign in to 1min.AI, open the members surface, and extract the visible member roster, statuses, and credit-limit hints for owner reconciliation.",
        "publish": True,
        "mcp_ready": False,
        "inputs": onemin_login_modal_inputs(),
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "slug": "onemin_members_reconciliation_live",
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "status": "pending_browseract_seed",
            "workflow_kind": "page_extract",
            "blocked_url_markers": [
                "tawk.to",
                "growthbook",
                "google-analytics.com",
                "region1.google-analytics.com",
                "otlp.1min.ai",
                "appleid.cdn-apple.com",
            ],
        },
    }


def build_skill_payload() -> dict[str, object]:
    return {
        "skill_key": "browseract_bootstrap_manager",
        "task_key": "browseract_bootstrap_manager",
        "name": "BrowserAct Bootstrap Manager",
        "description": "Planner-executed BrowserAct workflow-spec builder for stage-0 BrowserAct template creation and architect packets.",
        "deliverable_type": "browseract_workflow_spec_packet",
        "default_risk_class": "medium",
        "default_approval_class": "none",
        "workflow_template": "tool_then_artifact",
        "allowed_tools": ["browseract.build_workflow_spec", "artifact_repository"],
        "evidence_requirements": ["target_domain_brief", "workflow_spec", "browseract_seed_state"],
        "memory_write_policy": "none",
        "memory_reads": ["entities", "relationships"],
        "memory_writes": [],
        "tags": ["browseract", "bootstrap", "workflow", "architect"],
        "authority_profile_json": {"authority_class": "draft", "review_class": "operator"},
        "provider_hints_json": {"primary": ["BrowserAct"]},
        "tool_policy_json": {"allowed_tools": ["browseract.build_workflow_spec", "artifact_repository"]},
        "human_policy_json": {"review_roles": ["automation_architect"]},
        "evaluation_cases_json": [{"case_key": "browseract_bootstrap_manager_golden", "priority": "medium"}],
        "budget_policy_json": {
            "class": "medium",
            "workflow_template": "tool_then_artifact",
            "pre_artifact_capability_key": "workflow_spec_build",
            "browseract_failure_strategy": "retry",
            "browseract_max_attempts": 2,
            "browseract_retry_backoff_seconds": 1,
        },
    }


def templates() -> list[dict[str, object]]:
    return [
        {
            "slug": "onemin_daily_bonus_checkin_live",
            "workflow_name": "1min Daily Bonus Check-in",
            "purpose": "Sign in to the 1min.AI app, open the billing surface, trigger the free-credit unlock path, and extract the visible daily bonus or check-in state so operators can verify the recurring credit claim path.",
            "login_url": ONEMIN_LOGIN_URL,
            "tool_url": ONEMIN_BILLING_USAGE_URL,
            "workflow_kind": "page_extract",
            "wait_selector": "main, body",
            "title_selector": "h1, h2, [role='heading']",
            "result_selector": "main, body",
            "result_field_name": "daily_bonus_page",
            "dismiss_selectors": COMMON_CLOSE_SELECTORS,
            "workflow_spec_json": onemin_daily_bonus_workflow_spec(),
        },
        {
            "slug": "onemin_billing_usage_reader_live",
            "workflow_name": "1min Billing Usage Reader",
            "purpose": "Sign in to the 1min.AI app, extract billing settings plus usage records, capture the full billing page before and after opening the free-credit unlock path, and keep enough text for stable balance, burn, plan, and bonus normalization from one BrowserAct run.",
            "login_url": ONEMIN_LOGIN_URL,
            "tool_url": ONEMIN_BILLING_USAGE_URL,
            "workflow_kind": "page_extract",
            "wait_selector": "main, body",
            "title_selector": "h1, h2, [role='heading']",
            "result_selector": "main, body",
            "result_field_name": "billing_usage_bonus_page",
            "dismiss_selectors": COMMON_CLOSE_SELECTORS,
            "workflow_spec_json": onemin_billing_usage_workflow_spec(),
        },
        {
            "slug": "onemin_members_reconciliation_live",
            "workflow_name": "1min Members Reconciliation Reader",
            "purpose": "Sign in to the 1min.AI app, open the members surface, and extract the visible member roster, statuses, and credit-limit hints for owner reconciliation.",
            "login_url": ONEMIN_LOGIN_URL,
            "tool_url": ONEMIN_MEMBERS_URL,
            "workflow_kind": "page_extract",
            "wait_selector": "main, body",
            "title_selector": "h1, h2, [role='heading']",
            "result_selector": "main, body",
            "result_field_name": "members_page",
            "dismiss_selectors": COMMON_CLOSE_SELECTORS,
            "workflow_spec_json": onemin_members_workflow_spec(),
        },
        {
            "slug": "economist_article_reader_live",
            "workflow_name": "Economist Article Reader",
            "purpose": "Open a logged-in Economist article URL, dismiss overlays, and extract the readable title and article body.",
            "login_url": "https://www.economist.com/login",
            "tool_url": "https://www.economist.com",
            "workflow_kind": "page_extract",
            "runtime_input_name": "article_url",
            "wait_selector": "article",
            "title_selector": "article h1, h1[data-test-id='ArticleHeadline']",
            "result_selector": "article",
            "dismiss_selectors": ["button[aria-label='Close']", "button[data-testid='closeButton']"],
        },
        {
            "slug": "atlantic_article_reader_live",
            "workflow_name": "Atlantic Article Reader",
            "purpose": "Open a logged-in Atlantic article URL, dismiss overlays, and extract the readable headline and story body.",
            "login_url": "https://accounts.theatlantic.com/login",
            "tool_url": "https://www.theatlantic.com",
            "workflow_kind": "page_extract",
            "runtime_input_name": "article_url",
            "wait_selector": "article",
            "title_selector": "article h1, main h1",
            "result_selector": "article, main article, .article-body",
            "dismiss_selectors": ["button[aria-label='Close']", "button[title='Close']"],
        },
        {
            "slug": "nytimes_article_reader_live",
            "workflow_name": "NYTimes Article Reader",
            "purpose": "Open a logged-in New York Times article URL, dismiss overlays, and extract the readable headline and body.",
            "login_url": "https://myaccount.nytimes.com/auth/login",
            "tool_url": "https://www.nytimes.com",
            "workflow_kind": "page_extract",
            "runtime_input_name": "article_url",
            "wait_selector": "article",
            "title_selector": "article h1, header h1",
            "result_selector": "article, section[name='articleBody']",
            "dismiss_selectors": ["button[aria-label='Close']", "button[data-testid='GDPR-close']"],
        },
        {
            "slug": "approvethis_queue_reader_live",
            "workflow_name": "ApproveThis Queue Reader",
            "purpose": "Open the logged-in ApproveThis queue/dashboard and extract the pending approvals view without relying on manual clicks.",
            "login_url": "https://app.approvethis.com/login",
            "tool_url": "https://app.approvethis.com",
            "workflow_kind": "page_extract",
            "wait_selector": "main, table, [data-testid='approvals-list']",
            "title_selector": "h1, h2",
            "result_selector": "main",
            "dismiss_selectors": ["button[aria-label='Close']", "button[title='Close']"],
        },
        {
            "slug": "metasurvey_results_reader_live",
            "workflow_name": "MetaSurvey Results Reader",
            "purpose": "Open a logged-in MetaSurvey survey or results page, dismiss overlays, and extract the visible survey summary/results content.",
            "login_url": "https://getmetasurvey.com/login",
            "tool_url": "https://getmetasurvey.com",
            "workflow_kind": "page_extract",
            "runtime_input_name": "survey_url",
            "wait_selector": "main, article, [data-testid='survey-results']",
            "title_selector": "h1, h2",
            "result_selector": "main",
            "dismiss_selectors": ["button[aria-label='Close']", "button[title='Close']"],
        },
    ]


def client():
    from fastapi.testclient import TestClient

    os.environ.setdefault("EA_STORAGE_BACKEND", "memory")
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ.setdefault("EA_API_TOKEN", "")
    from app.api.app import create_app

    test_client = TestClient(create_app())
    test_client.headers.update({"X-EA-Principal-ID": "exec-1"})
    return test_client


def main() -> int:
    output_dir = Path(os.environ.get("EA_BROWSERACT_TEMPLATE_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    api = client()
    response = api.post("/v1/skills", json=build_skill_payload())
    response.raise_for_status()

    summary: list[dict[str, object]] = []
    for template in templates():
        execute = api.post(
            "/v1/plans/execute",
            json={
                "skill_key": "browseract_bootstrap_manager",
                "goal": f"build the {template['workflow_name']} workflow spec packet",
                "input_json": {k: v for k, v in template.items() if k != "slug"},
            },
        )
        execute.raise_for_status()
        body = execute.json()
        slug = str(template["slug"])
        structured_output = dict(body["structured_output_json"] or {})
        packet_path = output_dir / f"{slug}.packet.json"
        workflow_path = output_dir / f"{slug}.workflow.json"
        payload_text = json.dumps(structured_output, indent=2) + "\n"
        packet_path.write_text(payload_text, encoding="utf-8")
        workflow_path.write_text(payload_text, encoding="utf-8")
        summary.append(
            {
                "slug": slug,
                "workflow_name": structured_output.get("workflow_name"),
                "path": str(packet_path),
                "workflow_path": str(workflow_path),
                "kind": body.get("kind"),
            }
        )

    summary_path = output_dir / "browseract_content_templates.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "output_dir": str(output_dir), "count": len(summary)}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
