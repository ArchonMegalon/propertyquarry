package releasecontrol

import (
	"bytes"
	"strings"
	"testing"
)

const crossLanguageGoldenRequest = installedRequestSmokePayload

func TestQuarantineParserMatchesPythonAuthorityGoldenVector(t *testing.T) {
	request, err := parseQuarantinedRequest([]byte(crossLanguageGoldenRequest))
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if string(request.canonicalBody) != crossLanguageGoldenRequest {
		t.Fatalf("canonical body changed:\n%s", request.canonicalBody)
	}
	if request.rawBodyDigest != "sha256:b5dce15a624b61f98cdcb258f11e11e487052c96b2136959fa87c2c5b4b56654" {
		t.Fatalf("raw digest mismatch: %s", request.rawBodyDigest)
	}
	if request.canonicalBodyDigest != request.rawBodyDigest {
		t.Fatalf("canonical body digest mismatch: %s", request.canonicalBodyDigest)
	}
	if request.canonicalEnvelopeDigest != "sha256:f9c9160c494309599e9a8c0c768fee086dcc2e5a81f4d91735b630281085211b" {
		t.Fatalf("envelope digest mismatch: %s", request.canonicalEnvelopeDigest)
	}
	if request.signaturePayloadDigest != "sha256:fb4d592cf32f161b1425ea8d55906c901114be0d267d3d89f672f3afed10a148" {
		t.Fatalf("signature payload digest mismatch: %s", request.signaturePayloadDigest)
	}
	if !request.envelopeDigestMatches || request.authenticationEstablished {
		t.Fatal("syntax/digest comparison was confused with authentication")
	}
	if request.envelope.Operation != "release-preflight" ||
		request.envelope.RequestID != "socket-request-1" ||
		request.envelope.Identity.RunAttempt != 1 {
		t.Fatalf("parsed envelope mismatch: %#v", request.envelope)
	}
}

