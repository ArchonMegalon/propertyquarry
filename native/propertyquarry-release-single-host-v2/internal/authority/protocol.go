package authority

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"sync"
	"syscall"
	"time"
)

const (
	requestSchema        = "propertyquarry.release-control.single-host-request.v2"
	maximumRequestBytes  = 65536
	maximumResponseBytes = maximumJournalBytes
	// The protected GitHub job has a hard six-hour ceiling. The three ordered
	// client calls consume at most 350 minutes in total, preserving ten minutes
	// for runner setup, shell handoff, and fail-closed receipt cleanup.
	releaseWorkflowJobTimeout       = 360 * time.Minute
	releaseWorkflowSafetyMargin     = 10 * time.Minute
	preflightExecutionTimeout       = 11*time.Minute + 30*time.Second
	preflightServerProtocolTimeout  = 12 * time.Minute
	preflightClientProtocolTimeout  = 13 * time.Minute
	releaseExecutionTimeout         = 290 * time.Minute
	rollbackExecutionTimeout        = 10 * time.Minute
	releaseServerProtocolTimeout    = 300*time.Minute + 30*time.Second
	releaseClientProtocolTimeout    = 301 * time.Minute
	aiInstallExecutionTimeout       = 32 * time.Minute
	aiInstallServerProtocolTimeout  = 34*time.Minute + 30*time.Second
	aiInstallClientProtocolTimeout  = 36 * time.Minute
	aiCloseoutExecutionTimeout      = 15 * time.Minute
	aiCloseoutServerProtocolTimeout = 17 * time.Minute
	aiCloseoutClientProtocolTimeout = 18 * time.Minute
)

type workflowOperationTimeouts struct {
	Execution time.Duration
	Server    time.Duration
	Client    time.Duration
}

func validateWorkflowTimeoutEnvelope() error {
	installPhaseEnvelope :=
		aiPanoramaBootstrapPhaseTimeout +
			aiPanoramaDiscoveryPhaseTimeout +
			aiPanoramaPreflightPhaseTimeout +
			aiPanoramaApplyPhaseTimeout +
			6*aiPanoramaCleanupTimeout
	if preflightExecutionTimeout <= 11*time.Minute ||
		releaseExecutionTimeout <=
			time.Duration(maximumReleaseVerifyStepSeconds)*time.Second ||
		releaseExecutionTimeout+rollbackExecutionTimeout >=
			releaseServerProtocolTimeout ||
		aiInstallExecutionTimeout <= installPhaseEnvelope+5*time.Minute ||
		aiInstallExecutionTimeout+2*aiPanoramaCleanupTimeout >=
			aiInstallServerProtocolTimeout ||
		preflightClientProtocolTimeout+releaseClientProtocolTimeout+
			aiInstallClientProtocolTimeout >
			releaseWorkflowJobTimeout-releaseWorkflowSafetyMargin {
		return fmt.Errorf("workflow-timeout-envelope-invalid")
	}
	return nil
}

func timeoutsForWorkflowOperation(
	operation string,
) (workflowOperationTimeouts, error) {
	if err := validateWorkflowTimeoutEnvelope(); err != nil {
		return workflowOperationTimeouts{}, err
	}
	var result workflowOperationTimeouts
	switch operation {
	case "release-preflight":
		result = workflowOperationTimeouts{
			Execution: preflightExecutionTimeout,
			Server:    preflightServerProtocolTimeout,
			Client:    preflightClientProtocolTimeout,
		}
	case "release-run":
		result = workflowOperationTimeouts{
			Execution: releaseExecutionTimeout,
			Server:    releaseServerProtocolTimeout,
			Client:    releaseClientProtocolTimeout,
		}
	case aiPanoramaInstallOperation:
		result = workflowOperationTimeouts{
			Execution: aiInstallExecutionTimeout,
			Server:    aiInstallServerProtocolTimeout,
			Client:    aiInstallClientProtocolTimeout,
		}
	case aiPanoramaCloseoutOperation:
		result = workflowOperationTimeouts{
			Execution: aiCloseoutExecutionTimeout,
			Server:    aiCloseoutServerProtocolTimeout,
			Client:    aiCloseoutClientProtocolTimeout,
		}
	default:
		return workflowOperationTimeouts{},
			fmt.Errorf("workflow-operation-timeout-invalid")
	}
	if result.Execution <= 0 || result.Execution >= result.Server ||
		result.Server >= result.Client {
		return workflowOperationTimeouts{},
			fmt.Errorf("workflow-operation-timeout-envelope-invalid")
	}
	return result, nil
}

