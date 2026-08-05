package releasecontrol

import (
	"bytes"
	"os"
	"runtime"
	"strconv"
	"syscall"
	"testing"
	"time"
)

func TestBuildInfoIsExplicitlyNonAuthoritative(t *testing.T) {
	for _, component := range []Component{Controller, Supervisor, Watchdog} {
		for _, test := range []struct {
			argument string
			selfTest bool
		}{
			{"--build-info-json", false},
			{"--self-test", true},
		} {
			var stdout bytes.Buffer
			var stderr bytes.Buffer
			if code := Run(component, []string{test.argument}, &stdout, &stderr); code != 0 {
				t.Fatalf("%s %s returned %d: %q", component, test.argument, code, stderr.String())
			}
			expected := `{"authoritative":false,"component":"` + string(component) +
				`","performs_release_effects":false,"production_ready":false,` +
				`"schema":"propertyquarry.release-control.native-build-info.v2",` +
				`"scratch_execution_contract":"` + ScratchExecutionContract +
				`","self_test":` + strconv.FormatBool(test.selfTest) +
				`,"source_manifest_digest":"` + SourceManifestDigest +
				`","toolchain":"` + runtime.Version() + `","version":2}` + "\n"
			if stdout.String() != expected {
				t.Fatalf("%s %s output is not exact canonical JSON:\n got %q\nwant %q", component, test.argument, stdout.String(), expected)
			}
			if stderr.Len() != 0 {
				t.Fatalf("%s %s emitted stderr: %q", component, test.argument, stderr.String())
			}
		}
	}
}

type shortWriter struct{}

func (shortWriter) Write(value []byte) (int, error) {
	return len(value) - 1, nil
}

func TestBuildInfoFailsClosedOnNonASCIIOrShortWrite(t *testing.T) {
	t.Run("non-ASCII", func(t *testing.T) {
		original := SourceManifestDigest
		SourceManifestDigest = "sha256:non-ascii-\u00e9"
		defer func() { SourceManifestDigest = original }()
		var stdout bytes.Buffer
		var stderr bytes.Buffer
		if code := Run(Controller, []string{"--self-test"}, &stdout, &stderr); code != ExitProtocolFailure {
			t.Fatalf("non-ASCII build information returned %d", code)
		}
		if stdout.Len() != 0 || stderr.Len() != 0 {
			t.Fatalf("non-ASCII failure emitted output: stdout=%q stderr=%q", stdout.String(), stderr.String())
		}
	})
	t.Run("short-write", func(t *testing.T) {
		var stderr bytes.Buffer
		if code := Run(Controller, []string{"--self-test"}, shortWriter{}, &stderr); code != ExitProtocolFailure {
			t.Fatalf("short write returned %d", code)
		}
		if stderr.Len() != 0 {
			t.Fatalf("short-write failure emitted stderr: %q", stderr.String())
		}
	})
}

func TestOperationalModesRefuse(t *testing.T) {
	for _, test := range []struct {
		component Component
		args      []string
	}{
		{Supervisor, nil},
		{Supervisor, []string{"--installed-local-authority", "--unknown"}},
		{Controller, nil},
		{Watchdog, []string{"--config", WatchdogConfig}},
		{Watchdog, []string{"--installed-local-authority", "--unknown"}},
	} {
		var stdout bytes.Buffer
		var stderr bytes.Buffer
		if code := Run(test.component, test.args, &stdout, &stderr); code != ExitProtocolFailure {
			t.Fatalf("%s returned %d", test.component, code)
		}
		if stdout.Len() != 0 {
			t.Fatalf("%s emitted operational stdout", test.component)
		}
		if stderr.Len() != 0 {
			t.Fatalf("%s emitted operational stderr", test.component)
		}
	}
}

