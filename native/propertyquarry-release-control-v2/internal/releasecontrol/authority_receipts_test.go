package releasecontrol

import (
	"bytes"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"
)

type authorityReceiptTestFixture struct {
	bindings        authorityReceiptBindings
	storage         evidenceStoreReceiptBindings
	roots           authorityReceiptTrustRoots
	resourcePrivate ed25519.PrivateKey
	evidencePrivate ed25519.PrivateKey
	launchPayload   map[string]any
	evidencePayload map[string]any
	launchRaw       []byte
	evidenceRaw     []byte
	now             time.Time
}

func TestAuthorityReceiptPairVerifiesCanonicalReplayDeterministically(t *testing.T) {
	first := newAuthorityReceiptTestFixture(t)
	second := newAuthorityReceiptTestFixture(t)
	if !bytes.Equal(first.launchRaw, second.launchRaw) ||
		!bytes.Equal(first.evidenceRaw, second.evidenceRaw) {
		t.Fatal("same signed receipt input did not produce byte-identical replay")
	}

	pair, err := verifyAuthorityReceiptPair(
		first.launchRaw,
		first.evidenceRaw,
		first.roots,
		first.bindings,
		first.storage,
		first.now,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer pair.release()
	if !bytes.Equal(pair.LaunchDecision.CanonicalReceipt, first.launchRaw) ||
		!bytes.Equal(pair.EvidenceStore.CanonicalReceipt, first.evidenceRaw) {
		t.Fatal("verified receipts differ from their canonical input")
	}
	if pair.LaunchDecision.PayloadDigest != authorityTestPayloadDigest(t, first.launchPayload) ||
		pair.EvidenceStore.PayloadDigest != authorityTestPayloadDigest(t, first.evidencePayload) ||
		pair.LaunchDecision.BindingDigest != pair.EvidenceStore.BindingDigest {
		t.Fatal("verified receipt digest binding changed")
	}

	replay, err := verifyAuthorityReceiptPair(
		append([]byte(nil), first.launchRaw...),
		append([]byte(nil), first.evidenceRaw...),
		first.roots,
		first.bindings,
		first.storage,
		first.now,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer replay.release()
	if !bytes.Equal(pair.LaunchDecision.CanonicalReceipt, replay.LaunchDecision.CanonicalReceipt) ||
		!bytes.Equal(pair.EvidenceStore.CanonicalReceipt, replay.EvidenceStore.CanonicalReceipt) ||
		pair.LaunchDecision.ReceiptDigest != replay.LaunchDecision.ReceiptDigest ||
		pair.EvidenceStore.ReceiptDigest != replay.EvidenceStore.ReceiptDigest {
		t.Fatal("verified replay is not deterministic")
	}
}

func TestAuthorityReceiptPairRequiresSeparatedPinnedRootsAndDomains(t *testing.T) {
	fixture := newAuthorityReceiptTestFixture(t)
	tests := map[string]func(*authorityReceiptTestFixture){
		"swapped-public-roots": func(value *authorityReceiptTestFixture) {
			value.roots.ResourceMediatorPublicKey, value.roots.EvidenceAuthorityPublicKey =
				value.roots.EvidenceAuthorityPublicKey, value.roots.ResourceMediatorPublicKey
		},
		"swapped-key-ids": func(value *authorityReceiptTestFixture) {
			value.roots.ResourceMediatorKeyID, value.roots.EvidenceAuthorityKeyID =
				value.roots.EvidenceAuthorityKeyID, value.roots.ResourceMediatorKeyID
		},
		"same-root-for-both-authorities": func(value *authorityReceiptTestFixture) {
			value.roots.EvidenceAuthorityPublicKey =
				append(ed25519.PublicKey(nil), value.roots.ResourceMediatorPublicKey...)
			value.roots.EvidenceAuthorityKeyID = value.roots.ResourceMediatorKeyID
		},
		"launch-signed-under-evidence-domain": func(value *authorityReceiptTestFixture) {
			value.launchRaw = authorityTestSignReceipt(
				t,
				launchAuthorityDecisionSchema,
				launchAuthorityProducer,
				value.roots.ResourceMediatorKeyID,
				value.launchPayload,
				evidenceStoreSignatureDomain,
				value.resourcePrivate,
			)
		},
		"launch-signed-by-wrong-key": func(value *authorityReceiptTestFixture) {
			value.launchRaw = authorityTestSignReceipt(
				t,
				launchAuthorityDecisionSchema,
				launchAuthorityProducer,
				value.roots.ResourceMediatorKeyID,
				value.launchPayload,
				launchAuthoritySignatureDomain,
				value.evidencePrivate,
			)
		},
		"evidence-signed-under-launch-domain": func(value *authorityReceiptTestFixture) {
			value.evidenceRaw = authorityTestSignReceipt(
				t,
				evidenceStoreAcknowledgementSchema,
				evidenceStoreProducer,
				value.roots.EvidenceAuthorityKeyID,
				value.evidencePayload,
				launchAuthoritySignatureDomain,
				value.evidencePrivate,
			)
		},
		"evidence-signature-bit-flipped": func(value *authorityReceiptTestFixture) {
			outer := authorityTestDecodeObject(t, value.evidenceRaw)
			signature, err := base64.RawURLEncoding.DecodeString(outer["signature"].(string))
			if err != nil {
				t.Fatal(err)
			}
			signature[0] ^= 0x01
			outer["signature"] = base64.RawURLEncoding.EncodeToString(signature)
			zero(signature)
			value.evidenceRaw, err = canonicalJSON(outer)
			if err != nil {
				t.Fatal(err)
			}
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			value := fixture.clone()
			mutate(value)
			assertAuthorityReceiptPairRejected(t, value)
		})
	}
}

func TestAuthorityReceiptPairRejectsEveryConflictingCommonBinding(t *testing.T) {
	tests := map[string]func(map[string]any){
		"identity": func(payload map[string]any) {
			authorityTestObject(authorityTestObject(payload, "request"), "identity")["job"] = "other-job"
		},
		"operation": func(payload map[string]any) {
			authorityTestObject(payload, "request")["operation"] = "release-preflight"
		},
		"request-id": func(payload map[string]any) {
			authorityTestObject(payload, "request")["request_id"] = "request-other"
		},
		"request-digest": func(payload map[string]any) {
			authorityTestObject(payload, "request")["request_sha256"] = authorityTestDigest("f")
		},
		"envelope-digest": func(payload map[string]any) {
			authorityTestObject(payload, "request")["envelope_sha256"] = authorityTestDigest("f")
		},
		"root-policy-digest": func(payload map[string]any) {
			authorityTestObject(payload, "request")["root_policy_sha256"] = authorityTestDigest("f")
		},
		"release-commit": func(payload map[string]any) {
			authorityTestObject(payload, "release")["commit_sha"] = strings.Repeat("b", 40)
		},
		"release-image": func(payload map[string]any) {
			authorityTestObject(payload, "release")["image_digest"] = authorityTestDigest("f")
		},
		"operations-schema": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["schema"] = "other"
		},
		"operations-payload": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["payload_sha256"] = authorityTestDigest("f")
		},
		"deployment": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["deployment_id"] = "deployment-other"
		},
		"challenge": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["challenge_sha256"] = authorityTestDigest("f")
		},
		"challenge-nonce": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["challenge_nonce"] = "challenge-other"
		},
		"operations-policy": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["policy_sha256"] = authorityTestDigest("f")
		},
		"replicas": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["replica_ids"] =
				[]any{"replica-b", "replica-a"}
		},
		"window": func(payload map[string]any) {
			authorityTestObject(
				authorityTestObject(payload, "gold_operations_verification"),
				"window",
			)["end_unix"] = json.Number("22599")
		},
		"dashboard-receipt": func(payload map[string]any) {
			hashes := authorityTestObject(
				authorityTestObject(payload, "gold_operations_verification"),
				"raw_receipt_hashes",
			)
			hashes["dashboard_render"] = authorityTestDigest("f")
		},
		"log-receipt": func(payload map[string]any) {
			hashes := authorityTestObject(
				authorityTestObject(payload, "gold_operations_verification"),
				"raw_receipt_hashes",
			)
			hashes["structured_log_query"] = authorityTestDigest("f")
		},
		"trace-receipt": func(payload map[string]any) {
			hashes := authorityTestObject(
				authorityTestObject(payload, "gold_operations_verification"),
				"raw_receipt_hashes",
			)
			hashes["distributed_trace_query"] = authorityTestDigest("f")
		},
		"result": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["result"] = "pass"
		},
		"cross-link": func(payload map[string]any) {
			authorityTestObject(payload, "gold_operations_verification")["cross_link_sha256"] = authorityTestDigest("f")
		},
		"lifecycle-id": func(payload map[string]any) {
			authorityTestObject(payload, "lifecycle")["lifecycle_id"] = "lifecycle-other"
		},
		"lifecycle-digest": func(payload map[string]any) {
			authorityTestObject(payload, "lifecycle")["lifecycle_sha256"] = authorityTestDigest("f")
		},
		"fence-token": func(payload map[string]any) {
			authorityTestObject(payload, "lifecycle")["fence_token_sha256"] = authorityTestDigest("f")
		},
		"fence-epoch": func(payload map[string]any) {
			authorityTestObject(payload, "lifecycle")["fence_epoch"] = json.Number("8")
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newAuthorityReceiptTestFixture(t)
			mutate(fixture.launchPayload)
			authorityTestRefreshBindingDigest(t, fixture.launchPayload)
			fixture.launchRaw = authorityTestSignReceipt(
				t,
				launchAuthorityDecisionSchema,
				launchAuthorityProducer,
				fixture.roots.ResourceMediatorKeyID,
				fixture.launchPayload,
				launchAuthoritySignatureDomain,
				fixture.resourcePrivate,
			)
			assertAuthorityReceiptPairRejected(t, fixture)
		})
	}
}

