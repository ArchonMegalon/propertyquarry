package authority

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	githubOIDCIssuer      = "https://token.actions.githubusercontent.com"
	githubDiscoveryURL    = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
	maximumOIDCResponse   = 32768
	maximumJWTBytes       = 16384
	maximumJWKSBytes      = 262144
	maximumGitHubAPIBytes = 1048576
	pinnedRunnerVersion   = "2.335.1"
	githubHTTPTimeout     = 15 * time.Second
	clockSkew             = 30 * time.Second
)

type Identity struct {
	Subject           string
	Repository        string
	RepositoryID      string
	RepositoryOwnerID string
	Ref               string
	CandidateSHA      string
	WorkflowRef       string
	WorkflowSHA       string
	RunID             string
	RunAttempt        int64
	Environment       string
	CheckRunID        string
	TokenID           string
	IssuedAt          int64
	NotBefore         int64
	ExpiresAt         int64
	KeyID             string
	TokenDigest       string
	JWKSdigest        string
	RunnerID          string
	RunnerName        string
	RunnerLabel       string
}

type oidcKeySet struct {
	raw    []byte
	digest string
}

func (set *oidcKeySet) release() {
	if set != nil {
		zero(set.raw)
		*set = oidcKeySet{}
	}
}

type httpDoer interface {
	Do(*http.Request) (*http.Response, error)
}

var productionHTTPClient = func() httpDoer { return hardenedHTTPClient() }

func authenticateGitHubRequest(ctx context.Context, config *Config, requestURL string, actionsToken []byte, now time.Time) (*Identity, error) {
	if config == nil || len(actionsToken) < 1 || len(actionsToken) > maximumJWTBytes || now.IsZero() {
		return nil, fmt.Errorf("github-authentication-input-invalid")
	}
	client := productionHTTPClient()
	jwt, err := exchangeGitHubOIDC(ctx, client, requestURL, actionsToken)
	if err != nil {
		return nil, err
	}
	defer zero(jwt)
	keySet, err := fetchGitHubOIDCKeys(ctx, client)
	if err != nil {
		return nil, err
	}
	defer keySet.release()
	identity, err := verifyGitHubJWT(jwt, keySet, config, now)
	if err != nil {
		return nil, err
	}
	if err := correlateGitHubJob(ctx, client, config, identity); err != nil {
		return nil, err
	}
	return identity, nil
}

func exchangeGitHubOIDC(ctx context.Context, client httpDoer, rawURL string, actionsToken []byte) ([]byte, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "https" || parsed.Host != "vstoken.actions.githubusercontent.com" || parsed.User != nil || parsed.Fragment != "" || parsed.Path == "" {
		return nil, fmt.Errorf("oidc-request-url-invalid")
	}
	query := parsed.Query()
	query.Set("audience", Audience)
	parsed.RawQuery = query.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, parsed.String(), nil)
	if err != nil {
		return nil, fmt.Errorf("oidc-request-invalid")
	}
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Authorization", "Bearer "+string(actionsToken))
	response, err := client.Do(request)
	request.Header.Del("Authorization")
	if err != nil {
		return nil, fmt.Errorf("oidc-exchange-failed")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.Request == nil || response.Request.URL.Scheme != "https" || response.Request.URL.Host != parsed.Host {
		return nil, fmt.Errorf("oidc-exchange-rejected")
	}
	raw, err := boundedRead(response.Body, maximumOIDCResponse)
	if err != nil {
		return nil, fmt.Errorf("oidc-response-invalid")
	}
	value, err := decodedJSONObject(raw, maximumOIDCResponse)
	zero(raw)
	if err != nil || !hasKeys(value, "value") {
		return nil, fmt.Errorf("oidc-response-invalid")
	}
	token, ok := exactString(value["value"])
	if !ok || len(token) > maximumJWTBytes || strings.Count(token, ".") != 2 {
		return nil, fmt.Errorf("oidc-token-invalid")
	}
	return []byte(token), nil
}

