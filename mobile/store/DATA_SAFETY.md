# Data Safety declaration source

This document is the source for the Play Console declaration and must be reconciled with live behavior for every release.

The questionnaire below was saved in Google Play Console on 2026-08-12. It is
queued in Publishing overview and has **not** been sent for review.

## Data handled

| Data | Purpose | Stored | Shared with third parties |
|---|---|---:|---:|
| Account email and display name | Authentication and account identity | Yes | Google only during user-initiated sign-in |
| Property links and listing facts | Build private searches and shortlists | Yes | Property source and configured research providers as required to fetch/evaluate the listing |
| Search preferences and decisions | Personalization and comparison | Yes | No sale; provider processing only where required for the requested feature |
| App/runtime diagnostics | Reliability, abuse prevention and release verification | Bounded | Hosting/observability processors under the service contract |
| Push token | Notifications | Not collected until push is explicitly configured | Firebase Cloud Messaging only after configuration and consent |

All network traffic must use HTTPS. Identity cookies are HttpOnly and app login uses a short-lived, one-time PKCE-bound handoff. Users can sign out and use the account deletion controls surfaced by PropertyQuarry. Retention and deletion claims must match the live privacy policy and server receipts; the Android shell does not maintain a separate customer database.

## Console answers to verify

- Data is encrypted in transit: Yes.
- Users can request deletion: Yes, only after the live deletion flow is reverified.
- Data is sold: No.
- Advertising data use: No.
- Optional data: notifications/push token remains absent until the feature is configured and consented.

## Saved Play Console declaration

### Collection and security

- Collects or shares required user data: **Yes**.
- All collected data is encrypted in transit: **Yes**.
- Account creation methods: **Username and other authentication** and
  **OAuth**.
- Accounts can be created outside the app: **Yes**. The external method is
  declared as **Other** with the explanation: Google Accounts are created and
  managed by Google, then connected through OAuth.
- Account deletion URL: `https://propertyquarry.com/data-deletion`.
- Partial-data deletion instructions URL: **No**. PropertyQuarry has bounded
  revocation controls, but the release does not yet publish a separate public
  partial-deletion instructions URL.
- Data sold: **No**.
- Advertising use: **No**.

### Declared data types and handling

All rows below are declared as **collected**, **not shared**, **not processed
ephemerally**, and **required** for the released product. Transfers to
contracted hosting/observability processors, user-directed Google OAuth, and
provider processing required for the requested property-research feature are
handled under the applicable Play exemptions; PropertyQuarry does not sell the
data or disclose it to advertising partners.

| Play category | Data type | Purposes |
|---|---|---|
| Personal info | Name | App functionality; Account management |
| Personal info | Email address | App functionality; Account management |
| Personal info | User IDs | App functionality; Fraud prevention, security and compliance; Account management |
| App info and performance | Diagnostics | App functionality; Analytics; Fraud prevention, security and compliance |
| App activity | App interactions | App functionality; Analytics |
| App activity | In-app search history | App functionality; Personalisation |
| App activity | Other user-generated content | App functionality; Personalisation |

### Explicitly not declared for this release

The Android shell has no device-location permission and the released flow does
not collect precise or approximate device location, payment information,
purchase history, credit scores, other personal financial information, phone
numbers, contacts, messages, photos, videos, audio, files/documents, calendar
data, health data, browsing history, installed-app inventory, crash logs, other
app-performance data, or device/advertising identifiers. A target property
area, listing URL, price range, shortlist decision, or other search preference
is represented by the declared in-app search history or other user-generated
content categories; it is not represented as the user's device location or
personal payment data.