type workflowRequest struct {
	Operation                       string
	RequestID                       string
	OIDCRequestURL                  string
	ActionsToken                    []byte
	DiagnosticRunID                 string
	DiagnosticRunAttempt            int64
	DiagnosticJob                   string
	DiagnosticSHA                   string
	DiagnosticWorkflowSHA           string
	DiagnosticRunnerLabel           string
	RunnerTicketDigest              string
	RunnerLaunchTicketDigest        string
	RunnerNonce                     string
	SecurityBootstrapAttestationSHA string
	SecurityBootstrapRunID          string
	SecurityBootstrapArtifactDigest string
}

type clientResponseExpectation struct {
	Operation                       string
	RequestID                       string
	RunID                           string
	RunAttempt                      int64
	RuntimeSHA                      string
	WorkflowSHA                     string
	SecurityBootstrapAttestationSHA string
	SecurityBootstrapRunID          string
	SecurityBootstrapArtifactDigest string
	RunnerTicketDigest              string
	RunnerLabel                     string
}

func (request *workflowRequest) release() {
	if request != nil {
		zero(request.ActionsToken)
		*request = workflowRequest{}
	}
}

func clientRequest(operation string, stdout io.Writer) error {
	if !validWorkflowOperation(operation) {
		return fmt.Errorf("client-operation-invalid")
	}
	timeouts, err := timeoutsForWorkflowOperation(operation)
	if err != nil {
		return err
	}
	if os.Geteuid() == 0 {
		return fmt.Errorf("client-root-forbidden")
	}
	if os.Getenv("PROPERTYQUARRY_OIDC_TOKEN_FD") != "9" {
		return fmt.Errorf("client-token-fd-invalid")
	}
	token, err := readTokenFD(9)
	if err != nil {
		return err
	}
	defer zero(token)
	runAttempt, err := strconv.ParseInt(os.Getenv("GITHUB_RUN_ATTEMPT"), 10, 64)
	if err != nil || runAttempt < 1 || runAttempt > 1<<31-1 {
		return fmt.Errorf("client-run-attempt-invalid")
	}
	runID := os.Getenv("GITHUB_RUN_ID")
	bootstrapAttestationSHA := os.Getenv("PROPERTYQUARRY_SECURITY_BOOTSTRAP_ATTESTATION_SHA256")
	bootstrapRunID := os.Getenv("PROPERTYQUARRY_SECURITY_BOOTSTRAP_RUN_ID")
	bootstrapArtifactDigest := os.Getenv("PROPERTYQUARRY_SECURITY_BOOTSTRAP_ARTIFACT_DIGEST")
	runnerLabel := os.Getenv("PROPERTYQUARRY_RELEASE_RUNNER_LABEL")
	runnerTicketDigest := os.Getenv("PROPERTYQUARRY_RELEASE_RUNNER_TICKET_SHA256")
	if !envelopeSHAPattern.MatchString(bootstrapAttestationSHA) || !decimal(bootstrapRunID) || !digestPattern.MatchString(bootstrapArtifactDigest) {
		return fmt.Errorf("client-security-bootstrap-evidence-invalid")
	}
	if !runnerLabelPattern.MatchString(runnerLabel) || !digestPattern.MatchString(runnerTicketDigest) {
		return fmt.Errorf("client-runner-ticket-evidence-invalid")
	}
	requestID, err := requestIDForRun(operation, runID, runAttempt)
	if err != nil {
		return err
	}
	requestValue := map[string]any{
		"schema": requestSchema, "version": json.Number("2"), "operation": operation, "request_id": requestID,
		"oidc_request_url":      os.Getenv("ACTIONS_ID_TOKEN_REQUEST_URL"),
		"actions_request_token": base64.RawURLEncoding.EncodeToString(token),
		"security_bootstrap_attestation": map[string]any{
			"artifact_digest": bootstrapArtifactDigest, "attestation_sha256": bootstrapAttestationSHA, "run_id": bootstrapRunID,
		},
		"diagnostic_identity": map[string]any{
			"repository": os.Getenv("GITHUB_REPOSITORY"), "ref": os.Getenv("GITHUB_REF"), "candidate_sha": os.Getenv("GITHUB_SHA"),
			"workflow_ref": os.Getenv("GITHUB_WORKFLOW_REF"), "workflow_sha": os.Getenv("GITHUB_WORKFLOW_SHA"),
			"run_id": runID, "run_attempt": json.Number(strconv.FormatInt(runAttempt, 10)),
			"job": os.Getenv("GITHUB_JOB"), "environment": Environment,
			"runner_label": runnerLabel, "runner_ticket_sha256": runnerTicketDigest,
		},
	}
	raw, err := canonicalJSON(requestValue)
	if err != nil {
		return err
	}
	defer zero(raw)
	connection, err := net.DialTimeout("unix", SocketPath, 5*time.Second)
	if err != nil {
		return fmt.Errorf("authority-socket-unavailable")
	}
	defer connection.Close()
	unix, ok := connection.(*net.UnixConn)
	if !ok {
		return fmt.Errorf("authority-socket-invalid")
	}
	if err := unix.SetDeadline(time.Now().Add(timeouts.Client)); err != nil {
		return err
	}
	peer, err := unixPeer(unix)
	if err != nil || peer.Uid != 0 || peer.Gid != 0 {
		return fmt.Errorf("authority-peer-invalid")
	}
	if err := writeFrame(unix, raw, maximumRequestBytes); err != nil {
		return err
	}
	response, err := readFrame(unix, maximumResponseBytes)
	if err != nil {
		return err
	}
	defer zero(response)
	anchorRaw, err := secureRead("/", ReceiptAnchorPath, 0o444, 0, 0, 65536)
	if err != nil {
		return fmt.Errorf("client-receipt-anchor-unavailable")
	}
	defer zero(anchorRaw)
	anchor, _, err := parsePublicKey(anchorRaw)
	if err != nil {
		return fmt.Errorf("client-receipt-anchor-invalid")
	}
	return emitClientResponse(response, anchor, clientResponseExpectation{
		Operation: operation, RequestID: requestID, RunID: runID, RunAttempt: runAttempt, WorkflowSHA: os.Getenv("GITHUB_SHA"),
		SecurityBootstrapAttestationSHA: bootstrapAttestationSHA, SecurityBootstrapRunID: bootstrapRunID,
		SecurityBootstrapArtifactDigest: bootstrapArtifactDigest,
		RunnerTicketDigest:              runnerTicketDigest, RunnerLabel: runnerLabel,
	}, stdout)
}

