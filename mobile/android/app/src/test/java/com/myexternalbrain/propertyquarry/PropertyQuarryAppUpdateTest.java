package com.myexternalbrain.propertyquarry;

import android.app.Activity;

import com.google.android.play.core.install.model.InstallStatus;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class PropertyQuarryAppUpdateTest {
    @Test
    public void requiredUpdateReasonIsExact() {
        assertTrue(PropertyQuarryAppUpdate.isRequiredUpdateReason("android_build_below_minimum"));
        assertFalse(PropertyQuarryAppUpdate.isRequiredUpdateReason("runtime_contract_unavailable"));
        assertFalse(PropertyQuarryAppUpdate.isRequiredUpdateReason(null));
        assertFalse(PropertyQuarryAppUpdate.isRequiredUpdateReason(""));
    }

    @Test
    public void policyForcesImmediateWhenBelowMinimum() {
        assertEquals(
            PropertyQuarryAppUpdate.Policy.IMMEDIATE,
            PropertyQuarryAppUpdate.policy(5, 6, false, 5)
        );
    }

    @Test
    public void policyUsesFlexibleWhenPlayHasANewerBuild() {
        assertEquals(
            PropertyQuarryAppUpdate.Policy.FLEXIBLE,
            PropertyQuarryAppUpdate.policy(6, 1, true, 7)
        );
    }

    @Test
    public void policyStaysIdleWhenCurrent() {
        assertEquals(
            PropertyQuarryAppUpdate.Policy.NONE,
            PropertyQuarryAppUpdate.policy(6, 1, false, 6)
        );
        assertEquals(
            PropertyQuarryAppUpdate.Policy.NONE,
            PropertyQuarryAppUpdate.policy(6, 1, true, 6)
        );
    }

    @Test
    public void requiredUpdateStartsImmediateWhenPlayAllowsIt() {
        assertEquals(
            PropertyQuarryAppUpdate.RequiredAction.START_IMMEDIATE,
            PropertyQuarryAppUpdate.requiredAction(true, true)
        );
    }

    @Test
    public void requiredUpdateFallsBackToPlayStoreWhenImmediateIsUnavailable() {
        assertEquals(
            PropertyQuarryAppUpdate.RequiredAction.OPEN_PLAY_STORE,
            PropertyQuarryAppUpdate.requiredAction(false, false)
        );
        assertEquals(
            PropertyQuarryAppUpdate.RequiredAction.OPEN_PLAY_STORE,
            PropertyQuarryAppUpdate.requiredAction(true, false)
        );
    }

    @Test
    public void cancelledRequiredFlowFallsBackButOptionalFlowDoesNot() {
        assertEquals(
            PropertyQuarryAppUpdate.FlowResultAction.OPEN_PLAY_STORE,
            PropertyQuarryAppUpdate.flowResultAction(Activity.RESULT_CANCELED, true)
        );
        assertEquals(
            PropertyQuarryAppUpdate.FlowResultAction.NONE,
            PropertyQuarryAppUpdate.flowResultAction(Activity.RESULT_CANCELED, false)
        );
        assertEquals(
            PropertyQuarryAppUpdate.FlowResultAction.NONE,
            PropertyQuarryAppUpdate.flowResultAction(Activity.RESULT_OK, true)
        );
    }

    @Test
    public void downloadedUpdatePromptsOnceWhileDialogIsVisible() {
        assertTrue(PropertyQuarryAppUpdate.shouldPromptToRestart(false, false, InstallStatus.DOWNLOADED));
        assertFalse(PropertyQuarryAppUpdate.shouldPromptToRestart(false, true, InstallStatus.DOWNLOADED));
        assertFalse(PropertyQuarryAppUpdate.shouldPromptToRestart(true, false, InstallStatus.DOWNLOADED));
        assertFalse(PropertyQuarryAppUpdate.shouldPromptToRestart(false, false, InstallStatus.INSTALLING));
    }

    @Test
    public void playStoreUrisTargetTheProductionPackage() {
        assertEquals(
            "market://details?id=com.myexternalbrain.propertyquarry",
            PropertyQuarryAppUpdate.PLAY_STORE_MARKET
        );
        assertEquals(
            "https://play.google.com/store/apps/details?id=com.myexternalbrain.propertyquarry",
            PropertyQuarryAppUpdate.PLAY_STORE_HTTPS
        );
    }
}
