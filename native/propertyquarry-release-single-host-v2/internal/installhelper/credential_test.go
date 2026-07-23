package installhelper

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func testCredentialRoot(t *testing.T) (string, uint32, uint32) {
	t.Helper()
	root := t.TempDir()
	if err := os.Chmod(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "etc"), 0o755); err != nil {
		t.Fatal(err)
	}
	return root, uint32(os.Geteuid()), uint32(os.Getegid())
}

func testGitHubToken(value byte) []byte {
	return []byte("github_pat_" + strings.Repeat(string(value), 48))
}

func testEncryptedCredential(value byte) []byte {
	return append(append(bytes.Repeat([]byte{value}, 79), '\n'), append(bytes.Repeat([]byte{value}, 17), '\n')...)
}

func decryptsTo(token []byte) func([]byte) ([]byte, error) {
	return func(_ []byte) ([]byte, error) {
		return append([]byte(nil), token...), nil
	}
}

func TestPublishEncryptedGitHubCredentialIsNoReplaceAndIdempotent(t *testing.T) {
	root, uid, gid := testCredentialRoot(t)
	token := testGitHubToken('a')
	encrypted := testEncryptedCredential('A')
	result, err := publishEncryptedGitHubCredential(root, uid, gid, token, encrypted, decryptsTo(token))
	if err != nil {
		t.Fatal(err)
	}
	if result.disposition != "provisioned" || !result.installPerformed || result.recoveryPerformed {
		t.Fatalf("unexpected result: %#v", result)
	}
	target := filepath.Join(root, "etc/propertyquarry-release-single-host-v2/github-api-token.cred")
	info, err := os.Lstat(target)
	if err != nil || info.Mode().Perm() != 0o400 {
		t.Fatalf("credential metadata invalid: %v %#o", err, info.Mode().Perm())
	}
	raw, err := os.ReadFile(target)
	if err != nil || !bytes.Equal(raw, encrypted) {
		t.Fatalf("credential content invalid: %v", err)
	}
	secondCiphertext := testEncryptedCredential('B')
	second, err := publishEncryptedGitHubCredential(root, uid, gid, token, secondCiphertext, decryptsTo(token))
	if err != nil {
		t.Fatal(err)
	}
	if second.disposition != "already-provisioned" || second.installPerformed || !bytes.Equal(second.ciphertext, encrypted) {
		t.Fatalf("unexpected idempotent result: %#v", second)
	}
	after, err := os.ReadFile(target)
	if err != nil || !bytes.Equal(after, encrypted) {
		t.Fatalf("idempotent call replaced credential: %v", err)
	}
}

func TestPublishEncryptedGitHubCredentialRejectsImplicitRotation(t *testing.T) {
	root, uid, gid := testCredentialRoot(t)
	originalToken := testGitHubToken('a')
	original := testEncryptedCredential('A')
	if _, err := publishEncryptedGitHubCredential(root, uid, gid, originalToken, original, decryptsTo(originalToken)); err != nil {
		t.Fatal(err)
	}
	newToken := testGitHubToken('b')
	if _, err := publishEncryptedGitHubCredential(root, uid, gid, newToken, testEncryptedCredential('B'), decryptsTo(originalToken)); err == nil || err.Error() != "credential-current-cas-required" {
		t.Fatalf("implicit rotation accepted: %v", err)
	}
	target := filepath.Join(root, "etc/propertyquarry-release-single-host-v2/github-api-token.cred")
	raw, err := os.ReadFile(target)
	if err != nil || !bytes.Equal(raw, original) {
		t.Fatalf("rejected rotation changed credential: %v", err)
	}
}

