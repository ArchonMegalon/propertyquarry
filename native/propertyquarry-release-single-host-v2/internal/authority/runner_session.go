//go:build linux && amd64

package authority

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	runnerSessionRoot         = "/var/lib/propertyquarry-release-runner-v2/sessions"
	runnerSessionResultSchema = "propertyquarry.release-control.single-host-runner-session-result.v2"
)

var runnerSessionNamePattern = regexp.MustCompile(`^session-[0-9a-f]{32}\.[A-Za-z0-9]{6}$`)

type runnerSessionObservation struct {
	Path       string
	Device     uint64
	Inode      uint64
	TreeDigest string
}

func runnerSessionOwner(root string) (uint32, uint32) {
	if root == "" || root == "/" {
		return uint32(runnerUID), uint32(runnerGID)
	}
	return secureOwner(root)
}

func writeSessionHashField(writer io.Writer, value []byte) error {
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(value)))
	if _, err := writer.Write(length[:]); err != nil {
		return err
	}
	_, err := writer.Write(value)
	return err
}

func hashRunnerSessionFile(path string, before os.FileInfo) (string, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return "", fmt.Errorf("runner-session-file-open-failed")
	}
	file := os.NewFile(uintptr(fd), path)
	defer file.Close()
	opened, err := file.Stat()
	if err != nil {
		return "", fmt.Errorf("runner-session-file-stat-failed")
	}
	hasher := sha256.New()
	if _, err := io.Copy(hasher, io.LimitReader(file, 1<<30)); err != nil {
		return "", fmt.Errorf("runner-session-file-read-failed")
	}
	after, err := file.Stat()
	if err != nil || opened.Size() > 1<<30 || opened.Size() != before.Size() || after.Size() != before.Size() || opened.ModTime() != before.ModTime() || after.ModTime() != before.ModTime() {
		return "", fmt.Errorf("runner-session-file-mutated")
	}
	beforeMetadata, beforeOK := infoSys(before)
	openedMetadata, openedOK := infoSys(opened)
	afterMetadata, afterOK := infoSys(after)
	if !beforeOK || !openedOK || !afterOK || beforeMetadata.Dev != openedMetadata.Dev || beforeMetadata.Ino != openedMetadata.Ino || beforeMetadata.Dev != afterMetadata.Dev || beforeMetadata.Ino != afterMetadata.Ino {
		return "", fmt.Errorf("runner-session-file-identity-invalid")
	}
	return hex.EncodeToString(hasher.Sum(nil)), nil
}

func observeConfiguredRunnerSession(root string, runnerLabel string) (*runnerSessionObservation, error) {
	if !runnerLabelPattern.MatchString(runnerLabel) {
		return nil, fmt.Errorf("runner-session-label-invalid")
	}
	base := rooted(root, runnerSessionRoot)
	baseInfo, err := os.Lstat(base)
	baseMetadata, baseOK := infoSys(baseInfo)
	expectedUID, expectedGID := runnerSessionOwner(root)
	if err != nil || !baseOK || baseInfo.Mode()&os.ModeSymlink != 0 || !baseInfo.IsDir() || baseInfo.Mode().Perm() != 0o700 || baseMetadata.Uid != expectedUID || baseMetadata.Gid != expectedGID {
		return nil, fmt.Errorf("runner-session-root-invalid")
	}
	items, err := os.ReadDir(base)
	if err != nil {
		return nil, fmt.Errorf("runner-session-list-failed")
	}
	prefix := "session-" + strings.TrimPrefix(runnerLabel, "pqrelease-") + "."
	var selected string
	for _, item := range items {
		if !strings.HasPrefix(item.Name(), prefix) {
			continue
		}
		if selected != "" || !runnerSessionNamePattern.MatchString(item.Name()) || !item.IsDir() {
			return nil, fmt.Errorf("runner-session-selection-invalid")
		}
		selected = filepath.Join(base, item.Name())
	}
	if selected == "" {
		return nil, fmt.Errorf("runner-session-missing")
	}
	rootInfo, err := os.Lstat(selected)
	rootMetadata, rootOK := infoSys(rootInfo)
	if err != nil || !rootOK || rootInfo.Mode()&os.ModeSymlink != 0 || !rootInfo.IsDir() || rootInfo.Mode().Perm() != 0o700 || rootMetadata.Uid != expectedUID || rootMetadata.Gid != expectedGID {
		return nil, fmt.Errorf("runner-session-directory-invalid")
	}
	hasher := sha256.New()
	paths := []string{}
	err = filepath.WalkDir(selected, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		paths = append(paths, path)
		return nil
	})
	if err != nil || len(paths) < 4 || len(paths) > 200000 {
		return nil, fmt.Errorf("runner-session-tree-invalid")
	}
	sort.Strings(paths)
	for _, path := range paths {
		info, err := os.Lstat(path)
		metadata, ok := infoSys(info)
		if err != nil || !ok || metadata.Uid != expectedUID || metadata.Gid != expectedGID {
			return nil, fmt.Errorf("runner-session-entry-metadata-invalid")
		}
		relative, err := filepath.Rel(selected, path)
		if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) || strings.ContainsAny(relative, "\x00\r\n") {
			return nil, fmt.Errorf("runner-session-entry-path-invalid")
		}
		kind := ""
		content := ""
		switch {
		case info.IsDir():
			kind = "directory"
		case info.Mode().IsRegular():
			if metadata.Nlink != 1 {
				return nil, fmt.Errorf("runner-session-file-link-invalid")
			}
			kind = "file"
			content, err = hashRunnerSessionFile(path, info)
			if err != nil {
				return nil, err
			}
		case info.Mode()&os.ModeSymlink != 0:
			kind = "symlink"
			content, err = os.Readlink(path)
			if err != nil || filepath.IsAbs(content) {
				return nil, fmt.Errorf("runner-session-symlink-invalid")
			}
			resolved := filepath.Clean(filepath.Join(filepath.Dir(path), content))
			if resolved != selected && !strings.HasPrefix(resolved, selected+string(filepath.Separator)) {
				return nil, fmt.Errorf("runner-session-symlink-escape")
			}
		default:
			return nil, fmt.Errorf("runner-session-entry-kind-invalid")
		}
		record := []byte(strings.Join([]string{
			relative, kind, strconv.FormatUint(uint64(info.Mode().Perm()), 8),
			strconv.FormatUint(uint64(metadata.Uid), 10), strconv.FormatUint(uint64(metadata.Gid), 10),
			strconv.FormatInt(info.Size(), 10), content,
		}, "\x00"))
		if err := writeSessionHashField(hasher, record); err != nil {
			zero(record)
			return nil, fmt.Errorf("runner-session-hash-failed")
		}
		zero(record)
	}
	for evidence, expected := range map[string]string{
		".configuration-complete": "configured-without-host-authority\n",
		"configure-exit-status":   "0\n",
	} {
		raw, err := os.ReadFile(filepath.Join(selected, evidence))
		if err != nil || string(raw) != expected {
			zero(raw)
			return nil, fmt.Errorf("runner-session-evidence-invalid")
		}
		zero(raw)
	}
	registrationRaw, err := os.ReadFile(filepath.Join(selected, ".registration-token.sha256"))
	if err != nil || !digestPattern.MatchString(strings.TrimSuffix(string(registrationRaw), "\n")) || strings.Count(string(registrationRaw), "\n") != 1 {
		zero(registrationRaw)
		return nil, fmt.Errorf("runner-session-registration-evidence-invalid")
	}
	zero(registrationRaw)
	return &runnerSessionObservation{
		Path: selected, Device: uint64(rootMetadata.Dev), Inode: rootMetadata.Ino,
		TreeDigest: "sha256:" + hex.EncodeToString(hasher.Sum(nil)),
	}, nil
}

