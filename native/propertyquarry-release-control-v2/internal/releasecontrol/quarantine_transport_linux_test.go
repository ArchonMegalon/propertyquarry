//go:build linux

package releasecontrol

import (
	"bytes"
	"encoding/binary"
	"errors"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"
)

type brokerWireOptions struct {
	responseFDs []int
	lateFDs     []int
	trailing    []byte
	halfClose   bool
	linger      time.Duration
	chunkSize   int
}

func brokerSocketPair(t *testing.T, passCredentials bool) [2]int {
	t.Helper()
	fds, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	if passCredentials {
		if err := syscall.SetsockoptInt(fds[1], syscall.SOL_SOCKET, syscall.SO_PASSCRED, 1); err != nil {
			_ = syscall.Close(fds[0])
			_ = syscall.Close(fds[1])
			t.Fatal(err)
		}
	}
	return fds
}

func brokerPipe(t *testing.T) [2]int {
	t.Helper()
	var fds [2]int
	if err := syscall.Pipe2(fds[:], syscall.O_CLOEXEC); err != nil {
		t.Fatal(err)
	}
	return fds
}

func frameRequest(payload []byte) []byte {
	length := uint32(len(payload))
	frame := []byte{
		byte(length >> 24),
		byte(length >> 16),
		byte(length >> 8),
		byte(length),
	}
	return append(frame, payload...)
}

func sendBrokerWire(fd int, payload []byte, options brokerWireOptions) error {
	defer syscall.Close(fd)
	defer closeDescriptors(options.responseFDs)
	defer closeDescriptors(options.lateFDs)
	frame := frameRequest(payload)
	rights := []byte(nil)
	if len(options.responseFDs) > 0 {
		rights = syscall.UnixRights(options.responseFDs...)
	}
	written, err := syscall.SendmsgN(fd, frame[:1], rights, nil, syscall.MSG_NOSIGNAL)
	if err != nil || written != 1 {
		if err == nil {
			err = errors.New("short first sendmsg")
		}
		return err
	}
	offset := 1
	if len(options.lateFDs) > 0 {
		written, err = syscall.SendmsgN(
			fd,
			frame[offset:offset+1],
			syscall.UnixRights(options.lateFDs...),
			nil,
			syscall.MSG_NOSIGNAL,
		)
		if err != nil || written != 1 {
			if err == nil {
				err = errors.New("short late sendmsg")
			}
			return err
		}
		offset++
	}
	wire := append(append([]byte(nil), frame[offset:]...), options.trailing...)
	for len(wire) > 0 {
		chunk := wire
		if options.chunkSize > 0 && len(chunk) > options.chunkSize {
			chunk = chunk[:options.chunkSize]
		}
		count, writeErr := syscall.Write(fd, chunk)
		if writeErr == syscall.EINTR {
			continue
		}
		if writeErr != nil {
			return writeErr
		}
		if count < 1 || count > len(chunk) {
			return errors.New("short socket write")
		}
		wire = wire[count:]
	}
	if options.halfClose {
		return syscall.Shutdown(fd, syscall.SHUT_WR)
	}
	if options.linger > 0 {
		time.Sleep(options.linger)
	}
	return nil
}

