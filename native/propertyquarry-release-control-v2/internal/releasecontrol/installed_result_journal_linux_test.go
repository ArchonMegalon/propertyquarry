//go:build linux

package releasecontrol

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func newInstalledResultJournalTestPaths(t *testing.T) installedRuntimePaths {
	t.Helper()
	paths := newInstalledReplayTestPaths(t)
	root, err := installedResultJournalRoot(paths)
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(paths.Root, strings.TrimPrefix(root, "/"))
	if err := os.Mkdir(target, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(target, 0o700); err != nil {
		t.Fatal(err)
	}
	return paths
}

func installedResultJournalTestDirectory(
	t *testing.T,
	paths installedRuntimePaths,
) string {
	t.Helper()
	root, err := installedResultJournalRoot(paths)
	if err != nil {
		t.Fatal(err)
	}
	return filepath.Join(paths.Root, strings.TrimPrefix(root, "/"))
}

func newInstalledResultJournalTestClaim(
	t *testing.T,
	paths installedRuntimePaths,
) *installedReplayClaim {
	t.Helper()
	request := newInstalledReplayTestRequest(t)
	binding := newInstalledReplayTestBinding(t, paths)
	if err := claimInstalledReplayTest(paths, request, binding); err != nil {
		t.Fatal(err)
	}
	claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(claim.release)
	return claim
}

func writeInstalledResultJournalTestRecord(
	t *testing.T,
	paths installedRuntimePaths,
	name string,
	body []byte,
	mode os.FileMode,
) string {
	t.Helper()
	target := filepath.Join(
		installedResultJournalTestDirectory(t, paths),
		name,
	)
	if err := os.WriteFile(target, body, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(target, mode); err != nil {
		t.Fatal(err)
	}
	return target
}

func installedResultJournalTestFrame(
	t *testing.T,
	claim *installedReplayClaim,
	response []byte,
) (string, []byte) {
	t.Helper()
	metadata, err := canonicalInstalledResultMetadata(claim, response)
	if err != nil {
		t.Fatal(err)
	}
	defer zero(metadata)
	frame, err := encodeInstalledResultJournalFrame(metadata, response)
	if err != nil {
		t.Fatal(err)
	}
	name, err := installedResultJournalName(claim.Digest)
	if err != nil {
		zero(frame)
		t.Fatal(err)
	}
	return name, frame
}

func TestInstalledResultJournalReplaysExactOpaqueBytes(t *testing.T) {
	paths := newInstalledResultJournalTestPaths(t)
	claim := newInstalledResultJournalTestClaim(t, paths)
	response := []byte{
		0x00, 0xff, '{', '"', 'o', 'k', '"', ':', 't', 'r', 'u', 'e', '}',
		'\r', '\n', 0x80, 0x00,
	}
	committed, err := commitInstalledResultJournal(paths, claim, response)
	if err != nil {
		t.Fatal(err)
	}
	defer committed.release()
	if !bytes.Equal(committed.Response, response) ||
		committed.ResponseDigest != sha256Digest(response) ||
		!installedResultRecordMatchesClaim(committed, claim) {
		t.Fatal("committed result did not preserve its exact claim and response")
	}
	replay := committed.replayBytes()
	if !bytes.Equal(replay, response) {
		t.Fatal("committed response replay was not byte-identical")
	}
	replay[0] ^= 0xff
	zero(replay)

	loaded, err := loadInstalledResultJournal(paths, claim)
	if err != nil {
		t.Fatal(err)
	}
	defer loaded.release()
	if !bytes.Equal(loaded.Response, response) {
		t.Fatal("loaded response was changed by a returned caller buffer")
	}
	duplicate, err := commitInstalledResultJournal(paths, claim, response)
	if err != nil {
		t.Fatalf("exact retry did not adopt the immutable result: %v", err)
	}
	defer duplicate.release()
	if !sameInstalledResultJournalRecord(loaded, duplicate) {
		t.Fatal("exact retry returned a different result record")
	}
	if _, err := validateInstalledAuthorityState(paths); err != nil {
		t.Fatalf("separate result journal contaminated replay state: %v", err)
	}
	stateEntries, err := os.ReadDir(installedReplayStateDirectory(paths))
	if err != nil {
		t.Fatal(err)
	}
	journalEntries, err := os.ReadDir(
		installedResultJournalTestDirectory(t, paths),
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(stateEntries) != 1 || len(journalEntries) != 1 {
		t.Fatalf(
			"closed stores changed: replay=%d journal=%d",
			len(stateEntries),
			len(journalEntries),
		)
	}
	info, err := journalEntries[0].Info()
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != installedResultJournalMode ||
		!info.Mode().IsRegular() {
		t.Fatalf("result record metadata changed: %v", info.Mode())
	}
}

func TestInstalledResultJournalConcurrentCommitHasOneImmutableWinner(t *testing.T) {
	paths := newInstalledResultJournalTestPaths(t)
	claim := newInstalledResultJournalTestClaim(t, paths)
	responses := [][]byte{
		[]byte("signed-response-A\x00"),
		[]byte("signed-response-B\xff"),
	}
	type outcome struct {
		response []byte
		record   *installedResultJournalRecord
		err      error
	}
	const contenders = 24
	start := make(chan struct{})
	results := make(chan outcome, contenders)
	var group sync.WaitGroup
	for index := 0; index < contenders; index++ {
		response := responses[index%len(responses)]
		group.Add(1)
		go func() {
			defer group.Done()
			<-start
			record, err := commitInstalledResultJournal(paths, claim, response)
			results <- outcome{response: response, record: record, err: err}
		}()
	}
	close(start)
	group.Wait()
	close(results)

	loaded, err := loadInstalledResultJournal(paths, claim)
	if err != nil {
		t.Fatal(err)
	}
	defer loaded.release()
	succeeded := 0
	conflicted := 0
	for result := range results {
		if result.record != nil {
			defer result.record.release()
		}
		if result.err == nil {
			succeeded++
			if result.record == nil ||
				!bytes.Equal(result.response, loaded.Response) ||
				!bytes.Equal(result.record.Response, loaded.Response) {
				t.Fatal("a non-winning response reported a successful commit")
			}
			continue
		}
		var conflict installedResultConflictError
		if !errors.As(result.err, &conflict) ||
			bytes.Equal(result.response, loaded.Response) {
			t.Fatalf("concurrent conflict was not fail-closed: %v", result.err)
		}
		conflicted++
	}
	if succeeded != contenders/2 || conflicted != contenders/2 {
		t.Fatalf(
			"unexpected concurrent result: succeeded=%d conflicted=%d",
			succeeded,
			conflicted,
		)
	}
	entries, err := os.ReadDir(installedResultJournalTestDirectory(t, paths))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("exclusive commit created %d result files", len(entries))
	}
}

func TestInstalledResultJournalRequiresExactDurableClaim(t *testing.T) {
	t.Run("missing-claim", func(t *testing.T) {
		paths := newInstalledResultJournalTestPaths(t)
		request := newInstalledReplayTestRequest(t)
		binding := newInstalledReplayTestBinding(t, paths)
		claim, err := replayClaimForInstalledReplayTest(paths, request, binding)
		if err != nil {
			t.Fatal(err)
		}
		defer claim.release()
		if result, err := commitInstalledResultJournal(
			paths,
			claim,
			[]byte("response"),
		); err == nil || result != nil {
			if result != nil {
				result.release()
			}
			t.Fatal("result committed without its durable replay claim")
		}
		entries, err := os.ReadDir(
			installedResultJournalTestDirectory(t, paths),
		)
		if err != nil {
			t.Fatal(err)
		}
		if len(entries) != 0 {
			t.Fatal("missing claim left a result artifact")
		}
	})

	mutations := map[string]func(*installedReplayClaim){
		"request-key-id": func(claim *installedReplayClaim) {
			claim.RequestKeyID = "sha256:" + strings.Repeat("1", 64)
		},
		"request-id": func(claim *installedReplayClaim) {
			claim.RequestID = "mismatched-request"
		},
		"nonce": func(claim *installedReplayClaim) {
			claim.Nonce = "mismatched-nonce"
		},
		"raw-request-digest": func(claim *installedReplayClaim) {
			claim.RawRequestDigest = "sha256:" + strings.Repeat("5", 64)
		},
		"canonical-envelope-digest": func(claim *installedReplayClaim) {
			claim.CanonicalEnvelopeDigest = "sha256:" + strings.Repeat("2", 64)
		},
		"root-policy-digest": func(claim *installedReplayClaim) {
			claim.RootPolicyDigest = "sha256:" + strings.Repeat("6", 64)
		},
		"authority-generation-digest": func(claim *installedReplayClaim) {
			claim.AuthorityGenerationDigest =
				"sha256:" + strings.Repeat("7", 64)
		},
		"state-generation-digest": func(claim *installedReplayClaim) {
			claim.StateGenerationDigest =
				"sha256:" + strings.Repeat("8", 64)
		},
		"state-generation-object": func(claim *installedReplayClaim) {
			claim.StateGeneration.Inode++
		},
		"claim-digest": func(claim *installedReplayClaim) {
			claim.Digest = "sha256:" + strings.Repeat("3", 64)
		},
		"claim-name": func(claim *installedReplayClaim) {
			claim.Name = installedReplayClaimPrefix +
				strings.Repeat("4", 64) +
				installedReplayClaimSuffix
		},
		"claim-canonical": func(claim *installedReplayClaim) {
			claim.Canonical[0] ^= 1
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			paths := newInstalledResultJournalTestPaths(t)
			claim := newInstalledResultJournalTestClaim(t, paths)
			changed := *claim
			changed.Canonical = append([]byte(nil), claim.Canonical...)
			defer zero(changed.Canonical)
			mutate(&changed)
			result, err := commitInstalledResultJournal(
				paths,
				&changed,
				[]byte("response"),
			)
			if result != nil {
				result.release()
			}
			if err == nil {
				t.Fatal("result journal accepted a mismatched replay claim")
			}
			entries, readErr := os.ReadDir(
				installedResultJournalTestDirectory(t, paths),
			)
			if readErr != nil {
				t.Fatal(readErr)
			}
			if len(entries) != 0 {
				t.Fatal("mismatched claim left a result artifact")
			}
		})
	}
}

func TestInstalledResultJournalRejectsUnboundedResponses(t *testing.T) {
	paths := newInstalledResultJournalTestPaths(t)
	claim := newInstalledResultJournalTestClaim(t, paths)
	tooLarge := make([]byte, maxInstalledResultResponse+1)
	defer zero(tooLarge)
	for name, response := range map[string][]byte{
		"empty":     nil,
		"too-large": tooLarge,
	} {
		t.Run(name, func(t *testing.T) {
			record, err := commitInstalledResultJournal(paths, claim, response)
			if record != nil {
				record.release()
			}
			if err == nil {
				t.Fatal("result journal accepted an unbounded response")
			}
		})
	}
	entries, err := os.ReadDir(installedResultJournalTestDirectory(t, paths))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatal("invalid response left a result artifact")
	}
}