func TestEvidenceStoreReceiptBindsLaunchAndDurableCASAcknowledgements(t *testing.T) {
	tests := map[string]func(map[string]any){
		"launch-payload-digest": func(payload map[string]any) {
			payload["launch_decision_payload_sha256"] = authorityTestDigest("1")
		},
		"launch-receipt-digest": func(payload map[string]any) {
			payload["launch_decision_receipt_sha256"] = authorityTestDigest("1")
		},
		"cas-generation": func(payload map[string]any) {
			authorityTestObject(payload, "storage")["cas_generation"] = json.Number("12")
		},
		"previous-digest": func(payload map[string]any) {
			authorityTestObject(payload, "storage")["previous_sha256"] = authorityTestDigest("2")
		},
		"persisted-ack": func(payload map[string]any) {
			authorityTestObject(payload, "storage")["persisted_ack_sha256"] = authorityTestDigest("3")
		},
		"fsynced-ack": func(payload map[string]any) {
			authorityTestObject(payload, "storage")["fsynced_ack_sha256"] = authorityTestDigest("4")
		},
		"conflicting-common-binding": func(payload map[string]any) {
			authorityTestObject(payload, "request")["request_id"] = "request-conflict"
			authorityTestRefreshBindingDigest(t, payload)
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newAuthorityReceiptTestFixture(t)
			mutate(fixture.evidencePayload)
			fixture.evidenceRaw = authorityTestSignReceipt(
				t,
				evidenceStoreAcknowledgementSchema,
				evidenceStoreProducer,
				fixture.roots.EvidenceAuthorityKeyID,
				fixture.evidencePayload,
				evidenceStoreSignatureDomain,
				fixture.evidencePrivate,
			)
			assertAuthorityReceiptPairRejected(t, fixture)
		})
	}

	for name, window := range map[string][2]int64{
		"ack-before-launch":    {22999, 23400},
		"ack-at-launch-expiry": {23500, 23501},
		"ack-outlives-launch":  {23001, 23501},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newAuthorityReceiptTestFixture(t)
			fixture.evidencePayload["issued_at"] = json.Number(strconv.FormatInt(window[0], 10))
			fixture.evidencePayload["expires_at"] = json.Number(strconv.FormatInt(window[1], 10))
			fixture.evidenceRaw = authorityTestSignReceipt(
				t,
				evidenceStoreAcknowledgementSchema,
				evidenceStoreProducer,
				fixture.roots.EvidenceAuthorityKeyID,
				fixture.evidencePayload,
				evidenceStoreSignatureDomain,
				fixture.evidencePrivate,
			)
			assertAuthorityReceiptPairRejected(t, fixture)
		})
	}
}