func fetchGitHubOIDCKeys(ctx context.Context, client httpDoer) (*oidcKeySet, error) {
	discoveryRaw, err := fetchJSON(ctx, client, githubDiscoveryURL, maximumOIDCResponse, "token.actions.githubusercontent.com")
	if err != nil {
		return nil, err
	}
	discovery, err := decodedJSONObject(discoveryRaw, maximumOIDCResponse)
	zero(discoveryRaw)
	if err != nil {
		return nil, fmt.Errorf("oidc-discovery-invalid")
	}
	issuer, issuerOK := exactString(discovery["issuer"])
	jwksURL, jwksOK := exactString(discovery["jwks_uri"])
	parsed, parseErr := url.Parse(jwksURL)
	if !issuerOK || issuer != githubOIDCIssuer || !jwksOK || parseErr != nil || parsed.Scheme != "https" || parsed.Host != "token.actions.githubusercontent.com" || parsed.User != nil || parsed.Fragment != "" {
		return nil, fmt.Errorf("oidc-discovery-binding-invalid")
	}
	raw, err := fetchJSON(ctx, client, parsed.String(), maximumJWKSBytes, "token.actions.githubusercontent.com")
	if err != nil {
		return nil, err
	}
	if _, err := decodedJSONObject(raw, maximumJWKSBytes); err != nil {
		zero(raw)
		return nil, fmt.Errorf("oidc-jwks-invalid")
	}
	return &oidcKeySet{raw: raw, digest: digest(raw)}, nil
}

func verifyGitHubJWT(token []byte, keySet *oidcKeySet, config *Config, now time.Time) (*Identity, error) {
	parts := bytes.Split(token, []byte{'.'})
	if len(parts) != 3 || len(token) > maximumJWTBytes || keySet == nil || config == nil {
		return nil, fmt.Errorf("oidc-jwt-framing-invalid")
	}
	headerRaw, err := decodeBase64URL(parts[0], 4096)
	if err != nil {
		return nil, err
	}
	defer zero(headerRaw)
	claimsRaw, err := decodeBase64URL(parts[1], maximumJWTBytes)
	if err != nil {
		return nil, err
	}
	defer zero(claimsRaw)
	signature, err := decodeBase64URL(parts[2], 4096)
	if err != nil {
		return nil, err
	}
	defer zero(signature)
	header, err := decodedJSONObject(headerRaw, 4096)
	if err != nil || header["alg"] != "RS256" || header["typ"] != "JWT" {
		return nil, fmt.Errorf("oidc-jwt-header-invalid")
	}
	if _, present := header["crit"]; present {
		return nil, fmt.Errorf("oidc-jwt-critical-header-unsupported")
	}
	if _, present := header["b64"]; present {
		return nil, fmt.Errorf("oidc-jwt-critical-header-unsupported")
	}
	keyID, ok := exactString(header["kid"])
	if !ok || len(keyID) < 1 || len(keyID) > 512 || strings.IndexFunc(keyID, func(character rune) bool { return character < 0x21 || character > 0x7e }) >= 0 {
		return nil, fmt.Errorf("oidc-jwt-key-invalid")
	}
	public, err := selectRSAKey(keySet.raw, keyID)
	if err != nil {
		return nil, err
	}
	signingInput := append(append([]byte(nil), parts[0]...), '.')
	signingInput = append(signingInput, parts[1]...)
	hashed := sha256.Sum256(signingInput)
	zero(signingInput)
	if err := rsa.VerifyPKCS1v15(public, crypto.SHA256, hashed[:], signature); err != nil {
		return nil, fmt.Errorf("oidc-jwt-signature-invalid")
	}
	claims, err := decodedJSONObject(claimsRaw, maximumJWTBytes)
	if err != nil {
		return nil, fmt.Errorf("oidc-jwt-claims-invalid")
	}
	get := func(key string) (string, bool) { return exactString(claims[key]) }
	issuer, _ := get("iss")
	audience, _ := get("aud")
	subject, _ := get("sub")
	repository, _ := get("repository")
	repositoryID, _ := get("repository_id")
	ownerID, _ := get("repository_owner_id")
	ref, _ := get("ref")
	candidate, _ := get("sha")
	workflowRef, _ := get("workflow_ref")
	workflowSHA, _ := get("workflow_sha")
	runID, _ := get("run_id")
	runAttemptText, _ := get("run_attempt")
	environment, _ := get("environment")
	checkRunID, _ := get("check_run_id")
	tokenID, _ := get("jti")
	iat, iatOK := claimInt(claims["iat"])
	nbf, nbfOK := claimInt(claims["nbf"])
	exp, expOK := claimInt(claims["exp"])
	runAttempt, runAttemptErr := strconv.ParseInt(runAttemptText, 10, 64)
	expectedSubject := ImmutableOIDCSubjectPrefix + ":environment:" + Environment
	if issuer != githubOIDCIssuer || audience != Audience || subject != expectedSubject || repository != Repository || repositoryID != config.RepositoryID ||
		ownerID != config.RepositoryOwnerID || ref != "refs/heads/main" || candidate != config.WorkflowSHA || workflowRef != WorkflowRef || workflowSHA != config.WorkflowSHA ||
		!decimal(runID) || runAttemptErr != nil || runAttempt < 1 || runAttempt > 1<<31-1 || environment != Environment || !decimal(checkRunID) ||
		!idPattern.MatchString(tokenID) || !iatOK || !nbfOK || !expOK || exp <= nbf || exp-iat > 3600 ||
		now.Unix() < nbf-int64(clockSkew/time.Second) || now.Unix() > exp+int64(clockSkew/time.Second) || iat > now.Unix()+int64(clockSkew/time.Second) {
		return nil, fmt.Errorf("oidc-jwt-binding-invalid")
	}
	return &Identity{Subject: subject, Repository: repository, RepositoryID: repositoryID, RepositoryOwnerID: ownerID,
		Ref: ref, CandidateSHA: candidate, WorkflowRef: workflowRef, WorkflowSHA: workflowSHA, RunID: runID,
		RunAttempt: runAttempt, Environment: environment, CheckRunID: checkRunID, TokenID: tokenID,
		IssuedAt: iat, NotBefore: nbf, ExpiresAt: exp, KeyID: keyID, TokenDigest: digest(token), JWKSdigest: keySet.digest}, nil
}

