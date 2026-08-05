package com.myexternalbrain.propertyquarry;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class SharedPropertyUrlTest {
    @Test
    public void extractsOneCanonicalHttpsListing() {
        var parsed = SharedPropertyUrl.parse(
            "Look at this home: https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/test-123?x=1)."
        );

        assertTrue(parsed.isPresent());
        assertEquals("www.willhaben.at", parsed.orElseThrow().host());
        assertFalse(parsed.orElseThrow().url().contains("#"));
        assertTrue(parsed.orElseThrow().idempotencyKey().startsWith("android-"));
    }

    @Test
    public void rejectsLocalCleartextAndCredentialedUrls() {
        assertTrue(SharedPropertyUrl.parse("http://example.com/listing/1").isEmpty());
        assertTrue(SharedPropertyUrl.parse("https://127.0.0.1/listing/1").isEmpty());
        assertTrue(SharedPropertyUrl.parse("https://user:pass@example.com/listing/1").isEmpty());
    }

    @Test
    public void skipsAnInvalidUrlAndAcceptsTheNextSecureUrl() {
        var parsed = SharedPropertyUrl.parse(
            "https://localhost/private then https://example.com/property/42"
        );

        assertTrue(parsed.isPresent());
        assertEquals("example.com", parsed.orElseThrow().host());
    }
}
