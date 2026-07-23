//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	runnerSupervisorTimeout = 330 * time.Minute
	runnerRegisterTimeout   = 3 * time.Minute
)

type githubRunnerObservation struct {
	ID        string
	Name      string
	Status    string
	Busy      bool
	Ephemeral bool
	Version   string
	Labels    map[string]string
	RawDigest string
}

func githubRunnerAPI(ctx context.Context, client httpDoer, method, endpoint, token string, body []byte, expected ...int) ([]byte, error) {
	if client == nil || token == "" || strings.ContainsAny(token, "\r\n\x00") {
		return nil, fmt.Errorf("runner-github-api-input-invalid")
	}
	request, err := http.NewRequestWithContext(ctx, method, "https://api.github.com/repos/"+Repository+endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("runner-github-api-request-invalid")
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	request.Header.Set("User-Agent", "propertyquarry-release-runner-supervisor-v2")
	if len(body) > 0 {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := client.Do(request)
	request.Header.Del("Authorization")
	if err != nil {
		return nil, fmt.Errorf("runner-github-api-request-failed")
	}
	defer response.Body.Close()
	accepted := false
	for _, status := range expected {
		accepted = accepted || response.StatusCode == status
	}
	if !accepted || response.Request == nil || response.Request.URL.Scheme != "https" || response.Request.URL.Host != "api.github.com" {
		return nil, fmt.Errorf("runner-github-api-response-rejected")
	}
	if response.StatusCode == http.StatusNoContent || response.StatusCode == http.StatusNotFound {
		return nil, nil
	}
	raw, err := boundedRead(response.Body, maximumGitHubAPIBytes)
	if err != nil {
		return nil, fmt.Errorf("runner-github-api-response-invalid")
	}
	return raw, nil
}

func listGitHubRunners(ctx context.Context, client httpDoer, token string) ([]githubRunnerObservation, []byte, error) {
	raw, err := githubRunnerAPI(ctx, client, http.MethodGet, "/actions/runners?per_page=100", token, nil, http.StatusOK)
	if err != nil {
		return nil, nil, err
	}
	value, err := decodedJSONObject(raw, maximumGitHubAPIBytes)
	total, totalOK := claimInt(value["total_count"])
	items, itemsOK := value["runners"].([]any)
	if err != nil || !totalOK || total < 0 || total > 100 || !itemsOK || int64(len(items)) != total {
		zero(raw)
		return nil, nil, fmt.Errorf("runner-github-list-invalid")
	}
	result := make([]githubRunnerObservation, 0, len(items))
	for _, itemRaw := range items {
		item, ok := itemRaw.(map[string]any)
		id, idOK := claimInt(item["id"])
		name, nameOK := exactString(item["name"])
		status, statusOK := exactString(item["status"])
		busy, busyOK := item["busy"].(bool)
		ephemeral, ephemeralOK := item["ephemeral"].(bool)
		version, versionOK := exactString(item["version"])
		labelsRaw, labelsOK := item["labels"].([]any)
		labels := map[string]string{}
		if labelsOK {
			for _, labelRaw := range labelsRaw {
				label, labelOK := labelRaw.(map[string]any)
				labelName, labelNameOK := exactString(label["name"])
				labelType, labelTypeOK := exactString(label["type"])
				if !labelOK || !labelNameOK || !labelTypeOK || labels[labelName] != "" {
					zero(raw)
					return nil, nil, fmt.Errorf("runner-github-list-label-invalid")
				}
				labels[labelName] = labelType
			}
		}
		if !ok || !idOK || id < 1 || !nameOK || !statusOK || !busyOK || !ephemeralOK || !versionOK || !labelsOK {
			zero(raw)
			return nil, nil, fmt.Errorf("runner-github-list-item-invalid")
		}
		result = append(result, githubRunnerObservation{ID: strconv.FormatInt(id, 10), Name: name, Status: status, Busy: busy, Ephemeral: ephemeral, Version: version, Labels: labels})
	}
	return result, raw, nil
}

func exactPendingRunner(items []githubRunnerObservation, binding *runnerTicketBinding) (*githubRunnerObservation, error) {
	expectedName := "pq-release-" + binding.RunnerNonce
	var selected *githubRunnerObservation
	for index := range items {
		item := &items[index]
		_, hasNonce := item.Labels[binding.RunnerLabel]
		if item.Name != expectedName && !hasNonce {
			continue
		}
		if selected != nil || item.Name != expectedName || item.Status != "offline" || item.Busy || !item.Ephemeral || item.Version != pinnedRunnerVersion || len(item.Labels) != 2 || item.Labels["propertyquarry-release-controller-v2"] != "custom" || item.Labels[binding.RunnerLabel] != "custom" || item.Labels["self-hosted"] != "" || item.Labels["Linux"] != "" || item.Labels["X64"] != "" {
			return nil, fmt.Errorf("runner-github-registered-identity-invalid")
		}
		selected = item
	}
	return selected, nil
}

func requireNoPreexistingRunner(items []githubRunnerObservation, binding *runnerTicketBinding) error {
	expectedName := "pq-release-" + binding.RunnerNonce
	for _, item := range items {
		if item.Name == expectedName {
			return fmt.Errorf("runner-github-preexisting-name")
		}
		if _, present := item.Labels[binding.RunnerLabel]; present {
			return fmt.Errorf("runner-github-preexisting-label")
		}
	}
	return nil
}

func deleteMatchingRunners(ctx context.Context, client httpDoer, adminToken string, items []githubRunnerObservation, binding *runnerTicketBinding) error {
	expectedName := "pq-release-" + binding.RunnerNonce
	for _, item := range items {
		_, hasNonce := item.Labels[binding.RunnerLabel]
		if item.Name != expectedName && !hasNonce {
			continue
		}
		if err := deleteGitHubRunner(ctx, client, adminToken, item.ID); err != nil {
			return err
		}
	}
	return nil
}

func createRunnerRegistrationToken(ctx context.Context, client httpDoer, adminToken string) ([]byte, error) {
	raw, err := githubRunnerAPI(ctx, client, http.MethodPost, "/actions/runners/registration-token", adminToken, []byte("{}"), http.StatusCreated)
	if err != nil {
		return nil, err
	}
	defer zero(raw)
	value, err := decodedJSONObject(raw, maximumGitHubAPIBytes)
	token, tokenOK := exactString(value["token"])
	_, expiresOK := exactString(value["expires_at"])
	if err != nil || !tokenOK || len(token) < 20 || len(token) > 2048 || strings.ContainsAny(token, "\r\n\x00") || !expiresOK {
		return nil, fmt.Errorf("runner-registration-token-invalid")
	}
	return []byte(token), nil
}

func deleteGitHubRunner(ctx context.Context, client httpDoer, adminToken, runnerID string) error {
	if !decimal(runnerID) {
		return nil
	}
	_, err := githubRunnerAPI(ctx, client, http.MethodDelete, "/actions/runners/"+url.PathEscape(runnerID), adminToken, nil, http.StatusNoContent, http.StatusNotFound)
	return err
}

func decodePendingDeployments(raw []byte) ([]any, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var items []any
	decodeErr := decoder.Decode(&items)
	var tail any
	tailErr := decoder.Decode(&tail)
	if decodeErr != nil || tailErr != io.EOF || len(items) > 1 {
		return nil, fmt.Errorf("runner-pending-deployment-response-invalid")
	}
	return items, nil
}

func approveRunnerPendingDeployment(ctx context.Context, client httpDoer, adminToken string, binding *runnerTicketBinding) (string, error) {
	raw, err := githubRunnerAPI(ctx, client, http.MethodGet, "/actions/runs/"+url.PathEscape(binding.RunID)+"/pending_deployments", adminToken, nil, http.StatusOK)
	if err != nil {
		return "", err
	}
	initialDigest := digest(raw)
	items, decodeErr := decodePendingDeployments(raw)
	zero(raw)
	if decodeErr != nil || len(items) != 1 {
		return "", fmt.Errorf("runner-pending-deployment-required")
	}
	item, itemOK := items[0].(map[string]any)
	environment, environmentOK := item["environment"].(map[string]any)
	environmentID, idOK := claimInt(environment["id"])
	environmentName, nameOK := exactString(environment["name"])
	canApprove, approveOK := item["current_user_can_approve"].(bool)
	if !itemOK || !environmentOK || !idOK || environmentID < 1 || !nameOK || environmentName != Environment || !approveOK || !canApprove {
		return "", fmt.Errorf("runner-pending-deployment-binding-invalid")
	}
	body, err := canonicalJSON(map[string]any{
		"comment":         "PropertyQuarry governed single-host release ticket " + binding.LaunchTicketDigest,
		"environment_ids": []any{json.Number(strconv.FormatInt(environmentID, 10))},
		"state":           "approved",
	})
	if err != nil {
		return "", err
	}
	defer zero(body)
	response, err := githubRunnerAPI(ctx, client, http.MethodPost, "/actions/runs/"+url.PathEscape(binding.RunID)+"/pending_deployments", adminToken, body, http.StatusOK)
	zero(response)
	if err != nil {
		return "", err
	}
	confirmedRaw, err := githubRunnerAPI(ctx, client, http.MethodGet, "/actions/runs/"+url.PathEscape(binding.RunID)+"/pending_deployments", adminToken, nil, http.StatusOK)
	if err != nil {
		return "", err
	}
	confirmedDigest := digest(confirmedRaw)
	confirmed, confirmErr := decodePendingDeployments(confirmedRaw)
	zero(confirmedRaw)
	if confirmErr != nil || len(confirmed) != 0 {
		return "", fmt.Errorf("runner-pending-deployment-approval-unconfirmed")
	}
	proofRaw, err := canonicalJSON(map[string]any{
		"approved_environment": Environment, "environment_id": json.Number(strconv.FormatInt(environmentID, 10)),
		"initial_sha256": initialDigest, "post_approval_sha256": confirmedDigest, "run_id": binding.RunID,
	})
	if err != nil {
		return "", err
	}
	defer zero(proofRaw)
	return digest(proofRaw), nil
}

type runnerRegistrationGateProof struct {
	ApprovalDigest  string
	AbsenceDigest   string
	QueuedJobDigest string
}

func establishRunnerRegistrationGate(ctx context.Context, client httpDoer, adminToken string, config *Config, binding *runnerTicketBinding, startPresent bool, initialItems []githubRunnerObservation) (*runnerRegistrationGateProof, error) {
	// This condition is intentionally inside the same helper that performs the
	// approval call: no refactor may move the protected-environment effect ahead
	// of the exact stale runner/start rejection.
	if startPresent || requireNoPreexistingRunner(initialItems, binding) != nil {
		return nil, fmt.Errorf("runner-registration-gate-stale-state")
	}
	approvalProof, err := approveRunnerPendingDeployment(ctx, client, adminToken, binding)
	if err != nil {
		return nil, err
	}
	postApprovalItems, postApprovalRaw, err := listGitHubRunners(ctx, client, adminToken)
	if err != nil || requireNoPreexistingRunner(postApprovalItems, binding) != nil {
		zero(postApprovalRaw)
		return nil, fmt.Errorf("runner-supervisor-post-approval-absence-invalid")
	}
	postApprovalAbsenceProof := digest(postApprovalRaw)
	zero(postApprovalRaw)
	queuedBeforeRegistrationProof, err := verifyQueuedRunnerJob(ctx, client, adminToken, config, binding)
	if err != nil {
		return nil, err
	}
	return &runnerRegistrationGateProof{
		ApprovalDigest: approvalProof, AbsenceDigest: postApprovalAbsenceProof,
		QueuedJobDigest: queuedBeforeRegistrationProof,
	}, nil
}

func waitRunnerAbsent(ctx context.Context, client httpDoer, adminToken string, binding *runnerTicketBinding) error {
	deadline := time.NewTimer(60 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		items, raw, err := listGitHubRunners(ctx, client, adminToken)
		zero(raw)
		if err == nil && requireNoPreexistingRunner(items, binding) == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("runner-cleanup-context-ended")
		case <-deadline.C:
			return fmt.Errorf("runner-cleanup-timeout")
		case <-ticker.C:
		}
	}
}

func cleanupRunnerRemoteAndLifecycle(root string, client httpDoer, adminToken string, binding *runnerTicketBinding, runnerID string) error {
	cleanup, stop := context.WithTimeout(context.Background(), 75*time.Second)
	defer stop()
	if decimal(runnerID) {
		if err := deleteGitHubRunner(cleanup, client, adminToken, runnerID); err != nil {
			return fmt.Errorf("runner-cleanup-delete-failed")
		}
	}
	items, raw, err := listGitHubRunners(cleanup, client, adminToken)
	zero(raw)
	if err != nil || deleteMatchingRunners(cleanup, client, adminToken, items, binding) != nil || waitRunnerAbsent(cleanup, client, adminToken, binding) != nil {
		return fmt.Errorf("runner-cleanup-absence-failed")
	}
	fresh, freshRaw, err := listGitHubRunners(cleanup, client, adminToken)
	if err != nil || requireNoPreexistingRunner(fresh, binding) != nil {
		zero(freshRaw)
		return fmt.Errorf("runner-cleanup-proof-failed")
	}
	proof := digest(freshRaw)
	zero(freshRaw)
	if _, err := recoverRunnerLifecycle(root, authorityNow().UTC(), proof); err != nil {
		return fmt.Errorf("runner-cleanup-terminal-convergence-failed")
	}
	return nil
}

func waitForRegisteredRunner(ctx context.Context, client httpDoer, adminToken string, binding *runnerTicketBinding) (*githubRunnerObservation, string, error) {
	deadline := time.NewTimer(runnerRegisterTimeout)
	defer deadline.Stop()
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		items, raw, err := listGitHubRunners(ctx, client, adminToken)
		if err == nil {
			selected, selectErr := exactPendingRunner(items, binding)
			if selectErr != nil {
				zero(raw)
				return nil, "", selectErr
			}
			if selected != nil {
				observationDigest := digest(raw)
				zero(raw)
				return selected, observationDigest, nil
			}
		}
		zero(raw)
		select {
		case <-ctx.Done():
			return nil, "", fmt.Errorf("runner-registration-context-ended")
		case <-deadline.C:
			return nil, "", fmt.Errorf("runner-registration-timeout")
		case <-ticker.C:
		}
	}
}