func correlateGitHubJob(ctx context.Context, client httpDoer, config *Config, identity *Identity) error {
	credential, err := secureRead("/", config.GitHubAPICredentialPath, 0o400, 0, 0, 4096)
	if err != nil {
		return fmt.Errorf("github-api-credential-unavailable")
	}
	defer zero(credential)
	token := strings.TrimSuffix(string(credential), "\n")
	if token == "" || strings.ContainsAny(token, "\r\n\x00") {
		return fmt.Errorf("github-api-credential-invalid")
	}
	return correlateGitHubJobWithToken(ctx, client, config, identity, token)
}

func correlateGitHubJobWithToken(ctx context.Context, client httpDoer, config *Config, identity *Identity, token string) error {
	if client == nil || config == nil || identity == nil || token == "" || strings.ContainsAny(token, "\r\n\x00") {
		return fmt.Errorf("github-job-correlation-input-invalid")
	}
	apiRoot := "https://api.github.com/repos/" + Repository
	runRaw, err := fetchGitHubAPI(ctx, client, apiRoot+"/actions/runs/"+identity.RunID, token)
	if err != nil {
		return err
	}
	defer zero(runRaw)
	jobRaw, err := fetchGitHubAPI(ctx, client, apiRoot+"/actions/jobs/"+identity.CheckRunID, token)
	if err != nil {
		return err
	}
	defer zero(jobRaw)
	if err := validateRunCorrelation(runRaw, config, identity); err != nil {
		return err
	}
	runnerID, runnerName, err := validateJobCorrelation(jobRaw, config, identity)
	if err != nil {
		return err
	}
	runnerRaw, err := fetchGitHubAPI(ctx, client, apiRoot+"/actions/runners/"+runnerID, token)
	if err != nil {
		return err
	}
	defer zero(runnerRaw)
	runnerLabel, err := validateRunnerCorrelation(runnerRaw, config, runnerID, runnerName)
	if err != nil {
		return err
	}
	identity.RunnerID, identity.RunnerName, identity.RunnerLabel = runnerID, runnerName, runnerLabel
	return nil
}