func TestQuarantineParserAcceptsDigestMismatchAsUnauthenticatedSyntax(t *testing.T) {
	raw := strings.Replace(
		crossLanguageGoldenRequest,
		"sha256:f9c9160c494309599e9a8c0c768fee086dcc2e5a81f4d91735b630281085211b",
		"sha256:0000000000000000000000000000000000000000000000000000000000000000",
		1,
	)
	request, err := parseQuarantinedRequest([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if request.envelopeDigestMatches || request.authenticationEstablished {
		t.Fatal("mismatched claimed digest became authenticated")
	}
	if request.claimedEnvelopeDigest == request.canonicalEnvelopeDigest {
		t.Fatal("test vector did not retain distinct digests")
	}
}

func TestQuarantineParserCanonicalizesWithoutTrustingRawEncoding(t *testing.T) {
	raw := []byte(" \n\t" + strings.Replace(crossLanguageGoldenRequest, `"schema":`, `"schema" : `, 1) + " \r\n")
	request, err := parseQuarantinedRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if bytes.Equal(request.rawBody, request.canonicalBody) {
		t.Fatal("noncanonical transport was not distinguished")
	}
	if request.rawBodyDigest == request.canonicalBodyDigest {
		t.Fatal("raw and canonical body digests were conflated")
	}
	if string(request.canonicalBody) != crossLanguageGoldenRequest {
		t.Fatalf("unexpected canonical body: %s", request.canonicalBody)
	}
}

func TestQuarantineParserRejectsAdversarialJSONAndSchema(t *testing.T) {
	digest := "sha256:f9c9160c494309599e9a8c0c768fee086dcc2e5a81f4d91735b630281085211b"
	tests := map[string][]byte{
		"empty":               {},
		"bom":                 append([]byte{0xef, 0xbb, 0xbf}, []byte(crossLanguageGoldenRequest)...),
		"invalid-utf8":        []byte{'{', '"', 'x', '"', ':', '"', 0xff, '"', '}'},
		"trailing-object":     []byte(crossLanguageGoldenRequest + `{}`),
		"duplicate-outer":     []byte(strings.Replace(crossLanguageGoldenRequest, `"schema":"propertyquarry.release-request.v2"`, `"schema":"propertyquarry.release-request.v2","schema":"propertyquarry.release-request.v2"`, 1)),
		"duplicate-identity":  []byte(strings.Replace(crossLanguageGoldenRequest, `"audience":"propertyquarry-release-v2"`, `"audience":"propertyquarry-release-v2","audience":"propertyquarry-release-v2"`, 1)),
		"unknown-outer":       []byte(strings.Replace(crossLanguageGoldenRequest, `"schema":`, `"unknown":false,"schema":`, 1)),
		"unknown-envelope":    []byte(strings.Replace(crossLanguageGoldenRequest, `"expires_at":1100`, `"unknown":false,"expires_at":1100`, 1)),
		"unknown-identity":    []byte(strings.Replace(crossLanguageGoldenRequest, `"audience":`, `"unknown":false,"audience":`, 1)),
		"nonfinite":           []byte(strings.Replace(crossLanguageGoldenRequest, `"issued_at":1000`, `"issued_at":NaN`, 1)),
		"float-int":           []byte(strings.Replace(crossLanguageGoldenRequest, `"run_attempt":1`, `"run_attempt":1.0`, 1)),
		"exponent-int":        []byte(strings.Replace(crossLanguageGoldenRequest, `"run_attempt":1`, `"run_attempt":1e0`, 1)),
		"boolean-int":         []byte(strings.Replace(crossLanguageGoldenRequest, `"run_attempt":1`, `"run_attempt":true`, 1)),
		"string-int":          []byte(strings.Replace(crossLanguageGoldenRequest, `"run_attempt":1`, `"run_attempt":"1"`, 1)),
		"negative-time":       []byte(strings.Replace(crossLanguageGoldenRequest, `"issued_at":1000`, `"issued_at":-1`, 1)),
		"int64-overflow":      []byte(strings.Replace(crossLanguageGoldenRequest, `"expires_at":1100`, `"expires_at":9223372036854775808`, 1)),
		"wrong-schema":        []byte(strings.Replace(crossLanguageGoldenRequest, requestSchema, "propertyquarry.release-request.v3", 1)),
		"wrong-operation":     []byte(strings.Replace(crossLanguageGoldenRequest, "release-preflight", "reconcile-run", 1)),
		"bad-identifier":      []byte(strings.Replace(crossLanguageGoldenRequest, "socket-request-1", "space forbidden", 1)),
		"uppercase-sha":       []byte(strings.Replace(crossLanguageGoldenRequest, strings.Repeat("a", 40), strings.Repeat("A", 40), 1)),
		"empty-signature":     []byte(strings.Replace(crossLanguageGoldenRequest, "sig:transport-conformance-test", "", 1)),
		"bad-digest":          []byte(strings.Replace(crossLanguageGoldenRequest, digest, "sha256:ABC", 1)),
		"lone-high-surrogate": []byte(strings.Replace(crossLanguageGoldenRequest, "sig:transport-conformance-test", `sig:\ud800`, 1)),
		"lone-low-surrogate":  []byte(strings.Replace(crossLanguageGoldenRequest, "sig:transport-conformance-test", `sig:\udc00`, 1)),
	}
	deep := strings.Repeat(`{"x":`, maxRequestJSONDepth+2) + "0" + strings.Repeat("}", maxRequestJSONDepth+2)
	tests["depth"] = []byte(deep)
	tests["oversize"] = bytes.Repeat([]byte{' '}, maxRequestBytes+1)
	for name, raw := range tests {
		t.Run(name, func(t *testing.T) {
			if request, err := parseQuarantinedRequest(raw); err == nil {
				request.release()
				t.Fatal("adversarial request accepted")
			}
		})
	}
}

func TestQuarantineParserAllowsValidUnicodePairButCanonicalizesASCII(t *testing.T) {
	raw := []byte(strings.Replace(
		crossLanguageGoldenRequest,
		"sig:transport-conformance-test",
		`sig:\ud83d\ude00`,
		1,
	))
	request, err := parseQuarantinedRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if !bytes.Contains(request.canonicalBody, []byte(`\ud83d\ude00`)) {
		t.Fatalf("canonical Unicode profile changed: %s", request.canonicalBody)
	}
	if request.canonicalBodyDigest != "sha256:250c915312fe8bd67b1eecf990037ab1d54f34bb2f48e40ec4fef5730e81dc73" {
		t.Fatalf("cross-language Unicode digest changed: %s", request.canonicalBodyDigest)
	}
}

func TestQuarantineParserAcceptsExactMaximumTransportSize(t *testing.T) {
	const originalSignature = "sig:transport-conformance-test"
	padding := strings.Repeat("s", maxRequestBytes-(len(crossLanguageGoldenRequest)-len(originalSignature)))
	raw := []byte(strings.Replace(crossLanguageGoldenRequest, originalSignature, padding, 1))
	if len(raw) != maxRequestBytes {
		t.Fatalf("bad maximum-size fixture: %d", len(raw))
	}
	request, err := parseQuarantinedRequest(raw)
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if len(request.rawBody) != maxRequestBytes || request.rawBodyDigest == "" {
		t.Fatal("maximum-size request was not retained exactly")
	}
}

func TestQuarantinedRequestReleaseZeroesRetainedBuffers(t *testing.T) {
	request, err := parseQuarantinedRequest([]byte(crossLanguageGoldenRequest))
	if err != nil {
		t.Fatal(err)
	}
	retained := [][]byte{
		request.rawBody,
		request.canonicalBody,
		request.canonicalEnvelope,
		request.signaturePayload,
	}
	request.release()
	for _, value := range retained {
		if !bytes.Equal(value, make([]byte, len(value))) {
			t.Fatal("retained request bytes were not zeroed")
		}
	}
	if request.rawBody != nil || request.rawBodyDigest != "" ||
		request.canonicalBody != nil || request.canonicalBodyDigest != "" ||
		request.canonicalEnvelope != nil || request.canonicalEnvelopeDigest != "" ||
		request.claimedEnvelopeDigest != "" || request.envelopeDigestMatches ||
		request.signaturePayload != nil || request.signaturePayloadDigest != "" ||
		request.requestSignature != "" || request.envelope != (quarantinedEnvelope{}) ||
		request.authenticationEstablished {
		t.Fatalf("released request retained metadata: %#v", request)
	}
	request.release()
}
