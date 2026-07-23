//go:build linux && amd64

package installhelper

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"syscall"
	"time"
)

const hostSystemdMutationCanaryUnit = "propertyquarry-release-install-mutation-canary-v2.service"

func hostSystemdMutationCanarySpecs() ([]hostCommandSpec, []string) {
	command := []string{
		"/usr/bin/systemd-run",
		"--unit=" + hostSystemdMutationCanaryUnit,
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
	return []hostCommandSpec{
		{argv: []string{"/usr/bin/systemctl", "show", "--property=LoadState", "--value", hostSystemdMutationCanaryUnit}, expectedOutput: exactHostOutput("not-found\n")},
		{argv: command},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=ActiveState", "--value", hostSystemdMutationCanaryUnit}, expectedOutput: exactHostOutput("inactive\n")},
		{argv: []string{"/usr/bin/systemctl", "show", "--property=LoadState", "--value", hostSystemdMutationCanaryUnit}, expectedOutput: exactHostOutput("not-found\n")},
	}, command
}

func cleanupHostSystemdMutationCanary() {
	for _, argv := range [][]string{
		{"/usr/bin/systemctl", "stop", hostSystemdMutationCanaryUnit},
		{"/usr/bin/systemctl", "reset-failed", hostSystemdMutationCanaryUnit},
	} {
		raw, _ := runHostCommand(hostCommandSpec{argv: argv}, 15*time.Second)
		zero(raw)
	}
}

func RunHostSystemdMutationCanary() ([]byte, error) {
	if os.Geteuid() != 0 || os.Getegid() != 0 {
		return nil, fmt.Errorf("host-systemd-canary-root-required")
	}
	if err := syscall.Chroot(FixedHostRoot); err != nil {
		return nil, fmt.Errorf("host-systemd-canary-chroot-failed")
	}
	if err := os.Chdir("/"); err != nil {
		return nil, fmt.Errorf("host-systemd-canary-chdir-failed")
	}
	comm, err := os.ReadFile("/proc/1/comm")
	if err != nil || strings.TrimSpace(string(comm)) != "systemd" {
		zero(comm)
		return nil, fmt.Errorf("host-systemd-canary-pid-invalid")
	}
	zero(comm)
	defer cleanupHostSystemdMutationCanary()
	specifications, command := hostSystemdMutationCanarySpecs()
	if err := runHostSpecs(specifications, 45*time.Second); err != nil {
		return nil, fmt.Errorf("host-systemd-canary-mutation-failed")
	}
	receipt := map[string]any{
		"apparmor_contract":      "explicitly-unconfined-root-helper-envelope",
		"command":                stringSliceValue(command),
		"host_install_performed": false,
		"mutation_performed":     true,
		"no_new_privileges":      true,
		"residue_present":        false,
		"schema":                 "propertyquarry.release-control.single-host-systemd-mutation-canary.v2",
		"unit":                   hostSystemdMutationCanaryUnit,
		"version":                json.Number("2"),
	}
	raw, err := canonicalJSON(receipt)
	if err != nil {
		return nil, fmt.Errorf("host-systemd-canary-receipt-invalid")
	}
	return append(raw, '\n'), nil
}

func stringSliceValue(values []string) []any {
	result := make([]any, len(values))
	for index, value := range values {
		result[index] = value
	}
	return result
}
