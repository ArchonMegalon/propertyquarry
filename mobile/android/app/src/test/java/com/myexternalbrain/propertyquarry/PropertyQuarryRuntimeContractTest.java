package com.myexternalbrain.propertyquarry;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertThrows;

import java.io.ByteArrayInputStream;

import org.junit.Test;

public final class PropertyQuarryRuntimeContractTest {
    @Test
    public void boundedReaderAcceptsContractSizedPayload() throws Exception {
        byte[] payload = "{\"status\":\"ok\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        assertArrayEquals(
            payload,
            PropertyQuarryRuntimeContract.readBounded(new ByteArrayInputStream(payload))
        );
    }

    @Test
    public void boundedReaderRejectsOversizedPayload() {
        byte[] payload = new byte[PropertyQuarryRuntimeContract.MAX_RESPONSE_BYTES + 1];
        assertThrows(
            IllegalStateException.class,
            () -> PropertyQuarryRuntimeContract.readBounded(new ByteArrayInputStream(payload))
        );
    }
}