func waitConfiguredRunnerSession(ctxDone <-chan struct{}, root string, binding *runnerTicketBinding) (*runnerSessionObservation, error) {
	timer := time.NewTimer(90 * time.Second)
	defer timer.Stop()
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	for {
		if observed, err := observeConfiguredRunnerSession(root, binding.RunnerLabel); err == nil {
			return observed, nil
		}
		select {
		case <-ctxDone:
			return nil, fmt.Errorf("runner-session-observation-context-ended")
		case <-timer.C:
			return nil, fmt.Errorf("runner-session-observation-timeout")
		case <-ticker.C:
		}
	}
}

func verifyRunnerSessionCommand(args []string, stdout io.Writer) error {
	if len(args) != 4 || os.Geteuid() != int(runnerUID) || os.Getegid() != int(runnerGID) {
		return fmt.Errorf("runner-session-command-input-invalid")
	}
	groups, err := os.Getgroups()
	if err != nil || len(groups) != 1 || groups[0] != int(dockerSocketGID) {
		return fmt.Errorf("runner-session-command-groups-invalid")
	}
	device, deviceErr := strconv.ParseUint(args[1], 10, 64)
	inode, inodeErr := strconv.ParseUint(args[2], 10, 64)
	if !runnerLabelPattern.MatchString(args[0]) || deviceErr != nil || device < 1 || inodeErr != nil || inode < 1 || !digestPattern.MatchString(args[3]) {
		return fmt.Errorf("runner-session-command-binding-invalid")
	}
	if err := verifyRunnerSessionObservation("/", args[0], device, inode, args[3]); err != nil {
		return fmt.Errorf("runner-session-command-observation-invalid")
	}
	value := map[string]any{
		"runner_label": args[0], "session_device": json.Number(strconv.FormatUint(device, 10)),
		"session_inode": json.Number(strconv.FormatUint(inode, 10)), "session_tree_sha256": args[3],
		"schema": runnerSessionResultSchema, "version": json.Number("2"),
	}
	raw, err := canonicalJSON(value)
	if err != nil {
		return err
	}
	defer zero(raw)
	raw = append(raw, '\n')
	written, err := stdout.Write(raw)
	if err != nil || written != len(raw) {
		return fmt.Errorf("runner-session-command-write-failed")
	}
	return nil
}

func verifyRunnerSessionObservation(root, runnerLabel string, device, inode uint64, treeDigest string) error {
	if device < 1 || inode < 1 || !digestPattern.MatchString(treeDigest) {
		return fmt.Errorf("runner-session-observation-input-invalid")
	}
	observed, err := observeConfiguredRunnerSession(root, runnerLabel)
	if err != nil || observed.Device != device || observed.Inode != inode || observed.TreeDigest != treeDigest {
		return fmt.Errorf("runner-session-observation-binding-invalid")
	}
	return nil
}
