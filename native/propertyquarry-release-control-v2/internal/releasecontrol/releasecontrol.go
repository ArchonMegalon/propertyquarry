// Package releasecontrol implements the native v2 fail-closed bootstrap.
//
// Its executable paths deliberately perform no release effect, network request,
// or response write. Legacy and unconfigured modes dispose of descriptors and return the
// protocol/authentication failure class. The explicit installed-local-authority
// mode additionally verifies its externally anchored local package and runs a
// fixed controller that retains the same fail-closed exit-50 behavior.
package releasecontrol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"runtime"
	"strconv"
	"syscall"
	"time"
)

type Component string

// SourceManifestDigest and ScratchExecutionContract are set only by the pinned
// build script. A raw `go build` leaves fail-closed sentinels visible in build
// information.
var SourceManifestDigest = "sha256:unbound"
var ScratchExecutionContract = "unbound"

const (
	Supervisor Component = "propertyquarry-release-supervisor-v2"
	Controller Component = "propertyquarry-release-controller-v2"
	Watchdog   Component = "propertyquarry-release-watchdog-v2"

	ExitProtocolFailure = 50
	MaxBearerBytes      = 16_384
	BearerReadTimeout   = 5 * time.Second

	ControllerConfig = "/etc/propertyquarry-release-control/controller-v2.json"
	WatchdogConfig   = "/etc/propertyquarry-release-control/watchdog-v2.json"
)

var (
	eventIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
	digestPattern  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
)

type buildInfo struct {
	Schema                 string `json:"schema"`
	Version                int    `json:"version"`
	Component              string `json:"component"`
	Toolchain              string `json:"toolchain"`
	SourceManifest         string `json:"source_manifest_digest"`
	ScratchExecution       string `json:"scratch_execution_contract"`
	Authoritative          bool   `json:"authoritative"`
	ProductionReady        bool   `json:"production_ready"`
	PerformsReleaseEffects bool   `json:"performs_release_effects"`
	SelfTest               bool   `json:"self_test"`
}

func Run(component Component, args []string, stdout, stderr io.Writer) int {
	if !validComponent(component) {
		return refuse(stderr)
	}
	if len(args) == 1 && (args[0] == "--build-info-json" || args[0] == "--self-test") {
		info := buildInfo{
			Schema:                 "propertyquarry.release-control.native-build-info.v2",
			Version:                2,
			Component:              string(component),
			Toolchain:              runtime.Version(),
			SourceManifest:         SourceManifestDigest,
			ScratchExecution:       ScratchExecutionContract,
			Authoritative:          false,
			ProductionReady:        false,
			PerformsReleaseEffects: false,
			SelfTest:               args[0] == "--self-test",
		}
		encoded, err := marshalBuildInfo(info)
		if err != nil {
			return refuse(stderr)
		}
		written, err := stdout.Write(encoded)
		if err != nil || written != len(encoded) {
			return refuse(stderr)
		}
		return 0
	}

	switch component {
	case Supervisor:
		return runSupervisor(args, stderr)
	case Controller:
		return runController(args, stderr)
	case Watchdog:
		return runWatchdog(args, stdout, stderr)
	default:
		return refuse(stderr)
	}
}

func marshalBuildInfo(info buildInfo) ([]byte, error) {
	// encoding/json sorts string map keys lexicographically. Keep this map as
	// the single producer for the Python verifier's exact canonical-JSON
	// contract; struct declaration order must never affect the wire bytes.
	encoded, err := json.Marshal(map[string]any{
		"authoritative":              info.Authoritative,
		"component":                  info.Component,
		"performs_release_effects":   info.PerformsReleaseEffects,
		"production_ready":           info.ProductionReady,
		"schema":                     info.Schema,
		"scratch_execution_contract": info.ScratchExecution,
		"self_test":                  info.SelfTest,
		"source_manifest_digest":     info.SourceManifest,
		"toolchain":                  info.Toolchain,
		"version":                    info.Version,
	})
	if err != nil {
		return nil, err
	}
	for _, value := range encoded {
		if value > 0x7f {
			return nil, fmt.Errorf("build information is not ASCII")
		}
	}
	return append(encoded, '\n'), nil
}

func validComponent(component Component) bool {
	return component == Supervisor || component == Controller || component == Watchdog
}