func validateRunCorrelation(raw []byte, config *Config, identity *Identity) error {
	value, err := decodedJSONObject(raw, maximumGitHubAPIBytes)
	if err != nil {
		return fmt.Errorf("github-run-response-invalid")
	}
	id, idOK := claimInt(value["id"])
	runID, _ := strconv.ParseInt(identity.RunID, 10, 64)
	attempt, attemptOK := claimInt(value["run_attempt"])
	repository, repositoryOK := value["repository"].(map[string]any)
	fullName, _ := exactString(repository["full_name"])
	repositoryID, repositoryIDOK := claimInt(repository["id"])
	expectedRepositoryID, _ := strconv.ParseInt(config.RepositoryID, 10, 64)
	if !idOK || id != runID || !attemptOK || attempt != identity.RunAttempt || value["event"] != "workflow_dispatch" || value["status"] != "in_progress" ||
		value["conclusion"] != nil || value["head_sha"] != config.WorkflowSHA || value["head_branch"] != "main" || value["path"] != ".github/workflows/smoke-runtime.yml" ||
		!repositoryOK || fullName != Repository || !repositoryIDOK || repositoryID != expectedRepositoryID {
		return fmt.Errorf("github-run-correlation-invalid")
	}
	return nil
}

func validateJobCorrelation(raw []byte, config *Config, identity *Identity) (string, string, error) {
	value, err := decodedJSONObject(raw, maximumGitHubAPIBytes)
	if err != nil {
		return "", "", fmt.Errorf("github-job-response-invalid")
	}
	id, idOK := claimInt(value["id"])
	expectedID, _ := strconv.ParseInt(identity.CheckRunID, 10, 64)
	runnerID, runnerIDOK := claimInt(value["runner_id"])
	runnerName, runnerOK := exactString(value["runner_name"])
	labelsValue, labelsOK := value["labels"].([]any)
	labels := map[string]bool{}
	if labelsOK {
		for _, rawLabel := range labelsValue {
			label, ok := exactString(rawLabel)
			if !ok || labels[label] {
				return "", "", fmt.Errorf("github-job-label-invalid")
			}
			labels[label] = true
		}
	}
	_, runnerNameOK := runnerLabelForName(runnerName, config.EphemeralRunnerLabelPrefix)
	runnerLabel, _ := runnerLabelForName(runnerName, config.EphemeralRunnerLabelPrefix)
	expectedRunURL := "https://api.github.com/repos/" + Repository + "/actions/runs/" + identity.RunID
	if !idOK || id != expectedID || value["name"] != ReleaseJob || value["status"] != "in_progress" || value["conclusion"] != nil ||
		value["run_url"] != expectedRunURL || value["head_sha"] != config.WorkflowSHA || !runnerOK || !labelsOK ||
		!labels["propertyquarry-release-controller-v2"] || !labels[runnerLabel] || len(labels) != 2 || labels["self-hosted"] || labels["Linux"] || labels["X64"] || !runnerIDOK || runnerID < 1 || !runnerNameOK {
		return "", "", fmt.Errorf("github-job-correlation-invalid")
	}
	return strconv.FormatInt(runnerID, 10), runnerName, nil
}

func runnerLabelForName(name, labelPrefix string) (string, bool) {
	const runnerPrefix = "pq-release-"
	if labelPrefix != "pqrelease-" || len(name) != len(runnerPrefix)+32 || !strings.HasPrefix(name, runnerPrefix) {
		return "", false
	}
	label := labelPrefix + name[len(runnerPrefix):]
	return label, regexpLabel(label)
}

