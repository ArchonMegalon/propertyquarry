#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

import requests


API_BASE = "https://api.cloudflare.com/client/v4"
EA_ENV_PATH = Path("/docker/EA/.env")
PROPERTY_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _effective_env() -> dict[str, str]:
    env = _load_env_file(EA_ENV_PATH)
    env.update(_load_env_file(PROPERTY_ENV_PATH))
    for key, value in os.environ.items():
        if value:
            env[key] = value
    return env


def _cf_headers(env: dict[str, str], *, content_type: str = "application/json") -> dict[str, str]:
    email = str(env.get("CLOUDFLARE_EMAIL") or "").strip()
    api_key = str(env.get("CLOUDFLARE_GLOBAL_API_KEY") or "").strip()
    api_token = str(env.get("CLOUDFLARE_API_TOKEN") or env.get("CF_API_TOKEN") or "").strip()
    if api_token:
        return {"Authorization": f"Bearer {api_token}", "Content-Type": content_type}
    if email and api_key:
        return {
            "X-Auth-Email": email,
            "X-Auth-Key": api_key,
            "Content-Type": content_type,
        }
    raise SystemExit("Cloudflare credentials missing. Set CLOUDFLARE_API_TOKEN or CLOUDFLARE_EMAIL + CLOUDFLARE_GLOBAL_API_KEY.")


def _cf_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    data: bytes | str | None = None,
) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{API_BASE}{path}",
        headers=headers,
        json=payload,
        params=params,
        data=data,
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise SystemExit(f"Cloudflare API error for {path}: {json.dumps(body.get('errors') or body, ensure_ascii=True)}")
    return body


def _discover_account_id(*, headers: dict[str, str], env: dict[str, str]) -> str:
    configured = str(env.get("PROPERTYQUARRY_CF_ACCOUNT_ID") or env.get("EA_CF_ACCOUNT_ID") or "").strip()
    if configured:
        return configured
    body = _cf_request("GET", "/accounts", headers=headers)
    accounts = list(body.get("result") or [])
    if len(accounts) != 1:
        summary = ", ".join(f"{item.get('name')}:{item.get('id')}" for item in accounts[:10])
        raise SystemExit(
            f"Could not uniquely determine Cloudflare account. Set PROPERTYQUARRY_CF_ACCOUNT_ID. Visible accounts: {summary}"
        )
    return str(accounts[0].get("id") or "").strip()


def _discover_zone_id(*, account_id: str, headers: dict[str, str], zone_name: str) -> str:
    body = _cf_request(
        "GET",
        "/zones",
        headers=headers,
        params={"name": zone_name, "account.id": account_id, "per_page": 100},
    )
    zones = list(body.get("result") or [])
    if not zones:
        raise SystemExit(f"Cloudflare zone {zone_name!r} not found for account {account_id}")
    return str(zones[0].get("id") or "").strip()