func verifyQueuedRunnerJob(ctx context.Context, client httpDoer, adminToken string, config *Config, binding *runnerTicketBinding) (string, error) {
	if config == nil || binding == nil || !decimal(binding.RunID) || binding.RunAttempt < 1 || !decimal(binding.JobID) || !runnerLabelPattern.MatchString(binding.RunnerLabel) {
		return "", fmt.Errorf("runner-queued-job-input-invalid")
	}
	runRaw, err := githubRunnerAPI(ctx, client, http.MethodGet, "/actions/runs/"+url.PathEscape(binding.RunID), adminToken, nil, http.StatusOK)
	if err != nil {
		return "", err
	}
	defer zero(runRaw)
	jobRaw, err := githubRunnerAPI(ctx, client, http.MethodGet, "/actions/jobs/"+url.PathEscape(binding.JobID), adminToken, nil, http.StatusOK)
	if err != nil {
		return "", err
	}
	defer zero(jobRaw)
	run, runErr := decodedJSONObject(runRaw, maximumGitHubAPIBytes)
	job, jobErr := decodedJSONObject(jobRaw, maximumGitHubAPIBytes)
	runID, runIDOK := claimInt(run["id"])
	expectedRunID, _ := strconv.ParseInt(binding.RunID, 10, 64)
	runAttempt, attemptOK := claimInt(run["run_attempt"])
	repository, repositoryOK := run["repository"].(map[string]any)
	repositoryID, repositoryIDOK := claimInt(repository["id"])
	expectedRepositoryID, _ := strconv.ParseInt(config.RepositoryID, 10, 64)
	fullName, fullNameOK := exactString(repository["full_name"])
	if runErr != nil || !runIDOK || runID != expectedRunID || !attemptOK || runAttempt != binding.RunAttempt ||
		run["event"] != "workflow_dispatch" || run["status"] != "in_progress" || run["conclusion"] != nil ||
		run["head_sha"] != config.WorkflowSHA || run["head_branch"] != "main" || run["path"] != ".github/workflows/smoke-runtime.yml" ||
		!repositoryOK || !repositoryIDOK || repositoryID != expectedRepositoryID || !fullNameOK || fullName != Repository {
		return "", fmt.Errorf("runner-queued-run-binding-invalid")
	}
	jobID, jobIDOK := claimInt(job["id"])
	expectedJobID, _ := strconv.ParseInt(binding.JobID, 10, 64)
	labelsRaw, labelsOK := job["labels"].([]any)
	labels := map[string]bool{}
	if labelsOK {
		for _, rawLabel := range labelsRaw {
			label, ok := exactString(rawLabel)
			if !ok || labels[label] {
				return "", fmt.Errorf("runner-queued-job-label-invalid")
			}
			labels[label] = true
		}
	}
	expectedRunURL := "https://api.github.com/repos/" + Repository + "/actions/runs/" + binding.RunID
	if jobErr != nil || !jobIDOK || jobID != expectedJobID || job["name"] != ReleaseJob || job["status"] != "queued" || job["conclusion"] != nil ||
		job["run_url"] != expectedRunURL || job["head_sha"] != config.WorkflowSHA || job["runner_id"] != nil || job["runner_name"] != nil ||
		!labelsOK || len(labels) != 2 || !labels["propertyquarry-release-controller-v2"] || !labels[binding.RunnerLabel] ||
		labels["self-hosted"] || labels["Linux"] || labels["X64"] {
		return "", fmt.Errorf("runner-queued-job-binding-invalid")
	}
	proofRaw, err := canonicalJSON(map[string]any{
		"environment": Environment, "job_sha256": digest(jobRaw), "release_job": ReleaseJob,
		"run_sha256": digest(runRaw), "workflow_ref": WorkflowRef, "workflow_sha": config.WorkflowSHA,
	})
	if err != nil {
		return "", err
	}
	defer zero(proofRaw)
	return digest(proofRaw), nil
}