func emitClientResponse(response []byte, public ed25519.PublicKey, expected clientResponseExpectation, stdout io.Writer) error {
	payload, canonicalPayload, err := verifySignedReceiptPayload(response, public)
	if err != nil {
		return fmt.Errorf("client-response-authentication-failed")
	}
	defer zero(canonicalPayload)
	outcomeErr := validateClientResponsePayload(payload, public, expected)
	wire := append(append([]byte(nil), response...), '\n')
	written, writeErr := stdout.Write(wire)
	zero(wire)
	if writeErr != nil || written != len(response)+1 {
		return fmt.Errorf("client-response-write-failed")
	}
	if outcomeErr != nil {
		return outcomeErr
	}
	return nil
}

func validateClientResponsePayload(payload map[string]any, public ed25519.PublicKey, expected clientResponseExpectation) error {
	keyID, keyErr := publicKeyID(public)
	runAttempt, attemptOK := exactInt(payload["run_attempt"], expected.RunAttempt, expected.RunAttempt)
	checkRunID, checkOK := exactString(payload["check_run_id"])
	runnerID, runnerIDOK := exactString(payload["runner_id"])
	runnerName, runnerNameOK := exactString(payload["runner_name"])
	runnerLabel, runnerLabelOK := exactString(payload["runner_label"])
	runnerNonce, runnerNonceOK := exactString(payload["runner_label_nonce"])
	runnerDispatchTicket, dispatchTicketOK := exactString(payload["runner_dispatch_ticket_sha256"])
	runnerLaunchTicket, launchTicketOK := exactString(payload["runner_launch_ticket_sha256"])
	expectedRunnerLabel, runnerBound := runnerLabelForName(runnerName, "pqrelease-")
	if keyErr != nil || payload["schema"] != journalSchema || payload["version"] != json.Number("2") ||
		payload["operation"] != expected.Operation || payload["request_id"] != expected.RequestID || payload["run_id"] != expected.RunID || !attemptOK || runAttempt != expected.RunAttempt ||
		(!shaPattern.MatchString(stringValue(payload["runtime_sha"])) || (expected.RuntimeSHA != "" && payload["runtime_sha"] != expected.RuntimeSHA)) ||
		payload["workflow_sha"] != expected.WorkflowSHA || payload["repository"] != Repository || payload["workflow_ref"] != WorkflowRef ||
		payload["security_bootstrap_attestation_sha256"] != expected.SecurityBootstrapAttestationSHA || payload["security_bootstrap_run_id"] != expected.SecurityBootstrapRunID ||
		payload["security_bootstrap_artifact_digest"] != expected.SecurityBootstrapArtifactDigest || payload["security_bootstrap_evidence_bound"] != true ||
		payload["security_bootstrap_evidence_source"] != "workflow-needs-output" || payload["security_bootstrap_artifact_authenticated"] != false ||
		payload["configured_receipt_key_id"] != keyID || payload["authoritative"] != true || payload["single_host_authority"] != true || payload["external_cas_profile"] != false ||
		payload["github_oidc_signature_verified"] != true || payload["github_job_correlation_verified"] != true || !checkOK || !decimal(checkRunID) ||
		!runnerIDOK || !decimal(runnerID) || !runnerNameOK || !runnerLabelOK || !runnerBound || runnerLabel != expectedRunnerLabel || runnerLabel != expected.RunnerLabel ||
		!runnerNonceOK || runnerNonce != runnerLabel[len("pqrelease-"):] || !dispatchTicketOK || runnerDispatchTicket != expected.RunnerTicketDigest ||
		!launchTicketOK || !digestPattern.MatchString(runnerLaunchTicket) || payload["runner_ticket_authenticated"] != true {
		return fmt.Errorf("client-response-binding-invalid")
	}
	eventType, eventOK := exactString(payload["event_type"])
	disposition, dispositionOK := exactString(payload["disposition"])
	if !eventOK || !dispositionOK {
		return fmt.Errorf("client-response-outcome-invalid")
	}
	switch expected.Operation {
	case "release-preflight":
		if eventType != "preflight-ready" || disposition != "ready" || payload["ready"] != true || payload["production_ready"] != false ||
			payload["release_effects_authorized"] != false || payload["release_effects_performed"] != false {
			return fmt.Errorf("client-preflight-not-ready")
		}
	case "release-run":
		if eventType != "run-succeeded" || disposition != "succeeded" || payload["ready"] != false || payload["production_ready"] != true ||
			payload["release_effects_authorized"] != true || payload["release_effects_performed"] != true || payload["rollback_performed"] != false {
			return fmt.Errorf("client-release-not-successful")
		}
	case aiPanoramaInstallOperation:
		if eventType != aiPanoramaInstallSucceededEvent || disposition != "succeeded" ||
			payload["ready"] != false || payload["production_ready"] != true ||
			payload["release_effects_authorized"] != true || payload["release_effects_performed"] != true ||
			payload["rollback_performed"] != false ||
			payload["ai_panorama_install_verified"] != true ||
			payload["ai_panorama_slug"] != aiPanoramaPraterSlug ||
			payload["ai_panorama_control_url"] != aiPanoramaPraterControlURL {
			return fmt.Errorf("client-ai-panorama-install-not-successful")
		}
	case aiPanoramaCloseoutOperation:
		if eventType != aiPanoramaCloseoutSucceededEvent || disposition != "revoked" ||
			payload["ready"] != false || payload["production_ready"] != false ||
			payload["release_effects_authorized"] != true ||
			payload["rollback_performed"] != false ||
			payload["ai_panorama_revocation_verified"] != true ||
			payload["ai_panorama_slug"] != aiPanoramaPraterSlug {
			return fmt.Errorf("client-ai-panorama-closeout-not-successful")
		}
	default:
		return fmt.Errorf("client-response-operation-invalid")
	}
	return nil
}

