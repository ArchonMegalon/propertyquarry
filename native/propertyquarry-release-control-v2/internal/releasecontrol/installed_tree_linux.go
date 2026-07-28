//go:build linux

package releasecontrol

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"sort"
	"strconv"
	"strings"
	"syscall"
)

const (
	maxInstalledMetadataBytes = 4 * 1024 * 1024
	maxInstalledRoleBytes     = 128 * 1024 * 1024
	maxInstalledTreeBytes     = 512 * 1024 * 1024
	maxInstalledTreeEntries   = 256
)

type stableIdentity struct {
	Device      uint64
	Inode       uint64
	Rdevice     uint64
	Mode        uint32
	Links       uint64
	UID         uint32
	GID         uint32
	Size        int64
	ModifiedSec int64
	ModifiedNS  int64
	ChangedSec  int64
	ChangedNS   int64
}

type expectedFileMetadata struct {
	Mode uint32
	UID  uint32
	GID  uint32
}

type payloadTreeEntry struct {
	Path   string
	Type   string
	Mode   uint32
	Size   int64
	Digest string
}

type payloadTreeSnapshot struct {
	Canonical      []byte
	Digest         string
	FileCount      int64
	DirectoryCount int64
	Entries        []payloadTreeEntry
	Files          map[string][]byte
}

func (snapshot *payloadTreeSnapshot) release() {
	if snapshot == nil {
		return
	}
	zero(snapshot.Canonical)
	for key, value := range snapshot.Files {
		zero(value)
		delete(snapshot.Files, key)
	}
	snapshot.Canonical = nil
	snapshot.Digest = ""
	snapshot.FileCount = 0
	snapshot.DirectoryCount = 0
	snapshot.Entries = nil
}

func identityFromStat(stat syscall.Stat_t) stableIdentity {
	return stableIdentity{
		Device:      stat.Dev,
		Inode:       stat.Ino,
		Rdevice:     stat.Rdev,
		Mode:        stat.Mode,
		Links:       stat.Nlink,
		UID:         stat.Uid,
		GID:         stat.Gid,
		Size:        stat.Size,
		ModifiedSec: stat.Mtim.Sec,
		ModifiedNS:  stat.Mtim.Nsec,
		ChangedSec:  stat.Ctim.Sec,
		ChangedNS:   stat.Ctim.Nsec,
	}
}

func validateRegularIdentity(identity stableIdentity, expected expectedFileMetadata, maximum int64) error {
	if identity.Mode&syscall.S_IFMT != syscall.S_IFREG || identity.Links != 1 {
		return fmt.Errorf("installed file type invalid")
	}
	if identity.Mode&0o7777 != expected.Mode || identity.UID != expected.UID || identity.GID != expected.GID {
		return fmt.Errorf("installed file metadata invalid")
	}
	if identity.Size < 1 || maximum < 1 || identity.Size > maximum {
		return fmt.Errorf("installed file size invalid")
	}
	return nil
}

func validateDirectoryIdentity(identity stableIdentity, expected expectedFileMetadata) error {
	if identity.Mode&syscall.S_IFMT != syscall.S_IFDIR {
		return fmt.Errorf("installed directory type invalid")
	}
	if identity.Mode&0o7777 != expected.Mode || identity.UID != expected.UID || identity.GID != expected.GID {
		return fmt.Errorf("installed directory metadata invalid")
	}
	return nil
}

func openRootedAbsolute(root, absolute string, finalFlags int) (int, error) {
	if root == "" || !path.IsAbs(absolute) || path.Clean(absolute) != absolute || absolute == "/" {
		return -1, fmt.Errorf("installed path invalid")
	}
	rootFD, err := syscall.Open(root, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return -1, err
	}
	current := rootFD
	components := strings.Split(strings.TrimPrefix(absolute, "/"), "/")
	for index, component := range components {
		if component == "" || component == "." || component == ".." || strings.ContainsRune(component, 0) {
			_ = syscall.Close(current)
			return -1, fmt.Errorf("installed path component invalid")
		}
		flags := syscall.O_RDONLY | syscall.O_CLOEXEC | syscall.O_NOFOLLOW | syscall.O_NONBLOCK
		if index < len(components)-1 {
			flags |= syscall.O_DIRECTORY
		} else {
			flags |= finalFlags
		}
		next, openErr := syscall.Openat(current, component, flags, 0)
		_ = syscall.Close(current)
		if openErr != nil {
			return -1, openErr
		}
		current = next
	}
	return current, nil
}

func readStableRootedFile(
	root string,
	absolute string,
	maximum int64,
	expected expectedFileMetadata,
) ([]byte, stableIdentity, error) {
	fd, err := openRootedAbsolute(root, absolute, 0)
	if err != nil {
		return nil, stableIdentity{}, err
	}
	value, identity, err := readStableFD(fd, maximum, expected)
	_ = syscall.Close(fd)
	if err != nil {
		zero(value)
		return nil, stableIdentity{}, err
	}
	reopened, err := openRootedAbsolute(root, absolute, 0)
	if err != nil {
		zero(value)
		return nil, stableIdentity{}, err
	}
	var reopenedStat syscall.Stat_t
	err = syscall.Fstat(reopened, &reopenedStat)
	_ = syscall.Close(reopened)
	if err != nil || identityFromStat(reopenedStat) != identity {
		zero(value)
		return nil, stableIdentity{}, fmt.Errorf("installed file path changed")
	}
	return value, identity, nil
}