func githubJobTerminal(ctx context.Context, client httpDoer, adminToken, jobID string) (bool, string, error) {
	raw, err := githubRunnerAPI(ctx, client, http.MethodGet, "/actions/jobs/"+url.PathEscape(jobID), adminToken, nil, http.StatusOK)
	if err != nil {
		return false, "", err
	}
	defer zero(raw)
	value, err := decodedJSONObject(raw, maximumGitHubAPIBytes)
	id, idOK := claimInt(value["id"])
	expected, _ := strconv.ParseInt(jobID, 10, 64)
	status, statusOK := exactString(value["status"])
	if err != nil || !idOK || id != expected || !statusOK || (status != "queued" && status != "in_progress" && status != "completed") {
		return false, "", fmt.Errorf("runner-job-observation-invalid")
	}
	if status != "completed" {
		return false, "", nil
	}
	conclusion, conclusionOK := exactString(value["conclusion"])
	if !conclusionOK {
		return false, "", fmt.Errorf("runner-job-conclusion-invalid")
	}
	return true, conclusion, nil
}

func writeRunnerControlLine(output io.Writer, fields ...string) error {
	for _, field := range fields {
		if field == "" || strings.ContainsAny(field, " \r\n\x00") {
			return fmt.Errorf("runner-control-line-invalid")
		}
	}
	raw := []byte(strings.Join(fields, " ") + "\n")
	written, err := output.Write(raw)
	zero(raw)
	if err != nil || written < 1 {
		return fmt.Errorf("runner-control-line-write-failed")
	}
	return nil
}