func serverConnection(stdin *os.File, stdout io.Writer, root string) error {
	if os.Geteuid() != 0 || stdin == nil {
		return fmt.Errorf("server-root-required")
	}
	duplicated, err := syscall.Dup(int(stdin.Fd()))
	if err != nil {
		return fmt.Errorf("server-socket-dup-failed")
	}
	file := os.NewFile(uintptr(duplicated), "single-host-request")
	defer file.Close()
	connection, err := net.FileConn(file)
	if err != nil {
		return fmt.Errorf("server-socket-invalid")
	}
	defer connection.Close()
	unix, ok := connection.(*net.UnixConn)
	if !ok {
		return fmt.Errorf("server-socket-type-invalid")
	}
	if err := unix.SetDeadline(
		time.Now().Add(releaseServerProtocolTimeout),
	); err != nil {
		return err
	}
	config, key, err := LoadConfig(root)
	if err != nil {
		return err
	}
	defer config.release()
	defer zero(key)
	peer, err := unixPeer(unix)
	if err != nil || int64(peer.Uid) != config.AllowedRunnerUID || int64(peer.Gid) != config.AllowedRunnerGID {
		return fmt.Errorf("server-peer-rejected")
	}
	raw, err := readFrame(unix, maximumRequestBytes)
	if err != nil {
		return err
	}
	defer zero(raw)
	request, err := parseWorkflowRequest(raw, config)
	if err != nil {
		return err
	}
	defer request.release()
	timeouts, err := timeoutsForWorkflowOperation(request.Operation)
	if err != nil {
		return err
	}
	if err := unix.SetDeadline(time.Now().Add(timeouts.Server)); err != nil {
		return err
	}
	baseContext, baseCancel := context.WithTimeout(
		context.Background(), timeouts.Execution,
	)
	defer baseCancel()
	requestContext, requestCancel, requestComplete := peerBoundContext(baseContext, unix)
	defer requestCancel()
	defer requestComplete()
	response, err := processWorkflowRequestContext(requestContext, root, config, key, request)
	if err != nil {
		return err
	}
	defer zero(response)
	if err := writeFrame(unix, response, maximumResponseBytes); err != nil {
		return err
	}
	_ = stdout
	return nil
}