func validateRunnerCorrelation(raw []byte, config *Config, expectedRunnerID, expectedRunnerName string) (string, error) {
	value, err := decodedJSONObject(raw, maximumGitHubAPIBytes)
	if err != nil {
		return "", fmt.Errorf("github-runner-response-invalid")
	}
	runnerID, runnerIDOK := claimInt(value["id"])
	expectedID, expectedIDErr := strconv.ParseInt(expectedRunnerID, 10, 64)
	runnerName, runnerNameOK := exactString(value["name"])
	busy, busyOK := value["busy"].(bool)
	ephemeral, ephemeralOK := value["ephemeral"].(bool)
	runnerVersion, versionOK := exactString(value["version"])
	runnerLabel, runnerNameBound := runnerLabelForName(runnerName, config.EphemeralRunnerLabelPrefix)
	labelsValue, labelsOK := value["labels"].([]any)
	labels := make(map[string]string, 2)
	if labelsOK {
		for _, rawLabel := range labelsValue {
			label, ok := rawLabel.(map[string]any)
			if !ok {
				return "", fmt.Errorf("github-runner-label-invalid")
			}
			name, nameOK := exactString(label["name"])
			kind, kindOK := exactString(label["type"])
			if !nameOK || !kindOK || (kind != "read-only" && kind != "custom") || labels[name] != "" {
				return "", fmt.Errorf("github-runner-label-invalid")
			}
			labels[name] = kind
		}
	}
	if !runnerIDOK || expectedIDErr != nil || runnerID != expectedID || !runnerNameOK || runnerName != expectedRunnerName || !runnerNameBound ||
		value["os"] != "linux" || value["status"] != "online" || !busyOK || !busy || !ephemeralOK || !ephemeral || !versionOK || runnerVersion != pinnedRunnerVersion || !labelsOK || len(labels) != 2 ||
		labels["self-hosted"] != "" || labels["Linux"] != "" || labels["X64"] != "" ||
		labels["propertyquarry-release-controller-v2"] != "custom" || labels[runnerLabel] != "custom" {
		return "", fmt.Errorf("github-runner-correlation-invalid")
	}
	return runnerLabel, nil
}

func fetchGitHubAPI(ctx context.Context, client httpDoer, rawURL, token string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, fmt.Errorf("github-api-request-invalid")
	}
	request.Header.Set("Accept", "application/vnd.github+json")
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	response, err := client.Do(request)
	request.Header.Del("Authorization")
	if err != nil {
		return nil, fmt.Errorf("github-api-request-failed")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || !githubResponseURLIsExact(response, rawURL) {
		return nil, fmt.Errorf("github-api-response-rejected")
	}
	return boundedRead(response.Body, maximumGitHubAPIBytes)
}

func fetchJSON(ctx context.Context, client httpDoer, rawURL string, maximum int, host string) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, fmt.Errorf("https-request-invalid")
	}
	request.Header.Set("Accept", "application/json")
	response, err := client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("https-request-failed")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.Request == nil || response.Request.URL.Scheme != "https" || response.Request.URL.Host != host {
		return nil, fmt.Errorf("https-response-rejected")
	}
	return boundedRead(response.Body, maximum)
}

func hardenedHTTPClient() *http.Client {
	dialer := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: -1}
	transport := &http.Transport{Proxy: nil, DisableKeepAlives: true, ForceAttemptHTTP2: false, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
	transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
		host, port, err := net.SplitHostPort(address)
		if err != nil {
			return nil, err
		}
		addresses, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
		if err != nil || len(addresses) == 0 {
			return nil, fmt.Errorf("dns-resolution-failed")
		}
		for _, address := range addresses {
			if !publicAddress(address) {
				return nil, fmt.Errorf("nonpublic-address-rejected")
			}
		}
		return dialer.DialContext(ctx, network, net.JoinHostPort(addresses[0].String(), port))
	}
	return &http.Client{Transport: transport, Timeout: githubHTTPTimeout, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
}