func superviseRunner(parent context.Context, root string, adminToken []byte, output io.Writer) (returnErr error) {
	ctx, cancel := context.WithTimeout(parent, runnerSupervisorTimeout)
	defer cancel()
	config, key, err := LoadConfig(root)
	if err != nil {
		return err
	}
	defer config.release()
	defer zero(key)
	binding, _, err := loadRunnerClaim(root, config, key.Public().(ed25519.PublicKey), authorityNow().UTC(), false)
	if err != nil || authorityNow().UTC().Unix() > binding.TicketExpiresAt {
		return fmt.Errorf("runner-supervisor-claim-invalid")
	}
	client := productionHTTPClient()
	admin := string(adminToken)
	items, raw, err := listGitHubRunners(ctx, client, admin)
	if err != nil {
		zero(raw)
		return fmt.Errorf("runner-supervisor-runner-list-invalid")
	}
	startPresent, presenceErr := runnerStateExists(root, runnerStartPath)
	if presenceErr != nil {
		zero(raw)
		return presenceErr
	}
	if startPresent {
		if _, _, startErr := loadRunnerStart(root, config, key.Public().(ed25519.PublicKey), authorityNow().UTC(), true); startErr != nil {
			zero(raw)
			return fmt.Errorf("runner-supervisor-stale-start-invalid")
		}
	}
	if startPresent || requireNoPreexistingRunner(items, binding) != nil {
		zero(raw)
		if cleanupRunnerRemoteAndLifecycle(root, client, admin, binding, "") != nil {
			return fmt.Errorf("runner-supervisor-stale-cleanup-failed")
		}
		return fmt.Errorf("runner-supervisor-preexisting-runner")
	}
	zero(raw)
	registrationGate, err := establishRunnerRegistrationGate(ctx, client, admin, config, binding, startPresent, items)
	if err != nil {
		return err
	}
	cleanupRequired := true
	observedRunnerID := ""
	defer func() {
		if !cleanupRequired {
			return
		}
		if cleanupErr := cleanupRunnerRemoteAndLifecycle(root, client, admin, binding, observedRunnerID); cleanupErr != nil {
			returnErr = fmt.Errorf("runner-supervisor-failure-cleanup-failed")
		}
	}()
	registrationToken, err := createRunnerRegistrationToken(ctx, client, admin)
	if err != nil {
		return err
	}
	registrationLine := append(append([]byte(nil), registrationToken...), '\n')
	written, writeErr := output.Write(registrationLine)
	zero(registrationLine)
	zero(registrationToken)
	if writeErr != nil || written < 21 {
		return fmt.Errorf("runner-registration-token-write-failed")
	}
	runner, observationDigest, err := waitForRegisteredRunner(ctx, client, admin, binding)
	if err != nil {
		return err
	}
	observedRunnerID = runner.ID
	sessionObservation, err := waitConfiguredRunnerSession(ctx.Done(), root, binding)
	if err != nil {
		return err
	}
	queuedJobProof, err := verifyQueuedRunnerJob(ctx, client, admin, config, binding)
	if err != nil {
		return err
	}
	startProofRaw, err := canonicalJSON(map[string]any{
		"pending_deployment_approval_sha256": registrationGate.ApprovalDigest, "queued_job_sha256": queuedJobProof,
		"queued_job_before_registration_sha256": registrationGate.QueuedJobDigest, "runner_absence_after_approval_sha256": registrationGate.AbsenceDigest,
		"runner_inventory_sha256": observationDigest, "runner_session_tree_sha256": sessionObservation.TreeDigest,
		"schema": "propertyquarry.release-control.single-host-runner-start-proof.v2", "version": json.Number("2"),
	})
	if err != nil {
		return err
	}
	startProof := digest(startProofRaw)
	zero(startProofRaw)
	if err := authorizeRunnerStart(root, binding, runner.ID, startProof, sessionObservation, authorityNow().UTC()); err != nil {
		return err
	}
	if err := writeRunnerControlLine(output, "START", runner.ID, binding.LaunchTicketDigest); err != nil {
		return err
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		terminal, conclusion, observeErr := githubJobTerminal(ctx, client, admin, binding.JobID)
		if observeErr == nil && terminal {
			if conclusion != "success" {
				return fmt.Errorf("runner-job-not-successful")
			}
			break
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("runner-supervisor-timeout")
		case <-ticker.C:
		}
	}
	if cleanupRunnerRemoteAndLifecycle(root, client, admin, binding, runner.ID) != nil {
		return fmt.Errorf("runner-supervisor-cleanup-failed")
	}
	cleanupRequired = false
	return writeRunnerControlLine(output, "CLEAN", runner.ID, binding.LaunchTicketDigest)
}

func runnerSupervisorCommand(args []string, stdout io.Writer) error {
	if os.Geteuid() != 0 || os.Getegid() != 0 || len(args) != 0 || os.Getenv("PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD") != "8" {
		return fmt.Errorf("runner-supervisor-command-input-invalid")
	}
	token, err := readTokenFD(8)
	if err != nil {
		return err
	}
	defer zero(token)
	if len(token) < 20 || strings.ContainsAny(string(token), "\r\n\x00") {
		return fmt.Errorf("runner-supervisor-token-invalid")
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP)
	defer stop()
	return superviseRunner(ctx, "/", token, stdout)
}
