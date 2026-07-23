//go:build linux && amd64

package authority

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

type runnerLifecycleFixture struct {
	authority *authorityFixture
	listener  net.Listener
	now       time.Time
	nonce     string
	label     string
}

func newRunnerLifecycleFixture(t *testing.T) *runnerLifecycleFixture {
	t.Helper()
	fixture := newAuthorityFixture(t, false)
	nonce := strings.Repeat("01", 32)
	nonceRaw, err := hex.DecodeString(nonce)
	if err != nil {
		fixture.close()
		t.Fatal(err)
	}
	derived := sha256.Sum256(append([]byte(runnerLabelDerivationDomain), nonceRaw...))
	zero(nonceRaw)
	label := "pqrelease-" + hex.EncodeToString(derived[:16])

	fixture.plan["runner_label"] = label
	planRaw, err := canonicalJSON(fixture.plan)
	if err != nil {
		fixture.close()
		t.Fatal(err)
	}
	writeFixture(t, rooted(fixture.root, PlanPath), planRaw, 0o444)

	configRaw, err := os.ReadFile(rooted(fixture.root, ConfigPath))
	if err != nil {
		zero(planRaw)
		fixture.close()
		t.Fatal(err)
	}
	configValue, err := strictJSON(configRaw, maximumConfigBytes)
	zero(configRaw)
	if err != nil {
		zero(planRaw)
		fixture.close()
		t.Fatal(err)
	}
	configValue["runner_label"] = label
	configValue["plan_digest"] = digest(planRaw)
	configRaw, err = canonicalJSON(configValue)
	if err != nil {
		zero(planRaw)
		fixture.close()
		t.Fatal(err)
	}
	configSignature := ed25519.Sign(fixture.packageKey, framed(configDomain, configRaw))
	writeFixture(t, rooted(fixture.root, ConfigPath), configRaw, 0o400)
	writeFixture(t, rooted(fixture.root, ConfigSignaturePath), configSignature, 0o444)
	zero(configRaw)
	zero(configSignature)

	fixture.config.release()
	var configKey ed25519.PrivateKey
	fixture.config, configKey, err = LoadConfig(fixture.root)
	if err != nil {
		zero(planRaw)
		fixture.close()
		t.Fatal(err)
	}
	zero(configKey)
	zero(fixture.planRaw)
	fixture.planRaw = planRaw

	runDirectory := rooted(fixture.root, "/var/run")
	if err := os.Mkdir(runDirectory, 0o700); err != nil {
		fixture.close()
		t.Fatal(err)
	}
	socketPath := rooted(fixture.root, "/var/run/docker.sock")
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		fixture.close()
		t.Fatal(err)
	}
	if err := os.Chmod(socketPath, 0o660); err != nil {
		_ = listener.Close()
		fixture.close()
		t.Fatal(err)
	}

	now := time.Unix(1_900_000_000, 0).UTC()
	observedSocket, err := observeDockerSocket(fixture.root)
	if err != nil {
		_ = listener.Close()
		fixture.close()
		t.Fatal(err)
	}
	ticketPayload := map[string]any{
		"authority_profile":      "single-host-production-v2",
		"bound_at_epoch":         json.Number(strconv.FormatInt(now.Unix(), 10)),
		"config_digest":          fixture.config.Digest,
		"dispatch_ticket_sha256": fixture.config.RunnerReservationDigest,
		"docker_socket": map[string]any{
			"device": json.Number(strconv.FormatUint(observedSocket.Device, 10)),
			"gid":    json.Number(strconv.FormatUint(uint64(observedSocket.GID), 10)),
			"inode":  json.Number(strconv.FormatUint(observedSocket.Inode, 10)),
			"mode":   "0660", "nlink": json.Number("1"), "path": "/var/run/docker.sock",
			"uid": json.Number(strconv.FormatUint(uint64(observedSocket.UID), 10)),
		},
		"environment":                         Environment,
		"expires_at_epoch":                    json.Number(strconv.FormatInt(now.Add(20*time.Minute).Unix(), 10)),
		"job_id":                              fixture.config.RunnerJobID,
		"plan_digest":                         fixture.config.PlanDigest,
		"receipt_authority_key_id":            fixture.config.ReceiptAuthorityKeyID,
		"release_job":                         ReleaseJob,
		"repository":                          Repository,
		"repository_id":                       fixture.config.RepositoryID,
		"repository_owner_id":                 fixture.config.RepositoryOwnerID,
		"reservation_nonce":                   nonce,
		"run_attempt":                         json.Number(strconv.FormatInt(fixture.config.RunnerRunAttempt, 10)),
		"run_id":                              fixture.config.RunnerRunID,
		"runner_image":                        fixture.config.WebImage,
		"runner_label":                        label,
		"runner_label_nonce":                  label[len("pqrelease-"):],
		"runner_prerequisite_intent_sha256":   fixture.config.RunnerPrerequisiteIntentDigest,
		"runner_prerequisite_approval_sha256": fixture.config.RunnerPrerequisiteApprovalDigest,
		"runner_prerequisite_approval_payload_sha256": fixture.config.RunnerPrerequisiteApprovalPayloadDigest,
		"runner_prerequisite_job_id":                  fixture.config.RunnerPrerequisiteJobID,
		"runtime_sha":                                 fixture.config.RuntimeSHA,
		"schema":                                      runnerTicketSchema,
		"version":                                     json.Number("2"),
		"workflow_path":                               ".github/workflows/smoke-runtime.yml",
		"workflow_ref":                                WorkflowRef,
		"workflow_sha":                                fixture.config.WorkflowSHA,
	}
	ticketRaw, err := signRunnerWire(ticketPayload, fixture.receiptKey, runnerTicketSignatureDomain)
	if err != nil {
		_ = listener.Close()
		fixture.close()
		t.Fatal(err)
	}
	writeFixture(t, rooted(fixture.root, RunnerLaunchTicketPath), ticketRaw, 0o400)
	manifest := map[string]any{
		"config_digest": fixture.config.Digest,
		"files": []any{map[string]any{
			"install_path": RunnerLaunchTicketPath, "mode": "0400", "package_path": "runner-launch-ticket.v2.json",
			"purpose": "ephemeral-runner-launch-ticket", "sha256": digest(ticketRaw), "size": json.Number(strconv.Itoa(len(ticketRaw))),
		}},
		"plan_digest": fixture.config.PlanDigest, "receipt_authority_key_id": fixture.config.ReceiptAuthorityKeyID,
		"schema": "propertyquarry.release-control.single-host-package.v2",
	}
	manifestRaw, err := canonicalJSON(manifest)
	if err != nil {
		zero(ticketRaw)
		_ = listener.Close()
		fixture.close()
		t.Fatal(err)
	}
	manifestSignature := ed25519.Sign(fixture.packageKey, framed(packageManifestSignatureDomain, manifestRaw))
	writeFixture(t, rooted(fixture.root, runnerPackageManifestPath), manifestRaw, 0o444)
	writeFixture(t, rooted(fixture.root, runnerPackageManifestSignaturePath), manifestSignature, 0o444)
	zero(ticketRaw)
	zero(manifestRaw)
	zero(manifestSignature)

	result := &runnerLifecycleFixture{authority: fixture, listener: listener, now: now, nonce: nonce, label: label}
	t.Cleanup(func() {
		_ = listener.Close()
		fixture.close()
	})
	return result
}

