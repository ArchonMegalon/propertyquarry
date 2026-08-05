# Privacy, offline and accessibility contract

PropertyQuarry uses the system browser for Google authentication and returns through an app-owned, PKCE-bound one-time code. The Android WebView accepts only HTTPS PropertyQuarry navigation; cleartext and mixed content are disabled, backups exclude native handoff state, and native pending auth/share data is cleared after successful use.

The product is online-first because listings, research, accounts and hosted tours are live services. A failed or mismatched runtime contract produces a local status and retry dialog. It must never silently display cached decision data as current.

Accessibility acceptance requires TalkBack-readable controls, logical focus order, no gesture-only critical action, 200% text support, adequate contrast, reduced-motion behavior and a non-VR path for every tour. VR and 3D-glasses viewing are enhancements, not required to inspect a property.
