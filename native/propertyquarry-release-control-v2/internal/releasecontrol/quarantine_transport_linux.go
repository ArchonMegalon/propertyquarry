//go:build linux

package releasecontrol

import (
	"encoding/binary"
	"errors"
	"os"
	"regexp"
	"strconv"
	"syscall"
	"time"
)

const (
	brokerReadTimeout = 10 * time.Second
	linuxSCMMaxFD     = 253
	linuxPipeFSMagic  = 0x50495045
)

var (
	errBrokerIntake     = errors.New("broker quarantine intake failed")
	anonymousPipeTarget = regexp.MustCompile(`^pipe:\[[0-9]+\]\z`)
)

type brokerPeerCredentials struct {
	PID int32
	UID uint32
	GID uint32
}

type brokerAncillaryBatch struct {
	rights             []int
	credentials        []brokerPeerCredentials
	rightsMessages     int
	credentialMessages int
}

type responsePipeMetadata struct {
	Device          uint64
	Inode           uint64
	Rdevice         uint64
	ModeType        uint32
	StatusFlags     int
	AccessMode      int
	DescriptorFlags int
	FilesystemType  int64
	Target          string
}

// brokerControllerIntake is the transport material retained only long enough
// to launch the fixed controller. It owns both the parsed request buffers and
// the adopted response pipe until release or explicit transfer.
type brokerControllerIntake struct {
	request          *quarantinedRequest
	responseFD       int
	responseMetadata responsePipeMetadata
	peer             brokerPeerCredentials
}

func (intake *brokerControllerIntake) release() {
	if intake == nil {
		return
	}
	if intake.responseFD >= 0 {
		_ = syscall.Close(intake.responseFD)
		intake.responseFD = -1
	}
	if intake.request != nil {
		intake.request.release()
		intake.request = nil
	}
	intake.peer = brokerPeerCredentials{}
}

// receiveBrokerQuarantine owns socketFD. A successful return proves only local
// framing and strict syntax; it never authenticates the request. The sole
// response descriptor is closed before this function returns.
func receiveBrokerQuarantine(socketFD int, timeout time.Duration) (*quarantinedRequest, error) {
	intake, err := receiveBrokerControllerIntake(socketFD, timeout)
	if err != nil {
		return nil, err
	}
	request := intake.request
	intake.request = nil
	intake.release()
	return request, nil
}