func (fixture *runnerLifecycleFixture) admitAndStart(t *testing.T) *runnerTicketBinding {
	t.Helper()
	result, err := admitRunnerLaunch(fixture.authority.root, fixture.now)
	if err != nil || result["disposition"] != "admitted" {
		t.Fatalf("installed ticket was not admitted: result=%#v err=%v", result, err)
	}
	retry, err := admitRunnerLaunch(fixture.authority.root, fixture.now.Add(time.Second))
	if err != nil || retry["disposition"] != "already-admitted" {
		t.Fatalf("admission retry did not converge: result=%#v err=%v", retry, err)
	}
	binding, _, err := loadRunnerClaim(fixture.authority.root, fixture.authority.config, fixture.authority.receiptKey.Public().(ed25519.PublicKey), fixture.now, false)
	if err != nil {
		t.Fatal(err)
	}
	session := &runnerSessionObservation{Device: 101, Inode: 202, TreeDigest: "sha256:" + strings.Repeat("9", 64)}
	if err := authorizeRunnerStart(fixture.authority.root, binding, "789", "sha256:"+strings.Repeat("8", 64), session, fixture.now.Add(2*time.Second)); err != nil {
		t.Fatal(err)
	}
	started, _, err := loadRunnerStart(fixture.authority.root, fixture.authority.config, fixture.authority.receiptKey.Public().(ed25519.PublicKey), fixture.now.Add(3*time.Second), false)
	if err != nil || started.RunnerID != "789" || started.SessionDevice != session.Device || started.SessionInode != session.Inode || started.SessionTreeDigest != session.TreeDigest {
		t.Fatalf("signed start did not preserve runner/session binding: binding=%#v err=%v", started, err)
	}
	launcher, err := verifyRunnerStartForLauncher(fixture.authority.root, fixture.label, binding.LaunchTicketDigest, "789", fixture.now.Add(3*time.Second))
	if err != nil || launcher["session_tree_sha256"] != session.TreeDigest {
		t.Fatalf("launcher could not consume signed start: result=%#v err=%v", launcher, err)
	}
	return started
}

