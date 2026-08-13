package com.myexternalbrain.propertyquarry;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public final class AppContractInstrumentedTest {
    @Test
    public void installedPackageOwnsShareAndAuthCallbackIntents() {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        assertEquals(BuildConfig.APPLICATION_ID, context.getPackageName());

        Intent share = new Intent(Intent.ACTION_SEND)
            .setType("text/plain")
            .putExtra(Intent.EXTRA_TEXT, "https://example.com/property/1");
        assertTrue(
            context.getPackageManager().queryIntentActivities(share, 0).stream()
                .anyMatch(info -> BuildConfig.APPLICATION_ID.equals(info.activityInfo.packageName))
        );

        Intent auth = new Intent(
            Intent.ACTION_VIEW,
            Uri.parse("propertyquarry://auth/callback?code=abcdefghijklmnopqrstuvwxyzABCDEFG_123456")
        );
        assertFalse(context.getPackageManager().queryIntentActivities(auth, 0).isEmpty());
    }

    @Test
    public void embeddedGoogleLoginInterceptorAcceptsOnlyTheCanonicalFirstPartyRoute() {
        assertTrue(PropertyQuarryWebViewClient.isTrustedGoogleSignIn(
            Uri.parse("https://propertyquarry.com/sign-in/google?return_to=%2Fapp%2Fsearch")
        ));
        assertFalse(PropertyQuarryWebViewClient.isTrustedGoogleSignIn(
            Uri.parse("https://www.propertyquarry.com/sign-in/google")
        ));
        assertFalse(PropertyQuarryWebViewClient.isTrustedGoogleSignIn(
            Uri.parse("https://propertyquarry.com/sign-in/google/other")
        ));
        assertFalse(PropertyQuarryWebViewClient.isTrustedGoogleSignIn(
            Uri.parse("https://propertyquarry.com/sign-in/google#untrusted")
        ));
        assertFalse(PropertyQuarryWebViewClient.isTrustedGoogleSignIn(
            Uri.parse("https://attacker@propertyquarry.com/sign-in/google")
        ));
    }

    @Test
    public void nativeAuthPayloadIsValidatedAndConsumedExactlyOnce() {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        context.getSharedPreferences(PropertyQuarryNativePlugin.PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .putString("pending_auth_code", "a".repeat(32))
            .putString("pending_pkce_verifier", "b".repeat(43))
            .commit();

        PropertyQuarryNativePlugin.PendingAuth pending =
            PropertyQuarryNativePlugin.consumePendingAuth(context).orElseThrow();

        assertEquals("a".repeat(32), pending.code());
        assertEquals("b".repeat(43), pending.pkceVerifier());
        assertFalse(PropertyQuarryNativePlugin.hasPendingAuth(context));
        assertTrue(PropertyQuarryNativePlugin.consumePendingAuth(context).isEmpty());
    }

    @Test
    public void nativeSharePayloadIsValidatedAndConsumedExactlyOnce() {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        SharedPropertyUrl.Value shared = SharedPropertyUrl.parse(
            "https://example.com/listing/secure-handoff"
        ).orElseThrow();
        PropertyQuarryNativePlugin.acceptSharedProperty(
            context,
            shared.url(),
            shared.idempotencyKey()
        );

        PropertyQuarryNativePlugin.PendingShare pending =
            PropertyQuarryNativePlugin.consumePendingShare(context).orElseThrow();

        assertEquals(shared.url(), pending.propertyUrl());
        assertEquals(shared.idempotencyKey(), pending.idempotencyKey());
        assertFalse(PropertyQuarryNativePlugin.hasPendingShare(context));
        assertTrue(PropertyQuarryNativePlugin.consumePendingShare(context).isEmpty());
    }
}
