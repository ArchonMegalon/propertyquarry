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

import org.json.JSONObject;

import java.util.Optional;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends BridgeActivity {
    private static final String AUTH_BRIDGE_PATH = "/mobile/auth/bridge";
    private static final String SHARE_BRIDGE_PATH = "/mobile/share/bridge";

    private final ExecutorService runtimeExecutor = Executors.newSingleThreadExecutor();
    private volatile boolean runtimeReady = false;
    private Intent pendingIntent;
    private PropertyQuarryRuntimeContract.Verified verifiedRuntime;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        registerPlugin(PropertyQuarryNativePlugin.class);
        super.onCreate(savedInstanceState);
        hardenWebView();
        pendingIntent = getIntent();
        verifyRuntimeAndContinue();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (!runtimeReady) {
            pendingIntent = intent;
            return;
        }
        handleIntent(intent);
    }

    @Override
    public void onDestroy() {
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
    }

    private void verifyRuntimeAndContinue() {
        runtimeReady = false;
        runtimeExecutor.execute(() -> {
            try {
                PropertyQuarryRuntimeContract.Verified verified = PropertyQuarryRuntimeContract.verify();
                runOnUiThread(() -> {
                    verifiedRuntime = verified;
                    runtimeReady = true;
                    Intent intent = pendingIntent;
                    pendingIntent = null;
                    if (intent == null || !handleIntent(intent)) {
                        resumePendingFlowOrStart();
                    }
                });
            } catch (Exception exception) {
                String reason = String.valueOf(exception.getMessage());
                runOnUiThread(() -> showRuntimeFailure(reason));
            }
        });
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
                    showMessage("Sign-in could not be completed", "The secure handoff was invalid. Please start sign-in again.");
                    return true;
                }
                PropertyQuarryNativePlugin.acceptAuthCode(this, code);
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
                    "No property link found",
                    "Share a secure HTTPS listing link from a supported property site."
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
            .setTitle("Add to PropertyQuarry?")
            .setMessage("Evaluate this listing and add it to your private shortlist?\n\n" + property.host())
            .setNegativeButton("Cancel", (dialog, which) -> resumePendingFlowOrStart())
            .setPositiveButton("Add property", (dialog, which) -> {
                PropertyQuarryNativePlugin.acceptSharedProperty(
                    this,
                    property.url(),
                    property.idempotencyKey()
                );
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

    private void showRuntimeFailure(String reason) {
        String safeReason = reason == null || reason.isBlank() ? "runtime_contract_unavailable" : reason;
        bridge.getWebView().evaluateJavascript(
            "window.dispatchEvent(new CustomEvent('propertyquarry:runtime-error',{detail:"
                + JSONObject.quote("Secure connection unavailable (" + safeReason + ")") + "}));",
            null
        );
        new AlertDialog.Builder(this)
            .setTitle("Secure connection unavailable")
            .setMessage("PropertyQuarry could not verify the app service. Check your connection and try again.")
            .setNegativeButton("Close", (dialog, which) -> finish())
            .setPositiveButton("Try again", (dialog, which) -> verifyRuntimeAndContinue())
            .setCancelable(false)
            .show();
    }

    void showMessage(String title, String message) {
        new AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setPositiveButton("OK", null)
            .show();
    }
}