func peerBoundContext(parent context.Context, connection *net.UnixConn) (context.Context, context.CancelFunc, func()) {
	ctx, cancel := context.WithCancel(parent)
	completed := make(chan struct{})
	var once sync.Once
	complete := func() { once.Do(func() { close(completed) }) }
	go func() {
		one := make([]byte, 1)
		_, _ = connection.Read(one)
		zero(one)
		select {
		case <-completed:
			return
		default:
			cancel()
		}
	}()
	return ctx, cancel, complete
}

func parseWorkflowRequest(raw []byte, config *Config) (*workflowRequest, error) {
	value, err := strictJSON(raw, maximumRequestBytes)
	if err != nil {
		return nil, err
	}
	if !hasKeys(value, "actions_request_token", "diagnostic_identity", "oidc_request_url", "operation", "request_id", "schema", "security_bootstrap_attestation", "version") ||
		value["schema"] != requestSchema || value["version"] != json.Number("2") {
		return nil, fmt.Errorf("request-shape-invalid")
	}
	operation, opOK := exactString(value["operation"])
	requestID, requestOK := exactString(value["request_id"])
	requestURL, urlOK := exactString(value["oidc_request_url"])
	tokenText, tokenOK := exactString(value["actions_request_token"])
	identity, identityOK := value["diagnostic_identity"].(map[string]any)
	bootstrap, bootstrapOK := value["security_bootstrap_attestation"].(map[string]any)
	if !opOK || !validWorkflowOperation(operation) || !requestOK || !idPattern.MatchString(requestID) || !urlOK || !tokenOK || !identityOK ||
		!hasKeys(identity, "candidate_sha", "environment", "job", "ref", "repository", "run_attempt", "run_id", "runner_label", "runner_ticket_sha256", "workflow_ref", "workflow_sha") || !bootstrapOK ||
		!hasKeys(bootstrap, "artifact_digest", "attestation_sha256", "run_id") {
		return nil, fmt.Errorf("request-binding-invalid")
	}
	token, err := base64.RawURLEncoding.DecodeString(tokenText)
	if err != nil || len(token) < 1 || len(token) > maximumJWTBytes {
		zero(token)
		return nil, fmt.Errorf("request-token-invalid")
	}
	runID, _ := exactString(identity["run_id"])
	runAttempt, attemptOK := exactInt(identity["run_attempt"], 1, 1<<31-1)
	job, _ := exactString(identity["job"])
	sha, _ := exactString(identity["candidate_sha"])
	workflowSHA, _ := exactString(identity["workflow_sha"])
	runnerLabel, runnerLabelOK := exactString(identity["runner_label"])
	runnerTicketDigest, runnerTicketOK := exactString(identity["runner_ticket_sha256"])
	bootstrapAttestationSHA, bootstrapAttestationOK := exactString(bootstrap["attestation_sha256"])
	bootstrapRunID, bootstrapRunIDOK := exactString(bootstrap["run_id"])
	bootstrapArtifactDigest, bootstrapArtifactOK := exactString(bootstrap["artifact_digest"])
	if config == nil || identity["repository"] != Repository || identity["ref"] != "refs/heads/main" || sha != config.WorkflowSHA || identity["workflow_ref"] != WorkflowRef ||
		workflowSHA != config.WorkflowSHA || !decimal(runID) || !attemptOK || job != ReleaseJob || identity["environment"] != Environment ||
		!bootstrapAttestationOK || !envelopeSHAPattern.MatchString(bootstrapAttestationSHA) || !bootstrapRunIDOK || !decimal(bootstrapRunID) ||
		!bootstrapArtifactOK || !digestPattern.MatchString(bootstrapArtifactDigest) || !runnerLabelOK || !runnerLabelPattern.MatchString(runnerLabel) ||
		!runnerTicketOK || !digestPattern.MatchString(runnerTicketDigest) {
		zero(token)
		return nil, fmt.Errorf("request-diagnostic-binding-invalid")
	}
	return &workflowRequest{Operation: operation, RequestID: requestID, OIDCRequestURL: requestURL, ActionsToken: token,
		DiagnosticRunID: runID, DiagnosticRunAttempt: runAttempt, DiagnosticJob: job, DiagnosticSHA: sha, DiagnosticWorkflowSHA: workflowSHA,
		DiagnosticRunnerLabel: runnerLabel, RunnerTicketDigest: runnerTicketDigest,
		SecurityBootstrapAttestationSHA: bootstrapAttestationSHA, SecurityBootstrapRunID: bootstrapRunID,
		SecurityBootstrapArtifactDigest: bootstrapArtifactDigest}, nil
}

