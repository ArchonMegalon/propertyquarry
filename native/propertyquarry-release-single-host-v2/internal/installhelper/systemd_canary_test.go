//go:build linux && amd64

package installhelper

import (
	"reflect"
	"testing"
)

func TestHostSystemdMutationCanaryUsesOneFixedReversibleHardenedUnit(t *testing.T) {
	specifications, command := hostSystemdMutationCanarySpecs()
	expected := []string{
		"/usr/bin/systemd-run",
		"--unit=propertyquarry-release-install-mutation-canary-v2.service",
		"--collect", "--wait", "--pipe", "--quiet",
		"--property=Type=oneshot",
		"--property=NoNewPrivileges=yes",
		"--property=PrivateDevices=yes",
		"--property=PrivateTmp=yes",
		"--property=ProtectHome=yes",
		"--property=ProtectSystem=strict",
		"--property=RestrictAddressFamilies=",
		"--property=CapabilityBoundingSet=",
		"--property=LockPersonality=yes",
		"--property=MemoryDenyWriteExecute=yes",
		"--", "/usr/bin/true",
	}
	if !reflect.DeepEqual(command, expected) {
		t.Fatalf("unexpected mutation command: %#v", command)
	}
	if len(specifications) != 4 {
		t.Fatalf("unexpected canary sequence length: %d", len(specifications))
	}
	if got := specifications[0].argv; !reflect.DeepEqual(got, []string{"/usr/bin/systemctl", "show", "--property=LoadState", "--value", hostSystemdMutationCanaryUnit}) {
		t.Fatalf("unexpected prestate probe: %#v", got)
	}
	if !reflect.DeepEqual(specifications[1].argv, expected) {
		t.Fatalf("mutation command not used exactly")
	}
	if specifications[2].expectedOutput == nil || *specifications[2].expectedOutput != "inactive\n" || specifications[3].expectedOutput == nil || *specifications[3].expectedOutput != "not-found\n" {
		t.Fatalf("terminal residue probes are not exact")
	}
}