func readStableFD(
	fd int,
	maximum int64,
	expected expectedFileMetadata,
) ([]byte, stableIdentity, error) {
	var before syscall.Stat_t
	if err := syscall.Fstat(fd, &before); err != nil {
		return nil, stableIdentity{}, err
	}
	identity := identityFromStat(before)
	if err := validateRegularIdentity(identity, expected, maximum); err != nil {
		return nil, stableIdentity{}, err
	}
	duplicate, err := fcntl(fd, syscall.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		return nil, stableIdentity{}, err
	}
	file := os.NewFile(uintptr(duplicate), "installed-file")
	if file == nil {
		_ = syscall.Close(duplicate)
		return nil, stableIdentity{}, fmt.Errorf("installed file wrapper failed")
	}
	value, readErr := io.ReadAll(io.LimitReader(file, maximum+1))
	closeErr := file.Close()
	if readErr != nil || closeErr != nil || int64(len(value)) != identity.Size || int64(len(value)) > maximum {
		zero(value)
		return nil, stableIdentity{}, fmt.Errorf("installed file read failed")
	}
	var after syscall.Stat_t
	if err := syscall.Fstat(fd, &after); err != nil || identityFromStat(after) != identity {
		zero(value)
		return nil, stableIdentity{}, fmt.Errorf("installed file changed")
	}
	return value, identity, nil
}

func inspectStableRootedDirectory(
	root string,
	absolute string,
	expected expectedFileMetadata,
	requireEmpty bool,
) (stableIdentity, error) {
	fd, err := openRootedAbsolute(root, absolute, syscall.O_DIRECTORY)
	if err != nil {
		return stableIdentity{}, err
	}
	defer syscall.Close(fd)
	var before syscall.Stat_t
	if err := syscall.Fstat(fd, &before); err != nil {
		return stableIdentity{}, err
	}
	identity := identityFromStat(before)
	if err := validateDirectoryIdentity(identity, expected); err != nil {
		return stableIdentity{}, err
	}
	if requireEmpty {
		names, err := directoryNames(fd)
		if err != nil || len(names) != 0 {
			return stableIdentity{}, fmt.Errorf("installed directory is not empty")
		}
	}
	var after syscall.Stat_t
	if err := syscall.Fstat(fd, &after); err != nil || identityFromStat(after) != identity {
		return stableIdentity{}, fmt.Errorf("installed directory changed")
	}
	reopened, err := openRootedAbsolute(root, absolute, syscall.O_DIRECTORY)
	if err != nil {
		return stableIdentity{}, err
	}
	var reopenedStat syscall.Stat_t
	err = syscall.Fstat(reopened, &reopenedStat)
	_ = syscall.Close(reopened)
	if err != nil || identityFromStat(reopenedStat) != identity {
		return stableIdentity{}, fmt.Errorf("installed directory path changed")
	}
	return identity, nil
}

func directoryNames(fd int) ([]string, error) {
	duplicate, err := fcntl(fd, syscall.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(duplicate), "installed-directory")
	if file == nil {
		_ = syscall.Close(duplicate)
		return nil, fmt.Errorf("installed directory wrapper failed")
	}
	entries, readErr := file.ReadDir(-1)
	closeErr := file.Close()
	if readErr != nil || closeErr != nil {
		return nil, fmt.Errorf("installed directory read failed")
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if name == "" || name == "." || name == ".." || strings.ContainsRune(name, '/') || strings.ContainsRune(name, 0) {
			return nil, fmt.Errorf("installed directory name invalid")
		}
		names = append(names, name)
	}
	sort.Strings(names)
	return names, nil
}

