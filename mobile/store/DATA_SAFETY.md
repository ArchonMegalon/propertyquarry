# Data Safety declaration source

This document is the source for the Play Console declaration and must be reconciled with live behavior for every release.

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
