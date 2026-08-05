# Google Play release checklist

## Identity and signing

- Package is exactly `com.myexternalbrain.propertyquarry`.
- Google Play App Signing is enabled; upload key and app-signing key are distinct.
- The app-signing SHA-256 fingerprint is present in the live `/.well-known/assetlinks.json` response.
- `adb shell pm get-app-links com.myexternalbrain.propertyquarry` reports the PropertyQuarry host as verified.
- External Google login, PKCE handoff, replay rejection and sign-out pass on a physical device.

## Product quality

- Search, results, shortlist, research, account and billing render at 360 dp and 600 dp widths.
- Shared property link requires native confirmation and a server POST; cancel causes no mutation.
- Diorama thumbnail is the preferred shortlist visual when available.
- Normal camera walkthrough is first/default; 3DVista/Matterport is secondary; VR is optional.
- Karl-Czerny-Gasse renders from the corrected source floorplan: no Vertragsgrundlage room/stamp in generated input, entrance in the VR room, and no invented stairwell-to-balcony connection.
- TalkBack labels, focus order, text scaling at 200%, contrast and reduced-motion behavior are checked.
- Offline/runtime-contract failure shows a retry state and never loads an unverified origin.

## Privacy and policy

- Privacy policy URL: `https://propertyquarry.com/privacy`.
- Data Safety answers match `DATA_SAFETY.md` and the current production behavior.
- Account deletion is available in the account surface and its live receipt is verified.
- No credentials, signing secrets, `google-services.json`, customer data or Memorial state are bundled.
- Notification permission is requested only in context after push is actually configured.

## Release evidence

- Python mobile/auth/schema/static tests pass.
- Android preview unit tests and APK build pass from a clean checkout.
- Release AAB is signed, `bundletool validate` passes and certificate digest is recorded.
- Emulator E2E covers cold start, external-auth bridge simulation, share cancel/confirm, App Link and tour hierarchy.
- Store screenshots, 512 px icon, 1024×500 feature graphic and listing copy are complete.
- Staged rollout stops before production submission until the user explicitly approves the irreversible Play action.
