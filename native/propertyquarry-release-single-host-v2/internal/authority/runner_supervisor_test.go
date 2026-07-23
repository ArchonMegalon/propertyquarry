//go:build linux && amd64

package authority

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func runnerSupervisorResponse(request *http.Request, status int, body []byte) *http.Response {
	return &http.Response{StatusCode: status, Body: io.NopCloser(bytes.NewReader(body)), Request: request}
}

func TestPendingDeploymentApprovalRequiresExactEnvironmentAndConfirmedTransition(t *testing.T) {
	binding := &runnerTicketBinding{RunID: "123", LaunchTicketDigest: "sha256:" + strings.Repeat("8", 64)}
	getCount := 0
	postCount := 0
	client := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		if request.Header.Get("Authorization") != "Bearer fixture-admin-token-long-enough" || request.URL.Path != "/repos/"+Repository+"/actions/runs/123/pending_deployments" {
			t.Fatalf("unexpected pending deployment request: %s %s", request.Method, request.URL)
		}
		switch request.Method {
		case http.MethodGet:
			getCount++
			if getCount == 1 {
				raw, _ := canonicalJSON([]any{map[string]any{
					"current_user_can_approve": true,
					"environment":              map[string]any{"id": json.Number("42"), "name": Environment},
				}})
				return runnerSupervisorResponse(request, http.StatusOK, raw), nil
			}
			return runnerSupervisorResponse(request, http.StatusOK, []byte("[]")), nil
		case http.MethodPost:
			postCount++
			body, err := io.ReadAll(request.Body)
			if err != nil {
				t.Fatal(err)
			}
			value, err := decodedJSONObject(body, maximumGitHubAPIBytes)
			if err != nil || value["state"] != "approved" || value["comment"] != "PropertyQuarry governed single-host release ticket "+binding.LaunchTicketDigest {
				t.Fatalf("approval body not exact: %s", body)
			}
			ids, ok := value["environment_ids"].([]any)
			if !ok || len(ids) != 1 || ids[0] != json.Number("42") {
				t.Fatalf("approval environment not exact: %#v", value)
			}
			return runnerSupervisorResponse(request, http.StatusOK, []byte(`[{"state":"approved"}]`)), nil
		default:
			t.Fatalf("unexpected method %s", request.Method)
			return nil, nil
		}
	})
	proof, err := approveRunnerPendingDeployment(context.Background(), client, "fixture-admin-token-long-enough", binding)
	if err != nil || !digestPattern.MatchString(proof) || getCount != 2 || postCount != 1 {
		t.Fatalf("exact approval transition rejected: proof=%q gets=%d posts=%d err=%v", proof, getCount, postCount, err)
	}

	zeroPending := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		return runnerSupervisorResponse(request, http.StatusOK, []byte("[]")), nil
	})
	if _, err := approveRunnerPendingDeployment(context.Background(), zeroPending, "fixture-admin-token-long-enough", binding); err == nil {
		t.Fatal("missing protected-environment approval was accepted")
	}

	wrongEnvironment := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		return runnerSupervisorResponse(request, http.StatusOK, []byte(`[{"current_user_can_approve":true,"environment":{"id":42,"name":"production"}}]`)), nil
	})
	if _, err := approveRunnerPendingDeployment(context.Background(), wrongEnvironment, "fixture-admin-token-long-enough", binding); err == nil {
		t.Fatal("approval for a different environment was accepted")
	}
}

func queuedRunAndJob(config *Config, binding *runnerTicketBinding) (map[string]any, map[string]any) {
	run := map[string]any{
		"id": json.Number(binding.RunID), "run_attempt": json.Number("1"), "event": "workflow_dispatch", "status": "in_progress", "conclusion": nil,
		"head_sha": config.WorkflowSHA, "head_branch": "main", "path": ".github/workflows/smoke-runtime.yml",
		"repository": map[string]any{"full_name": Repository, "id": json.Number(config.RepositoryID)},
	}
	job := map[string]any{
		"id": json.Number(binding.JobID), "name": ReleaseJob, "status": "queued", "conclusion": nil,
		"run_url": "https://api.github.com/repos/" + Repository + "/actions/runs/" + binding.RunID, "head_sha": config.WorkflowSHA,
		"runner_id": nil, "runner_name": nil,
		"labels": []any{"propertyquarry-release-controller-v2", binding.RunnerLabel},
	}
	return run, job
}

func queuedJobClient(t *testing.T, run, job map[string]any) httpDoer {
	t.Helper()
	return httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		var value map[string]any
		switch request.URL.Path {
		case "/repos/" + Repository + "/actions/runs/123":
			value = run
		case "/repos/" + Repository + "/actions/jobs/456":
			value = job
		default:
			t.Fatalf("unexpected queued-job endpoint: %s", request.URL.Path)
		}
		raw, err := canonicalJSON(value)
		if err != nil {
			t.Fatal(err)
		}
		return runnerSupervisorResponse(request, http.StatusOK, raw), nil
	})
}