func TestInstalledResultJournalCrashStateNeverOverwrites(t *testing.T) {
	tests := map[string]func(
		*testing.T,
		installedRuntimePaths,
		*installedReplayClaim,
		string,
		[]byte,
	){
		"exclusive-create": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			_ []byte,
		) {
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				name,
				nil,
				installedResultJournalMode,
			)
		},
		"partial-write": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			frame []byte,
		) {
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				name,
				frame[:len(frame)/2],
				installedResultJournalMode,
			)
		},
	}
	for name, inject := range tests {
		t.Run(name, func(t *testing.T) {
			paths := newInstalledResultJournalTestPaths(t)
			claim := newInstalledResultJournalTestClaim(t, paths)
			response := []byte("signed-durable-response")
			recordName, frame := installedResultJournalTestFrame(
				t,
				claim,
				response,
			)
			defer zero(frame)
			inject(t, paths, claim, recordName, frame)
			target := filepath.Join(
				installedResultJournalTestDirectory(t, paths),
				recordName,
			)
			before, err := os.ReadFile(target)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(before)
			if loaded, err := loadInstalledResultJournal(paths, claim); err == nil {
				loaded.release()
				t.Fatal("partial crash record was replayed")
			}
			if committed, err := commitInstalledResultJournal(
				paths,
				claim,
				response,
			); err == nil {
				committed.release()
				t.Fatal("partial crash record was overwritten")
			}
			after, err := os.ReadFile(target)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(after)
			if !bytes.Equal(after, before) {
				t.Fatal("failed commit mutated a partial crash record")
			}
		})
	}
}