// receiveBrokerControllerIntake retains the verified anonymous response pipe
// for a fixed controller child. It still establishes transport syntax only;
// callers must authenticate the installed local authority and revalidate the
// pinned controller before transferring the descriptor.
func receiveBrokerControllerIntake(socketFD int, timeout time.Duration) (*brokerControllerIntake, error) {
	if socketFD < 0 || timeout <= 0 {
		if socketFD >= 0 {
			_ = syscall.Close(socketFD)
		}
		return nil, errBrokerIntake
	}
	defer syscall.Close(socketFD)
	deadline := time.Now().Add(timeout)
	peer, err := validateBrokerSocket(socketFD)
	if err != nil {
		return nil, errBrokerIntake
	}

	first := make([]byte, 1)
	firstCount, firstBatch, err := recvBrokerBatch(socketFD, first, deadline)
	if err != nil {
		return nil, errBrokerIntake
	}
	if firstCount != 1 || firstBatch.rightsMessages != 1 || len(firstBatch.rights) != 1 {
		closeDescriptors(firstBatch.rights)
		return nil, errBrokerIntake
	}
	responseFD := firstBatch.rights[0]
	responseFDOwned := true
	defer func() {
		if responseFDOwned {
			_ = syscall.Close(responseFD)
		}
	}()
	if err := validateBrokerCredentials(firstBatch, peer, true, false); err != nil {
		return nil, errBrokerIntake
	}
	originalMetadata, err := inspectResponsePipe(responseFD, socketFD)
	if err != nil {
		return nil, errBrokerIntake
	}

	header := []byte{first[0], 0, 0, 0}
	for offset := 1; offset < len(header); {
		count, batch, err := recvBrokerBatch(socketFD, header[offset:], deadline)
		if err != nil {
			return nil, errBrokerIntake
		}
		if err := validateLaterBrokerBatch(batch, peer, count == 0); err != nil {
			closeDescriptors(batch.rights)
			return nil, errBrokerIntake
		}
		if count == 0 {
			return nil, errBrokerIntake
		}
		offset += count
	}
	payloadLength := uint32(header[0])<<24 |
		uint32(header[1])<<16 |
		uint32(header[2])<<8 |
		uint32(header[3])
	if payloadLength < 1 || payloadLength > maxRequestBytes {
		return nil, errBrokerIntake
	}
	payload := make([]byte, int(payloadLength))
	defer zero(payload)
	for offset := 0; offset < len(payload); {
		count, batch, err := recvBrokerBatch(socketFD, payload[offset:], deadline)
		if err != nil {
			return nil, errBrokerIntake
		}
		if err := validateLaterBrokerBatch(batch, peer, count == 0); err != nil {
			closeDescriptors(batch.rights)
			return nil, errBrokerIntake
		}
		if count == 0 {
			return nil, errBrokerIntake
		}
		offset += count
	}

	trailing := make([]byte, 1)
	count, batch, err := recvBrokerBatch(socketFD, trailing, deadline)
	if err != nil {
		return nil, errBrokerIntake
	}
	if err := validateLaterBrokerBatch(batch, peer, count == 0); err != nil {
		closeDescriptors(batch.rights)
		return nil, errBrokerIntake
	}
	if count != 0 {
		return nil, errBrokerIntake
	}

	parsed, err := parseQuarantinedRequest(payload)
	if err != nil {
		return nil, errBrokerIntake
	}
	finalMetadata, err := inspectResponsePipe(responseFD, socketFD)
	if err != nil || finalMetadata != originalMetadata {
		parsed.release()
		return nil, errBrokerIntake
	}
	responseFDOwned = false
	return &brokerControllerIntake{
		request:          parsed,
		responseFD:       responseFD,
		responseMetadata: finalMetadata,
		peer:             peer,
	}, nil
}