func TestAuthorityReceiptsRejectUnsafeOrNoncanonicalJSON(t *testing.T) {
	fixture := newAuthorityReceiptTestFixture(t)
	keyPrefix := []byte(`{"key_id":"` + fixture.roots.ResourceMediatorKeyID + `",`)
	tests := map[string][]byte{
		"leading-whitespace": append([]byte(" "), fixture.launchRaw...),
		"trailing-newline":   append(append([]byte(nil), fixture.launchRaw...), '\n'),
		"duplicate-key": bytes.Replace(
			fixture.launchRaw,
			keyPrefix,
			[]byte(
				`{"key_id":"`+fixture.roots.ResourceMediatorKeyID+
					`","key_id":"`+fixture.roots.ResourceMediatorKeyID+`",`,
			),
			1,
		),
		"floating-integer": bytes.Replace(
			fixture.launchRaw,
			[]byte(`"issued_at":23000`),
			[]byte(`"issued_at":23000.0`),
			1,
		),
		"nonfinite-number": bytes.Replace(
			fixture.launchRaw,
			[]byte(`"issued_at":23000`),
			[]byte(`"issued_at":NaN`),
			1,
		),
		"excessive-depth": []byte(`{"a":` + strings.Repeat("[", 40) + `0` + strings.Repeat("]", 40) + `}`),
		"oversized":       bytes.Repeat([]byte("x"), maxAuthorityReceiptBytes+1),
	}
	for name, raw := range tests {
		t.Run(name, func(t *testing.T) {
			value := fixture.clone()
			value.launchRaw = raw
			assertAuthorityReceiptPairRejected(t, value)
		})
	}

	outer := authorityTestDecodeObject(t, fixture.launchRaw)
	outer["signature"] = outer["signature"].(string) + "="
	padded, err := canonicalJSON(outer)
	if err != nil {
		t.Fatal(err)
	}
	value := fixture.clone()
	value.launchRaw = padded
	assertAuthorityReceiptPairRejected(t, value)
}