func refuse(stderr io.Writer) int {
	// Remain silent until every inherited descriptor has been proven disjoint
	// from the authority channels. Even redacted text can contaminate a response
	// pipe or request socket when a hostile caller aliases descriptors.
	_ = stderr
	return ExitProtocolFailure
}

func runSupervisor(args []string, stderr io.Writer) int {
	if len(args) == 2 && args[0] == "--installed-local-authority" && args[1] == "--docker-broker" {
		return runInstalledSupervisor(stderr)
	}
	if len(args) == 2 && args[0] == "--installed-local-authority" && args[1] == "--request-smoke" {
		return runInstalledRequestSmoke(stderr)
	}
	if len(args) == 2 && args[0] == "--installed-local-authority" && args[1] == "--docker-restart-stimulus" {
		return runInstalledSupervisorRestartStimulus(stderr)
	}
	if len(args) == 1 && (args[0] == "release-preflight" || args[0] == "release-run") {
		bearer, err := readBearerFD(9)
		if bearer != nil {
			zero(bearer)
		}
		if err != nil || os.Getenv("PROPERTYQUARRY_OIDC_TOKEN_FD") != "9" {
			return refuse(stderr)
		}
		return refuse(stderr)
	}
	if len(args) == 4 &&
		args[0] == "--server-broker" &&
		args[1] == "--config" &&
		args[2] == ControllerConfig &&
		args[3] == "--socket-activation" {
		request, err := receiveBrokerQuarantine(0, brokerReadTimeout)
		if request != nil {
			request.release()
		}
		if err != nil {
			return refuse(stderr)
		}
		return refuse(stderr)
	}
	return refuse(stderr)
}

func runController(args []string, stderr io.Writer) int {
	responseFD, responseFDOwned := responseFDAtFixedPosition(args)
	installedExecutableFD, installedExecutableFDOwned := installedExecutableFDAtFixedPosition(args)
	authenticatedRequestFD, authenticatedRequestFDOwned := authenticatedRequestFDAtFixedPosition(args)
	defer func() {
		closeOwnedControllerDescriptors(
			syscall.Close,
			ownedControllerDescriptor{fd: responseFD, owned: responseFDOwned},
			ownedControllerDescriptor{fd: installedExecutableFD, owned: installedExecutableFDOwned},
			ownedControllerDescriptor{fd: authenticatedRequestFD, owned: authenticatedRequestFDOwned},
		)
	}()
	if (len(args) != 10 && len(args) != 12 && len(args) != 14) ||
		args[0] != "--config" || args[1] != ControllerConfig ||
		args[2] != "--operation" ||
		args[4] != "--response-fd" ||
		args[6] != "--event-id" ||
		args[8] != "--request-transport-digest" {
		return refuse(stderr)
	}
	if len(args) >= 12 {
		if !installedExecutableFDOwned ||
			(responseFDOwned && installedExecutableFD == responseFD) {
			return refuse(stderr)
		}
	}
	if len(args) == 14 {
		if args[12] != "--authenticated-request-fd" ||
			!authenticatedRequestFDOwned ||
			authenticatedRequestFD == responseFD ||
			authenticatedRequestFD == installedExecutableFD {
			return refuse(stderr)
		}
	}
	if len(args) >= 12 {
		installedExecutableFDOwned = false
		if !closeInstalledExecutableFD(args) {
			return refuse(stderr)
		}
	}
	if args[3] != "release-preflight" && args[3] != "release-run" && args[3] != "reconcile-run" {
		return refuse(stderr)
	}
	if !eventIDPattern.MatchString(args[7]) || !digestPattern.MatchString(args[9]) {
		return refuse(stderr)
	}
	if !responseFDOwned {
		return refuse(stderr)
	}
	if err := validateWritePipe(responseFD); err != nil {
		return refuse(stderr)
	}
	if len(args) == 14 {
		authenticatedRequestFDOwned = false
		transaction, err := authenticateControllerTransaction(
			authenticatedRequestFD,
			args[3],
			args[7],
			args[9],
			time.Now(),
		)
		if err != nil || transaction == nil {
			return refuse(stderr)
		}
	}
	_ = syscall.Close(responseFD)
	responseFDOwned = false
	return refuse(stderr)
}