func TestQueuedJobProofBindsRunWorkflowAttemptLabelsAndUnassignedState(t *testing.T) {
	fixture := newAuthorityFixture(t, false)
	defer fixture.close()
	binding := &runnerTicketBinding{RunID: "123", RunAttempt: 1, JobID: "456", RunnerLabel: "pqrelease-" + strings.Repeat("a", 32)}
	run, job := queuedRunAndJob(fixture.config, binding)
	proof, err := verifyQueuedRunnerJob(context.Background(), queuedJobClient(t, run, job), "fixture-admin-token-long-enough", fixture.config, binding)
	if err != nil || !digestPattern.MatchString(proof) {
		t.Fatalf("exact queued job proof rejected: proof=%q err=%v", proof, err)
	}

	tests := []struct {
		name   string
		mutate func(map[string]any, map[string]any)
	}{
		{"wrong-attempt", func(run, _ map[string]any) { run["run_attempt"] = json.Number("2") }},
		{"wrong-workflow-sha", func(run, _ map[string]any) { run["head_sha"] = strings.Repeat("f", 40) }},
		{"wrong-workflow-path", func(run, _ map[string]any) { run["path"] = ".github/workflows/other.yml" }},
		{"wrong-repository", func(run, _ map[string]any) {
			run["repository"] = map[string]any{"full_name": "elsewhere/repository", "id": json.Number(fixture.config.RepositoryID)}
		}},
		{"assigned-runner", func(_ map[string]any, job map[string]any) { job["runner_id"] = json.Number("789") }},
		{"running-job", func(_ map[string]any, job map[string]any) { job["status"] = "in_progress" }},
		{"wrong-run", func(_ map[string]any, job map[string]any) {
			job["run_url"] = "https://api.github.com/repos/" + Repository + "/actions/runs/124"
		}},
		{"default-label", func(_ map[string]any, job map[string]any) {
			job["labels"] = []any{"self-hosted", "propertyquarry-release-controller-v2", binding.RunnerLabel}
		}},
		{"missing-nonce-label", func(_ map[string]any, job map[string]any) {
			job["labels"] = []any{"propertyquarry-release-controller-v2"}
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			testRun, testJob := queuedRunAndJob(fixture.config, binding)
			test.mutate(testRun, testJob)
			if _, err := verifyQueuedRunnerJob(context.Background(), queuedJobClient(t, testRun, testJob), "fixture-admin-token-long-enough", fixture.config, binding); err == nil {
				t.Fatal("adversarial queued-job state was accepted")
			}
		})
	}
}

func TestRegisteredRunnerIdentityRejectsDefaultLabelsAndDuplicates(t *testing.T) {
	binding := &runnerTicketBinding{RunnerNonce: strings.Repeat("a", 32), RunnerLabel: "pqrelease-" + strings.Repeat("a", 32)}
	exact := githubRunnerObservation{
		ID: "789", Name: "pq-release-" + strings.Repeat("a", 32), Status: "offline", Busy: false, Ephemeral: true, Version: pinnedRunnerVersion,
		Labels: map[string]string{"propertyquarry-release-controller-v2": "custom", binding.RunnerLabel: "custom"},
	}
	selected, err := exactPendingRunner([]githubRunnerObservation{exact}, binding)
	if err != nil || selected == nil || selected.ID != "789" {
		t.Fatalf("exact pending runner rejected: %#v %v", selected, err)
	}
	withDefault := exact
	withDefault.Labels = map[string]string{"self-hosted": "read-only", "propertyquarry-release-controller-v2": "custom", binding.RunnerLabel: "custom"}
	if _, err := exactPendingRunner([]githubRunnerObservation{withDefault}, binding); err == nil {
		t.Fatal("runner with default labels accepted")
	}
	duplicate := exact
	duplicate.ID = "790"
	if _, err := exactPendingRunner([]githubRunnerObservation{exact, duplicate}, binding); err == nil {
		t.Fatal("duplicate matching runners accepted")
	}
}