func TestAuthorityReceiptsRejectUnknownFieldsAndBrokenPayloadDigests(t *testing.T) {
	fixture := newAuthorityReceiptTestFixture(t)
	tests := map[string]func(map[string]any){
		"unknown-envelope-key": func(outer map[string]any) {
			outer["extra"] = true
		},
		"wrong-schema": func(outer map[string]any) {
			outer["schema"] = evidenceStoreAcknowledgementSchema
		},
		"wrong-producer": func(outer map[string]any) {
			outer["producer"] = evidenceStoreProducer
		},
		"wrong-key-id": func(outer map[string]any) {
			outer["key_id"] = fixture.roots.EvidenceAuthorityKeyID
		},
		"wrong-payload-digest": func(outer map[string]any) {
			outer["payload_sha256"] = authorityTestDigest("1")
		},
		"unknown-payload-key": func(outer map[string]any) {
			outer["payload"].(map[string]any)["extra"] = true
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			outer := authorityTestDecodeObject(t, fixture.launchRaw)
			mutate(outer)
			raw, err := canonicalJSON(outer)
			if err != nil {
				t.Fatal(err)
			}
			value := fixture.clone()
			value.launchRaw = raw
			assertAuthorityReceiptPairRejected(t, value)
		})
	}
}

func TestAuthorityReceiptFreshnessAndOperationsWindowFailClosed(t *testing.T) {
	tests := map[string]func(*authorityReceiptTestFixture){
		"expired": func(value *authorityReceiptTestFixture) {
			value.launchPayload["expires_at"] = json.Number("23010")
			value.now = time.Unix(23010, 0)
		},
		"future-issued": func(value *authorityReceiptTestFixture) {
			value.launchPayload["issued_at"] = json.Number("23011")
		},
		"ttl-too-long": func(value *authorityReceiptTestFixture) {
			value.launchPayload["expires_at"] = json.Number("23601")
		},
		"operations-window-stale-at-verification": func(value *authorityReceiptTestFixture) {
			value.now = time.Unix(23201, 0)
		},
		"operations-window-after-decision": func(value *authorityReceiptTestFixture) {
			operations := authorityTestObject(value.launchPayload, "gold_operations_verification")
			window := authorityTestObject(operations, "window")
			window["start_unix"] = json.Number("1600")
			window["end_unix"] = json.Number("23200")
			value.bindings.Operations.WindowStart = 1600
			value.bindings.Operations.WindowEnd = 23200
			authorityTestRefreshBindingDigest(t, value.launchPayload)
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			value := newAuthorityReceiptTestFixture(t)
			mutate(value)
			value.launchRaw = authorityTestSignReceipt(
				t,
				launchAuthorityDecisionSchema,
				launchAuthorityProducer,
				value.roots.ResourceMediatorKeyID,
				value.launchPayload,
				launchAuthoritySignatureDomain,
				value.resourcePrivate,
			)
			assertAuthorityReceiptPairRejected(t, value)
		})
	}
}