func collectPayloadTree(
	installRoot string,
	absolute string,
	expectedRoot expectedFileMetadata,
) (*payloadTreeSnapshot, error) {
	rootFD, err := openRootedAbsolute(installRoot, absolute, syscall.O_DIRECTORY)
	if err != nil {
		return nil, err
	}
	defer syscall.Close(rootFD)
	var rootBefore syscall.Stat_t
	if err := syscall.Fstat(rootFD, &rootBefore); err != nil {
		return nil, err
	}
	rootIdentity := identityFromStat(rootBefore)
	if err := validateDirectoryIdentity(rootIdentity, expectedRoot); err != nil {
		return nil, err
	}
	snapshot := &payloadTreeSnapshot{Files: make(map[string][]byte)}
	total := int64(0)
	if err := collectPayloadDirectory(rootFD, "", snapshot, &total); err != nil {
		snapshot.release()
		return nil, err
	}
	var rootAfter syscall.Stat_t
	if err := syscall.Fstat(rootFD, &rootAfter); err != nil || identityFromStat(rootAfter) != rootIdentity {
		snapshot.release()
		return nil, fmt.Errorf("payload root changed")
	}
	sort.Slice(snapshot.Entries, func(left, right int) bool {
		return snapshot.Entries[left].Path < snapshot.Entries[right].Path
	})
	canonicalEntries := make([]any, 0, len(snapshot.Entries))
	for _, entry := range snapshot.Entries {
		value := map[string]any{
			"path": entry.Path,
			"type": entry.Type,
			"mode": json.Number(strconv.FormatUint(uint64(entry.Mode), 10)),
		}
		if entry.Type == "file" {
			value["size"] = json.Number(strconv.FormatInt(entry.Size, 10))
			value["sha256"] = entry.Digest
		}
		canonicalEntries = append(canonicalEntries, value)
	}
	canonical, err := canonicalJSON(map[string]any{
		"schema":  "propertyquarry.release-control.payload-tree.v2",
		"entries": canonicalEntries,
	})
	if err != nil {
		snapshot.release()
		return nil, err
	}
	snapshot.Canonical = canonical
	snapshot.Digest = domainSeparatedDigest(
		[]byte("propertyquarry.release-control.payload-tree.v2\x00"),
		canonical,
	)
	return snapshot, nil
}

func collectPayloadDirectory(
	directoryFD int,
	relative string,
	snapshot *payloadTreeSnapshot,
	total *int64,
) error {
	names, err := directoryNames(directoryFD)
	if err != nil {
		return err
	}
	for _, name := range names {
		if len(snapshot.Entries) >= maxInstalledTreeEntries {
			return fmt.Errorf("payload entry limit exceeded")
		}
		childFD, err := syscall.Openat(
			directoryFD,
			name,
			syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK,
			0,
		)
		if err != nil {
			return err
		}
		childPath := name
		if relative != "" {
			childPath = path.Join(relative, name)
		}
		var before syscall.Stat_t
		if err := syscall.Fstat(childFD, &before); err != nil {
			_ = syscall.Close(childFD)
			return err
		}
		identity := identityFromStat(before)
		switch identity.Mode & syscall.S_IFMT {
		case syscall.S_IFDIR:
			snapshot.DirectoryCount++
			snapshot.Entries = append(snapshot.Entries, payloadTreeEntry{
				Path: childPath,
				Type: "directory",
				Mode: identity.Mode & 0o7777,
			})
			if err := collectPayloadDirectory(childFD, childPath, snapshot, total); err != nil {
				_ = syscall.Close(childFD)
				return err
			}
		case syscall.S_IFREG:
			if identity.Links != 1 || identity.Size < 1 || identity.Size > maxInstalledRoleBytes || *total > maxInstalledTreeBytes-identity.Size {
				_ = syscall.Close(childFD)
				return fmt.Errorf("payload file invalid")
			}
			value, stable, err := readStableFD(childFD, maxInstalledRoleBytes, expectedFileMetadata{
				Mode: identity.Mode & 0o7777,
				UID:  identity.UID,
				GID:  identity.GID,
			})
			if err != nil || stable != identity {
				_ = syscall.Close(childFD)
				zero(value)
				return fmt.Errorf("payload file changed")
			}
			*total += identity.Size
			snapshot.FileCount++
			snapshot.Entries = append(snapshot.Entries, payloadTreeEntry{
				Path:   childPath,
				Type:   "file",
				Mode:   identity.Mode & 0o7777,
				Size:   identity.Size,
				Digest: sha256Digest(value),
			})
			snapshot.Files[childPath] = value
		default:
			_ = syscall.Close(childFD)
			return fmt.Errorf("payload special file rejected")
		}
		var after syscall.Stat_t
		statErr := syscall.Fstat(childFD, &after)
		_ = syscall.Close(childFD)
		if statErr != nil || identityFromStat(after) != identity {
			return fmt.Errorf("payload entry changed")
		}
		reopened, err := syscall.Openat(
			directoryFD,
			name,
			syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW|syscall.O_NONBLOCK,
			0,
		)
		if err != nil {
			return err
		}
		var reopenedStat syscall.Stat_t
		err = syscall.Fstat(reopened, &reopenedStat)
		_ = syscall.Close(reopened)
		if err != nil || identityFromStat(reopenedStat) != identity {
			return fmt.Errorf("payload entry path changed")
		}
	}
	return nil
}

func domainSeparatedMessage(domain, canonical []byte) []byte {
	message := make([]byte, 0, len(domain)+8+len(canonical))
	message = append(message, domain...)
	length := make([]byte, 8)
	binary.BigEndian.PutUint64(length, uint64(len(canonical)))
	message = append(message, length...)
	message = append(message, canonical...)
	zero(length)
	return message
}

func domainSeparatedDigest(domain, canonical []byte) string {
	message := domainSeparatedMessage(domain, canonical)
	digest := sha256.Sum256(message)
	zero(message)
	return "sha256:" + hex.EncodeToString(digest[:])
}