func readTokenFD(fd int) ([]byte, error) {
	file := os.NewFile(uintptr(fd), "oidc-token")
	if file == nil {
		return nil, fmt.Errorf("token-fd-invalid")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || info.Mode()&os.ModeNamedPipe == 0 {
		return nil, fmt.Errorf("token-fd-type-invalid")
	}
	raw, err := io.ReadAll(io.LimitReader(file, maximumJWTBytes+2))
	if err != nil || len(raw) < 2 || len(raw) > maximumJWTBytes+1 || raw[len(raw)-1] != '\n' || bytes.Contains(raw[:len(raw)-1], []byte{'\n'}) || bytes.Contains(raw[:len(raw)-1], []byte{'\r'}) {
		zero(raw)
		return nil, fmt.Errorf("token-fd-content-invalid")
	}
	token := append([]byte(nil), raw[:len(raw)-1]...)
	zero(raw)
	return token, nil
}

func writeFrame(writer io.Writer, raw []byte, maximum int) error {
	if len(raw) < 1 || len(raw) > maximum {
		return fmt.Errorf("frame-size-invalid")
	}
	header := make([]byte, 8)
	binary.BigEndian.PutUint64(header, uint64(len(raw)))
	if err := writeWriter(writer, header); err != nil {
		return err
	}
	return writeWriter(writer, raw)
}

func readFrame(reader io.Reader, maximum int) ([]byte, error) {
	header := make([]byte, 8)
	if _, err := io.ReadFull(reader, header); err != nil {
		return nil, fmt.Errorf("frame-header-invalid")
	}
	length := binary.BigEndian.Uint64(header)
	zero(header)
	if length < 1 || length > uint64(maximum) {
		return nil, fmt.Errorf("frame-size-invalid")
	}
	raw := make([]byte, int(length))
	if _, err := io.ReadFull(reader, raw); err != nil {
		zero(raw)
		return nil, fmt.Errorf("frame-body-invalid")
	}
	return raw, nil
}

func writeWriter(writer io.Writer, raw []byte) error {
	for len(raw) > 0 {
		written, err := writer.Write(raw)
		if err != nil || written < 1 {
			return fmt.Errorf("frame-write-failed")
		}
		raw = raw[written:]
	}
	return nil
}

func unixPeer(connection *net.UnixConn) (*syscall.Ucred, error) {
	raw, err := connection.SyscallConn()
	if err != nil {
		return nil, err
	}
	var credential *syscall.Ucred
	var controlErr error
	err = raw.Control(func(fd uintptr) {
		credential, controlErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	})
	if err != nil {
		return nil, err
	}
	if controlErr != nil {
		return nil, controlErr
	}
	if credential == nil {
		return nil, fmt.Errorf("peer-credential-missing")
	}
	return credential, nil
}

func randomRequestID(operation string) (string, error) {
	raw := make([]byte, 16)
	if _, err := io.ReadFull(randReader, raw); err != nil {
		return "", err
	}
	text := fmt.Sprintf("%s-%x", operation, raw)
	zero(raw)
	return text, nil
}

func requestIDForRun(operation, runID string, runAttempt int64) (string, error) {
	if !validWorkflowOperation(operation) || !decimal(runID) || runAttempt < 1 || runAttempt > 1<<31-1 {
		return "", fmt.Errorf("client-request-id-invalid")
	}
	requestID := operation + "-" + runID + "-" + strconv.FormatInt(runAttempt, 10)
	if !idPattern.MatchString(requestID) {
		return "", fmt.Errorf("client-request-id-invalid")
	}
	return requestID, nil
}

func validWorkflowOperation(operation string) bool {
	return operation == "release-preflight" || operation == "release-run" ||
		operation == aiPanoramaInstallOperation || operation == aiPanoramaCloseoutOperation
}

var randReader io.Reader = rand.Reader