func TestPublishEncryptedGitHubCredentialRecoversAuthenticatedPendingStage(t *testing.T) {
	root, uid, gid := testCredentialRoot(t)
	directory := filepath.Join(root, "etc/propertyquarry-release-single-host-v2")
	if err := os.Mkdir(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	token := testGitHubToken('c')
	pendingCiphertext := testEncryptedCredential('C')
	pending := filepath.Join(directory, githubCredentialPendingName)
	if err := os.WriteFile(pending, pendingCiphertext, 0o400); err != nil {
		t.Fatal(err)
	}
	result, err := publishEncryptedGitHubCredential(root, uid, gid, token, testEncryptedCredential('D'), decryptsTo(token))
	if err != nil {
		t.Fatal(err)
	}
	if result.disposition != "recovered-and-provisioned" || !result.installPerformed || !result.recoveryPerformed || !bytes.Equal(result.ciphertext, pendingCiphertext) {
		t.Fatalf("unexpected recovery result: %#v", result)
	}
	if _, err := os.Lstat(pending); !os.IsNotExist(err) {
		t.Fatalf("pending stage survived: %v", err)
	}
	target := filepath.Join(directory, "github-api-token.cred")
	raw, err := os.ReadFile(target)
	if err != nil || !bytes.Equal(raw, pendingCiphertext) {
		t.Fatalf("recovered credential invalid: %v", err)
	}
}

func TestPublishEncryptedGitHubCredentialRejectsHardlinksAndUnexpectedEntries(t *testing.T) {
	root, uid, gid := testCredentialRoot(t)
	directory := filepath.Join(root, "etc/propertyquarry-release-single-host-v2")
	if err := os.Mkdir(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(directory, "github-api-token.cred")
	if err := os.WriteFile(target, testEncryptedCredential('A'), 0o400); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(target, filepath.Join(directory, "unexpected")); err != nil {
		t.Fatal(err)
	}
	if _, err := publishEncryptedGitHubCredential(root, uid, gid, testGitHubToken('a'), testEncryptedCredential('B'), decryptsTo(testGitHubToken('a'))); err == nil || err.Error() != "credential-directory-state-invalid" {
		t.Fatalf("hardlinked/unexpected state accepted: %v", err)
	}
}

func TestReadGitHubTokenFIFOAcceptsOneTrailingNewlineOnly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token.pipe")
	if err := syscall.Mkfifo(path, 0o600); err != nil {
		t.Fatal(err)
	}
	token := testGitHubToken('z')
	done := make(chan error, 1)
	go func() {
		file, err := os.OpenFile(path, os.O_WRONLY, 0)
		if err == nil {
			_, err = file.Write(append(append([]byte(nil), token...), '\n'))
			_ = file.Close()
		}
		done <- err
	}()
	raw, err := readGitHubTokenFIFO(path, uint32(os.Geteuid()), uint32(os.Getegid()))
	if err != nil || !bytes.Equal(raw, token) {
		t.Fatalf("fifo read failed: %v", err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestCredentialValidatorsRejectWhitespaceAndNonCiphertext(t *testing.T) {
	for _, invalid := range [][]byte{
		[]byte(strings.Repeat("a", 31)),
		[]byte("github_pat_" + strings.Repeat("a", 40) + "\n"),
		[]byte("github_pat_" + strings.Repeat("a", 19)),
		[]byte("github_pat_" + strings.Repeat("a", 20) + "-"),
		[]byte("ghp_" + strings.Repeat("a", 48)),
		[]byte("gho_" + strings.Repeat("a", 48)),
		[]byte("ghu_" + strings.Repeat("a", 48)),
		[]byte("ghs_" + strings.Repeat("a", 48)),
		[]byte("ghr_" + strings.Repeat("a", 48)),
	} {
		if validGitHubToken(invalid) {
			t.Fatalf("invalid/classic token accepted: %q", invalid[:4])
		}
	}
	if !validGitHubToken(testGitHubToken('a')) {
		t.Fatal("valid token rejected")
	}
	if validEncryptedCredential([]byte(strings.Repeat("A", 63))) || validEncryptedCredential([]byte(strings.Repeat("A", 79)+"\n-\n")) {
		t.Fatal("invalid ciphertext accepted")
	}
	if !validEncryptedCredential(testEncryptedCredential('A')) {
		t.Fatal("valid ciphertext rejected")
	}
}

func TestLiveHostSystemdCredentialRoundTrip(t *testing.T) {
	if os.Getenv("PROPERTYQUARRY_RUN_LIVE_SYSTEMD_CREDENTIAL_TEST") != "1" {
		t.Skip("live host credential runtime test is opt-in")
	}
	if os.Geteuid() != 0 {
		t.Fatal("live host credential runtime test requires container root")
	}
	if err := validateHostSystemdCredentialRuntime(FixedHostRoot); err != nil {
		t.Fatal(err)
	}
	token := []byte("github_pat_propertyquarry_dummy_round_trip_1234567890")
	encrypted, err := runHostSystemdCredentialTransform(FixedHostRoot, "encrypt", token)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(encrypted)
	if !validEncryptedCredential(encrypted) || bytes.Contains(encrypted, token) {
		t.Fatal("systemd-creds returned an invalid encrypted envelope")
	}
	decrypted, err := runHostSystemdCredentialTransform(FixedHostRoot, "decrypt", encrypted)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(decrypted)
	if !bytes.Equal(decrypted, token) {
		t.Fatal("systemd-creds round trip changed the credential")
	}
}