type ownedControllerDescriptor struct {
	fd    int
	owned bool
}

func closeOwnedControllerDescriptors(
	closeFD func(int) error,
	descriptors ...ownedControllerDescriptor,
) {
	if closeFD == nil {
		return
	}
	closed := make(map[int]struct{}, len(descriptors))
	for _, descriptor := range descriptors {
		if !descriptor.owned {
			continue
		}
		if _, duplicate := closed[descriptor.fd]; duplicate {
			continue
		}
		closed[descriptor.fd] = struct{}{}
		_ = closeFD(descriptor.fd)
	}
}

func responseFDAtFixedPosition(args []string) (int, bool) {
	if len(args) < 6 || args[4] != "--response-fd" {
		return 0, false
	}
	fd, err := strconv.Atoi(args[5])
	if err != nil || fd < 3 || strconv.Itoa(fd) != args[5] {
		return 0, false
	}
	return fd, true
}

func authenticatedRequestFDAtFixedPosition(args []string) (int, bool) {
	if len(args) != 14 || args[12] != "--authenticated-request-fd" {
		return 0, false
	}
	fd, err := strconv.Atoi(args[13])
	if err != nil || fd < 3 || strconv.Itoa(fd) != args[13] {
		return 0, false
	}
	return fd, true
}

func runWatchdog(args []string, stdout, stderr io.Writer) int {
	if len(args) == 2 && args[0] == "--installed-local-authority" {
		return runInstalledWatchdog(args, stdout, stderr)
	}
	if len(args) != 2 || args[0] != "--config" || args[1] != WatchdogConfig {
		return refuse(stderr)
	}
	// In particular, do not write READY=1 to NOTIFY_SOCKET.
	return refuse(stderr)
}

func readBearerFD(fd int) ([]byte, error) {
	return readBearerFDWithTimeout(fd, BearerReadTimeout)
}

func readBearerFDWithTimeout(fd int, timeout time.Duration) ([]byte, error) {
	if timeout <= 0 {
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("invalid bearer deadline")
	}
	if value := os.Getenv("PROPERTYQUARRY_OIDC_TOKEN_FD"); value != "" && value != strconv.Itoa(fd) {
		_ = syscall.Close(fd)
		return nil, fmt.Errorf("unexpected bearer descriptor")
	}
	if err := validateReadPipe(fd); err != nil {
		_ = syscall.Close(fd)
		return nil, err
	}
	defer syscall.Close(fd)
	if err := syscall.SetNonblock(fd, true); err != nil {
		return nil, fmt.Errorf("bearer nonblocking setup failed")
	}
	value, err := readBearerPipeUntilEOF(fd, timeout)
	if err != nil {
		zero(value)
		return nil, err
	}
	if len(value) < 2 || len(value) > MaxBearerBytes+1 || value[len(value)-1] != '\n' {
		zero(value)
		return nil, fmt.Errorf("bearer framing invalid")
	}
	bearer := value[:len(value)-1]
	if bytes.IndexAny(bearer, "\x00\r\n") >= 0 {
		zero(value)
		return nil, fmt.Errorf("bearer content invalid")
	}
	result := append([]byte(nil), bearer...)
	zero(value)
	return result, nil
}

func readBearerPipeUntilEOF(fd int, timeout time.Duration) ([]byte, error) {
	return readPipeUntilEOF(fd, MaxBearerBytes+1, timeout)
}

func readPipeUntilEOF(fd int, maximum int, timeout time.Duration) ([]byte, error) {
	if maximum < 1 || timeout <= 0 {
		return nil, fmt.Errorf("pipe read bounds invalid")
	}
	deadline := time.Now().Add(timeout)
	value := make([]byte, 0, maximum)
	chunk := make([]byte, 4096)
	defer zero(chunk)

	for {
		if time.Until(deadline) <= 0 {
			return value, fmt.Errorf("pipe read timed out")
		}
		count, err := syscall.Read(fd, chunk)
		if count > 0 {
			if count > maximum-len(value) {
				zero(chunk[:count])
				return value, fmt.Errorf("pipe framing invalid")
			}
			value = append(value, chunk[:count]...)
			zero(chunk[:count])
		}
		if err == nil {
			if count == 0 {
				return value, nil
			}
			continue
		}
		if err == syscall.EINTR {
			continue
		}
		if err != syscall.EAGAIN && err != syscall.EWOULDBLOCK {
			return value, fmt.Errorf("pipe read failed")
		}

		remaining := time.Until(deadline)
		if remaining <= 0 {
			return value, fmt.Errorf("pipe read timed out")
		}
		pause := time.Millisecond
		if remaining < pause {
			pause = remaining
		}
		time.Sleep(pause)
	}
}