func publicAddress(address netip.Addr) bool {
	if !address.IsValid() || !address.IsGlobalUnicast() || address.IsLoopback() || address.IsPrivate() ||
		address.IsLinkLocalUnicast() || address.IsLinkLocalMulticast() || address.IsMulticast() || address.IsUnspecified() {
		return false
	}
	if address.Is4() {
		value := address.As4()
		if value[0] == 0 || value[0] == 127 || value[0] >= 224 ||
			(value[0] == 100 && value[1]&0xc0 == 64) ||
			(value[0] == 169 && value[1] == 254) ||
			(value[0] == 192 && value[1] == 0 && value[2] == 0) ||
			(value[0] == 192 && value[1] == 0 && value[2] == 2) ||
			(value[0] == 192 && value[1] == 88 && value[2] == 99) ||
			(value[0] == 198 && value[1]&0xfe == 18) ||
			(value[0] == 198 && value[1] == 51 && value[2] == 100) ||
			(value[0] == 203 && value[1] == 0 && value[2] == 113) {
			return false
		}
	} else {
		for _, prefix := range []netip.Prefix{
			netip.MustParsePrefix("2001:db8::/32"),
			netip.MustParsePrefix("2001:10::/28"),
			netip.MustParsePrefix("2001:20::/28"),
		} {
			if prefix.Contains(address) {
				return false
			}
		}
	}
	return true
}

func selectRSAKey(raw []byte, selectedID string) (*rsa.PublicKey, error) {
	value, err := decodedJSONObject(raw, maximumJWKSBytes)
	if err != nil {
		return nil, fmt.Errorf("jwks-invalid")
	}
	items, ok := value["keys"].([]any)
	if !ok || len(items) < 1 || len(items) > 128 {
		return nil, fmt.Errorf("jwks-keys-invalid")
	}
	var selected *rsa.PublicKey
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("jwk-invalid")
		}
		kid, _ := exactString(item["kid"])
		if kid != selectedID {
			continue
		}
		if selected != nil || item["kty"] != "RSA" || item["alg"] != "RS256" || item["use"] != "sig" {
			return nil, fmt.Errorf("jwk-selection-invalid")
		}
		nText, nOK := exactString(item["n"])
		eText, eOK := exactString(item["e"])
		if !nOK || !eOK {
			return nil, fmt.Errorf("jwk-material-invalid")
		}
		nRaw, err := decodeBase64URL([]byte(nText), 1024)
		if err != nil {
			return nil, err
		}
		defer zero(nRaw)
		eRaw, err := decodeBase64URL([]byte(eText), 8)
		if err != nil {
			return nil, err
		}
		defer zero(eRaw)
		exponent := 0
		for _, value := range eRaw {
			exponent = exponent<<8 | int(value)
		}
		modulus := new(big.Int).SetBytes(nRaw)
		if modulus.BitLen() < 2048 || exponent < 3 || exponent%2 == 0 {
			return nil, fmt.Errorf("jwk-strength-invalid")
		}
		selected = &rsa.PublicKey{N: modulus, E: exponent}
	}
	if selected == nil {
		return nil, fmt.Errorf("jwk-not-found")
	}
	return selected, nil
}

func decodeBase64URL(raw []byte, maximum int) ([]byte, error) {
	if len(raw) == 0 || len(raw) > maximum*2 || bytes.Contains(raw, []byte{'='}) {
		return nil, fmt.Errorf("base64url-invalid")
	}
	decoded, err := base64.RawURLEncoding.DecodeString(string(raw))
	if err != nil || len(decoded) == 0 || len(decoded) > maximum {
		zero(decoded)
		return nil, fmt.Errorf("base64url-invalid")
	}
	return decoded, nil
}

func claimInt(value any) (int64, bool) {
	switch item := value.(type) {
	case json.Number:
		parsed, err := item.Int64()
		return parsed, err == nil
	case string:
		parsed, err := strconv.ParseInt(item, 10, 64)
		return parsed, err == nil && strconv.FormatInt(parsed, 10) == item
	default:
		return 0, false
	}
}

func boundedRead(reader io.Reader, maximum int) ([]byte, error) {
	raw, err := io.ReadAll(io.LimitReader(reader, int64(maximum)+1))
	if err != nil || len(raw) == 0 || len(raw) > maximum {
		zero(raw)
		return nil, fmt.Errorf("bounded-read-invalid")
	}
	return raw, nil
}

func regexpLabel(value string) bool {
	if len(value) != len("pqrelease-")+32 || !strings.HasPrefix(value, "pqrelease-") {
		return false
	}
	for _, character := range value[len("pqrelease-"):] {
		if !strings.ContainsRune("0123456789abcdef", character) {
			return false
		}
	}
	return true
}