func TestBearerPipeRequiresExactlyOneTrailingLF(t *testing.T) {
	for _, value := range [][]byte{
		[]byte("token\n"),
		[]byte("token\n\n"),
		[]byte("token\r\n"),
		[]byte("\n"),
	} {
		var pipeFDs [2]int
		if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
			t.Fatal(err)
		}
		if _, err := syscall.Write(pipeFDs[1], value); err != nil {
			t.Fatal(err)
		}
		_ = syscall.Close(pipeFDs[1])
		bearer, readErr := readBearerFD(pipeFDs[0])
		if bytes.Equal(value, []byte("token\n")) {
			if readErr != nil || string(bearer) != "token" {
				t.Fatalf("valid bearer rejected: %v", readErr)
			}
			zero(bearer)
		} else if readErr == nil {
			t.Fatalf("invalid bearer accepted: %q", value)
		}
	}
}

func TestBearerPipeAcceptsFragmentedMaximumValue(t *testing.T) {
	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	token := bytes.Repeat([]byte{'t'}, MaxBearerBytes)
	framed := append(append([]byte(nil), token...), '\n')
	writeResult := make(chan error, 1)
	go func() {
		defer syscall.Close(pipeFDs[1])
		for offset := 0; offset < len(framed); {
			end := offset + 127
			if end > len(framed) {
				end = len(framed)
			}
			count, err := syscall.Write(pipeFDs[1], framed[offset:end])
			if err != nil {
				writeResult <- err
				return
			}
			offset += count
		}
		writeResult <- nil
	}()
	bearer, err := readBearerFD(pipeFDs[0])
	if err != nil {
		t.Fatal(err)
	}
	if err := <-writeResult; err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(bearer, token) {
		t.Fatal("fragmented bearer changed")
	}
	zero(bearer)
	zero(token)
	zero(framed)
}

func TestBearerReadHasDeadlineClosesFDAndSetsCloexec(t *testing.T) {
	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], 0); err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(pipeFDs[1])
	if err := validateReadPipe(pipeFDs[0]); err != nil {
		t.Fatal(err)
	}
	flags, err := fcntl(pipeFDs[0], syscall.F_GETFD, 0)
	if err != nil || flags&syscall.FD_CLOEXEC == 0 {
		t.Fatalf("CLOEXEC not established: %#x, %v", flags, err)
	}
	started := time.Now()
	if _, err := readBearerFDWithTimeout(pipeFDs[0], 20*time.Millisecond); err == nil {
		t.Fatal("idle bearer pipe did not time out")
	}
	if time.Since(started) > time.Second {
		t.Fatal("bearer deadline was not bounded")
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(pipeFDs[0], &stat); err != syscall.EBADF {
		t.Fatalf("bearer fd remained open: %v", err)
	}
}

