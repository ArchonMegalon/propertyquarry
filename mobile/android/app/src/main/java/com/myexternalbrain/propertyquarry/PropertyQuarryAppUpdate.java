package com.myexternalbrain.propertyquarry;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.net.Uri;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.IntentSenderRequest;

import com.google.android.play.core.appupdate.AppUpdateInfo;
import com.google.android.play.core.appupdate.AppUpdateManager;
import com.google.android.play.core.appupdate.AppUpdateManagerFactory;
import com.google.android.play.core.appupdate.AppUpdateOptions;
import com.google.android.play.core.install.InstallStateUpdatedListener;
import com.google.android.play.core.install.model.AppUpdateType;
import com.google.android.play.core.install.model.InstallStatus;
import com.google.android.play.core.install.model.UpdateAvailability;

final class PropertyQuarryAppUpdate {
    static final String PRODUCTION_APP_ID = "com.myexternalbrain.propertyquarry";
    static final String PLAY_STORE_MARKET =
        "market://details?id=" + PRODUCTION_APP_ID;
    static final String PLAY_STORE_HTTPS =
        "https://play.google.com/store/apps/details?id=" + PRODUCTION_APP_ID;

    enum Policy {
        NONE,
        FLEXIBLE,
        IMMEDIATE
    }

    private AppUpdateManager manager;
    private InstallStateUpdatedListener listener;
    private ActivityResultLauncher<IntentSenderRequest> launcher;
    private boolean flexibleStarted;

    static boolean isRequiredUpdateReason(String reason) {
        return "android_build_below_minimum".equals(reason);
    }

    static Policy policy(
        int localVersionCode,
        int minimumBuild,
        boolean playUpdateAvailable,
        int playAvailableVersionCode
    ) {
        if (localVersionCode < minimumBuild) {
            return Policy.IMMEDIATE;
        }
        if (playUpdateAvailable && playAvailableVersionCode > localVersionCode) {
            return Policy.FLEXIBLE;
        }
        return Policy.NONE;
    }

    static Uri playStoreMarketUri() {
        return Uri.parse(PLAY_STORE_MARKET);
    }

    static Uri playStoreHttpsUri() {
        return Uri.parse(PLAY_STORE_HTTPS);
    }

    static boolean openPlayStore(Activity activity) {
        Intent market = new Intent(Intent.ACTION_VIEW, playStoreMarketUri());
        try {
            activity.startActivity(market);
            return true;
        } catch (ActivityNotFoundException ignored) {
            try {
                activity.startActivity(new Intent(Intent.ACTION_VIEW, playStoreHttpsUri()));
                return true;
            } catch (ActivityNotFoundException missing) {
                return false;
            }
        }
    }

    void attach(Activity activity, ActivityResultLauncher<IntentSenderRequest> updateLauncher) {
        launcher = updateLauncher;
        if (!PRODUCTION_APP_ID.equals(BuildConfig.APPLICATION_ID)) {
            return;
        }
        manager = AppUpdateManagerFactory.create(activity.getApplicationContext());
        listener = state -> {
            if (state.installStatus() == InstallStatus.DOWNLOADED) {
                promptToRestart(activity);
            }
        };
        manager.registerListener(listener);
    }

    void detach() {
        if (manager != null && listener != null) {
            manager.unregisterListener(listener);
        }
        listener = null;
        manager = null;
        launcher = null;
        flexibleStarted = false;
    }

    void onRuntimeReady(Activity activity) {
        check(activity, false);
    }

    void onRequiredUpdate(Activity activity) {
        check(activity, true);
    }

    void onResume(Activity activity) {
        if (manager == null) {
            return;
        }
        manager.getAppUpdateInfo().addOnSuccessListener(info -> {
            if (info.updateAvailability() == UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) {
                start(activity, info, AppUpdateType.IMMEDIATE);
                return;
            }
            if (info.installStatus() == InstallStatus.DOWNLOADED) {
                promptToRestart(activity);
            }
        });
    }

    void onFlowResult(Activity activity, int resultCode, boolean required) {
        if (resultCode == Activity.RESULT_OK) {
            return;
        }
        if (required) {
            openPlayStore(activity);
        }
    }

    private void check(Activity activity, boolean required) {
        if (manager == null) {
            if (required) {
                openPlayStore(activity);
            }
            return;
        }
        manager.getAppUpdateInfo()
            .addOnSuccessListener(info -> handleInfo(activity, info, required))
            .addOnFailureListener(error -> {
                if (required) {
                    openPlayStore(activity);
                }
            });
    }

    private void handleInfo(Activity activity, AppUpdateInfo info, boolean required) {
        if (info.updateAvailability() == UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) {
            start(activity, info, AppUpdateType.IMMEDIATE);
            return;
        }
        if (info.installStatus() == InstallStatus.DOWNLOADED) {
            promptToRestart(activity);
            return;
        }
        boolean available = info.updateAvailability() == UpdateAvailability.UPDATE_AVAILABLE;
        int availableCode = available ? info.availableVersionCode() : BuildConfig.VERSION_CODE;
        if (required) {
            if (available && info.isUpdateTypeAllowed(AppUpdateType.IMMEDIATE)) {
                start(activity, info, AppUpdateType.IMMEDIATE);
                return;
            }
            openPlayStore(activity);
            return;
        }
        Policy decided = policy(BuildConfig.VERSION_CODE, 1, available, availableCode);
        if (decided == Policy.FLEXIBLE && !flexibleStarted) {
            if (info.isUpdateTypeAllowed(AppUpdateType.FLEXIBLE)) {
                flexibleStarted = true;
                start(activity, info, AppUpdateType.FLEXIBLE);
            } else if (info.isUpdateTypeAllowed(AppUpdateType.IMMEDIATE)) {
                start(activity, info, AppUpdateType.IMMEDIATE);
            }
        }
    }

    private void start(Activity activity, AppUpdateInfo info, int type) {
        if (launcher == null || manager == null) {
            if (type == AppUpdateType.IMMEDIATE) {
                openPlayStore(activity);
            }
            return;
        }
        try {
            manager.startUpdateFlowForResult(
                info,
                launcher,
                AppUpdateOptions.newBuilder(type).build()
            );
        } catch (Exception exception) {
            if (type == AppUpdateType.IMMEDIATE) {
                openPlayStore(activity);
            }
        }
    }

    private void promptToRestart(Activity activity) {
        if (activity.isFinishing()) {
            return;
        }
        new AlertDialog.Builder(activity)
            .setTitle("Update ready")
            .setMessage("PropertyQuarry downloaded an official Google Play update. Restart to install it.")
            .setPositiveButton("Restart", (dialog, which) -> completeUpdate())
            .setNegativeButton("Later", null)
            .setCancelable(true)
            .show();
    }

    private void completeUpdate() {
        if (manager != null) {
            manager.completeUpdate();
        }
    }
}