func TestRegistrationGateNeverApprovesWhileStaleRunnerOrStartExists(t *testing.T) {
	binding := &runnerTicketBinding{RunID: "123", RunnerNonce: strings.Repeat("a", 32), RunnerLabel: "pqrelease-" + strings.Repeat("a", 32)}
	called := false
	client := httpDoerFunc(func(request *http.Request) (*http.Response, error) {
		called = true
		t.Fatalf("GitHub API reached before stale-state rejection: %s %s", request.Method, request.URL)
		return nil, nil
	})
	stale := githubRunnerObservation{ID: "789", Name: "pq-release-" + strings.Repeat("a", 32), Labels: map[string]string{binding.RunnerLabel: "custom"}}
	if _, err := establishRunnerRegistrationGate(context.Background(), client, "fixture-admin-token-long-enough", nil, binding, false, []githubRunnerObservation{stale}); err == nil {
		t.Fatal("stale runner reached the registration approval gate")
	}
	if called {
		t.Fatal("environment approval API was called for a stale runner")
	}
	if _, err := establishRunnerRegistrationGate(context.Background(), client, "fixture-admin-token-long-enough", nil, binding, true, nil); err == nil {
		t.Fatal("stale start authorization reached the registration approval gate")
	}
	if called {
		t.Fatal("environment approval API was called for a stale start")
	}
}

func TestRunDispatchesRootStartVerificationWithExactArity(t *testing.T) {
	previousIdentity := runnerCommandIdentity
	previousVerifier := runnerStartVerification
	runnerCommandIdentity = func() (int, int) { return 0, 0 }
	called := false
	runnerStartVerification = func(root, label, ticket, runnerID string, now time.Time) (map[string]any, error) {
		called = true
		if root != "/" || label != "pqrelease-"+strings.Repeat("a", 32) || ticket != "sha256:"+strings.Repeat("8", 64) || runnerID != "789" || now.IsZero() {
			return nil, os.ErrInvalid
		}
		return map[string]any{
			"execution_expires_at_epoch": json.Number("1900000300"), "launch_ticket_sha256": ticket,
			"runner_id": runnerID, "runner_label": label, "schema": runnerStartResultSchema,
			"session_device": json.Number("123"), "session_inode": json.Number("456"),
			"session_tree_sha256": "sha256:" + strings.Repeat("9", 64), "version": json.Number("2"),
		}, nil
	}
	t.Cleanup(func() {
		runnerCommandIdentity = previousIdentity
		runnerStartVerification = previousVerifier
	})
	var stdout, stderr bytes.Buffer
	code := Run([]string{"runner-start-verify", "pqrelease-" + strings.Repeat("a", 32), "sha256:" + strings.Repeat("8", 64), "789"}, os.Stdin, &stdout, &stderr)
	if code != 0 || !called || stderr.Len() != 0 {
		t.Fatalf("start verification command not dispatched: code=%d called=%t stderr=%q", code, called, stderr.String())
	}
	value, err := decodedJSONObject(bytes.TrimSuffix(stdout.Bytes(), []byte{'\n'}), 65536)
	if err != nil || value["schema"] != runnerStartResultSchema || value["session_tree_sha256"] != "sha256:"+strings.Repeat("9", 64) {
		t.Fatalf("start verification result not canonical: %#v %v", value, err)
	}
	stdout.Reset()
	stderr.Reset()
	if code := Run([]string{"runner-start-verify", "pqrelease-" + strings.Repeat("a", 32)}, os.Stdin, &stdout, &stderr); code != ExitFailure {
		t.Fatal("wrong start verification arity accepted")
	}
}

func TestSignedSessionObservationRejectsContentAndMarkerRewrite(t *testing.T) {
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	base := rooted(root, runnerSessionRoot)
	if err := os.MkdirAll(base, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(base, 0o700); err != nil {
		t.Fatal(err)
	}
	label := "pqrelease-" + strings.Repeat("a", 32)
	session := filepath.Join(base, "session-"+strings.Repeat("a", 32)+".Ab12z9")
	if err := os.Mkdir(session, 0o700); err != nil {
		t.Fatal(err)
	}
	write := func(relative, value string, mode os.FileMode) {
		t.Helper()
		path := filepath.Join(session, relative)
		if err := os.WriteFile(path, []byte(value), mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, mode); err != nil {
			t.Fatal(err)
		}
	}
	write(".configuration-complete", "configured-without-host-authority\n", 0o600)
	write("configure-exit-status", "0\n", 0o600)
	write(".registration-token.sha256", "sha256:"+strings.Repeat("1", 64)+"\n", 0o600)
	write(".session-content.sha256", "sha256:"+strings.Repeat("2", 64)+"\n", 0o600)
	write("configured-payload", "original\n", 0o600)
	observed, err := observeConfiguredRunnerSession(root, label)
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyRunnerSessionObservation(root, label, observed.Device, observed.Inode, observed.TreeDigest); err != nil {
		t.Fatalf("exact session observation rejected: %v", err)
	}
	write("configured-payload", "replacement\n", 0o600)
	write(".session-content.sha256", "sha256:"+strings.Repeat("f", 64)+"\n", 0o600)
	if err := verifyRunnerSessionObservation(root, label, observed.Device, observed.Inode, observed.TreeDigest); err == nil {
		t.Fatal("content plus mutable marker rewrite matched the signed session observation")
	}
}