func TestInstalledRunnerTicketNormalLifecycleConvergesAfterTerminalFirstCrash(t *testing.T) {
	fixture := newRunnerLifecycleFixture(t)
	previousNow := authorityNow
	authorityNow = func() time.Time { return fixture.now.Add(4 * time.Second) }
	t.Cleanup(func() { authorityNow = previousNow })
	binding := fixture.admitAndStart(t)

	claimRaw, err := os.ReadFile(rooted(fixture.authority.root, runnerClaimPath))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(claimRaw)
	startRaw, err := os.ReadFile(rooted(fixture.authority.root, runnerStartPath))
	if err != nil {
		t.Fatal(err)
	}
	defer zero(startRaw)

	request := &workflowRequest{
		DiagnosticRunID: fixture.authority.config.RunnerRunID, DiagnosticRunAttempt: fixture.authority.config.RunnerRunAttempt,
		DiagnosticRunnerLabel: fixture.label, RunnerTicketDigest: fixture.authority.config.RunnerReservationDigest,
	}
	identity := &Identity{
		RunID: fixture.authority.config.RunnerRunID, RunAttempt: fixture.authority.config.RunnerRunAttempt, CheckRunID: fixture.authority.config.RunnerJobID,
		RunnerID: "789", RunnerName: "pq-release-" + fixture.label[len("pqrelease-"):], RunnerLabel: fixture.label,
	}
	verified, err := verifyRunnerTicketForRequest(fixture.authority.root, fixture.authority.config, request, identity, fixture.now.Add(3*time.Second))
	if err != nil || verified.LaunchTicketDigest != binding.LaunchTicketDigest {
		t.Fatalf("authorized ticket did not bind to request identity: binding=%#v err=%v", verified, err)
	}
	if err := consumeRunnerTicket(fixture.authority.root, verified); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(rooted(fixture.authority.root, RunnerLaunchTicketPath)); err != nil {
		t.Fatalf("immutable launch ticket was removed: %v", err)
	}

	// Recreate the state left by a crash after terminal publication but before
	// logical residue cleanup. The retry must authenticate it and converge.
	writeFixture(t, rooted(fixture.authority.root, runnerClaimPath), claimRaw, 0o400)
	writeFixture(t, rooted(fixture.authority.root, runnerStartPath), startRaw, 0o400)
	if err := consumeRunnerTicket(fixture.authority.root, verified); err != nil {
		t.Fatalf("terminal-first retry did not converge: %v", err)
	}
	for _, absolute := range []string{runnerClaimPath, runnerStartPath} {
		if _, err := os.Lstat(rooted(fixture.authority.root, absolute)); !os.IsNotExist(err) {
			t.Fatalf("logical state survived terminal convergence: %s err=%v", absolute, err)
		}
	}
	terminal, payload, err := loadRunnerTerminal(fixture.authority.root, fixture.authority.config, fixture.authority.receiptKey.Public().(ed25519.PublicKey), fixture.now.Add(5*time.Second), false)
	if err != nil || terminal.RunnerID != "789" || payload["disposition"] != "preflight-admitted-and-consumed" {
		t.Fatalf("normal terminal is not authoritative: binding=%#v payload=%#v err=%v", terminal, payload, err)
	}
	if _, err := verifyRunnerTicketForRequest(fixture.authority.root, fixture.authority.config, request, identity, fixture.now.Add(5*time.Second)); err != nil {
		t.Fatalf("same authenticated job could not use consumed terminal: %v", err)
	}
}

func TestInterruptedRunnerLifecyclePublishesIdempotentRecoveryTerminal(t *testing.T) {
	fixture := newRunnerLifecycleFixture(t)
	binding := fixture.admitAndStart(t)
	observation := "sha256:" + strings.Repeat("7", 64)
	recovered, err := recoverRunnerLifecycle(fixture.authority.root, fixture.now.Add(4*time.Second), observation)
	if err != nil || recovered.LaunchTicketDigest != binding.LaunchTicketDigest || recovered.RunnerID != "789" {
		t.Fatalf("interrupted lifecycle was not recovered: binding=%#v err=%v", recovered, err)
	}
	retry, err := recoverRunnerLifecycle(fixture.authority.root, fixture.now.Add(5*time.Second), observation)
	if err != nil || retry.LaunchTicketDigest != recovered.LaunchTicketDigest || retry.RunnerID != recovered.RunnerID {
		t.Fatalf("recovery retry did not converge: binding=%#v err=%v", retry, err)
	}
	for _, absolute := range []string{runnerClaimPath, runnerStartPath} {
		if _, err := os.Lstat(rooted(fixture.authority.root, absolute)); !os.IsNotExist(err) {
			t.Fatalf("logical state survived recovery terminal: %s err=%v", absolute, err)
		}
	}
	if _, err := os.Lstat(rooted(fixture.authority.root, RunnerLaunchTicketPath)); err != nil {
		t.Fatalf("recovery removed immutable launch ticket: %v", err)
	}
	_, payload, err := loadRunnerTerminal(fixture.authority.root, fixture.authority.config, fixture.authority.receiptKey.Public().(ed25519.PublicKey), fixture.now.Add(5*time.Second), true)
	if err != nil || payload["schema"] != runnerRecoveryTerminalSchema || payload["recovery_observation_sha256"] != observation || payload["remote_runner_absent"] != true {
		t.Fatalf("recovery terminal evidence is incomplete: payload=%#v err=%v", payload, err)
	}
}

func TestRunnerLifecycleFixtureDoesNotUseMaterializerWorktree(t *testing.T) {
	fixture := newRunnerLifecycleFixture(t)
	if strings.HasPrefix(filepath.Clean(fixture.authority.root), "/docker/property") {
		t.Fatal("runner lifecycle fixture unexpectedly used mutable production worktree")
	}
}
