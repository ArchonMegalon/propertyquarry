package com.myexternalbrain.propertyquarry;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

final class PropertyQuarryRuntimeContract {
    static final int MAX_RESPONSE_BYTES = 8192;
    static final int CONNECT_TIMEOUT_MS = 6000;
    static final int READ_TIMEOUT_MS = 6000;

    record Verified(String origin, String startPath) {}

    private PropertyQuarryRuntimeContract() {}

    static Verified verify() throws Exception {
        URI origin = URI.create(BuildConfig.PROPERTYQUARRY_ORIGIN);
        if (!"https".equals(origin.getScheme()) || origin.getHost() == null
            || origin.getUserInfo() != null || origin.getQuery() != null
            || origin.getFragment() != null || (origin.getPath() != null && !origin.getPath().isEmpty())) {
            throw new IllegalStateException("runtime_origin_invalid");
        }
        URL endpoint = origin.resolve("/mobile/runtime-contract").toURL();
        HttpURLConnection connection = (HttpURLConnection) endpoint.openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
        connection.setReadTimeout(READ_TIMEOUT_MS);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("Cache-Control", "no-cache");
        int status = connection.getResponseCode();
        if (status != 200) throw new IllegalStateException("runtime_contract_http_" + status);
        String contentType = String.valueOf(connection.getContentType()).toLowerCase(Locale.ROOT);
        if (!contentType.startsWith("application/json")) {
            throw new IllegalStateException("runtime_contract_content_type_invalid");
        }
        byte[] body;
        try (InputStream input = connection.getInputStream()) {
            body = readBounded(input);
        } finally {
            connection.disconnect();
        }
        JSONObject payload = new JSONObject(new String(body, StandardCharsets.UTF_8));
        requireExact(payload, "status", "ok");
        requireExact(payload, "contract_version", BuildConfig.MOBILE_CONTRACT_VERSION);
        requireExact(payload, "app_id", "com.myexternalbrain.propertyquarry");
        requireExact(payload, "public_origin", BuildConfig.PROPERTYQUARRY_ORIGIN);
        requireExact(payload, "walkthrough_default", "camera");
        requireExact(payload, "vr_mode", "optional");
        var appLinksReadiness = payload.getJSONObject("app_links_ready_by_app_id");
        if (appLinksReadiness.length() != 2
            || !appLinksReadiness.has("com.myexternalbrain.propertyquarry")
            || !appLinksReadiness.has("com.myexternalbrain.propertyquarry.preview")) {
            throw new IllegalStateException("runtime_app_links_contract_invalid");
        }
        if (!appLinksReadiness.getBoolean(BuildConfig.APPLICATION_ID)) {
            throw new IllegalStateException("runtime_app_links_unverified");
        }
        if (payload.getInt("minimum_android_build") > BuildConfig.VERSION_CODE) {
            throw new IllegalStateException("android_build_below_minimum");
        }
        String startPath = payload.getString("start_path");
        if (!startPath.startsWith("/app/") || startPath.contains("\\") || startPath.contains("//")) {
            throw new IllegalStateException("runtime_start_path_invalid");
        }
        var providers = payload.getJSONArray("spatial_tour_providers");
        if (providers.length() != 2
            || !"3dvista".equals(providers.getString(0))
            || !"matterport".equals(providers.getString(1))) {
            throw new IllegalStateException("runtime_spatial_provider_contract_invalid");
        }
        return new Verified(BuildConfig.PROPERTYQUARRY_ORIGIN, startPath);
    }

    private static void requireExact(JSONObject payload, String key, String expected) throws Exception {
        if (!expected.equals(payload.getString(key))) {
            throw new IllegalStateException("runtime_contract_" + key + "_invalid");
        }
    }

    static byte[] readBounded(InputStream input) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[1024];
        int total = 0;
        int read;
        while ((read = input.read(buffer)) != -1) {
            total += read;
            if (total > MAX_RESPONSE_BYTES) {
                throw new IllegalStateException("runtime_contract_response_too_large");
            }
            output.write(buffer, 0, read);
        }
        return output.toByteArray();
    }
}
