package com.myexternalbrain.propertyquarry;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Set;

@CapacitorPlugin(name = "PropertyQuarryNative")
public final class PropertyQuarryNativePlugin extends Plugin {
    static final String PREFS_NAME = "propertyquarry_native";
    private static final String AUTH_CODE = "pending_auth_code";
    private static final String PKCE_VERIFIER = "pending_pkce_verifier";
    private static final String SHARED_URL = "pending_shared_property_url";
    private static final String SHARED_IDEMPOTENCY = "pending_shared_idempotency_key";

    private SharedPreferences preferences() {
        return getContext().getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
    }

    private boolean requireTrustedPath(PluginCall call, Set<String> allowedPaths) {
        String currentUrl = getBridge().getWebView().getUrl();
        Uri current = Uri.parse(String.valueOf(currentUrl == null ? "" : currentUrl));
        Uri expectedOrigin = Uri.parse(BuildConfig.PROPERTYQUARRY_ORIGIN);
        if (!String.valueOf(expectedOrigin.getScheme()).equals(current.getScheme())
            || !String.valueOf(expectedOrigin.getHost()).equals(current.getHost())
            || current.getUserInfo() != null
            || current.getPort() != expectedOrigin.getPort()
            || current.getQuery() != null
            || current.getFragment() != null
            || !allowedPaths.contains(String.valueOf(current.getPath()))) {
            call.reject("native_bridge_path_not_allowed");
            return false;
        }
        return true;
    }

    static void acceptAuthCode(Context context, String code) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(AUTH_CODE, code)
            .apply();
    }

    static void acceptSharedProperty(Context context, String propertyUrl, String idempotencyKey) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(SHARED_URL, propertyUrl)
            .putString(SHARED_IDEMPOTENCY, idempotencyKey)
            .apply();
    }

    static boolean hasPendingAuth(Context context) {
        return !context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getString(AUTH_CODE, "")
            .isBlank();
    }

    static boolean hasPendingShare(Context context) {
        return !context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .getString(SHARED_URL, "")
            .isBlank();
    }

    @PluginMethod
    public void getRuntimeInfo(PluginCall call) {
        if (!requireTrustedPath(call, Set.of("/app", "/app/search", "/app/shortlist"))) return;
        JSObject payload = new JSObject();
        payload.put("appId", BuildConfig.APPLICATION_ID);
        payload.put("buildType", BuildConfig.BUILD_TYPE);
        payload.put("versionName", BuildConfig.VERSION_NAME);
        payload.put("versionCode", BuildConfig.VERSION_CODE);
        payload.put("origin", BuildConfig.PROPERTYQUARRY_ORIGIN);
        payload.put("contractVersion", BuildConfig.MOBILE_CONTRACT_VERSION);
        call.resolve(payload);
    }

    @PluginMethod
    public void startExternalLogin(PluginCall call) {
        if (!isTrustedAppOrBridgePage(call)) return;
        try {
            Activity activity = getActivity();
            if (activity == null) {
                call.reject("secure_browser_unavailable");
                return;
            }
            launchExternalLogin(activity);
            call.resolve();
        } catch (Exception exception) {
            preferences().edit().remove(PKCE_VERIFIER).apply();
            call.reject("mobile_login_start_failed", exception);
        }
    }

    @PluginMethod
    public void getPendingAuth(PluginCall call) {
        if (!requireTrustedPath(call, Set.of("/mobile/auth/bridge"))) return;
        JSObject payload = new JSObject();
        payload.put("code", preferences().getString(AUTH_CODE, ""));
        payload.put("pkceVerifier", preferences().getString(PKCE_VERIFIER, ""));
        call.resolve(payload);
    }

    @PluginMethod
    public void clearPendingAuth(PluginCall call) {
        if (!requireTrustedPath(call, Set.of("/mobile/auth/bridge"))) return;
        preferences().edit().remove(AUTH_CODE).remove(PKCE_VERIFIER).apply();
        call.resolve();
    }

    @PluginMethod
    public void getPendingShare(PluginCall call) {
        if (!requireTrustedPath(
            call,
            Set.of("/mobile/auth/bridge", "/mobile/share/bridge")
        )) return;
        JSObject payload = new JSObject();
        payload.put("propertyUrl", preferences().getString(SHARED_URL, ""));
        payload.put("idempotencyKey", preferences().getString(SHARED_IDEMPOTENCY, ""));
        call.resolve(payload);
    }

    @PluginMethod
    public void clearPendingShare(PluginCall call) {
        if (!requireTrustedPath(call, Set.of("/mobile/share/bridge"))) return;
        preferences().edit().remove(SHARED_URL).remove(SHARED_IDEMPOTENCY).apply();
        call.resolve();
    }

    private boolean isTrustedAppOrBridgePage(PluginCall call) {
        String currentUrl = getBridge().getWebView().getUrl();
        Uri current = Uri.parse(String.valueOf(currentUrl == null ? "" : currentUrl));
        Uri expectedOrigin = Uri.parse(BuildConfig.PROPERTYQUARRY_ORIGIN);
        String path = String.valueOf(current.getPath());
        boolean allowed = String.valueOf(expectedOrigin.getScheme()).equals(current.getScheme())
            && String.valueOf(expectedOrigin.getHost()).equals(current.getHost())
            && current.getUserInfo() == null
            && current.getPort() == expectedOrigin.getPort()
            && current.getFragment() == null
            && (path.equals("/app") || path.startsWith("/app/")
                || path.equals("/mobile/share/bridge"));
        if (!allowed) call.reject("native_login_path_not_allowed");
        return allowed;
    }

    static void launchExternalLogin(Activity activity) throws Exception {
        byte[] random = new byte[32];
        new SecureRandom().nextBytes(random);
        String verifier = Base64.encodeToString(
            random,
            Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING
        );
        byte[] digest = MessageDigest.getInstance("SHA-256")
            .digest(verifier.getBytes(StandardCharsets.US_ASCII));
        String challenge = Base64.encodeToString(
            digest,
            Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING
        );
        SharedPreferences prefs = activity.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        prefs.edit()
            .remove(AUTH_CODE)
            .putString(PKCE_VERIFIER, verifier)
            .apply();
        Uri loginUri = Uri.parse(BuildConfig.PROPERTYQUARRY_ORIGIN + "/sign-in/google")
            .buildUpon()
            .appendQueryParameter("return_to", "/mobile/auth/complete")
            .appendQueryParameter("mobile_challenge", challenge)
            .build();
        Intent browser = new Intent(Intent.ACTION_VIEW, loginUri);
        browser.addCategory(Intent.CATEGORY_BROWSABLE);
        if (browser.resolveActivity(activity.getPackageManager()) == null) {
            prefs.edit().remove(PKCE_VERIFIER).apply();
            throw new IllegalStateException("secure_browser_unavailable");
        }
        activity.startActivity(browser);
    }
}