def _encode_worker_literal(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _billing_noise_rules() -> list[tuple[str, str]]:
    return [
        (r"\bCurrent ranking bar:\s*\d+\s*\/\s*100\b[;:,-]?\s*", ""),
        (r"\bCurrent ranking bar:\s*", ""),
        (r"\beven below the (?:current|saved) ranking bar\b", "in the full list"),
        (r"\bbelow the (?:current|saved) ranking bar\b", "in the full list"),
        (r"\bbelow the current bar\b", "in the full list"),
        (r"\bTurn the ranking bar down or off\b", "Start a fresh search"),
        (r"\bLower the ranking bar or turn it off\b", "Start a fresh search"),
        (r"\bCurrent score filter:\s*\d+\s*\/\s*100\b[;:,-]?\s*", ""),
        (r"\bCurrent score filter:\s*", ""),
        (r"\bCurrent score ceiling:\s*\d+\s*\/\s*100\b[;:,-]?\s*", ""),
        (r"\bCurrent score ceiling:\s*", ""),
        (r"\beven below the (?:current|saved) score filter\b", "in the full list"),
        (r"\bbelow the (?:current|saved) score filter\b", "in the full list"),
        (r"\bbelow the current score ceiling\b", "in the full list"),
        (r"\bTurn the score filter down or off\b", "Start a fresh search"),
        (r"\bLower the score filter or turn it off\b", "Start a fresh search"),
        (r"\bScore ceiling\b", ""),
        (r"\bResult cap per provider\b", ""),
        (r"\bPer provider\b", ""),
        (r"\bAll ranked\b", ""),
        (r"\b35\s*\/\s*100\b", ""),
        (r"\b45\s*\/\s*100\b", ""),
        (r"\b60\s*\/\s*100\b", ""),
        (r"\bscore gate\b", ""),
        (r"\bpagespeed accessibility score\b", "pagespeed accessibility"),
    ]


def _billing_noise_fragments() -> list[str]:
    return [
        "current score filter",
        "current score ceiling",
        "current ranking bar",
        "score filter",
        "score ceiling",
        "score gate",
        "ranking bar",
        "match threshold",
        "watch tier",
        "result cap per provider",
        "all ranked",
        "per provider",
        "find an agent",
        "open houses",
        "featured agents",
        "featured agents & brokers",
        "list your home",
        "properties - join today",
        "properties \u2013 join today",
        "real estate tips",
    ]


def _worker_source(
    *,
    target_base_url: str,
    pricing_url: str,
    property_origin: str = "https://propertyquarry.com",
    bridge_path: str = "/sso/propertyquarry",
) -> str:
    normalized_target = str(target_base_url or "").strip().rstrip("/")
    if not normalized_target.startswith("https://"):
        raise SystemExit("target_base_url must be an https:// URL")
    parsed_target = requests.utils.urlparse(normalized_target)
    target_host = str(parsed_target.hostname or "").strip().lower()
    if not target_host:
        raise SystemExit("target_base_url must include a host")
    normalized_pricing_url = str(pricing_url or "").strip()
    parsed_pricing = requests.utils.urlparse(normalized_pricing_url)
    if parsed_pricing.scheme != "https" or not parsed_pricing.hostname:
        raise SystemExit("pricing_url must be an https:// URL with a host")
    normalized_property_origin = str(property_origin or "").strip().rstrip("/")
    parsed_property = requests.utils.urlparse(normalized_property_origin)
    if parsed_property.scheme != "https" or not parsed_property.hostname:
        raise SystemExit("property_origin must be an https:// URL with a host")
    normalized_bridge_path = "/" + str(bridge_path or "").strip().lstrip("/")
    if not normalized_bridge_path or normalized_bridge_path == "/" or "?" in normalized_bridge_path or "#" in normalized_bridge_path:
        raise SystemExit("bridge_path must be a non-root path without query or fragment")
    encoded_billing_noise_rules = json.dumps(
        [[_encode_worker_literal(pattern), _encode_worker_literal(replacement)] for pattern, replacement in _billing_noise_rules()]
    )
    encoded_billing_noise_fragments = json.dumps(
        [_encode_worker_literal(fragment) for fragment in _billing_noise_fragments()]
    )
    return (
        "export default {\n"
        "  async fetch(request, env) {\n"
        f"    const targetOrigin = {json.dumps(normalized_target)};\n"
        f"    const targetHost = {json.dumps(target_host)};\n"
        f"    const pricingUrl = {json.dumps(normalized_pricing_url)};\n"
        f"    const propertyOrigin = {json.dumps(normalized_property_origin)};\n"
        f"    const bridgePath = {json.dumps(normalized_bridge_path)};\n"
        "    const bridgeCookieName = 'pq_bridge';\n"
        "    const bridgeCookieMaxAge = 300;\n"
        "    const bridgeSecret = String((env && env.PQ_BRIDGE_SECRET) || '');\n"
        "    const incoming = new URL(request.url);\n"
        "    const normalizedPath = incoming.pathname.replace(/\\/+$/g, '') || '/';\n"
        "    const redirectResponse = (location, branch) => new Response(null, {\n"
        "      status: 302,\n"
        "      headers: {\n"
        "        'location': location,\n"
        "        'cache-control': 'no-store',\n"
        "        'x-robots-tag': 'noindex, nofollow',\n"
        "        'x-pq-billing-worker': 'propertyquarry-billing-handoff',\n"
        "        'x-pq-billing-worker-branch': branch,\n"
        "      },\n"
        "    });\n"
        "    const publicDirectoryRedirects = new Map([\n"
        "      ['/', `${propertyOrigin}/`],\n"
        "      ['/home', `${propertyOrigin}/`],\n"
        "      ['/search_results', `${propertyOrigin}/`],\n"
        "      ['/events', `${propertyOrigin}/`],\n"
        "      ['/blog', `${propertyOrigin}/`],\n"
        "      ['/about', `${propertyOrigin}/`],\n"
        "      ['/about/contact', `${propertyOrigin}/`],\n"
        "      ['/directory', `${propertyOrigin}/`],\n"
        "    ]);\n"
        "    if ((request.method === 'GET' || request.method === 'HEAD') && publicDirectoryRedirects.has(normalizedPath)) {\n"
        "      return redirectResponse(publicDirectoryRedirects.get(normalizedPath), 'hero-redirect');\n"
        "    }\n"
        "    const htmlEscape = (value) => String(value || '')\n"
        "      .replaceAll('&', '&amp;')\n"
        "      .replaceAll('<', '&lt;')\n"
        "      .replaceAll('>', '&gt;')\n"
        "      .replaceAll('\"', '&quot;');\n"
        "    const normalizePropertyOrigin = (value) => {\n"
        "      try {\n"
        "        const parsed = new URL(String(value || propertyOrigin));\n"
        "        if (parsed.protocol !== 'https:') return propertyOrigin;\n"
        "        return `${parsed.protocol}//${parsed.host}`;\n"
        "      } catch {\n"
        "        return propertyOrigin;\n"
        "      }\n"
        "    };\n"
        "    const safeReturnPath = (value) => {\n"
        "      const raw = String(value || '').trim();\n"
        "      if (!raw) return '/app/account';\n"
        "      try {\n"
        "        const absolute = new URL(raw);\n"
        "        const allowedOrigin = normalizePropertyOrigin(propertyOrigin);\n"
        "        if (absolute.protocol !== 'https:' || `${absolute.protocol}//${absolute.host}` !== allowedOrigin) return '/app/account';\n"
        "        return absolute.pathname.startsWith('/') ? `${absolute.pathname}${absolute.search}` : '/app/account';\n"
        "      } catch {\n"
        "        if (!raw.startsWith('/') || raw.startsWith('//')) return '/app/account';\n"
        "        const parsed = new URL(raw, 'https://propertyquarry.invalid');\n"
        "        return `${parsed.pathname}${parsed.search}`;\n"
        "      }\n"
        "    };\n"
        "    const readCookie = (name) => {\n"
        "      const source = String(request.headers.get('cookie') || '');\n"
        "      const parts = source.split(/;\\s*/);\n"
        "      for (const part of parts) {\n"
        "        const [cookieName, ...rest] = part.split('=');\n"
        "        if (cookieName === name) return decodeURIComponent(rest.join('='));\n"
        "      }\n"
        "      return '';\n"
        "    };\n"
        "    const toBase64 = (bytes) => {\n"
        "      let binary = '';\n"
        "      for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);\n"
        "      return btoa(binary).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/g, '');\n"
        "    };\n"
        "    const fromBase64 = (value) => {\n"
        "      const normalized = String(value || '').replace(/-/g, '+').replace(/_/g, '/');\n"
        "      const padding = normalized.length % 4 ? '='.repeat(4 - (normalized.length % 4)) : '';\n"
        "      const binary = atob(normalized + padding);\n"
        "      return Uint8Array.from(binary, (char) => char.charCodeAt(0));\n"
        "    };\n"
        "    const signBridgeToken = async (encoded) => {\n"
        "      const secretKey = await crypto.subtle.importKey(\n"
        "        'raw',\n"
        "        new TextEncoder().encode(bridgeSecret),\n"
        "        { name: 'HMAC', hash: 'SHA-256' },\n"
        "        false,\n"
        "        ['sign']\n"
        "      );\n"
        "      const signature = await crypto.subtle.sign('HMAC', secretKey, new TextEncoder().encode(encoded));\n"
        "      return toBase64(new Uint8Array(signature));\n"
        "    };\n"
        "    const verifyBridgeToken = async (token) => {\n"
        "      const raw = String(token || '').trim();\n"
        "      if (!bridgeSecret || !raw || !raw.includes('.')) return null;\n"
        "      const [encoded, signature] = raw.split('.', 2);\n"
        "      if (!encoded || !signature) return null;\n"
        "      const expected = await signBridgeToken(encoded);\n"
        "      if (expected !== signature) return null;\n"
        "      let payload = null;\n"
        "      try {\n"
        "        payload = JSON.parse(new TextDecoder().decode(fromBase64(encoded)));\n"
        "      } catch {\n"
        "        return null;\n"
        "      }\n"
        "      if (!payload || payload.aud !== 'propertyquarry.billing_sso_bridge') return null;\n"
        "      const now = Math.floor(Date.now() / 1000);\n"
        "      if (Number(payload.expires_at || 0) <= now) return null;\n"
        "      return {\n"
        "        principalId: String(payload.principal_id || '').trim(),\n"
        "        accessEmail: String(payload.access_email || '').trim().toLowerCase(),\n"
        "        planKey: String(payload.plan_key || '').trim().toLowerCase(),\n"
        "        returnTo: safeReturnPath(payload.return_to),\n"
        "        returnToOrigin: normalizePropertyOrigin(payload.return_to_origin || propertyOrigin),\n"
        "        token: raw,\n"
        "      };\n"
        "    };\n"
        "    const bridgeContextFromRequest = async () => verifyBridgeToken(readCookie(bridgeCookieName));\n"
        f"    const encodedBillingNoiseRules = {encoded_billing_noise_rules};\n"
        f"    const encodedBillingNoiseFragments = {encoded_billing_noise_fragments};\n"
        "    const decodeBillingNoiseValue = (value) => {\n"
        "      try {\n"
        "        return atob(String(value || ''));\n"
        "      } catch {\n"
        "        return '';\n"
        "      }\n"
        "    };\n"
        "    const billingNoiseRules = encodedBillingNoiseRules.map(([pattern, replacement]) => [\n"
        "      new RegExp(decodeBillingNoiseValue(pattern), 'ig'),\n"
        "      decodeBillingNoiseValue(replacement),\n"
        "    ]);\n"
        "    const scrubCustomerFacingBillingNoise = (value) => {\n"
        "      if (!value) return value;\n"
        "      let cleaned = String(value);\n"
        "      for (const [pattern, replacement] of billingNoiseRules) {\n"
        "        cleaned = cleaned.replace(pattern, replacement);\n"
        "      }\n"
        "      cleaned = cleaned\n"
        "        .replace(/>\\s*(?:[|·,:;\\/-]|&middot;|&#183;|&bull;|&#8226;)\\s*</g, '><')\n"
        "        .replace(/([>\\s])(?:[|·,:;\\/-]|&middot;|&#183;|&bull;|&#8226;)\\s*(?=<)/g, '$1');\n"
        "      for (let i = 0; i < 3; i += 1) {\n"
        "        const next = cleaned.replace(/<(p|li|div|span|strong|small|td|th)[^>]*>\\s*(?:&nbsp;|&#160;|[|·,:;\\/-]|&middot;|&#183;|&bull;|&#8226;|\\s)*<\\/\\1>/ig, '');\n"
        "        if (next === cleaned) break;\n"
        "        cleaned = next;\n"
        "      }\n"
        "      return cleaned;\n"
        "    };\n"
        "    const billingNoiseCleanupScript = (() => {\n"
        "      const encodedRulesJson = JSON.stringify(encodedBillingNoiseRules);\n"
        "      const encodedFragmentsJson = JSON.stringify(encodedBillingNoiseFragments);\n"
        "      return `<script>(function(){var decode=function(value){try{return atob(String(value||''));}catch(_error){return '';}};var encodedRules=${encodedRulesJson};var encodedFragments=${encodedFragmentsJson};var rules=[];for(var i=0;i<encodedRules.length;i+=1){rules.push([new RegExp(decode(encodedRules[i][0]),'ig'),decode(encodedRules[i][1])]);}var normalize=function(value){return String(value||'').toLowerCase().replace(/\\s+/g,' ').trim();};var fragments=[];for(var k=0;k<encodedFragments.length;k+=1){var fragment=normalize(decode(encodedFragments[k]));if(fragment)fragments.push(fragment);}var containsNoise=function(value){var normalized=normalize(value);if(!normalized)return false;for(var i=0;i<fragments.length;i+=1){if(normalized.indexOf(fragments[i])!==-1)return true;}return false;};var scrubText=function(value){var cleaned=String(value||'');for(var i=0;i<rules.length;i+=1){cleaned=cleaned.replace(rules[i][0],rules[i][1]);}cleaned=cleaned.replace(/\\s+([,.;:])/g,'$1').replace(/([.?!]){2,}/g,'$1').replace(/\\s{2,}/g,' ');cleaned=String(cleaned||'').replace(/^\\s+|\\s+$/g,'');return containsNoise(cleaned)?'':cleaned;};var selectors='p,li,div,span,strong,small,td,th,label,a,button';var sweep=function(root){if(!root||!root.querySelectorAll)return;var nodes=root.querySelectorAll(selectors);for(var i=0;i<nodes.length;i+=1){var node=nodes[i];if(node.closest&&node.closest('[data-pq-billing-bridge]'))continue;var original=String(node.textContent||'').replace(/\\s+/g,' ').trim();if(!original)continue;var cleaned=scrubText(original);if(!cleaned){node.remove();continue;}if(cleaned===original&&!containsNoise(original))continue;if(node.children.length===0){node.textContent=cleaned;continue;}if(original.length<=160||containsNoise(original)){node.remove();}}};if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',function(){sweep(document);});}else{sweep(document);}var observer=new MutationObserver(function(mutations){for(var i=0;i<mutations.length;i+=1){var mutation=mutations[i];for(var j=0;j<mutation.addedNodes.length;j+=1){var node=mutation.addedNodes[j];if(node&&node.nodeType===1){sweep(node);}}}});observer.observe(document.documentElement,{childList:true,subtree:true});})();</script>`;\n"
        "    })();\n"
        "    const planLabel = (value) => ({ agent: 'Agent lifetime', plus: 'Plus', free: 'Free' }[String(value || '').trim().toLowerCase()] || 'PropertyQuarry account');\n"
        "    const bridgeCardHtml = ({ title, detail, primaryHref, primaryLabel, secondaryHref, secondaryLabel, email, planKey }) => `<section class=\"pq-bridge-card\" data-pq-billing-bridge=\"1\"><p class=\"pq-bridge-eyebrow\">PropertyQuarry billing</p><h1>${htmlEscape(title)}</h1>${email ? `<p class=\"pq-bridge-email\">${htmlEscape(email)}</p>` : ''}${planKey ? `<p class=\"pq-bridge-plan\">${htmlEscape(planLabel(planKey))}</p>` : ''}<p>${htmlEscape(detail)}</p><div class=\"pq-bridge-actions\"><a class=\"pq-bridge-primary\" href=\"${htmlEscape(primaryHref)}\">${htmlEscape(primaryLabel)}</a>${secondaryHref ? `<a class=\"pq-bridge-secondary\" href=\"${htmlEscape(secondaryHref)}\">${htmlEscape(secondaryLabel || 'View plans')}</a>` : ''}</div></section>`;\n"
        "    const bridgeStyles = '<style>:root{color-scheme:light dark;--pq-bg:#f6f2ea;--pq-card:#fffaf0;--pq-ink:#201a12;--pq-muted:#6d6254;--pq-border:#e0d5c4;--pq-accent:#9a5a22}@media(prefers-color-scheme:dark){:root{--pq-bg:#15120e;--pq-card:#201a13;--pq-ink:#fff7ea;--pq-muted:#cbbda8;--pq-border:#3a3025;--pq-accent:#e3aa62}}.pq-bridge-card{width:min(100%,560px);margin:24px auto;padding:28px;border:1px solid var(--pq-border);border-radius:28px;background:color-mix(in srgb,var(--pq-card) 92%,transparent);box-shadow:0 24px 60px rgba(32,26,18,.12);font:16px/1.5 ui-serif,Georgia,serif;color:var(--pq-ink)}.pq-bridge-eyebrow{margin:0 0 10px;color:var(--pq-accent);font:700 12px/1.2 ui-sans-serif,system-ui,sans-serif;letter-spacing:.14em;text-transform:uppercase}.pq-bridge-card h1{margin:0 0 12px;font-size:clamp(30px,8vw,44px);line-height:.96;letter-spacing:-.04em}.pq-bridge-card p{margin:0 0 18px;color:var(--pq-muted)}.pq-bridge-actions{display:flex;flex-wrap:wrap;gap:12px}.pq-bridge-primary,.pq-bridge-secondary{display:inline-flex;min-height:48px;align-items:center;justify-content:center;border-radius:999px;padding:0 18px;font:700 14px/1 ui-sans-serif,system-ui,sans-serif;text-decoration:none}.pq-bridge-primary{background:var(--pq-ink);color:var(--pq-card)}.pq-bridge-secondary{border:1px solid var(--pq-border);color:var(--pq-ink)}.pq-bridge-email{margin:0 0 18px;font:600 14px/1.4 ui-sans-serif,system-ui,sans-serif;color:var(--pq-ink)}</style>';\n"
        "    const billingSkinStyles = '<style id=\"pq-billing-skin\">:root{color-scheme:light dark;--pq-bg:#f6f2ea;--pq-panel:#fffaf0;--pq-panel-2:#fffdf8;--pq-ink:#201a12;--pq-soft:#6d6254;--pq-line:#e0d5c4;--pq-gold:#9a5a22;--pq-shadow:0 24px 80px rgba(32,26,18,.12)}html,body{background:#f6f2ea!important;color:var(--pq-ink)!important}body{min-height:100vh!important;background:radial-gradient(circle at 12% 0%,rgba(154,90,34,.16),transparent 32%),linear-gradient(145deg,#f8f2e7,#f5ead9)!important;font-family:ui-serif,Georgia,serif!important}body:before{content:\"PropertyQuarry billing\";display:block;max-width:1120px;margin:18px auto 0;padding:0 18px;color:var(--pq-gold);font:800 12px/1.2 ui-sans-serif,system-ui,sans-serif;letter-spacing:.14em;text-transform:uppercase}.container,.container-fluid,.main,.content,.member-dashboard,.module,.module-wrapper,.account-content{max-width:1120px!important;margin-left:auto!important;margin-right:auto!important}.panel,.well,.card,.box,.module,.module-wrapper,.dashboard-panel,.member-panel,form{border:1px solid var(--pq-line)!important;border-radius:24px!important;background:color-mix(in srgb,var(--pq-panel) 94%,transparent)!important;box-shadow:var(--pq-shadow)!important}.panel-heading,.module h1,.module h2,.page-title,h1,h2,h3{color:var(--pq-ink)!important;font-family:ui-serif,Georgia,serif!important;letter-spacing:-.035em!important}.panel-heading,.panel-footer,.module-header{background:transparent!important;border-color:var(--pq-line)!important}p,li,label,td,th,small,.help-block,.text-muted{color:var(--pq-soft)!important}.btn,button,input[type=\"submit\"],a.btn{min-height:44px!important;border-radius:999px!important;border:1px solid var(--pq-line)!important;background:var(--pq-ink)!important;color:var(--pq-panel)!important;font:800 13px/1 ui-sans-serif,system-ui,sans-serif!important;text-decoration:none!important;box-shadow:none!important}.btn-default,.btn-link,a:not(.btn){color:var(--pq-ink)!important;background:transparent!important}.btn-primary,.btn-success{background:var(--pq-ink)!important;color:var(--pq-panel)!important}input,select,textarea{min-height:44px!important;border-radius:14px!important;border:1px solid var(--pq-line)!important;background:var(--pq-panel-2)!important;color:var(--pq-ink)!important;font-family:ui-sans-serif,system-ui,sans-serif!important}.navbar,.navbar-default,.header,.footer,.breadcrumb,.pagination{background:transparent!important;border-color:var(--pq-line)!important;box-shadow:none!important}.mobile-main-menu,#bs-main_menu,.tablet-menu-ul,.main_menu,.btn_get_listed,.btn_footer_get_listed,.homepage-join-module,.login-register-tabs,.display-recent-members,.slickMembers,a[href*=\"/join\"],a[href*=\"join=\"],a[href*=\"register\"]{display:none!important}.pq-bridge-plan{display:inline-flex;margin:0 0 16px;padding:8px 12px;border:1px solid var(--pq-line);border-radius:999px;background:var(--pq-panel-2);color:var(--pq-ink)!important;font:800 12px/1 ui-sans-serif,system-ui,sans-serif!important;letter-spacing:.03em}@media(max-width:760px){body:before{margin-top:10px;padding:0 12px}.panel,.well,.card,.box,.module,.module-wrapper,.dashboard-panel,.member-panel,form{border-radius:18px!important;box-shadow:none!important}.container,.container-fluid,.main,.content,.member-dashboard,.module,.module-wrapper,.account-content{padding-left:10px!important;padding-right:10px!important}}</style>';\n"
        "    const injectBillingSkin = (value) => {\n"
        "      const skin = billingSkinStyles;\n"
        "      if (!value) return skin;\n"
        "      if (/<head[^>]*>/i.test(value)) return value.replace(/<head[^>]*>/i, (match) => `${match}${skin}`);\n"
        "      return `${skin}${value}`;\n"
        "    };\n"
        "    const bridgeStandaloneHtml = (payload) => `<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><meta name=\"robots\" content=\"noindex, nofollow\">${bridgeStyles}<title>${htmlEscape(payload.title)}</title></head><body style=\"margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at 20% 0%,rgba(154,90,34,.16),transparent 34%),var(--pq-bg);\">${bridgeCardHtml(payload)}</body></html>`;\n"
        "    const bridgeCookieHeader = (token) => `${bridgeCookieName}=${encodeURIComponent(token)}; Max-Age=${bridgeCookieMaxAge}; Path=/; HttpOnly; Secure; SameSite=Lax`;\n"
        "    if (incoming.pathname === bridgePath) {\n"
        "      const bridgeContext = await verifyBridgeToken(incoming.searchParams.get('pq_bridge'));\n"
        "      if (!bridgeContext) {\n"
        "        const detail = bridgeSecret\n"
        "          ? 'This billing bridge link is no longer valid. Start again from your PropertyQuarry account.'\n"
        "          : 'This billing bridge is not configured yet. Start again from your PropertyQuarry account.';\n"
        "        const html = bridgeStandaloneHtml({\n"
        "          title: 'Billing bridge unavailable',\n"
        "          detail,\n"
        "          primaryHref: `${propertyOrigin}/app/account`,\n"
        "          primaryLabel: 'Back to account',\n"
        "          secondaryHref: pricingUrl,\n"
        "          secondaryLabel: 'View plans',\n"
        "          email: '',\n"
        "        });\n"
        "        return new Response(html, {\n"
        "          status: bridgeSecret ? 400 : 503,\n"
        "          headers: {\n"
        "            'cache-control': 'no-store',\n"
        "            'content-type': 'text/html; charset=utf-8',\n"
        "            'x-pq-billing-worker': 'propertyquarry-billing-handoff',\n"
        "            'x-pq-billing-worker-branch': 'bridge-unavailable',\n"
        "            'x-robots-tag': 'noindex, nofollow',\n"
        "          },\n"
        "        });\n"
        "      }\n"
        "      return new Response(null, {\n"
        "        status: 302,\n"
        "        headers: {\n"
        "          'location': `${incoming.origin}/account`,\n"
          "          'cache-control': 'no-store',\n"
        "          'set-cookie': bridgeCookieHeader(bridgeContext.token),\n"
        "          'x-pq-billing-worker': 'propertyquarry-billing-handoff',\n"
        "          'x-pq-billing-worker-branch': 'bridge-account-redirect',\n"
        "          'x-robots-tag': 'noindex, nofollow',\n"
        "        },\n"
        "      });\n"
        "    }\n"
        "    if (incoming.pathname === '/join' || incoming.pathname === '/join/' || incoming.pathname === '/account/upgrade' || incoming.pathname === '/account/upgrade/') {\n"
        "      return new Response(null, {\n"
        "        status: 302,\n"
        "        headers: {\n"
        "          'location': pricingUrl,\n"
        "          'cache-control': 'no-store',\n"
        "          'x-robots-tag': 'noindex, nofollow',\n"
        "          'x-pq-billing-worker': 'propertyquarry-billing-handoff',\n"
        "          'x-pq-billing-worker-branch': 'pricing-redirect',\n"
        "        },\n"
        "      });\n"
        "    }\n"
        "    const publicOrigin = incoming.origin;\n"
        "    const publicHost = incoming.hostname;\n"
        "    const rewriteToTargetText = (value) => {\n"
        "      if (!value) return value;\n"
        "      return String(value)\n"
        "        .replaceAll(publicOrigin, targetOrigin)\n"
        "        .replaceAll(`//${publicHost}`, `//${targetHost}`)\n"
        "        .replaceAll(publicHost, targetHost);\n"
        "    };\n"
        "    const upstreamUrl = new URL(incoming.pathname + incoming.search, targetOrigin + '/');\n"
        "    const upstreamHeaders = new Headers(request.headers);\n"
        "    upstreamHeaders.set('origin', targetOrigin);\n"
        "    const incomingReferer = request.headers.get('referer');\n"
        "    upstreamHeaders.set('referer', incomingReferer ? rewriteToTargetText(incomingReferer) : targetOrigin + '/');\n"
        "    const upstreamRequest = new Request(upstreamUrl.toString(), {\n"
        "      method: request.method,\n"
        "      headers: upstreamHeaders,\n"
        "      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,\n"
        "    });\n"
        "    const upstreamResponse = await fetch(upstreamRequest, { redirect: 'manual' });\n"
        "\n"
        "    const escapePattern = (value) => value.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');\n"
        "    const rewriteOriginText = (value) => {\n"
        "      if (!value) return value;\n"
        "      return String(value)\n"
        "        .replaceAll(targetOrigin, publicOrigin)\n"
        "        .replaceAll(`//${targetHost}`, `//${publicHost}`)\n"
        "        .replaceAll(targetHost, publicHost);\n"
        "    };\n"
        "    const rewriteCustomerFacingBillingLinks = (value) => {\n"
        "      if (!value) return value;\n"
        "      const heroHref = `${propertyOrigin}/`;\n"
        "      const publicOriginPattern = escapePattern(publicOrigin).replace(/\\/$/, '');\n"
        "      return String(value)\n"
        "        .replace(new RegExp(`href=(['\\\"])${publicOriginPattern}/?\\\\1`, 'ig'), `href=$1${heroHref}$1`)\n"
        "        .replace(/href=(['\\\"])(?:\\/|\\/home|\\/search_results|\\/events|\\/blog|\\/about|\\/about\\/contact)\\1/ig, `href=$1${heroHref}$1`)\n"
        "        .replace(/href=(['\\\"])(?:\\/join|\\/account\\/upgrade)\\1/ig, `href=$1${pricingUrl}$1`);\n"
        "    };\n"
        "    const stripPublicDirectoryChrome = (value) => {\n"
        "      if (!value) return value;\n"
        "      return String(value)\n"
        "        .replace(/<div\\b[^>]*class=(['\\\"])[^'\\\"]*mobile-main-menu[^'\\\"]*\\1[^>]*>[\\s\\S]*?<\\/div>/ig, '')\n"
        "        .replace(/<div\\b[^>]*id=(['\\\"])bs-main_menu\\1[^>]*>[\\s\\S]*?<\\/div>/ig, '')\n"
        "        .replace(/<ul\\b[^>]*class=(['\\\"])[^'\\\"]*(?:sidebar-nav|tablet-menu-ul|mini-nav)[^'\\\"]*\\1[^>]*>[\\s\\S]*?<\\/ul>/ig, '')\n"
        "        .replace(/<li\\b[^>]*>[\\s\\S]*?<a\\b[^>]*class=(['\\\"])[^'\\\"]*(?:btn_get_listed|btn_footer_get_listed)[^'\\\"]*\\1[\\s\\S]*?<\\/a>[\\s\\S]*?<\\/li>/ig, '')\n"
        "        .replace(/<a\\b[^>]*class=(['\\\"])[^'\\\"]*(?:btn_get_listed|btn_footer_get_listed)[^'\\\"]*\\1[\\s\\S]*?<\\/a>/ig, '')\n"
        "        .replace(/<section\\b[^>]*class=(['\\\"])[^'\\\"]*(?:display-recent-members|slickMembers)[^'\\\"]*\\1[\\s\\S]*?<\\/section>/ig, '');\n"
        "    };\n"
        "    const rewriteLocation = (value) => {\n"
        "      if (!value) return value;\n"
        "      try {\n"
        "        const nextUrl = new URL(String(value), targetOrigin + '/');\n"
        "        if ((nextUrl.pathname === '/join' || nextUrl.pathname === '/join/') && nextUrl.hostname === targetHost) {\n"
        "          return pricingUrl;\n"
        "        }\n"
        "        if (nextUrl.hostname === targetHost) {\n"
        "          nextUrl.protocol = incoming.protocol;\n"
        "          nextUrl.host = publicHost;\n"
        "        }\n"
        "        return nextUrl.toString();\n"
        "      } catch {\n"
        "        return rewriteOriginText(value);\n"
        "      }\n"
        "    };\n"
        "    const rewriteSetCookie = (value) => {\n"
        "      if (!value) return value;\n"
        "      const domainPattern = new RegExp(`Domain=${escapePattern(targetHost)}`, 'ig');\n"
        "      return String(value).replace(domainPattern, `Domain=${publicHost}`);\n"
        "    };\n"
        "    const getSetCookies = (headers) => {\n"
        "      if (headers && typeof headers.getAll === 'function') {\n"
        "        try {\n"
        "          const values = headers.getAll('Set-Cookie');\n"
        "          if (Array.isArray(values) && values.length) {\n"
        "            return values;\n"
        "          }\n"
        "        } catch {\n"
        "        }\n"
        "      }\n"
        "      const single = headers.get('set-cookie');\n"
        "      return single ? [single] : [];\n"
        "    };\n"
        "\n"
        "    const responseHeaders = new Headers(upstreamResponse.headers);\n"
        "    responseHeaders.set('x-pq-billing-worker', 'propertyquarry-billing-handoff');\n"
        "    responseHeaders.set('x-pq-billing-worker-branch', 'proxy');\n"
        "    responseHeaders.set('x-robots-tag', 'noindex, nofollow');\n"
        "    responseHeaders.delete('content-length');\n"
        "    const redirectLocation = responseHeaders.get('location');\n"
        "    if (redirectLocation) {\n"
        "      responseHeaders.set('location', rewriteLocation(redirectLocation));\n"
        "    }\n"
        "    const setCookies = getSetCookies(upstreamResponse.headers);\n"
        "    if (setCookies.length) {\n"
        "      responseHeaders.delete('set-cookie');\n"
        "      for (const cookie of setCookies) {\n"
        "        responseHeaders.append('set-cookie', rewriteSetCookie(cookie));\n"
        "      }\n"
        "    }\n"
        "\n"
        "    const contentType = String(responseHeaders.get('content-type') || '').toLowerCase();\n"
        "    if (!contentType.includes('text/html')) {\n"
        "      return new Response(upstreamResponse.body, {\n"
        "        status: upstreamResponse.status,\n"
        "        statusText: upstreamResponse.statusText,\n"
        "        headers: responseHeaders,\n"
        "      });\n"
        "    }\n"
        "\n"
        "    const bridgeContext = await bridgeContextFromRequest();\n"
        "    let html = injectBillingSkin(scrubCustomerFacingBillingNoise(stripPublicDirectoryChrome(rewriteCustomerFacingBillingLinks(rewriteOriginText(await upstreamResponse.text())))));\n"
        "    if (bridgeContext && incoming.pathname === '/account') {\n"
        "      const detail = bridgeContext.planKey\n"
        "        ? `PropertyQuarry access is managed on your main account. This billing lane is only the external account shell.`\n"
        "        : 'PropertyQuarry access is managed on your main account. This billing lane is only the external account shell.';\n"
        "      const banner = `${bridgeStyles}${bridgeCardHtml({\n"
        "        title: 'Billing account',\n"
        "        detail,\n"
        "        primaryHref: `${bridgeContext.returnToOrigin}${bridgeContext.returnTo}`,\n"
        "        primaryLabel: 'Back to PropertyQuarry',\n"
        "        secondaryHref: pricingUrl,\n"
        "        secondaryLabel: 'View plans',\n"
        "        email: bridgeContext.accessEmail,\n"
        "        planKey: bridgeContext.planKey,\n"
        "      })}`;\n"
        "      if (/<body[^>]*>/i.test(html)) {\n"
        "        html = html.replace(/<body[^>]*>/i, (match) => `${match}${banner}`);\n"
        "      } else {\n"
        "        html = `${banner}${html}`;\n"
        "      }\n"
        "    }\n"
        "    if (bridgeContext && incoming.pathname === '/login') {\n"
        "      const detail = bridgeContext.accessEmail\n"
        "        ? `Use ${bridgeContext.accessEmail} for the billing lane. Pricing stays on PropertyQuarry.`\n"
        "        : 'Use the same email as your PropertyQuarry account for the billing lane. Pricing stays on PropertyQuarry.';\n"
        "      const banner = `${bridgeStyles}${bridgeCardHtml({\n"
        "        title: 'Continue billing',\n"
        "        detail,\n"
        "        primaryHref: `${bridgeContext.returnToOrigin}${bridgeContext.returnTo}`,\n"
        "        primaryLabel: 'Back to PropertyQuarry',\n"
        "        secondaryHref: pricingUrl,\n"
        "        secondaryLabel: 'View plans',\n"
        "        email: bridgeContext.accessEmail,\n"
        "        planKey: bridgeContext.planKey,\n"
        "      })}`;\n"
        "      if (/<body[^>]*>/i.test(html)) {\n"
        "        html = html.replace(/<body[^>]*>/i, (match) => `${match}${banner}`);\n"
        "      } else {\n"
        "        html = `${banner}${html}`;\n"
        "      }\n"
        "      const emailJson = JSON.stringify(bridgeContext.accessEmail || '');\n"
        "      const pricingUrlJson = JSON.stringify(pricingUrl);\n"
        "      const assistScript = `<script>(function(){var email=${emailJson};var pricingUrl=${pricingUrlJson};var hideSelectors=['.login-register-tabs','.homepage-join-module','a[href*=\"/join\"]','a[href*=\"join=\"]','a[href*=\"register\"]'];if(document.title){document.title='PropertyQuarry billing sign-in';}var assistStyle=document.createElement('style');assistStyle.textContent='.login-register-tabs,.homepage-join-module,a[href*=\"/join\"],a[href*=\"join=\"],a[href*=\"register\"]{display:none !important;}';document.head.appendChild(assistStyle);for(var s=0;s<hideSelectors.length;s+=1){var nodes=document.querySelectorAll(hideSelectors[s]);for(var n=0;n<nodes.length;n+=1){nodes[n].remove();}}if(email){var inputs=document.querySelectorAll('input[name=\"email\"], input[type=\"email\"]');for(var i=0;i<inputs.length;i+=1){if(!inputs[i].value){inputs[i].value=email;}}var password=document.querySelector('input[name=\"pass\"], input[name=\"password\"], input[type=\"password\"]');if(password){password.focus();}}var joinLinks=document.querySelectorAll('a[href*=\"/join\"], a[href*=\"join\"]');for(var j=0;j<joinLinks.length;j+=1){joinLinks[j].setAttribute('href', pricingUrl);}var headings=document.querySelectorAll('h1,h2,h3');for(var h=0;h<headings.length;h+=1){var text=String(headings[h].textContent||'').trim().toLowerCase();if(text==='login'||text==='member login'||text==='sign in'){headings[h].textContent='Billing sign-in';break;}}})();</script>`;\n"
        "      html = /<\\/body>/i.test(html) ? html.replace(/<\\/body>/i, `${assistScript}</body>`) : `${html}${assistScript}`;\n"
        "    }\n"
        "    html = /<\\/body>/i.test(html) ? html.replace(/<\\/body>/i, `${billingNoiseCleanupScript}</body>`) : `${html}${billingNoiseCleanupScript}`;\n"
        "    return new Response(html, {\n"
        "      status: upstreamResponse.status,\n"
        "      statusText: upstreamResponse.statusText,\n"
        "      headers: responseHeaders,\n"
        "    });\n"
        "  },\n"
        "};\n"
    )


def _upsert_worker_script(
    *,
    account_id: str,
    headers: dict[str, str],
    script_name: str,
    source: str,
    bindings: list[dict[str, Any]] | None,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {
            "action": "deploy_script",
            "script_name": script_name,
            "binding_count": len(bindings or []),
            "dry_run": True,
        }
    response = requests.put(
        f"{API_BASE}/accounts/{account_id}/workers/scripts/{script_name}",
        headers={key: value for key, value in headers.items() if key.lower() != "content-type"},
        files={
            "metadata": (
                None,
                json.dumps({"main_module": "worker.mjs", "bindings": list(bindings or [])}),
                "application/json",
            ),
            "worker.mjs": ("worker.mjs", source, "application/javascript+module"),
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("success"):
        raise SystemExit(
            f"Cloudflare API error for /accounts/{account_id}/workers/scripts/{script_name}: "
            f"{json.dumps(body.get('errors') or body, ensure_ascii=True)}"
        )
    result = dict(body.get("result") or {})
    return {
        "action": "deploy_script",
        "script_name": script_name,
        "etag": str(result.get("etag") or ""),
        "size": int(result.get("size") or 0),
    }


def _upsert_worker_route(
    *,
    zone_id: str,
    headers: dict[str, str],
    route_pattern: str,
    script_name: str,
    dry_run: bool,
) -> dict[str, Any]:
    route_body = _cf_request(
        "GET",
        f"/zones/{zone_id}/workers/routes",
        headers=headers,
        params={"per_page": 100},
    )
    existing_routes = list(route_body.get("result") or [])
    current = next((row for row in existing_routes if str(row.get("pattern") or "").strip() == route_pattern), None)
    desired = {"pattern": route_pattern, "script": script_name}
    if current is None:
        if dry_run:
            return {"action": "create_route", "pattern": route_pattern, "script": script_name, "dry_run": True}
        body = _cf_request("POST", f"/zones/{zone_id}/workers/routes", headers=headers, payload=desired)
        return {"action": "create_route", "route": body.get("result") or {}}
    current_script = str(current.get("script") or "").strip()
    if current_script == script_name:
        return {"action": "already_ok", "route": current}
    route_id = str(current.get("id") or "").strip()
    if not route_id:
        raise SystemExit(f"Existing worker route for {route_pattern} is missing an id.")
    if dry_run:
        return {"action": "update_route", "pattern": route_pattern, "script": script_name, "dry_run": True}
    body = _cf_request("PUT", f"/zones/{zone_id}/workers/routes/{route_id}", headers=headers, payload=desired)
    return {"action": "update_route", "route": body.get("result") or {}}


def _ensure_dns_record_proxied(
    *,
    zone_id: str,
    headers: dict[str, str],
    host: str,
    target: str,
    dry_run: bool,
) -> dict[str, Any]:
    query = _cf_request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        headers=headers,
        params={"type": "CNAME", "name": host, "per_page": 100},
    )
    existing_records = list(query.get("result") or [])
    current = next(
        (
            row
            for row in existing_records
            if str(row.get("name") or "").strip().lower() == host
            and str(row.get("type") or "").strip().upper() == "CNAME"
        ),
        None,
    )
    desired = {
        "type": "CNAME",
        "name": host,
        "content": target,
        "ttl": 1,
        "proxied": True,
        "comment": "PropertyQuarry Brilliant Directories billing edge handoff",
    }
    if current is None:
        if dry_run:
            return {"action": "create_dns", "host": host, "target": target, "dry_run": True}
        body = _cf_request("POST", f"/zones/{zone_id}/dns_records", headers=headers, payload=desired)
        return {"action": "create_dns", "record": body.get("result") or {}}
    current_content = str(current.get("content") or "").strip().lower().rstrip(".")
    if current_content == target and bool(current.get("proxied")):
        return {"action": "already_ok", "record": current}
    updated = dict(current)
    updated.update(desired)
    record_id = str(current.get("id") or "").strip()
    if not record_id:
        raise SystemExit(f"Existing DNS record for {host} is missing an id.")
    if dry_run:
        return {"action": "update_dns", "host": host, "target": target, "dry_run": True}
    body = _cf_request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", headers=headers, payload=updated)
    return {"action": "update_dns", "record": body.get("result") or {}}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Front billing.propertyquarry.com with a Cloudflare Worker that reverse-proxies the external Brilliant Directories account lane."
    )
    parser.add_argument("--zone-name", default="propertyquarry.com", help="Cloudflare zone to update (default: propertyquarry.com).")
    parser.add_argument("--host", default="billing.propertyquarry.com", help="Billing host to front with the worker.")
    parser.add_argument("--target-host", default="propertyquarry.directoryup.com", help="Current Brilliant Directories host to receive the redirect.")
    parser.add_argument("--script-name", default="propertyquarry-billing-handoff", help="Cloudflare Worker script name.")
    parser.add_argument("--pricing-url", default="https://propertyquarry.com/pricing", help="PropertyQuarry-owned pricing URL to serve instead of the stock Brilliant Directories /join page.")
    parser.add_argument("--property-origin", default="", help="PropertyQuarry public origin used for bridge returns (defaults to PROPERTYQUARRY_PUBLIC_BASE_URL or the pricing origin).")
    parser.add_argument("--bridge-path", default="/sso/propertyquarry", help="Bridge entry path on the billing host.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing Cloudflare state.")
    args = parser.parse_args()

    host = str(args.host or "").strip().lower().rstrip(".")
    target_host = str(args.target_host or "").strip().lower().rstrip(".")
    if not host or not target_host:
        raise SystemExit("Both --host and --target-host are required.")
    route_pattern = f"{host}/*"
    target_base_url = f"https://{target_host}"
    pricing_url = str(args.pricing_url or "").strip()

    env = _effective_env()
    property_origin = str(args.property_origin or env.get("PROPERTYQUARRY_PUBLIC_BASE_URL") or "").strip()
    if not property_origin:
        parsed_pricing_origin = requests.utils.urlparse(pricing_url)
        property_origin = f"{parsed_pricing_origin.scheme}://{parsed_pricing_origin.netloc}"
    bridge_secret = str(
        env.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET")
        or env.get("BRILLIANT_DIRECTORIES_SSO_BRIDGE_SECRET")
        or ""
    ).strip()
    bindings: list[dict[str, Any]] = []
    if bridge_secret:
        bindings.append({"type": "plain_text", "name": "PQ_BRIDGE_SECRET", "text": bridge_secret})
    headers = _cf_headers(env)
    account_id = _discover_account_id(headers=headers, env=env)
    zone_id = _discover_zone_id(account_id=account_id, headers=headers, zone_name=str(args.zone_name or "").strip())
    source = _worker_source(
        target_base_url=target_base_url,
        pricing_url=pricing_url,
        property_origin=property_origin,
        bridge_path=str(args.bridge_path or "").strip() or "/sso/propertyquarry",
    )

    script_result = _upsert_worker_script(
        account_id=account_id,
        headers=headers,
        script_name=str(args.script_name or "").strip(),
        source=source,
        bindings=bindings,
        dry_run=args.dry_run,
    )
    route_result = _upsert_worker_route(
        zone_id=zone_id,
        headers=headers,
        route_pattern=route_pattern,
        script_name=str(args.script_name or "").strip(),
        dry_run=args.dry_run,
    )
    dns_result = _ensure_dns_record_proxied(
        zone_id=zone_id,
        headers=headers,
        host=host,
        target=target_host,
        dry_run=args.dry_run,
    )

    print(
        json.dumps(
            {
                "account_id": account_id,
                "zone_name": str(args.zone_name or "").strip(),
                "zone_id": zone_id,
                "host": host,
                "route_pattern": route_pattern,
                "target_base_url": target_base_url,
                "pricing_url": pricing_url,
                "property_origin": property_origin,
                "bridge_path": str(args.bridge_path or "").strip() or "/sso/propertyquarry",
                "bridge_secret_present": bool(bridge_secret),
                "script_result": script_result,
                "route_result": route_result,
                "dns_result": dns_result,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