func validateBrokerSocket(fd int) (brokerPeerCredentials, error) {
	if err := validateConnectedUnixStream(fd); err != nil {
		return brokerPeerCredentials{}, errBrokerIntake
	}
	passCredentials, err := syscall.GetsockoptInt(fd, syscall.SOL_SOCKET, syscall.SO_PASSCRED)
	if err != nil || passCredentials != 1 {
		return brokerPeerCredentials{}, errBrokerIntake
	}
	credentials, err := syscall.GetsockoptUcred(fd, syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	if err != nil || credentials == nil || credentials.Pid <= 0 {
		return brokerPeerCredentials{}, errBrokerIntake
	}
	return brokerPeerCredentials{
		PID: credentials.Pid,
		UID: credentials.Uid,
		GID: credentials.Gid,
	}, nil
}

func recvBrokerBatch(
	fd int,
	destination []byte,
	deadline time.Time,
) (int, brokerAncillaryBatch, error) {
	if len(destination) < 1 {
		return 0, brokerAncillaryBatch{}, errBrokerIntake
	}
	oob := make([]byte,
		syscall.CmsgSpace(linuxSCMMaxFD*4)+syscall.CmsgSpace(syscall.SizeofUcred),
	)
	defer zero(oob)
	for {
		if time.Until(deadline) <= 0 {
			return 0, brokerAncillaryBatch{}, errBrokerIntake
		}
		count, oobCount, flags, _, err := syscall.Recvmsg(
			fd,
			destination,
			oob,
			syscall.MSG_CMSG_CLOEXEC|syscall.MSG_DONTWAIT,
		)
		if err == syscall.EINTR {
			continue
		}
		if err == syscall.EAGAIN || err == syscall.EWOULDBLOCK {
			remaining := time.Until(deadline)
			if remaining <= 0 {
				return 0, brokerAncillaryBatch{}, errBrokerIntake
			}
			pause := time.Millisecond
			if remaining < pause {
				pause = remaining
			}
			time.Sleep(pause)
			continue
		}
		if err != nil || count < 0 || count > len(destination) || oobCount < 0 || oobCount > len(oob) {
			return 0, brokerAncillaryBatch{}, errBrokerIntake
		}
		batch, parseErr := decodeBrokerAncillary(oob[:oobCount], flags)
		if parseErr != nil {
			closeDescriptors(batch.rights)
			return 0, brokerAncillaryBatch{}, errBrokerIntake
		}
		return count, batch, nil
	}
}

func decodeBrokerAncillary(oob []byte, flags int) (brokerAncillaryBatch, error) {
	batch := brokerAncillaryBatch{}
	invalid := flags&(syscall.MSG_CTRUNC|syscall.MSG_TRUNC|syscall.MSG_OOB) != 0 ||
		flags & ^syscall.MSG_CMSG_CLOEXEC != 0
	// The release build is pinned to Linux AMD64. Parse one kernel control
	// message at a time instead of using ParseSocketControlMessage: that helper
	// returns a nil slice when a later header is malformed and would therefore
	// lose already installed SCM_RIGHTS descriptors that must be closed.
	if syscall.SizeofCmsghdr != 16 || syscall.CmsgLen(0) != 16 {
		return batch, errBrokerIntake
	}
	for offset := 0; offset < len(oob); {
		remaining := len(oob) - offset
		if remaining < syscall.CmsgLen(0) {
			invalid = true
			break
		}
		header := oob[offset : offset+syscall.CmsgLen(0)]
		messageLength64 := binary.NativeEndian.Uint64(header[:8])
		level := int(int32(binary.NativeEndian.Uint32(header[8:12])))
		messageType := int(int32(binary.NativeEndian.Uint32(header[12:16])))
		if messageLength64 < uint64(syscall.SizeofCmsghdr) || messageLength64 > uint64(remaining) {
			invalid = true
			break
		}
		messageLength := int(messageLength64)
		dataStart := offset + syscall.CmsgLen(0)
		dataEnd := offset + messageLength
		if dataStart > dataEnd {
			invalid = true
			break
		}
		data := oob[dataStart:dataEnd]
		if level != syscall.SOL_SOCKET {
			invalid = true
		} else {
			switch messageType {
			case syscall.SCM_RIGHTS:
				batch.rightsMessages++
				for dataOffset := 0; dataOffset+4 <= len(data); dataOffset += 4 {
					batch.rights = append(batch.rights, int(int32(binary.NativeEndian.Uint32(data[dataOffset:dataOffset+4]))))
				}
				if len(data) == 0 || len(data)%4 != 0 {
					invalid = true
				}
			case syscall.SCM_CREDENTIALS:
				batch.credentialMessages++
				if len(data) != syscall.SizeofUcred {
					invalid = true
				} else {
					batch.credentials = append(batch.credentials, brokerPeerCredentials{
						PID: int32(binary.NativeEndian.Uint32(data[:4])),
						UID: binary.NativeEndian.Uint32(data[4:8]),
						GID: binary.NativeEndian.Uint32(data[8:12]),
					})
				}
			default:
				invalid = true
			}
		}
		dataLength := messageLength - syscall.CmsgLen(0)
		messageSpace := syscall.CmsgSpace(dataLength)
		if messageSpace < messageLength || messageSpace > remaining {
			if messageLength == remaining {
				offset = len(oob)
				continue
			}
			invalid = true
			break
		}
		offset += messageSpace
	}
	if batch.rightsMessages > 1 || batch.credentialMessages > 1 || invalid {
		return batch, errBrokerIntake
	}
	return batch, nil
}

func validateBrokerCredentials(
	batch brokerAncillaryBatch,
	expected brokerPeerCredentials,
	first bool,
	eof bool,
) error {
	if first && (batch.credentialMessages != 1 || len(batch.credentials) != 1) {
		return errBrokerIntake
	}
	if batch.credentialMessages != len(batch.credentials) || len(batch.credentials) > 1 {
		return errBrokerIntake
	}
	if len(batch.credentials) == 1 {
		observed := batch.credentials[0]
		// Linux can synthesize PID zero credentials on an orderly EOF recvmsg
		// when SO_PASSCRED is enabled. It is accepted only for empty EOF.
		if eof && observed.PID == 0 {
			return nil
		}
		if observed.PID <= 0 || observed != expected {
			return errBrokerIntake
		}
	}
	return nil
}

func validateLaterBrokerBatch(
	batch brokerAncillaryBatch,
	expected brokerPeerCredentials,
	eof bool,
) error {
	if batch.rightsMessages != 0 || len(batch.rights) != 0 {
		return errBrokerIntake
	}
	return validateBrokerCredentials(batch, expected, false, eof)
}

func inspectResponsePipe(fd int, requestSocketFD int) (responsePipeMetadata, error) {
	if fd < 3 || requestSocketFD < 0 || fd == requestSocketFD {
		return responsePipeMetadata{}, errBrokerIntake
	}
	metadata, stat, err := inspectAdoptedResponsePipe(fd)
	if err != nil {
		return responsePipeMetadata{}, errBrokerIntake
	}
	var requestSocketStat syscall.Stat_t
	if syscall.Fstat(requestSocketFD, &requestSocketStat) != nil || sameDescriptorIdentity(stat, requestSocketStat) {
		return responsePipeMetadata{}, errBrokerIntake
	}
	return metadata, nil
}

func inspectAdoptedResponsePipe(fd int) (responsePipeMetadata, syscall.Stat_t, error) {
	if fd < 3 {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	var stat syscall.Stat_t
	if err := syscall.Fstat(fd, &stat); err != nil {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	statusFlags, err := fcntl(fd, syscall.F_GETFL, 0)
	if err != nil {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	descriptorFlags, err := fcntl(fd, syscall.F_GETFD, 0)
	if err != nil || descriptorFlags&syscall.FD_CLOEXEC == 0 {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	var filesystem syscall.Statfs_t
	if err := syscall.Fstatfs(fd, &filesystem); err != nil || filesystem.Type != linuxPipeFSMagic {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	target, err := os.Readlink("/proc/self/fd/" + strconv.Itoa(fd))
	if err != nil || !anonymousPipeTarget.MatchString(target) {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	if stat.Mode&syscall.S_IFMT != syscall.S_IFIFO || statusFlags&syscall.O_ACCMODE != syscall.O_WRONLY {
		return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
	}
	for otherFD := 0; otherFD <= 2; otherFD++ {
		if otherFD == fd {
			return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
		}
		var other syscall.Stat_t
		if syscall.Fstat(otherFD, &other) == nil && sameDescriptorIdentity(stat, other) {
			return responsePipeMetadata{}, syscall.Stat_t{}, errBrokerIntake
		}
	}
	return responsePipeMetadata{
		Device:          stat.Dev,
		Inode:           stat.Ino,
		Rdevice:         stat.Rdev,
		ModeType:        stat.Mode & syscall.S_IFMT,
		StatusFlags:     statusFlags,
		AccessMode:      statusFlags & syscall.O_ACCMODE,
		DescriptorFlags: descriptorFlags,
		FilesystemType:  filesystem.Type,
		Target:          target,
	}, stat, nil
}

func revalidateBrokerResponsePipe(intake *brokerControllerIntake) error {
	if intake == nil || intake.responseFD < 3 {
		return errBrokerIntake
	}
	metadata, _, err := inspectAdoptedResponsePipe(intake.responseFD)
	if err != nil || metadata != intake.responseMetadata {
		return errBrokerIntake
	}
	return nil
}

func closeDescriptors(descriptors []int) {
	seen := make(map[int]struct{}, len(descriptors))
	for _, descriptor := range descriptors {
		if descriptor < 0 {
			continue
		}
		if _, duplicate := seen[descriptor]; duplicate {
			continue
		}
		seen[descriptor] = struct{}{}
		_ = syscall.Close(descriptor)
	}
}