func TestBearerRejectsRegularFile(t *testing.T) {
	path := t.TempDir() + "/bearer"
	if err := os.WriteFile(path, []byte("token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := readBearerFDWithTimeout(fd, 20*time.Millisecond); err == nil {
		t.Fatal("regular-file bearer accepted")
	}
}

func TestControllerClosesResponsePipeWithoutWriting(t *testing.T) {
	var pipeFDs [2]int
	if err := syscall.Pipe2(pipeFDs[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(pipeFDs[0])
	fd := pipeFDs[1]
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	code := Run(Controller, []string{
		"--config", ControllerConfig,
		"--operation", "release-run",
		"--response-fd", strconvItoa(fd),
		"--event-id", "event-1",
		"--request-transport-digest", "sha256:" + string(bytes.Repeat([]byte{'a'}, 64)),
	}, &stdout, &stderr)
	if code != ExitProtocolFailure {
		t.Fatalf("unexpected exit: %d", code)
	}
	buffer := make([]byte, 1)
	count, err := syscall.Read(pipeFDs[0], buffer)
	if err != nil || count != 0 {
		t.Fatalf("response pipe was written: %q, %v", buffer[:count], err)
	}
}

func TestControllerDescriptorCleanupDeduplicatesAliases(t *testing.T) {
	var closed []int
	closeOwnedControllerDescriptors(
		func(fd int) error {
			closed = append(closed, fd)
			return nil
		},
		ownedControllerDescriptor{fd: 7, owned: true},
		ownedControllerDescriptor{fd: 7, owned: true},
		ownedControllerDescriptor{fd: 8, owned: false},
		ownedControllerDescriptor{fd: 9, owned: true},
		ownedControllerDescriptor{fd: 9, owned: true},
	)
	if len(closed) != 2 || closed[0] != 7 || closed[1] != 9 {
		t.Fatalf("controller descriptor cleanup was not unique and ordered: %v", closed)
	}
}

func TestControllerRejectsAliasedDescriptorRolesBeforeConsumption(t *testing.T) {
	tests := []struct {
		name                       string
		authenticatedAliasesWriter bool
		authenticatedAliasesFile   bool
	}{
		{name: "response-and-installed-executable"},
		{name: "response-and-authenticated-request", authenticatedAliasesWriter: true},
		{name: "installed-executable-and-authenticated-request", authenticatedAliasesFile: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var responseFDs [2]int
			if err := syscall.Pipe2(responseFDs[:], syscall.O_CLOEXEC); err != nil {
				t.Fatal(err)
			}
			defer syscall.Close(responseFDs[0])

			executablePath := t.TempDir() + "/controller"
			if err := os.WriteFile(executablePath, []byte("controller"), 0o755); err != nil {
				t.Fatal(err)
			}
			executableFD, err := syscall.Open(
				executablePath,
				syscall.O_RDONLY|syscall.O_CLOEXEC,
				0,
			)
			if err != nil {
				t.Fatal(err)
			}

			installedFD := executableFD
			if test.name == "response-and-installed-executable" {
				installedFD = responseFDs[1]
			}
			authenticatedFD := responseFDs[1]
			if test.authenticatedAliasesFile {
				authenticatedFD = executableFD
			} else if !test.authenticatedAliasesWriter {
				authenticatedFD = executableFD + responseFDs[1] + 1
			}

			args := []string{
				"--config", ControllerConfig,
				"--operation", "release-run",
				"--response-fd", strconv.Itoa(responseFDs[1]),
				"--event-id", "local-" + string(bytes.Repeat([]byte{'a'}, 64)),
				"--request-transport-digest", "sha256:" + string(bytes.Repeat([]byte{'a'}, 64)),
				"--installed-local-authority-executable-fd", strconv.Itoa(installedFD),
			}
			if test.name != "response-and-installed-executable" {
				args = append(
					args,
					"--authenticated-request-fd",
					strconv.Itoa(authenticatedFD),
				)
			}
			var stdout bytes.Buffer
			var stderr bytes.Buffer
			if code := Run(Controller, args, &stdout, &stderr); code != ExitProtocolFailure {
				t.Fatalf("aliased descriptors returned %d", code)
			}
			if stdout.Len() != 0 || stderr.Len() != 0 {
				t.Fatalf("aliased descriptor refusal emitted output")
			}
			if installedFD != executableFD {
				_ = syscall.Close(executableFD)
			}
			buffer := make([]byte, 1)
			count, err := syscall.Read(responseFDs[0], buffer)
			if err != nil || count != 0 {
				t.Fatalf("aliased descriptor refusal wrote a response: %q, %v", buffer[:count], err)
			}
		})
	}
}

func TestConnectedUnixStreamValidation(t *testing.T) {
	fds, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(fds[0])
	defer syscall.Close(fds[1])
	if err := validateConnectedUnixStream(fds[0]); err != nil {
		t.Fatal(err)
	}
	flags, err := fcntl(fds[0], syscall.F_GETFD, 0)
	if err != nil || flags&syscall.FD_CLOEXEC == 0 {
		t.Fatalf("CLOEXEC not established: %#x, %v", flags, err)
	}
}

func TestConnectedUnixStreamRejectsWrongSockets(t *testing.T) {
	for _, socketType := range []int{syscall.SOCK_DGRAM, syscall.SOCK_SEQPACKET} {
		fds, err := syscall.Socketpair(syscall.AF_UNIX, socketType|syscall.SOCK_CLOEXEC, 0)
		if err != nil {
			t.Fatal(err)
		}
		if err := validateConnectedUnixStream(fds[0]); err == nil {
			t.Fatalf("socket type %d accepted", socketType)
		}
		_ = syscall.Close(fds[0])
		_ = syscall.Close(fds[1])
	}
}

func strconvItoa(value int) string {
	return strconv.Itoa(value)
}
