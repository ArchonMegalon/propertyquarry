package com.myexternalbrain.propertyquarry;

import android.net.Uri;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;

import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeWebViewClient;

final class PropertyQuarryWebViewClient extends BridgeWebViewClient {
    private final MainActivity activity;

    PropertyQuarryWebViewClient(Bridge bridge, MainActivity activity) {
        super(bridge);
        this.activity = activity;
    }

    @Override
    public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
        Uri uri = request.getUrl();
        if (request.isForMainFrame() && isTrustedGoogleSignIn(uri)) {
            try {
                PropertyQuarryNativePlugin.launchExternalLogin(activity);
            } catch (Exception exception) {
                activity.showMessage(
                    "Secure browser unavailable",
                    "PropertyQuarry could not open the trusted browser. Check your browser settings and try again."
                );
            }
            return true;
        }
        return super.shouldOverrideUrlLoading(view, request);
    }

    @Override
    public void onPageFinished(WebView view, String url) {
        super.onPageFinished(view, url);
        activity.deliverPendingBridgePayload(view, url);
    }

    static boolean isTrustedGoogleSignIn(Uri uri) {
        Uri expectedOrigin = Uri.parse(BuildConfig.PROPERTYQUARRY_ORIGIN);
        return String.valueOf(expectedOrigin.getScheme()).equals(uri.getScheme())
            && String.valueOf(expectedOrigin.getHost()).equals(uri.getHost())
            && uri.getUserInfo() == null
            && uri.getPort() == expectedOrigin.getPort()
            && "/sign-in/google".equals(uri.getPath())
            && uri.getFragment() == null;
    }
}