func awaitPipeEOF(t *testing.T, fd int) {
	t.Helper()
	if err := syscall.SetNonblock(fd, true); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	buffer := make([]byte, 1)
	for {
		count, err := syscall.Read(fd, buffer)
		if count == 0 && err == nil {
			return
		}
		if err != syscall.EAGAIN && err != syscall.EWOULDBLOCK && err != syscall.EINTR {
			t.Fatalf("pipe read failed: %d, %v", count, err)
		}
		if time.Until(deadline) <= 0 {
			t.Fatal("response descriptor remained open")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestBrokerQuarantineReceivesValidSyntaxAndClosesResponsePipe(t *testing.T) {
	sockets := brokerSocketPair(t, true)
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	sent := make(chan error, 1)
	go func() {
		sent <- sendBrokerWire(sockets[0], []byte(crossLanguageGoldenRequest), brokerWireOptions{
			responseFDs: []int{response[1]},
			halfClose:   true,
		})
	}()
	request, err := receiveBrokerQuarantine(sockets[1], time.Second)
	if sendErr := <-sent; sendErr != nil {
		t.Fatal(sendErr)
	}
	if err != nil {
		t.Fatal(err)
	}
	if request.authenticationEstablished || !request.envelopeDigestMatches {
		t.Fatal("quarantine intake created authority")
	}
	request.release()
	awaitPipeEOF(t, response[0])
}

func TestBrokerQuarantineAcceptsByteAtATimeHeaderAndBodyFragmentation(t *testing.T) {
	sockets := brokerSocketPair(t, true)
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	sent := make(chan error, 1)
	go func() {
		sent <- sendBrokerWire(sockets[0], []byte(crossLanguageGoldenRequest), brokerWireOptions{
			responseFDs: []int{response[1]},
			halfClose:   true,
			chunkSize:   1,
		})
	}()
	request, err := receiveBrokerQuarantine(sockets[1], time.Second)
	if sendErr := <-sent; sendErr != nil {
		t.Fatal(sendErr)
	}
	if err != nil {
		t.Fatal(err)
	}
	request.release()
	awaitPipeEOF(t, response[0])
}

func TestBrokerQuarantineAcceptsSyntacticDigestMismatchWithoutAuthority(t *testing.T) {
	payload := []byte(strings.Replace(
		crossLanguageGoldenRequest,
		"sha256:f9c9160c494309599e9a8c0c768fee086dcc2e5a81f4d91735b630281085211b",
		"sha256:0000000000000000000000000000000000000000000000000000000000000000",
		1,
	))
	sockets := brokerSocketPair(t, true)
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	sent := make(chan error, 1)
	go func() {
		sent <- sendBrokerWire(sockets[0], payload, brokerWireOptions{
			responseFDs: []int{response[1]},
			halfClose:   true,
		})
	}()
	request, err := receiveBrokerQuarantine(sockets[1], time.Second)
	if sendErr := <-sent; sendErr != nil {
		t.Fatal(sendErr)
	}
	if err != nil {
		t.Fatal(err)
	}
	defer request.release()
	if request.envelopeDigestMatches || request.authenticationEstablished {
		t.Fatal("digest mismatch escaped quarantine")
	}
	awaitPipeEOF(t, response[0])
}

func TestBrokerQuarantineRejectsMissingPasscredBeforeAdoption(t *testing.T) {
	sockets := brokerSocketPair(t, false)
	if request, err := receiveBrokerQuarantine(sockets[1], 20*time.Millisecond); err == nil {
		request.release()
		t.Fatal("socket without SO_PASSCRED accepted")
	}
	_ = syscall.Close(sockets[0])
}

func TestBrokerQuarantineRejectsMalformedRightsAndClosesEveryFD(t *testing.T) {
	for _, test := range []struct {
		name       string
		firstPipes int
		latePipes  int
	}{
		{name: "missing", firstPipes: 0},
		{name: "multiple", firstPipes: 2},
		{name: "late", firstPipes: 1, latePipes: 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			sockets := brokerSocketPair(t, true)
			readers := make([]int, 0, test.firstPipes+test.latePipes)
			first := make([]int, 0, test.firstPipes)
			late := make([]int, 0, test.latePipes)
			for index := 0; index < test.firstPipes+test.latePipes; index++ {
				pipe := brokerPipe(t)
				readers = append(readers, pipe[0])
				if index < test.firstPipes {
					first = append(first, pipe[1])
				} else {
					late = append(late, pipe[1])
				}
			}
			sent := make(chan error, 1)
			go func() {
				sent <- sendBrokerWire(sockets[0], []byte(crossLanguageGoldenRequest), brokerWireOptions{
					responseFDs: first,
					lateFDs:     late,
					halfClose:   true,
				})
			}()
			if request, err := receiveBrokerQuarantine(sockets[1], time.Second); err == nil {
				request.release()
				t.Fatal("malformed descriptor batch accepted")
			}
			if sendErr := <-sent; sendErr != nil && sendErr != syscall.EPIPE {
				t.Fatal(sendErr)
			}
			for _, reader := range readers {
				awaitPipeEOF(t, reader)
				_ = syscall.Close(reader)
			}
		})
	}
}

func TestBrokerQuarantineRejectsWrongResponseDescriptorTypes(t *testing.T) {
	for _, name := range []string{"pipe-read-end", "regular-file", "unix-socket"} {
		t.Run(name, func(t *testing.T) {
			sockets := brokerSocketPair(t, true)
			var wrong int
			var cleanup []int
			switch name {
			case "pipe-read-end":
				pipe := brokerPipe(t)
				wrong = pipe[0]
				cleanup = append(cleanup, pipe[1])
			case "regular-file":
				fileFD, err := syscall.Open("/dev/null", syscall.O_RDONLY|syscall.O_CLOEXEC, 0)
				if err != nil {
					t.Fatal(err)
				}
				wrong = fileFD
			case "unix-socket":
				pair := brokerSocketPair(t, false)
				wrong = pair[0]
				cleanup = append(cleanup, pair[1])
			}
			defer closeDescriptors(cleanup)
			sent := make(chan error, 1)
			go func() {
				sent <- sendBrokerWire(sockets[0], []byte(crossLanguageGoldenRequest), brokerWireOptions{
					responseFDs: []int{wrong},
					halfClose:   true,
				})
			}()
			if request, err := receiveBrokerQuarantine(sockets[1], time.Second); err == nil {
				request.release()
				t.Fatal("wrong response descriptor type accepted")
			}
			if sendErr := <-sent; sendErr != nil && sendErr != syscall.EPIPE {
				t.Fatal(sendErr)
			}
		})
	}
}

func TestResponsePipeInspectionRejectsNamedFIFOAndAnonymousORDWR(t *testing.T) {
	sockets := brokerSocketPair(t, false)
	defer closeDescriptors(sockets[:])

	namedPath := t.TempDir() + "/named-fifo"
	if err := syscall.Mkfifo(namedPath, 0o600); err != nil {
		t.Fatal(err)
	}
	namedReader, err := syscall.Open(namedPath, syscall.O_RDONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(namedReader)
	namedWriter, err := syscall.Open(namedPath, syscall.O_WRONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(namedWriter)
	if _, err := inspectResponsePipe(namedWriter, sockets[0]); err == nil {
		t.Fatal("named FIFO accepted as anonymous response pipe")
	}

	pipe := brokerPipe(t)
	defer closeDescriptors(pipe[:])
	rdwr, err := syscall.Open(
		"/proc/self/fd/"+strconvItoa(pipe[1]),
		syscall.O_RDWR|syscall.O_NONBLOCK|syscall.O_CLOEXEC,
		0,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer syscall.Close(rdwr)
	if _, err := inspectResponsePipe(rdwr, sockets[0]); err == nil {
		t.Fatal("anonymous O_RDWR FIFO accepted as response write end")
	}
}

func TestBrokerResponsePipeRevalidationDetectsSharedDescriptionMutation(t *testing.T) {
	pipe := brokerPipe(t)
	defer syscall.Close(pipe[0])
	metadata, _, err := inspectAdoptedResponsePipe(pipe[1])
	if err != nil {
		t.Fatal(err)
	}
	intake := &brokerControllerIntake{
		responseFD:       pipe[1],
		responseMetadata: metadata,
	}
	defer intake.release()
	if err := revalidateBrokerResponsePipe(intake); err != nil {
		t.Fatal(err)
	}
	if err := syscall.SetNonblock(pipe[1], true); err != nil {
		t.Fatal(err)
	}
	if err := revalidateBrokerResponsePipe(intake); err == nil {
		t.Fatal("response pipe open-file-description mutation accepted")
	}
}

func TestBrokerQuarantineRequiresTerminalEOFAndRejectsTrailingByte(t *testing.T) {
	for _, test := range []struct {
		name      string
		trailing  []byte
		halfClose bool
	}{
		{name: "trailing", trailing: []byte{'x'}, halfClose: true},
		{name: "missing-eof", halfClose: false},
	} {
		t.Run(test.name, func(t *testing.T) {
			sockets := brokerSocketPair(t, true)
			response := brokerPipe(t)
			defer syscall.Close(response[0])
			sent := make(chan error, 1)
			go func() {
				sent <- sendBrokerWire(sockets[0], []byte(crossLanguageGoldenRequest), brokerWireOptions{
					responseFDs: []int{response[1]},
					trailing:    test.trailing,
					halfClose:   test.halfClose,
					linger:      100 * time.Millisecond,
				})
			}()
			started := time.Now()
			if request, err := receiveBrokerQuarantine(sockets[1], 30*time.Millisecond); err == nil {
				request.release()
				t.Fatal("nonterminal frame accepted")
			}
			if time.Since(started) > time.Second {
				t.Fatal("single absolute deadline was not bounded")
			}
			if sendErr := <-sent; sendErr != nil && sendErr != syscall.EPIPE {
				t.Fatal(sendErr)
			}
			awaitPipeEOF(t, response[0])
		})
	}
}

func TestBrokerQuarantineAppliesAbsoluteDeadlineToHeaderAndBody(t *testing.T) {
	for _, stage := range []string{"header", "body"} {
		t.Run(stage, func(t *testing.T) {
			sockets := brokerSocketPair(t, true)
			response := brokerPipe(t)
			defer syscall.Close(response[0])
			sent := make(chan error, 1)
			go func() {
				defer syscall.Close(sockets[0])
				defer syscall.Close(response[1])
				frame := frameRequest([]byte(crossLanguageGoldenRequest))
				count, err := syscall.SendmsgN(
					sockets[0],
					frame[:1],
					syscall.UnixRights(response[1]),
					nil,
					syscall.MSG_NOSIGNAL,
				)
				if err != nil || count != 1 {
					sent <- err
					return
				}
				if stage == "body" {
					// Complete the header and provide only a body prefix. A body
					// stall must use the same deadline established before byte zero.
					if _, err := syscall.Write(sockets[0], frame[1:12]); err != nil {
						sent <- err
						return
					}
				}
				time.Sleep(100 * time.Millisecond)
				sent <- nil
			}()
			started := time.Now()
			if request, err := receiveBrokerQuarantine(sockets[1], 30*time.Millisecond); err == nil {
				request.release()
				t.Fatal("stalled frame stage accepted")
			}
			elapsed := time.Since(started)
			if elapsed < 20*time.Millisecond || elapsed > time.Second {
				t.Fatalf("stage deadline not bounded: %s", elapsed)
			}
			if sendErr := <-sent; sendErr != nil && sendErr != syscall.EPIPE {
				t.Fatal(sendErr)
			}
			awaitPipeEOF(t, response[0])
		})
	}
}

func TestBrokerQuarantineRejectsInvalidLengthAndBody(t *testing.T) {
	for _, payload := range [][]byte{nil, []byte(`{}`)} {
		sockets := brokerSocketPair(t, true)
		response := brokerPipe(t)
		defer syscall.Close(response[0])
		sent := make(chan error, 1)
		go func(body []byte) {
			sent <- sendBrokerWire(sockets[0], body, brokerWireOptions{
				responseFDs: []int{response[1]},
				halfClose:   true,
			})
		}(payload)
		if request, err := receiveBrokerQuarantine(sockets[1], time.Second); err == nil {
			request.release()
			t.Fatal("invalid framed body accepted")
		}
		if sendErr := <-sent; sendErr != nil && sendErr != syscall.EPIPE {
			t.Fatal(sendErr)
		}
		awaitPipeEOF(t, response[0])
	}
}

func TestBrokerQuarantineRejectsCredentialMismatch(t *testing.T) {
	expected := brokerPeerCredentials{PID: 100, UID: 200, GID: 300}
	for _, observed := range []brokerPeerCredentials{
		{PID: 101, UID: 200, GID: 300},
		{PID: 100, UID: 201, GID: 300},
		{PID: 100, UID: 200, GID: 301},
	} {
		batch := brokerAncillaryBatch{
			credentials:        []brokerPeerCredentials{observed},
			credentialMessages: 1,
		}
		if validateBrokerCredentials(batch, expected, false, false) == nil {
			t.Fatalf("mismatched credentials accepted: %#v", observed)
		}
	}
}

func TestAncillaryDecoderRetainsOwnedRightsAcrossEveryLaterError(t *testing.T) {
	pipe := brokerPipe(t)
	defer closeDescriptors(pipe[:])
	duplicate := func(t *testing.T) int {
		t.Helper()
		fd, err := fcntl(pipe[1], syscall.F_DUPFD_CLOEXEC, 3)
		if err != nil {
			t.Fatal(err)
		}
		return fd
	}
	unknownMessage := func() []byte {
		message := syscall.UnixCredentials(&syscall.Ucred{
			Pid: int32(os.Getpid()),
			Uid: uint32(os.Getuid()),
			Gid: uint32(os.Getgid()),
		})
		binary.NativeEndian.PutUint32(message[12:16], 0x7ffffffe)
		return message
	}

	for _, name := range []string{
		"later-malformed-header",
		"truncated-flag",
		"later-unknown-message",
		"duplicate-rights-message",
		"malformed-rights-payload",
	} {
		t.Run(name, func(t *testing.T) {
			owned := []int{duplicate(t)}
			raw := append([]byte(nil), syscall.UnixRights(owned[0])...)
			flags := 0
			switch name {
			case "later-malformed-header":
				raw = append(raw, make([]byte, syscall.CmsgLen(0)-1)...)
			case "truncated-flag":
				flags = syscall.MSG_CTRUNC
			case "later-unknown-message":
				raw = append(raw, unknownMessage()...)
			case "duplicate-rights-message":
				second := duplicate(t)
				owned = append(owned, second)
				raw = append(raw, syscall.UnixRights(second)...)
			case "malformed-rights-payload":
				binary.NativeEndian.PutUint64(raw[:8], uint64(syscall.CmsgLen(4)+1))
			}
			batch, err := decodeBrokerAncillary(raw, flags)
			if err == nil {
				closeDescriptors(owned)
				t.Fatal("malformed ancillary accepted")
			}
			closeDescriptors(batch.rights)
			for _, descriptor := range owned {
				var stat syscall.Stat_t
				if err := syscall.Fstat(descriptor, &stat); err != syscall.EBADF {
					closeDescriptors(owned)
					t.Fatalf("owned right was lost instead of closed: fd=%d err=%v", descriptor, err)
				}
			}
		})
	}
}

func TestAncillaryDecoderRejectsDuplicateCredentialsAndUnknownOnly(t *testing.T) {
	credentials := syscall.UnixCredentials(&syscall.Ucred{
		Pid: int32(os.Getpid()),
		Uid: uint32(os.Getuid()),
		Gid: uint32(os.Getgid()),
	})
	duplicate := append(append([]byte(nil), credentials...), credentials...)
	if _, err := decodeBrokerAncillary(duplicate, 0); err == nil {
		t.Fatal("duplicate credentials accepted")
	}
	unknown := append([]byte(nil), credentials...)
	binary.NativeEndian.PutUint32(unknown[12:16], 0x7ffffffe)
	if _, err := decodeBrokerAncillary(unknown, 0); err == nil {
		t.Fatal("unknown control message accepted")
	}
}

func TestResponsePipeRevalidationDetectsSharedStatusFlagChange(t *testing.T) {
	sockets := brokerSocketPair(t, false)
	defer closeDescriptors(sockets[:])
	response := brokerPipe(t)
	defer closeDescriptors(response[:])
	before, err := inspectResponsePipe(response[1], sockets[0])
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.SetNonblock(response[1], true); err != nil {
		t.Fatal(err)
	}
	after, err := inspectResponsePipe(response[1], sockets[0])
	if err != nil {
		t.Fatal(err)
	}
	if before == after || before.StatusFlags == after.StatusFlags {
		t.Fatal("shared open-file-description flag race was not observable")
	}
}

func countOpenDescriptors(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	return len(entries)
}

func TestBrokerQuarantineMalformedRightsDoNotLeakDescriptors(t *testing.T) {
	baseline := countOpenDescriptors(t)
	for iteration := 0; iteration < 32; iteration++ {
		sockets := brokerSocketPair(t, true)
		first := brokerPipe(t)
		late := brokerPipe(t)
		sent := make(chan error, 1)
		go func() {
			sent <- sendBrokerWire(sockets[0], []byte(crossLanguageGoldenRequest), brokerWireOptions{
				responseFDs: []int{first[1]},
				lateFDs:     []int{late[1]},
				halfClose:   true,
			})
		}()
		if request, err := receiveBrokerQuarantine(sockets[1], time.Second); err == nil {
			request.release()
			t.Fatal("late right accepted")
		}
		if sendErr := <-sent; sendErr != nil && sendErr != syscall.EPIPE {
			t.Fatal(sendErr)
		}
		awaitPipeEOF(t, first[0])
		awaitPipeEOF(t, late[0])
		_ = syscall.Close(first[0])
		_ = syscall.Close(late[0])
	}
	if observed := countOpenDescriptors(t); observed != baseline {
		t.Fatalf("descriptor leak: before=%d after=%d", baseline, observed)
	}
}

func TestBrokerOperationalHelper(t *testing.T) {
	if os.Getenv("PROPERTYQUARRY_BROKER_TEST_HELPER") != "1" {
		return
	}
	code := Run(
		Supervisor,
		[]string{"--server-broker", "--config", ControllerConfig, "--socket-activation"},
		os.Stdout,
		os.Stderr,
	)
	os.Exit(code)
}

func TestInheritedSenderCredentialHelper(t *testing.T) {
	if os.Getenv("PROPERTYQUARRY_INHERITED_SENDER_HELPER") != "1" {
		return
	}
	err := sendBrokerWire(3, []byte(crossLanguageGoldenRequest), brokerWireOptions{
		responseFDs: []int{4},
		halfClose:   true,
	})
	if err != nil && err != syscall.EPIPE {
		os.Exit(1)
	}
	os.Exit(0)
}

func TestBrokerRejectsInheritedChildSenderCredentialMismatch(t *testing.T) {
	sockets := brokerSocketPair(t, true)
	response := brokerPipe(t)
	defer syscall.Close(response[0])
	peer, err := syscall.GetsockoptUcred(sockets[1], syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	if err != nil || peer == nil || peer.Pid != int32(os.Getpid()) {
		t.Fatalf("unexpected pre-fork peer snapshot: %#v, %v", peer, err)
	}
	clientFile := os.NewFile(uintptr(sockets[0]), "inherited-client-socket")
	responseFile := os.NewFile(uintptr(response[1]), "inherited-response-pipe")
	if clientFile == nil || responseFile == nil {
		t.Fatal("failed to wrap inherited descriptors")
	}
	command := exec.Command(os.Args[0], "-test.run=^TestInheritedSenderCredentialHelper$")
	command.ExtraFiles = []*os.File{clientFile, responseFile}
	var stdout bytes.Buffer
	var stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	command.Env = []string{"PROPERTYQUARRY_INHERITED_SENDER_HELPER=1"}
	if err := command.Start(); err != nil {
		_ = clientFile.Close()
		_ = responseFile.Close()
		_ = syscall.Close(sockets[1])
		t.Fatal(err)
	}
	_ = clientFile.Close()
	_ = responseFile.Close()
	if request, err := receiveBrokerQuarantine(sockets[1], time.Second); err == nil {
		request.release()
		t.Fatal("inherited child writer bypassed SO_PEERCRED binding")
	}
	if err := command.Wait(); err != nil {
		t.Fatalf("sender helper failed: %v, stderr=%q", err, stderr.Bytes())
	}
	if stdout.Len() != 0 || stderr.Len() != 0 {
		t.Fatalf("sender helper output: stdout=%q stderr=%q", stdout.Bytes(), stderr.Bytes())
	}
	awaitPipeEOF(t, response[0])
}

func TestBrokerOperationalProcessConsumesValidRequestButAlwaysRefusesSilently(t *testing.T) {
	for _, mismatchedDigest := range []bool{false, true} {
		t.Run(map[bool]string{false: "matching-digest", true: "mismatched-digest"}[mismatchedDigest], func(t *testing.T) {
			payload := crossLanguageGoldenRequest
			if mismatchedDigest {
				payload = strings.Replace(
					payload,
					"sha256:f9c9160c494309599e9a8c0c768fee086dcc2e5a81f4d91735b630281085211b",
					"sha256:0000000000000000000000000000000000000000000000000000000000000000",
					1,
				)
			}
			sockets := brokerSocketPair(t, true)
			response := brokerPipe(t)
			defer syscall.Close(response[0])
			stdin := os.NewFile(uintptr(sockets[1]), "broker-socket")
			if stdin == nil {
				t.Fatal("failed to wrap broker socket")
			}
			temporary := t.TempDir()
			command := exec.Command(os.Args[0], "-test.run=^TestBrokerOperationalHelper$")
			command.Stdin = stdin
			var stdout bytes.Buffer
			var stderr bytes.Buffer
			command.Stdout = &stdout
			command.Stderr = &stderr
			command.Dir = temporary
			command.Env = []string{"PROPERTYQUARRY_BROKER_TEST_HELPER=1"}
			if err := command.Start(); err != nil {
				t.Fatal(err)
			}
			_ = stdin.Close()
			sent := sendBrokerWire(sockets[0], []byte(payload), brokerWireOptions{
				responseFDs: []int{response[1]},
				halfClose:   true,
			})
			if sent != nil {
				t.Fatal(sent)
			}
			err := command.Wait()
			var exitError *exec.ExitError
			if !errors.As(err, &exitError) || exitError.ExitCode() != ExitProtocolFailure {
				t.Fatalf("unexpected helper result: %v", err)
			}
			if stdout.Len() != 0 || stderr.Len() != 0 {
				t.Fatalf("operational output: stdout=%q stderr=%q", stdout.Bytes(), stderr.Bytes())
			}
			entries, err := os.ReadDir(temporary)
			if err != nil || len(entries) != 0 {
				t.Fatalf("operational state mutation: %v, %#v", err, entries)
			}
			awaitPipeEOF(t, response[0])
		})
	}
}