func zero(value []byte) {
	for index := range value {
		value[index] = 0
	}
	runtime.KeepAlive(value)
}

func validateReadPipe(fd int) error {
	stat, flags, err := descriptorStatAndFlags(fd)
	if err != nil {
		return err
	}
	if stat.Mode&syscall.S_IFMT != syscall.S_IFIFO || flags&syscall.O_ACCMODE != syscall.O_RDONLY {
		return fmt.Errorf("descriptor is not a read pipe")
	}
	if aliasesStandardDescriptor(fd, stat) {
		return fmt.Errorf("descriptor aliases a standard stream")
	}
	return setCloseOnExec(fd)
}

func validateWritePipe(fd int) error {
	stat, flags, err := descriptorStatAndFlags(fd)
	if err != nil {
		return err
	}
	if stat.Mode&syscall.S_IFMT != syscall.S_IFIFO || flags&syscall.O_ACCMODE != syscall.O_WRONLY {
		return fmt.Errorf("descriptor is not a write pipe")
	}
	if aliasesStandardDescriptor(fd, stat) {
		return fmt.Errorf("descriptor aliases a standard stream")
	}
	return setCloseOnExec(fd)
}

func validateConnectedUnixStream(fd int) error {
	var stat syscall.Stat_t
	if err := syscall.Fstat(fd, &stat); err != nil {
		return err
	}
	if stat.Mode&syscall.S_IFMT != syscall.S_IFSOCK {
		return fmt.Errorf("descriptor is not a socket")
	}
	typeValue, err := syscall.GetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_TYPE)
	if err != nil || typeValue != syscall.SOCK_STREAM {
		return fmt.Errorf("descriptor is not a stream socket")
	}
	peer, err := syscall.Getpeername(fd)
	if err != nil {
		return fmt.Errorf("socket is not connected")
	}
	if _, ok := peer.(*syscall.SockaddrUnix); !ok {
		return fmt.Errorf("socket is not unix")
	}
	return setCloseOnExec(fd)
}

func descriptorStatAndFlags(fd int) (syscall.Stat_t, int, error) {
	var stat syscall.Stat_t
	if err := syscall.Fstat(fd, &stat); err != nil {
		return stat, 0, err
	}
	flags, err := fcntl(fd, syscall.F_GETFL, 0)
	return stat, flags, err
}

func setCloseOnExec(fd int) error {
	flags, err := fcntl(fd, syscall.F_GETFD, 0)
	if err != nil {
		return err
	}
	_, err = fcntl(fd, syscall.F_SETFD, flags|syscall.FD_CLOEXEC)
	if err != nil {
		return err
	}
	verified, err := fcntl(fd, syscall.F_GETFD, 0)
	if err != nil || verified&syscall.FD_CLOEXEC == 0 {
		return fmt.Errorf("close-on-exec verification failed")
	}
	return nil
}

func fcntl(fd int, command int, argument int) (int, error) {
	result, _, errno := syscall.Syscall(
		syscall.SYS_FCNTL,
		uintptr(fd),
		uintptr(command),
		uintptr(argument),
	)
	if errno != 0 {
		return 0, errno
	}
	return int(result), nil
}

func aliasesStandardDescriptor(fd int, stat syscall.Stat_t) bool {
	for candidate := 0; candidate <= 2; candidate++ {
		if candidate == fd {
			return true
		}
		var other syscall.Stat_t
		if syscall.Fstat(candidate, &other) == nil && sameDescriptorIdentity(stat, other) {
			return true
		}
	}
	return false
}

func sameDescriptorIdentity(left, right syscall.Stat_t) bool {
	return left.Dev == right.Dev &&
		left.Ino == right.Ino &&
		left.Rdev == right.Rdev &&
		left.Mode&syscall.S_IFMT == right.Mode&syscall.S_IFMT
}