func TestAuthorityReceiptSignatureFramingIsDomainAndLengthSeparated(t *testing.T) {
	launch, err := authorityReceiptSignatureMessage(
		launchAuthoritySignatureDomain,
		[]byte("ab"),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(launch)
	evidence, err := authorityReceiptSignatureMessage(
		evidenceStoreSignatureDomain,
		[]byte("ab"),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(evidence)
	ambiguous, err := authorityReceiptSignatureMessage(
		launchAuthoritySignatureDomain,
		[]byte("a"),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(ambiguous)
	if bytes.Equal(launch, evidence) ||
		bytes.Equal(launch, ambiguous) ||
		!bytes.HasPrefix(launch, []byte(launchAuthoritySignatureDomain)) {
		t.Fatal("authority receipt signature domains or lengths are ambiguous")
	}
}

func TestAuthorityReceiptPythonReferenceGoldenVectors(t *testing.T) {
	fixture := newAuthorityReceiptTestFixture(t)
	common := authorityTestCommonPayload(fixture.bindings)
	if got := authorityTestCommonBindingDigest(t, common); got !=
		"sha256:cc91112dbeeddba60f21e9a4e8f70b343de29377063a9da8c688ad6c669ea4d1" {
		t.Fatalf("common binding digest changed: %s", got)
	}
	if fixture.roots.ResourceMediatorKeyID !=
		"sha256:83ab31e2208ae4a4ecab44dab0e52db23dee6be569017054ef087af1ec505b09" ||
		fixture.roots.EvidenceAuthorityKeyID !=
			"sha256:67e828f7bc112276537282b88b9669e6de98f7399e611e91e848d17c2755891c" {
		t.Fatal("authority receipt SPKI key ID vector changed")
	}

	launchUnsigned, launchMessage, launchSignature :=
		authorityTestUnsignedMessage(
			t,
			fixture.launchRaw,
			launchAuthoritySignatureDomain,
		)
	defer zero(launchUnsigned)
	defer zero(launchMessage)
	if sha256Digest(launchUnsigned) !=
		"sha256:6e23c2aa070bb4062d917e7e53ca9e27038d1acadbcd378d7882c1d24ae7e8d5" ||
		sha256Digest(launchMessage) !=
			"sha256:1a1ec53d636c1f52aa72e1c310bc7d5331f5caac335ce83b4705f24cc73e62b3" ||
		authorityTestPayloadDigest(t, fixture.launchPayload) !=
			"sha256:9a8342f7e79ea2f31de82efc3cf0b6ce6f7c925ef3e325ce881f06f992cc719c" ||
		launchSignature !=
			"1O-2chsFytVW03eerShDSU1FxxUWhu0lMXcdf0M1F4ACYwkm9O07tMXIefzGk-sNCAac1tSpWftQvFW6wOIzBA" ||
		sha256Digest(fixture.launchRaw) !=
			"sha256:3d4da684fe8e24c2771193b14d0a0a82fd5046d4babe248af223b612a6231ac3" {
		t.Fatal("launch authority Python reference vector changed")
	}

	evidenceUnsigned, evidenceMessage, evidenceSignature :=
		authorityTestUnsignedMessage(
			t,
			fixture.evidenceRaw,
			evidenceStoreSignatureDomain,
		)
	defer zero(evidenceUnsigned)
	defer zero(evidenceMessage)
	if sha256Digest(evidenceUnsigned) !=
		"sha256:b2149dc4a292be0747f323997fc8606fe95af32bb74170458da7c6fbb986e89e" ||
		sha256Digest(evidenceMessage) !=
			"sha256:3d398a19013282ad2f03839ee2add8de32a117210b056167a6ba4f5e3569a3f3" ||
		authorityTestPayloadDigest(t, fixture.evidencePayload) !=
			"sha256:fc00442f21d1947df545d932f9b0a6b124d05a96f5baab584c2ab7bd59d93a7a" ||
		evidenceSignature !=
			"BQL5OoEE-osozPT0TrrtKQUyKj0n5T7KTWUlUk6FaZmqeXLfjVr9Fvp792oNa_WqzH5A1JjBQcWzYdux8Y5nBw" ||
		sha256Digest(fixture.evidenceRaw) !=
			"sha256:46a3076d57da8cb68f3dfbabf2870b444373cc153f353a8511afc492ecff998d" {
		t.Fatal("evidence-store Python reference vector changed")
	}
	if !exactStringEquals(
		fixture.evidencePayload["launch_decision_payload_sha256"],
		authorityTestPayloadDigest(t, fixture.launchPayload),
	) || !exactStringEquals(
		fixture.evidencePayload["launch_decision_receipt_sha256"],
		sha256Digest(fixture.launchRaw),
	) {
		t.Fatal("evidence-store receipt lost its exact launch receipt binding")
	}
}

func newAuthorityReceiptTestFixture(t *testing.T) *authorityReceiptTestFixture {
	t.Helper()
	resourceSeed := bytes.Repeat([]byte{0x51}, ed25519.SeedSize)
	resourcePrivate := ed25519.NewKeyFromSeed(resourceSeed)
	zero(resourceSeed)
	evidenceSeed := bytes.Repeat([]byte{0x62}, ed25519.SeedSize)
	evidencePrivate := ed25519.NewKeyFromSeed(evidenceSeed)
	zero(evidenceSeed)
	resourcePublic := resourcePrivate.Public().(ed25519.PublicKey)
	evidencePublic := evidencePrivate.Public().(ed25519.PublicKey)

	bindings := authorityReceiptBindings{
		Identity: quarantinedIdentity{
			Audience:     "propertyquarry-release-control-v2",
			Repository:   "example/property",
			Ref:          "refs/heads/main",
			CandidateSHA: strings.Repeat("a", 40),
			WorkflowRef:  "example/property/.github/workflows/release.yml@refs/heads/main",
			WorkflowSHA:  strings.Repeat("b", 40),
			RunID:        "12345",
			RunAttempt:   2,
			Job:          "propertyquarry-release-v2",
			Environment:  "production",
		},
		Operation:          "release-run",
		RequestID:          "request-123",
		RequestDigest:      authorityTestDigest("1"),
		EnvelopeDigest:     authorityTestDigest("2"),
		RootPolicyDigest:   authorityTestDigest("3"),
		ReleaseCommitSHA:   strings.Repeat("a", 40),
		ReleaseImageDigest: authorityTestDigest("4"),
		Operations: authorityOperationsBindings{
			PayloadDigest:              authorityTestDigest("5"),
			DeploymentID:               "deployment-123",
			ChallengeNonce:             "challenge-123",
			ChallengeDigest:            authorityTestDigest("6"),
			PolicyDigest:               authorityTestDigest("7"),
			ReplicaIDs:                 []string{"replica-a", "replica-b"},
			WindowStart:                1000,
			WindowEnd:                  22600,
			DashboardReceiptDigest:     authorityTestDigest("8"),
			StructuredLogReceiptDigest: authorityTestDigest("9"),
			DistributedTraceDigest:     authorityTestDigest("0"),
			CrossLinkDigest:            authorityTestDigest("a"),
		},
		Lifecycle: authorityLifecycleBindings{
			LifecycleID:      "lifecycle-123",
			LifecycleDigest:  authorityTestDigest("b"),
			FenceTokenDigest: authorityTestDigest("c"),
			FenceEpoch:       7,
		},
		MaxReceiptTTL:    600,
		MaxOperationsAge: 600,
	}
	storage := evidenceStoreReceiptBindings{
		CASGeneration:      11,
		PreviousDigest:     authorityTestDigest("d"),
		PersistedAckDigest: authorityTestDigest("e"),
		FsyncedAckDigest:   authorityTestDigest("f"),
	}
	roots := authorityReceiptTrustRoots{
		ResourceMediatorPublicKey:  append(ed25519.PublicKey(nil), resourcePublic...),
		ResourceMediatorKeyID:      authorityTestKeyID(t, resourcePublic),
		EvidenceAuthorityPublicKey: append(ed25519.PublicKey(nil), evidencePublic...),
		EvidenceAuthorityKeyID:     authorityTestKeyID(t, evidencePublic),
	}
	common := authorityTestCommonPayload(bindings)
	bindingDigest := authorityTestCommonBindingDigest(t, common)
	launchPayload := authorityTestCloneObject(t, common)
	launchPayload["decision"] = "allow"
	launchPayload["binding_sha256"] = bindingDigest
	launchPayload["issued_at"] = json.Number("23000")
	launchPayload["expires_at"] = json.Number("23500")
	launchRaw := authorityTestSignReceipt(
		t,
		launchAuthorityDecisionSchema,
		launchAuthorityProducer,
		roots.ResourceMediatorKeyID,
		launchPayload,
		launchAuthoritySignatureDomain,
		resourcePrivate,
	)
	evidencePayload := authorityTestCloneObject(t, common)
	evidencePayload["acknowledgement"] = "persisted-and-fsynced"
	evidencePayload["binding_sha256"] = bindingDigest
	evidencePayload["issued_at"] = json.Number("23001")
	evidencePayload["expires_at"] = json.Number("23400")
	evidencePayload["launch_decision_payload_sha256"] =
		authorityTestPayloadDigest(t, launchPayload)
	evidencePayload["launch_decision_receipt_sha256"] = sha256Digest(launchRaw)
	evidencePayload["storage"] = map[string]any{
		"cas_generation":       json.Number(strconv.FormatInt(storage.CASGeneration, 10)),
		"previous_sha256":      storage.PreviousDigest,
		"persisted_ack_sha256": storage.PersistedAckDigest,
		"fsynced_ack_sha256":   storage.FsyncedAckDigest,
	}
	evidenceRaw := authorityTestSignReceipt(
		t,
		evidenceStoreAcknowledgementSchema,
		evidenceStoreProducer,
		roots.EvidenceAuthorityKeyID,
		evidencePayload,
		evidenceStoreSignatureDomain,
		evidencePrivate,
	)
	return &authorityReceiptTestFixture{
		bindings:        bindings,
		storage:         storage,
		roots:           roots,
		resourcePrivate: append(ed25519.PrivateKey(nil), resourcePrivate...),
		evidencePrivate: append(ed25519.PrivateKey(nil), evidencePrivate...),
		launchPayload:   launchPayload,
		evidencePayload: evidencePayload,
		launchRaw:       launchRaw,
		evidenceRaw:     evidenceRaw,
		now:             time.Unix(23010, 0),
	}
}

func (fixture *authorityReceiptTestFixture) clone() *authorityReceiptTestFixture {
	result := *fixture
	result.bindings.Operations.ReplicaIDs =
		append([]string(nil), fixture.bindings.Operations.ReplicaIDs...)
	result.roots.ResourceMediatorPublicKey =
		append(ed25519.PublicKey(nil), fixture.roots.ResourceMediatorPublicKey...)
	result.roots.EvidenceAuthorityPublicKey =
		append(ed25519.PublicKey(nil), fixture.roots.EvidenceAuthorityPublicKey...)
	result.resourcePrivate = append(ed25519.PrivateKey(nil), fixture.resourcePrivate...)
	result.evidencePrivate = append(ed25519.PrivateKey(nil), fixture.evidencePrivate...)
	result.launchPayload = authorityTestCloneObjectNoTest(fixture.launchPayload)
	result.evidencePayload = authorityTestCloneObjectNoTest(fixture.evidencePayload)
	result.launchRaw = append([]byte(nil), fixture.launchRaw...)
	result.evidenceRaw = append([]byte(nil), fixture.evidenceRaw...)
	return &result
}

func authorityTestCommonPayload(bindings authorityReceiptBindings) map[string]any {
	return map[string]any{
		"request": map[string]any{
			"identity":           authorityIdentityValue(bindings.Identity),
			"operation":          bindings.Operation,
			"request_id":         bindings.RequestID,
			"request_sha256":     bindings.RequestDigest,
			"envelope_sha256":    bindings.EnvelopeDigest,
			"root_policy_sha256": bindings.RootPolicyDigest,
		},
		"release": map[string]any{
			"commit_sha":   bindings.ReleaseCommitSHA,
			"image_digest": bindings.ReleaseImageDigest,
		},
		"gold_operations_verification": map[string]any{
			"schema":           flagshipOperationsVerificationSchema,
			"payload_sha256":   bindings.Operations.PayloadDigest,
			"deployment_id":    bindings.Operations.DeploymentID,
			"challenge_nonce":  bindings.Operations.ChallengeNonce,
			"challenge_sha256": bindings.Operations.ChallengeDigest,
			"policy_sha256":    bindings.Operations.PolicyDigest,
			"replica_ids": []any{
				bindings.Operations.ReplicaIDs[0],
				bindings.Operations.ReplicaIDs[1],
			},
			"window": map[string]any{
				"start_unix": json.Number(strconv.FormatInt(bindings.Operations.WindowStart, 10)),
				"end_unix":   json.Number(strconv.FormatInt(bindings.Operations.WindowEnd, 10)),
			},
			"raw_receipt_hashes": map[string]any{
				"dashboard_render":        bindings.Operations.DashboardReceiptDigest,
				"structured_log_query":    bindings.Operations.StructuredLogReceiptDigest,
				"distributed_trace_query": bindings.Operations.DistributedTraceDigest,
			},
			"result":            flagshipOperationsVerifiedResult,
			"cross_link_sha256": bindings.Operations.CrossLinkDigest,
		},
		"lifecycle": map[string]any{
			"lifecycle_id":       bindings.Lifecycle.LifecycleID,
			"lifecycle_sha256":   bindings.Lifecycle.LifecycleDigest,
			"fence_token_sha256": bindings.Lifecycle.FenceTokenDigest,
			"fence_epoch":        json.Number(strconv.FormatInt(bindings.Lifecycle.FenceEpoch, 10)),
		},
	}
}

func authorityTestSignReceipt(
	t *testing.T,
	schema string,
	producer string,
	keyID string,
	payload map[string]any,
	domain string,
	privateKey ed25519.PrivateKey,
) []byte {
	t.Helper()
	payloadDigest := authorityTestPayloadDigest(t, payload)
	unsigned := map[string]any{
		"schema":         schema,
		"producer":       producer,
		"key_id":         keyID,
		"payload":        payload,
		"payload_sha256": payloadDigest,
	}
	canonicalUnsigned, err := canonicalJSON(unsigned)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(canonicalUnsigned)
	message, err := authorityReceiptSignatureMessage(domain, canonicalUnsigned)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(message)
	signature := ed25519.Sign(privateKey, message)
	defer zero(signature)
	signed := map[string]any{
		"schema":         schema,
		"producer":       producer,
		"key_id":         keyID,
		"payload":        payload,
		"payload_sha256": payloadDigest,
		"signature":      base64.RawURLEncoding.EncodeToString(signature),
	}
	raw, err := canonicalJSON(signed)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func authorityTestUnsignedMessage(
	t *testing.T,
	raw []byte,
	domain string,
) ([]byte, []byte, string) {
	t.Helper()
	outer := authorityTestDecodeObject(t, raw)
	signature, ok := exactString(outer["signature"])
	if !ok {
		t.Fatal("test receipt signature is not a string")
	}
	delete(outer, "signature")
	unsigned, err := canonicalJSON(outer)
	if err != nil {
		t.Fatal(err)
	}
	message, err := authorityReceiptSignatureMessage(domain, unsigned)
	if err != nil {
		zero(unsigned)
		t.Fatal(err)
	}
	return unsigned, message, signature
}

func authorityTestRefreshBindingDigest(t *testing.T, payload map[string]any) {
	t.Helper()
	common := map[string]any{
		"request":                      payload["request"],
		"release":                      payload["release"],
		"gold_operations_verification": payload["gold_operations_verification"],
		"lifecycle":                    payload["lifecycle"],
	}
	payload["binding_sha256"] = authorityTestCommonBindingDigest(t, common)
}

func authorityTestCommonBindingDigest(t *testing.T, common map[string]any) string {
	t.Helper()
	canonical, err := canonicalJSON(common)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(canonical)
	return sha256Digest(canonical)
}

func authorityTestPayloadDigest(t *testing.T, payload map[string]any) string {
	t.Helper()
	canonical, err := canonicalJSON(payload)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(canonical)
	return sha256Digest(canonical)
}

func authorityTestKeyID(t *testing.T, publicKey ed25519.PublicKey) string {
	t.Helper()
	der, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(der)
	return sha256Digest(der)
}

func authorityTestDigest(character string) string {
	return "sha256:" + strings.Repeat(character, 64)
}

func authorityTestObject(parent map[string]any, key string) map[string]any {
	return parent[key].(map[string]any)
}

func authorityTestDecodeObject(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	value, err := decodeStrictJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	result, ok := value.(map[string]any)
	if !ok {
		t.Fatal("test fixture is not an object")
	}
	return result
}

func authorityTestCloneObject(t *testing.T, value map[string]any) map[string]any {
	t.Helper()
	raw, err := canonicalJSON(value)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(raw)
	return authorityTestDecodeObject(t, raw)
}

func authorityTestCloneObjectNoTest(value map[string]any) map[string]any {
	raw, err := canonicalJSON(value)
	if err != nil {
		panic(err)
	}
	defer zero(raw)
	decoded, err := decodeStrictJSON(raw)
	if err != nil {
		panic(err)
	}
	return decoded.(map[string]any)
}

func assertAuthorityReceiptPairRejected(t *testing.T, fixture *authorityReceiptTestFixture) {
	t.Helper()
	pair, err := verifyAuthorityReceiptPair(
		fixture.launchRaw,
		fixture.evidenceRaw,
		fixture.roots,
		fixture.bindings,
		fixture.storage,
		fixture.now,
	)
	if pair != nil {
		pair.release()
	}
	if err == nil {
		t.Fatal("conflicting or invalid authority receipt pair was accepted")
	}
}
