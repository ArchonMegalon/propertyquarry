package com.myexternalbrain.propertyquarry;

import java.net.IDN;
import java.net.URI;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.Optional;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class SharedPropertyUrl {
    private static final Pattern HTTPS_URL = Pattern.compile("https://[^\\s<>\\\"]+", Pattern.CASE_INSENSITIVE);

    record Value(String url, String host, String idempotencyKey) {}

    private SharedPropertyUrl() {}

    static Optional<Value> parse(String sharedText) {
        String text = String.valueOf(sharedText == null ? "" : sharedText).trim();
        if (text.isEmpty() || text.length() > 12_000) return Optional.empty();
        Matcher matcher = HTTPS_URL.matcher(text);
        while (matcher.find()) {
            String candidate = stripTrailingPunctuation(matcher.group());
            if (candidate.length() > 2048) continue;
            try {
                URI uri = URI.create(candidate);
                String rawHost = uri.getHost();
                if (rawHost == null || rawHost.isBlank()) continue;
                String host = IDN.toASCII(rawHost).toLowerCase(Locale.ROOT);
                if (!"https".equalsIgnoreCase(uri.getScheme()) || host.isBlank()
                    || uri.getUserInfo() != null || uri.getPort() > 0 && uri.getPort() != 443
                    || uri.getFragment() != null || isLocalHost(host)) {
                    continue;
                }
                String normalized = new URI(
                    "https", null, host, uri.getPort(),
                    uri.getRawPath(), uri.getRawQuery(), null
                ).toASCIIString();
                byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(normalized.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                String idempotency = "android-" + lowercaseHex(digest, 20);
                return Optional.of(new Value(normalized, host, idempotency));
            } catch (Exception ignored) {
                // Continue to the next HTTPS URL in the shared text.
            }
        }
        return Optional.empty();
    }

    private static String stripTrailingPunctuation(String value) {
        String result = value;
        while (!result.isEmpty() && ").,;!?'\"]}".indexOf(result.charAt(result.length() - 1)) >= 0) {
            result = result.substring(0, result.length() - 1);
        }
        return result;
    }

    private static String lowercaseHex(byte[] bytes, int length) {
        char[] digits = "0123456789abcdef".toCharArray();
        int byteCount = Math.min(bytes.length, length);
        char[] encoded = new char[byteCount * 2];
        for (int index = 0; index < byteCount; index++) {
            int value = bytes[index] & 0xff;
            encoded[index * 2] = digits[value >>> 4];
            encoded[index * 2 + 1] = digits[value & 0x0f];
        }
        return new String(encoded);
    }

    private static boolean isLocalHost(String host) {
        return host.equals("localhost") || host.endsWith(".localhost")
            || host.equals("127.0.0.1") || host.equals("::1")
            || host.startsWith("10.") || host.startsWith("192.168.")
            || host.matches("172\\.(1[6-9]|2[0-9]|3[01])\\..*");
    }
}