func TestInstalledResultJournalDurableRecordSurvivesReopen(t *testing.T) {
	paths := newInstalledResultJournalTestPaths(t)
	claim := newInstalledResultJournalTestClaim(t, paths)
	response := bytes.Repeat([]byte{0x00, 0x7f, 0x80, 0xff}, 4096)
	defer zero(response)
	record, err := commitInstalledResultJournal(paths, claim, response)
	if err != nil {
		t.Fatal(err)
	}
	record.release()
	for index := 0; index < 4; index++ {
		loaded, err := loadInstalledResultJournal(paths, claim)
		if err != nil {
			t.Fatalf("reopen %d failed: %v", index, err)
		}
		if !bytes.Equal(loaded.Response, response) {
			loaded.release()
			t.Fatalf("reopen %d changed response bytes", index)
		}
		loaded.release()
	}
}

func TestInstalledResultJournalRejectsHostileEntries(t *testing.T) {
	tests := map[string]func(
		*testing.T,
		installedRuntimePaths,
		*installedReplayClaim,
		string,
		[]byte,
	){
		"journal-directory-mode": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			_ string,
			_ []byte,
		) {
			if err := os.Chmod(
				installedResultJournalTestDirectory(t, paths),
				0o750,
			); err != nil {
				t.Fatal(err)
			}
		},
		"journal-directory-symlink": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			_ string,
			_ []byte,
		) {
			journal := installedResultJournalTestDirectory(t, paths)
			if err := os.Remove(journal); err != nil {
				t.Fatal(err)
			}
			replacement := filepath.Join(paths.Root, "replacement-results")
			if err := os.Mkdir(replacement, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(replacement, journal); err != nil {
				t.Fatal(err)
			}
		},
		"unknown-entry": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			_ string,
			_ []byte,
		) {
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				"unexpected",
				[]byte("hostile"),
				installedResultJournalMode,
			)
		},
		"record-symlink": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			_ []byte,
		) {
			target := filepath.Join(
				installedResultJournalTestDirectory(t, paths),
				"target",
			)
			if err := os.WriteFile(target, []byte("hostile"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(
				target,
				filepath.Join(
					installedResultJournalTestDirectory(t, paths),
					name,
				),
			); err != nil {
				t.Fatal(err)
			}
		},
		"record-directory": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			_ []byte,
		) {
			if err := os.Mkdir(
				filepath.Join(
					installedResultJournalTestDirectory(t, paths),
					name,
				),
				0o600,
			); err != nil {
				t.Fatal(err)
			}
		},
		"record-mode": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			frame []byte,
		) {
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				name,
				frame,
				0o640,
			)
		},
		"record-hardlink": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			frame []byte,
		) {
			target := writeInstalledResultJournalTestRecord(
				t,
				paths,
				name,
				frame,
				installedResultJournalMode,
			)
			if err := os.Link(
				target,
				filepath.Join(paths.Root, "result-hardlink"),
			); err != nil {
				t.Fatal(err)
			}
		},
		"noncanonical-metadata": func(
			t *testing.T,
			paths installedRuntimePaths,
			claim *installedReplayClaim,
			name string,
			_ []byte,
		) {
			response := []byte("signed-response")
			metadata, err := canonicalInstalledResultMetadata(claim, response)
			if err != nil {
				t.Fatal(err)
			}
			metadata = append(metadata, '\n')
			defer zero(metadata)
			frame, err := encodeInstalledResultJournalFrame(metadata, response)
			if err != nil {
				t.Fatal(err)
			}
			defer zero(frame)
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				name,
				frame,
				installedResultJournalMode,
			)
		},
		"corrupt-response": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			name string,
			frame []byte,
		) {
			corrupt := append([]byte(nil), frame...)
			corrupt[len(corrupt)-1] ^= 0xff
			defer zero(corrupt)
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				name,
				corrupt,
				installedResultJournalMode,
			)
		},
		"wrong-valid-name": func(
			t *testing.T,
			paths installedRuntimePaths,
			_ *installedReplayClaim,
			_ string,
			frame []byte,
		) {
			writeInstalledResultJournalTestRecord(
				t,
				paths,
				installedResultJournalPrefix+
					strings.Repeat("f", 64)+
					installedResultJournalSuffix,
				frame,
				installedResultJournalMode,
			)
		},
	}
	for name, inject := range tests {
		t.Run(name, func(t *testing.T) {
			paths := newInstalledResultJournalTestPaths(t)
			claim := newInstalledResultJournalTestClaim(t, paths)
			response := []byte("signed-response")
			recordName, frame := installedResultJournalTestFrame(
				t,
				claim,
				response,
			)
			defer zero(frame)
			inject(t, paths, claim, recordName, frame)
			if record, err := loadInstalledResultJournal(
				paths,
				claim,
			); err == nil {
				record.release()
				t.Fatal("hostile result journal state was loaded")
			}
			if record, err := commitInstalledResultJournal(
				paths,
				claim,
				response,
			); err == nil {
				record.release()
				t.Fatal("hostile result journal state was replaced")
			}
		})
	}
}

func TestInstalledResultJournalRejectsOrphanedCommittedRecord(t *testing.T) {
	paths := newInstalledResultJournalTestPaths(t)
	claim := newInstalledResultJournalTestClaim(t, paths)
	response := []byte("signed-response")
	record, err := commitInstalledResultJournal(paths, claim, response)
	if err != nil {
		t.Fatal(err)
	}
	record.release()
	if err := os.Remove(
		filepath.Join(installedReplayStateDirectory(paths), claim.Name),
	); err != nil {
		t.Fatal(err)
	}
	if record, err := loadInstalledResultJournal(paths, claim); err == nil {
		record.release()
		t.Fatal("orphaned result record replayed without its durable claim")
	}
	if record, err := commitInstalledResultJournal(
		paths,
		claim,
		response,
	); err == nil {
		record.release()
		t.Fatal("orphaned result record was adopted without its durable claim")
	}
}
