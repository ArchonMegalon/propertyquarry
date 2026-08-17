package com.myexternalbrain.propertyquarry;

import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.JSExport;
import com.getcapacitor.PluginHandle;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.IntentSenderRequest;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import org.json.JSONObject;

import java.util.Collections;
import java.util.Optional;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends BridgeActivity {
    private static final String AUTH_BRIDGE_PATH = "/mobile/auth/bridge";
    private static final String SHARE_BRIDGE_PATH = "/mobile/share/bridge";
    private static final int BRIDGE_READY_MAX_ATTEMPTS = 100;
    private static final long BRIDGE_READY_RETRY_MILLIS = 100L;

    private final ExecutorService runtimeExecutor = Executors.newSingleThreadExecutor();
    private volatile boolean runtimeReady = false;
    private boolean activityResumed = false;
    private boolean navigationStarted = false;
    private boolean nativeAuthPayloadDelivered = false;
    private boolean nativeSharePayloadDelivered = false;
    private boolean nativeAuthPayloadDeliveryScheduled = false;
    private boolean nativeSharePayloadDeliveryScheduled = false;
    private int nativeAuthPayloadGeneration = 0;
    private int nativeSharePayloadGeneration = 0;
    private Intent pendingIntent;
    private PropertyQuarryRuntimeContract.Verified verifiedRuntime;
    private final PropertyQuarryAppUpdate appUpdate = new PropertyQuarryAppUpdate();
    private ActivityResultLauncher<IntentSenderRequest> appUpdateLauncher;
    private boolean requiredUpdatePending;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        appUpdateLauncher = registerForActivityResult(
            new ActivityResultContracts.StartIntentSenderForResult(),
            result -> appUpdate.onFlowResult(this, result.getResultCode(), requiredUpdatePending)
        );
        registerPlugin(PropertyQuarryNativePlugin.class);
        super.onCreate(savedInstanceState);
        appUpdate.attach(this, appUpdateLauncher);
        hardenWebView();
        pendingIntent = getIntent();
        verifyRuntimeAndContinue();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        pendingIntent = intent;
        continueWhenReady();
    }

    @Override
    public void onResume() {
        super.onResume();
        activityResumed = true;
        appUpdate.onResume(this);
        continueWhenReady();
    }

    @Override
    public void onPause() {
        activityResumed = false;
        super.onPause();
    }

    @Override
    public void onDestroy() {
        nativeAuthPayloadGeneration++;
        nativeSharePayloadGeneration++;
        nativeAuthPayloadDeliveryScheduled = false;
        nativeSharePayloadDeliveryScheduled = false;
        appUpdate.detach();
        runtimeExecutor.shutdownNow();
        super.onDestroy();
    }

    private void hardenWebView() {
        WebView webView = bridge.getWebView();
        webView.setWebViewClient(new PropertyQuarryWebViewClient(bridge, this));
        WebSettings settings = webView.getSettings();
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setSaveFormData(false);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            settings.setSafeBrowsingEnabled(true);
        }
        settings.setUserAgentString(settings.getUserAgentString() + " PropertyQuarryAndroid/" + BuildConfig.VERSION_NAME);
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, false);
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        installTrustedRemoteBridge(webView);
    }

    private void installTrustedRemoteBridge(WebView webView) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) return;
        PluginHandle nativePlugin = bridge.getPlugin("PropertyQuarryNative");
        if (nativePlugin == null) {
            throw new IllegalStateException("propertyquarry_native_plugin_missing");
        }
        try {
            String localOrigin = bridge.getScheme() + "://" + bridge.getHost();
            String script = JSExport.getGlobalJS(this, bridge.getConfig().isLoggingEnabled(), bridge.isDevMode())
                + "\nwindow.WEBVIEW_SERVER_URL = " + JSONObject.quote(localOrigin) + ";\n"
                + JSExport.getBridgeJS(this) + "\n"
                + JSExport.getPluginJS(Collections.singletonList(nativePlugin));
            WebViewCompat.addDocumentStartJavaScript(
                webView,
                script,
                Collections.singleton(BuildConfig.PROPERTYQUARRY_ORIGIN)
            );
        } catch (Exception exception) {
            throw new IllegalStateException("trusted_remote_bridge_install_failed", exception);
        }
    }

    private void verifyRuntimeAndContinue() {
        runtimeReady = false;
        runtimeExecutor.execute(() -> {
            try {
                PropertyQuarryRuntimeContract.Verified verified = PropertyQuarryRuntimeContract.verify();
                runOnUiThread(() -> {
                    verifiedRuntime = verified;
                    runtimeReady = true;
                    requiredUpdatePending = false;
                    appUpdate.onRuntimeReady(this);
                    continueWhenReady();
                });
            } catch (Exception exception) {
                String reason = String.valueOf(exception.getMessage());
                runOnUiThread(() -> showRuntimeFailure(reason));
            }
        });
    }

    private void continueWhenReady() {
        if (!runtimeReady || !activityResumed) return;
        Intent intent = pendingIntent;
        pendingIntent = null;
        if (intent != null && handleIntent(intent)) {
            navigationStarted = true;
            return;
        }
        if (!navigationStarted) {
            navigationStarted = true;
            resumePendingFlowOrStart();
        }
    }

    private void resumePendingFlowOrStart() {
        if (PropertyQuarryNativePlugin.hasPendingAuth(this)) {
            loadTrustedPath(AUTH_BRIDGE_PATH);
        } else if (PropertyQuarryNativePlugin.hasPendingShare(this)) {
            loadTrustedPath(SHARE_BRIDGE_PATH);
        } else {
            loadTrustedPath(verifiedRuntime.startPath());
        }
    }

    private boolean handleIntent(Intent intent) {
        if (intent == null) return false;
        Uri data = intent.getData();
        if (Intent.ACTION_VIEW.equals(intent.getAction()) && data != null) {
            if (isAuthCallback(data)) {
                String code = String.valueOf(data.getQueryParameter("code"));
                if (!code.matches("[A-Za-z0-9_-]{32,160}")) {
                    showMessage(
                        R.string.native_sign_in_failed_title,
                        R.string.native_sign_in_invalid_handoff
                    );
                    return true;
                }
                PropertyQuarryNativePlugin.acceptAuthCode(this, code);
                nativeAuthPayloadDelivered = false;
                nativeAuthPayloadDeliveryScheduled = false;
                nativeAuthPayloadGeneration++;
                loadTrustedPath(AUTH_BRIDGE_PATH);
                return true;
            }
            if (isTrustedAppLink(data)) {
                String path = String.valueOf(data.getEncodedPath());
                String query = data.getEncodedQuery();
                loadTrustedPath(path + (query == null || query.isBlank() ? "" : "?" + query));
                return true;
            }
        }
        if (Intent.ACTION_SEND.equals(intent.getAction()) && "text/plain".equals(intent.getType())) {
            CharSequence shared = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
            Optional<SharedPropertyUrl.Value> parsed = SharedPropertyUrl.parse(
                shared == null ? "" : shared.toString()
            );
            if (parsed.isEmpty()) {
                showMessage(
                    R.string.native_no_property_link_title,
                    R.string.native_no_property_link_message
                );
                return true;
            }
            confirmSharedProperty(parsed.get());
            return true;
        }
        return false;
    }

    private void confirmSharedProperty(SharedPropertyUrl.Value property) {
        new AlertDialog.Builder(this)
            .setTitle(R.string.native_add_property_title)
            .setMessage(getString(R.string.native_add_property_message, property.host()))
            .setNegativeButton(R.string.native_cancel, (dialog, which) -> resumePendingFlowOrStart())
            .setPositiveButton(R.string.native_add_property, (dialog, which) -> {
                PropertyQuarryNativePlugin.acceptSharedProperty(
                    this,
                    property.url(),
                    property.idempotencyKey()
                );
                nativeSharePayloadDelivered = false;
                nativeSharePayloadDeliveryScheduled = false;
                nativeSharePayloadGeneration++;
                loadTrustedPath(SHARE_BRIDGE_PATH);
            })
            .setCancelable(true)
            .setOnCancelListener(dialog -> resumePendingFlowOrStart())
            .show();
    }

    private boolean isAuthCallback(Uri uri) {
        return "propertyquarry".equals(uri.getScheme())
            && "auth".equals(uri.getHost())
            && "/callback".equals(uri.getPath())
            && uri.getQueryParameterNames().size() == 1
            && uri.getQueryParameterNames().contains("code");
    }

    private boolean isTrustedAppLink(Uri uri) {
        if (!"https".equals(uri.getScheme()) || !"propertyquarry.com".equals(uri.getHost())
            || uri.getUserInfo() != null || uri.getPort() != -1 || uri.getFragment() != null) {
            return false;
        }
        String path = String.valueOf(uri.getPath());
        return path.equals("/app") || path.startsWith("/app/")
            || path.equals("/shortlist") || path.startsWith("/shortlist/");
    }

    private void loadTrustedPath(String pathAndQuery) {
        if (verifiedRuntime == null || pathAndQuery == null || pathAndQuery.length() > 4096
            || !pathAndQuery.startsWith("/") || pathAndQuery.startsWith("//")
            || pathAndQuery.contains("\\") || pathAndQuery.indexOf('\0') >= 0) {
            showRuntimeFailure("trusted_path_invalid");
            return;
        }
        Uri relative = Uri.parse(pathAndQuery);
        if (relative.isAbsolute() || relative.getHost() != null || relative.getFragment() != null) {
            showRuntimeFailure("trusted_path_invalid");
            return;
        }
        bridge.getWebView().loadUrl(verifiedRuntime.origin() + pathAndQuery);
    }

    void schedulePendingBridgePayloadDelivery(WebView webView, String url) {
        if (isExactTrustedBridgeUrl(url, AUTH_BRIDGE_PATH)
            && !nativeAuthPayloadDelivered
            && !nativeAuthPayloadDeliveryScheduled) {
            nativeAuthPayloadDeliveryScheduled = true;
            probeBridgeReadiness(
                webView,
                AUTH_BRIDGE_PATH,
                nativeAuthPayloadGeneration,
                0
            );
            return;
        }
        if (isExactTrustedBridgeUrl(url, SHARE_BRIDGE_PATH)
            && !nativeSharePayloadDelivered
            && !nativeSharePayloadDeliveryScheduled) {
            nativeSharePayloadDeliveryScheduled = true;
            probeBridgeReadiness(
                webView,
                SHARE_BRIDGE_PATH,
                nativeSharePayloadGeneration,
                0
            );
        }
    }

    private void probeBridgeReadiness(
        WebView webView,
        String path,
        int generation,
        int attempt
    ) {
        boolean authMode = AUTH_BRIDGE_PATH.equals(path);
        boolean obsolete = authMode
            ? generation != nativeAuthPayloadGeneration || nativeAuthPayloadDelivered
            : generation != nativeSharePayloadGeneration || nativeSharePayloadDelivered;
        if (obsolete) return;
        if (!isExactTrustedBridgeUrl(webView.getUrl(), path)) {
            if (authMode) {
                nativeAuthPayloadDeliveryScheduled = false;
            } else {
                nativeSharePayloadDeliveryScheduled = false;
            }
            return;
        }
        webView.evaluateJavascript(
            "window.__propertyQuarryNativeBridgeReady === true",
            result -> {
                boolean callbackObsolete = authMode
                    ? generation != nativeAuthPayloadGeneration || nativeAuthPayloadDelivered
                    : generation != nativeSharePayloadGeneration || nativeSharePayloadDelivered;
                if (callbackObsolete) return;
                if (!isExactTrustedBridgeUrl(webView.getUrl(), path)) {
                    if (authMode) {
                        nativeAuthPayloadDeliveryScheduled = false;
                    } else {
                        nativeSharePayloadDeliveryScheduled = false;
                    }
                    return;
                }
                if ("true".equals(result)) {
                    deliverPendingBridgePayload(webView, path);
                    return;
                }
                if (attempt + 1 < BRIDGE_READY_MAX_ATTEMPTS) {
                    webView.postDelayed(
                        () -> probeBridgeReadiness(webView, path, generation, attempt + 1),
                        BRIDGE_READY_RETRY_MILLIS
                    );
                    return;
                }
                if (authMode) {
                    nativeAuthPayloadDeliveryScheduled = false;
                    showMessage(
                        R.string.native_sign_in_failed_title,
                        R.string.native_auth_page_not_ready
                    );
                } else {
                    nativeSharePayloadDeliveryScheduled = false;
                    showMessage(
                        R.string.native_property_add_failed_title,
                        R.string.native_share_page_not_ready
                    );
                }
            }
        );
    }

    private void deliverPendingBridgePayload(WebView webView, String path) {
        if (AUTH_BRIDGE_PATH.equals(path) && !nativeAuthPayloadDelivered) {
            Optional<PropertyQuarryNativePlugin.PendingAuth> pending;
            try {
                pending = PropertyQuarryNativePlugin.consumePendingAuth(this);
            } catch (Exception exception) {
                nativeAuthPayloadDeliveryScheduled = false;
                showMessage(
                    R.string.native_sign_in_failed_title,
                    R.string.native_auth_cleanup_failed
                );
                return;
            }
            if (pending.isEmpty()) return;
            nativeAuthPayloadDelivered = true;
            PropertyQuarryNativePlugin.PendingAuth auth = pending.get();
            String script = "window.__propertyQuarryNativeAuthOwned=true;"
                + "window.dispatchEvent(new CustomEvent('propertyquarry:native-auth-payload',{detail:{code:"
                + JSONObject.quote(auth.code())
                + ",pkceVerifier:" + JSONObject.quote(auth.pkceVerifier())
                + ",hasPendingShare:" + PropertyQuarryNativePlugin.hasPendingShare(this)
                + "}}));";
            webView.evaluateJavascript(script, null);
            return;
        }
        if (SHARE_BRIDGE_PATH.equals(path) && !nativeSharePayloadDelivered) {
            Optional<PropertyQuarryNativePlugin.PendingShare> pending;
            try {
                pending = PropertyQuarryNativePlugin.consumePendingShare(this);
            } catch (Exception exception) {
                nativeSharePayloadDeliveryScheduled = false;
                showMessage(
                    R.string.native_property_add_failed_title,
                    R.string.native_share_cleanup_failed
                );
                return;
            }
            if (pending.isEmpty()) return;
            nativeSharePayloadDelivered = true;
            PropertyQuarryNativePlugin.PendingShare share = pending.get();
            String script = "window.__propertyQuarryNativeShareOwned=true;"
                + "window.dispatchEvent(new CustomEvent('propertyquarry:native-share-payload',{detail:{propertyUrl:"
                + JSONObject.quote(share.propertyUrl())
                + ",idempotencyKey:" + JSONObject.quote(share.idempotencyKey())
                + "}}));";
            webView.evaluateJavascript(script, null);
        }
    }

    private boolean isExactTrustedBridgeUrl(String url, String path) {
        Uri current = Uri.parse(String.valueOf(url == null ? "" : url));
        Uri expected = Uri.parse(BuildConfig.PROPERTYQUARRY_ORIGIN);
        return String.valueOf(expected.getScheme()).equals(current.getScheme())
            && String.valueOf(expected.getHost()).equals(current.getHost())
            && current.getUserInfo() == null
            && current.getPort() == expected.getPort()
            && current.getQuery() == null
            && current.getFragment() == null
            && path.equals(current.getPath());
    }

    private void showRuntimeFailure(String reason) {
        String safeReason = reason == null || reason.isBlank() ? "runtime_contract_unavailable" : reason;
        if (PropertyQuarryAppUpdate.isRequiredUpdateReason(safeReason)) {
            requiredUpdatePending = true;
            bridge.getWebView().evaluateJavascript(
                "window.dispatchEvent(new CustomEvent('propertyquarry:runtime-error',{detail:"
                    + JSONObject.quote(
                        getString(R.string.native_update_required_runtime_message)
                    ) + "}));",
                null
            );
            new AlertDialog.Builder(this)
                .setTitle(R.string.native_update_required_title)
                .setMessage(R.string.native_update_required_message)
                .setNegativeButton(R.string.native_close, (dialog, which) -> finish())
                .setPositiveButton(R.string.native_update, (dialog, which) -> appUpdate.onRequiredUpdate(this))
                .setCancelable(false)
                .show();
            return;
        }
        requiredUpdatePending = false;
        bridge.getWebView().evaluateJavascript(
            "window.dispatchEvent(new CustomEvent('propertyquarry:runtime-error',{detail:"
                + JSONObject.quote(getString(R.string.native_secure_connection_message)) + "}));",
            null
        );
        new AlertDialog.Builder(this)
            .setTitle(R.string.native_secure_connection_title)
            .setMessage(R.string.native_secure_connection_message)
            .setNegativeButton(R.string.native_close, (dialog, which) -> finish())
            .setPositiveButton(R.string.native_try_again, (dialog, which) -> verifyRuntimeAndContinue())
            .setCancelable(false)
            .show();
    }

    void showMessage(int title, int message) {
        new AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setPositiveButton(R.string.native_ok, null)
            .show();
    }
}
